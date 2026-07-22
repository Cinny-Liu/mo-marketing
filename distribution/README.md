# 全网分发监控 / Distribution Monitor

定时监控各渠道的**矩阵起源官方账号**文章发布，自动把同一篇文章在各平台的链接，
关联（同步成一条评论）到 mo-marketing 里对应的 issue 上。

## 工作原理

```
渠道账号 ──抓取──▶ monitor.py ──DeepSeek匹配──▶ manifest 里的某篇文章 ──▶ 对应 issue 的「📡 全网分发」评论
```

1. **发现**：抓取可发现渠道的账号最新文章；
2. **匹配**：用 DeepSeek 把渠道文章标题匹配到 `manifest.yml` 里的某篇文章（跨平台/中英文标题差异也能认）；
3. **关联**：自动把链接登记到 `discovered.yml`，并把每篇文章的全网分发链接同步成对应 issue 下一条**幂等更新**的评论（不会刷屏）。

## 渠道支持现状

| 渠道 | 自动发现 | 说明 |
|------|:-------:|------|
| CSDN | ✅ api | 有稳定 JSON 接口，最可靠 |
| OSChina | 🟡 best-effort | 抓主页，偶尔失效则跳过 |
| SegmentFault | 🟡 best-effort | 抓用户主页 |
| 51CTO | ✋ 人工 | 常对 CI 出口 IP 返回 5xx |
| InfoQ 写作平台 | ✋ 人工 | 页面 JS 渲染 |
| 墨天轮 | ✋ 人工 | |
| 知乎 | ✋ 人工 | 反爬严格，需登录态 |
| 微信公众号 | ✋ 人工 | **无公开接口，无法自动抓取** |
| LinkedIn | ✋ 人工 | |
| X | ✋ 人工 | |
| Medium | ✋ 人工 | |

> 「人工」渠道：把链接填到 `manifest.yml` 对应文章的 `channels` 下即可，脚本会校验有效性并同步进评论。

## 文件

- `manifest.yml` — **人工维护**的源清单：每篇文章 → issue 号、可选的 blog slug、中英标题、各渠道链接。脚本不会改写它。
- `channels.yml` — 各渠道矩阵起源账号配置。
- `discovered.yml` — **脚本维护**的自动发现状态（勿手改）。
- `monitor.py` — 监控脚本。
- `../.github/workflows/distribution-monitor.yml` — 每天 09:00（北京）定时跑，也可手动触发。

## 配置（一次性）

需要在仓库加一个 Secret 给 DeepSeek 用：

```bash
gh secret set DEEPSEEK_API_KEY -R matrixorigin/mo-marketing   # 粘贴 DeepSeek key
```

> 不配 `DEEPSEEK_API_KEY` 也能跑：会退化为字符串相似度匹配（中文标题足够用），但跨中英匹配会变弱。
> `GITHUB_TOKEN` 由 Actions 自动注入，无需配置。

## 手动触发 / 本地调试

```bash
# 手动跑一次（GitHub 上：Actions → distribution-monitor → Run workflow）
# 本地 dry-run（只打印，不写 GitHub、不写 discovered.yml）：
cd distribution
pip install -r requirements.txt
DRY_RUN=1 GITHUB_TOKEN=$(gh auth token) DEEPSEEK_API_KEY=sk-xxx python monitor.py
```

## 新增一篇要追踪的文章

在 `manifest.yml` 的 `articles:` 下加一条：

```yaml
  - key: my-article-slug          # 唯一 key（建议用 blog slug）
    issue: 30                      # 对应 mo-marketing issue 号
    blog_slug: my-article-slug     # 可选；matrixorigin-blog 目录名，非博客来源可省略
    title_zh: "中文标题"
    title_en: "English title"
    channels: {}                   # 已知链接可直接填，未知的等脚本自动发现
```
