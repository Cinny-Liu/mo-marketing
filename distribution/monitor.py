#!/usr/bin/env python3
"""
全网分发监控 / Distribution monitor.

做三件事：
  1) 发现：抓取可发现渠道(CSDN/SegmentFault/OSChina)的矩阵起源账号最新文章，
     用 DeepSeek 把标题匹配到 manifest 里的某篇文章，自动把链接登记下来。
  2) 校验：检查所有渠道链接是否有效。
  3) 同步：把每篇文章的全网分发链接，写成对应 mo-marketing issue 下的一条
     「📡 全网分发」评论（用标记定位，幂等地原地更新，不会刷屏）。

人工维护的 manifest.yml 永不被脚本改写；脚本自动发现的链接写在 discovered.yml。

环境变量：
  GITHUB_TOKEN        必填（评论/读 issue）。GitHub Actions 自带。
  GITHUB_REPOSITORY   选填，默认 matrixorigin/mo-marketing
  DEEPSEEK_API_KEY    选填。没有则退化为字符串相似度匹配（中文标题足够用）。
  DEEPSEEK_BASE_URL   选填，默认 https://api.deepseek.com
  DEEPSEEK_MODEL      选填，默认 deepseek-chat
  DRY_RUN=1           只打印，不写 GitHub、不写 discovered.yml
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
REPO = os.environ.get("GITHUB_REPOSITORY", "matrixorigin/mo-marketing")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
GH_API = "https://api.github.com"
MARKER = "<!-- mo-dist-bot:{key} -->"
DISPLAY_ORDER = ["wechat", "zhihu", "csdn", "oschina", "infoq", "modb", "segmentfault", "51cto"]
# 只对这些渠道做有效性校验；微信/知乎等反爬站点对 bot 返回非 2xx 不代表链接失效，不标记。
CHECKABLE = {"csdn", "oschina", "segmentfault"}


def log(*a):
    print(*a, flush=True)


# ----------------------------- config / state -----------------------------
def load_yaml(name, default):
    p = HERE / name
    if not p.exists():
        return default
    return yaml.safe_load(p.read_text(encoding="utf-8")) or default


def save_discovered(discovered):
    if DRY_RUN:
        log("[dry-run] would write discovered.yml:", json.dumps(discovered, ensure_ascii=False))
        return
    p = HERE / "discovered.yml"
    header = ("# 自动发现的分发链接（由 distribution/monitor.py 维护，勿手改）\n"
              "# Auto-discovered distribution links — bot-owned, do not edit by hand.\n")
    p.write_text(header + yaml.safe_dump(discovered, allow_unicode=True, sort_keys=True),
                 encoding="utf-8")


def norm_url(u: str) -> str:
    return (u or "").split("?")[0].rstrip("/").lower()


# ----------------------------- channel fetchers -----------------------------
def fetch_csdn(cfg) -> list[dict]:
    """CSDN 社区接口，最可靠。返回 [{title, url}]."""
    url = cfg.get("list_api")
    if not url:
        return []
    r = requests.get(url, headers={"User-Agent": UA, "Referer": f"https://blog.csdn.net/{cfg.get('account','')}"},
                     timeout=25)
    r.raise_for_status()
    items = (r.json().get("data") or {}).get("list") or []
    return [{"title": it.get("title", "").strip(), "url": it.get("url", "")}
            for it in items if it.get("url")]


def fetch_segmentfault(cfg) -> list[dict]:
    """SegmentFault 用户主页，best-effort 抓 article 链接。"""
    acc = cfg.get("account_url")
    if not acc:
        return []
    r = requests.get(acc, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    out, seen = [], set()
    # /a/1190000047817870 形式
    for m in re.finditer(r'href="(/a/\d+)"[^>]*>([^<]{4,})</a>', r.text):
        path, title = m.group(1), m.group(2).strip()
        if path in seen:
            continue
        seen.add(path)
        out.append({"title": title, "url": "https://segmentfault.com" + path})
    return out


def fetch_oschina(cfg) -> list[dict]:
    """OSChina 用户博客页，best-effort。"""
    acc = cfg.get("account_url")
    if not acc:
        return []
    r = requests.get(acc + "/blog", headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    out, seen = [], set()
    for m in re.finditer(r'href="(https://my\.oschina\.net/u/\d+/blog/\d+)"[^>]*>\s*([^<]{4,})', r.text):
        u, title = m.group(1), m.group(2).strip()
        if u in seen:
            continue
        seen.add(u)
        out.append({"title": title, "url": u})
    return out


FETCHERS = {"csdn": fetch_csdn, "segmentfault": fetch_segmentfault, "oschina": fetch_oschina}


def discover(channels_cfg) -> dict[str, list[dict]]:
    found = {}
    for key, cfg in (channels_cfg.get("channels") or {}).items():
        if cfg.get("discover") not in ("api", "best-effort"):
            continue
        fetcher = FETCHERS.get(key)
        if not fetcher:
            continue
        try:
            arts = fetcher(cfg)
            log(f"[discover] {key}: {len(arts)} article(s)")
            found[key] = arts
        except Exception as e:  # best-effort: 单个渠道失败不影响整体
            log(f"[discover] {key}: FAILED ({e.__class__.__name__}: {e}) — skipped")
            found[key] = []
    return found


# ----------------------------- matching (DeepSeek) -----------------------------
def deepseek_match(title: str, articles: list[dict]) -> str | None:
    key = os.environ.get("DEEPSEEK_API_KEY")
    catalog = [{"key": a["key"], "title_zh": a.get("title_zh", ""), "title_en": a.get("title_en", "")}
               for a in articles]
    if not key:
        return fuzzy_match(title, articles)
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    prompt = (
        "你是内容分发匹配助手。下面是一份文章目录（每篇有 key 和中/英标题）。"
        "请判断『待匹配标题』指的是目录中的哪一篇（同一篇文章在不同平台标题可能略有差异，"
        "也可能是中英互译）。只输出匹配到的 key；若都不是同一篇，只输出 NONE。\n\n"
        f"文章目录:\n{json.dumps(catalog, ensure_ascii=False, indent=2)}\n\n"
        f"待匹配标题: {title}\n\n只输出 key 或 NONE，不要解释。"
    )
    try:
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40,
        )
        r.raise_for_status()
        ans = r.json()["choices"][0]["message"]["content"].strip()
        ans = ans.strip("`").strip()
        valid = {a["key"] for a in articles}
        return ans if ans in valid else None
    except Exception as e:
        log(f"[match] DeepSeek failed ({e}); falling back to fuzzy")
        return fuzzy_match(title, articles)


def fuzzy_match(title: str, articles: list[dict], threshold: float = 0.82) -> str | None:
    # 注意：阈值偏高，因为同系列文章标题模板相同（如「当X不再靠人肉Y，MOI如何把Z…」），
    # 低阈值会误配。跨中英匹配交给 DeepSeek，这里只兜底同语种近乎相同的标题。
    best, best_key = 0.0, None
    for a in articles:
        for cand in (a.get("title_zh", ""), a.get("title_en", "")):
            if not cand:
                continue
            score = SequenceMatcher(None, title, cand).ratio()
            if score > best:
                best, best_key = score, a["key"]
    return best_key if best >= threshold else None


# ----------------------------- liveness -----------------------------
def check_alive(url: str) -> bool:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        return False


# ----------------------------- GitHub -----------------------------
def gh(method, path, **kw):
    h = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
         "User-Agent": "mo-dist-bot"}
    r = requests.request(method, f"{GH_API}{path}", headers=h, timeout=30, **kw)
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:200]}")
    return r.json() if r.text else {}


def upsert_comment(issue: int, key: str, body: str):
    marker = MARKER.format(key=key)
    full = f"{marker}\n{body}"
    if DRY_RUN:
        log(f"[dry-run] issue #{issue} comment:\n{full}\n")
        return
    comments = gh("GET", f"/repos/{REPO}/issues/{issue}/comments?per_page=100")
    for c in comments:
        if marker in (c.get("body") or ""):
            gh("PATCH", f"/repos/{REPO}/issues/comments/{c['id']}", json={"body": full})
            log(f"[sync] issue #{issue}: updated distribution comment")
            return
    gh("POST", f"/repos/{REPO}/issues/{issue}/comments", json={"body": full})
    log(f"[sync] issue #{issue}: created distribution comment")


def build_comment(article, channels_cfg, eff_channels) -> str:
    names = {k: (v.get("name") or k) for k, v in (channels_cfg.get("channels") or {}).items()}
    rows = []
    for ch in DISPLAY_ORDER:
        url = eff_channels.get(ch)
        label = names.get(ch, ch)
        if url:
            mark = " ⚠️待核实" if (ch in CHECKABLE and not check_alive(url)) else ""
            rows.append(f"| {label} | [{url}]({url}){mark} |")
        else:
            rows.append(f"| {label} | — |")
    done = sum(1 for ch in DISPLAY_ORDER if eff_channels.get(ch))
    title = article.get("title_zh") or article.get("title_en") or article["key"]
    return (
        f"### 📡 全网分发 · {title}\n\n"
        f"已分发 **{done}/{len(DISPLAY_ORDER)}** 个渠道："
        f"（matrixorigin-blog: `{article.get('blog_slug','')}`）\n\n"
        "| 渠道 | 链接 |\n|------|------|\n" + "\n".join(rows) + "\n\n"
        "> 本评论由 `distribution/monitor.py` 自动维护。可抓取渠道(CSDN/OSChina/SegmentFault)"
        "会自动登记；微信/知乎/InfoQ/墨天轮/51CTO 请人工填入 `distribution/manifest.yml`。"
    )


# ----------------------------- main -----------------------------
def main():
    if not GH_TOKEN:
        log("ERROR: GITHUB_TOKEN not set")
        return 1
    manifest = load_yaml("manifest.yml", {"articles": []})
    channels_cfg = load_yaml("channels.yml", {"channels": {}})
    discovered = load_yaml("discovered.yml", {}) or {}
    articles = manifest["articles"]

    # 已知链接集合（manifest + discovered）
    known = set()
    for a in articles:
        for u in (a.get("channels") or {}).values():
            known.add(norm_url(u))
    for d in discovered.values():
        for u in d.values():
            known.add(norm_url(u))

    # 1) 发现 + 匹配 + 自动登记
    new_links = []
    found = discover(channels_cfg)
    for ch, arts in found.items():
        for art in arts:
            if norm_url(art["url"]) in known:
                continue
            key = deepseek_match(art["title"], articles)
            if not key:
                log(f"[discover] 未匹配: [{ch}] {art['title'][:40]} -> {art['url']}")
                continue
            discovered.setdefault(key, {})
            if ch not in discovered[key] and not any(
                    a["key"] == key and (a.get("channels") or {}).get(ch) for a in articles):
                discovered[key][ch] = art["url"]
                known.add(norm_url(art["url"]))
                new_links.append((key, ch, art["url"]))
                log(f"[discover] ✅ 自动关联: {key} [{ch}] {art['url']}")

    if new_links and not DRY_RUN:
        save_discovered(discovered)
    elif DRY_RUN and new_links:
        save_discovered(discovered)

    # 2)+3) 校验并同步每篇文章的 issue 评论
    for a in articles:
        eff = dict(a.get("channels") or {})
        eff.update(discovered.get(a["key"], {}))
        if not a.get("issue"):
            continue
        body = build_comment(a, channels_cfg, eff)
        try:
            upsert_comment(a["issue"], a["key"], body)
        except Exception as e:
            log(f"[sync] issue #{a.get('issue')} FAILED: {e}")

    log(f"\n完成。新自动关联 {len(new_links)} 条链接。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
