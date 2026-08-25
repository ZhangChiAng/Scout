# Scout（侦察兵）重建计划

## 0. 背景与产品定义

本仓库 /home/dev/workspace/signal-feed 原名 SignalFeed，曾从 6 家 AI 厂商 14 个官方
入口采集新闻并经 LLM 生成中文摘要推送飞书。产品形态已判定走偏，推倒重来为：

- **名称**：侦察兵（Scout）
- **定位**：个人自用的可学习个性化推荐信息采集工具，暂只服务 AI 话题
- **动机**：简体中文社区关于 DeepSeek 等 AI 话题的讨论饭圈化、情绪化，需要用规则
  过滤低质样本，只保留一线开发体验、不玩梗的内容；未来接入更多优质信源
- **V1 范围**：仅接入一个 RSS 信源 + 飞书推送 + 知乎调研文档。LLM 个性化、知乎代码
  接入、关键词过滤全部后置；LLM 仅保留 API 调用入口（无任何业务逻辑）
- **调度部署**：全部停用，V1 为本地手动运行（--dry-run / --send）

## 1. 已验证事实（不必重新调研）

### 1.1 RSS 信源（橘鸦AI早报）
- URL：https://daily.juya.uk/rss.xml （已验证 200，RSS 2.0，zh-CN）
- 结构：**每天一个 item**：
  - `title` = 日期（如 `2026-08-24`）
  - `link` = `guid`（isPermaLink）= `https://daily.juya.uk/issues/YYYY-MM-DD/`
  - `pubDate` RFC822，约北京时间 09:30 发布
  - `description` = 无链接纯文本概览
  - `content:encoded` = 完整 HTML 日报：封面 img → `<h1>AI 早报 日期</h1>` →
    视频版链接 p → `<h2>概览</h2>` 内按 `<h3>分类</h3>`（要闻/开发生态/前瞻与
    传闻等）分组，每组 `<ul><li>标题 <a href>↗</a> <code>#N</code></li></ul>` →
    `<hr>` → 正文区 `<h2>` 下每条新闻 `<h3>`（常含原文链接）+ `<blockquote>`
    一句话摘要 + `<p>` 详情 + 图片 + `<ul>相关链接</ul>` → 尾部"查看网页全文"
- 单 item 约 100KB+，历史条目多，必须用 window_size 截断
- 备用接口：https://daily.juya.uk/markdown/YYYY-MM-DD.md
- 内容已是人工策展中文日报，**不需要抓原文、不需要 LLM 摘要**

### 1.2 知乎数据开放平台与 Zhihu CLI（调研结论原文素材）
- 平台：developer.zhihu.com，邀测阶段，API 权限需邮件申请
  （openplatform@zhihu.com，说明场景与调用量，1 个工作日答复），计费商务定制
- HTTP API 鉴权：`Authorization: Bearer <access_secret>`（个人中心
  developer.zhihu.com/profile 生成）+ `X-Request-Timestamp`（秒级 Unix 时间戳）
  + `Content-Type: application/json`；已知端点：
  `GET https://developer.zhihu.com/api/v1/content/zhihu_search?Query=...`、
  `global_search`、`hot_list`；"直答 API"支持流式，搜索 API 不支持
- **Zhihu CLI**（官方，面向 AI Agent 的命令行工具，读取向能力完备）：
  - 定位："让你的 Agent 读懂知乎"；Agent 只需自然语言下达任务，CLI 负责取数并
    保留原始内容链接，输出机器可读
  - 安装：把 skill 包发给 Agent——
    `https://developer-cdn.zhihu.com/zhihu-cli/releases/stable/skill/zhihu-cli-skill.zip`
  - 能力：搜索知乎（真实经验/观点/案例）、搜索全网、知乎热榜、知乎直答、
    我的创作、我的关注、我的收藏、知识库（查看/检索/上传单文件）
  - 凭证：Access Secret 由 Agent 经标准输入交给 CLI；验证后存 OS 凭证管理器
    （macOS Keychain / Windows Credential Manager / Linux Secret Service）；
    Linux SSH/CI/容器场景由宿主 Secret Store 经进程级环境变量注入；不写入
    skill 文件夹或普通配置文件
  - 边界：只查凭证所属账号（不接受 OAuth/用户 ID/代查）；个人数据按需读取；
    摘要不等于原文；更新需征得同意且更新服务不接收 Secret/数据；第三方 Web
    应用代表其他用户访问需另行接入知乎 OAuth，不得分发 Access Secret
  - 行为：每 session 首次激活检查一次兼容性；不自动升级
- 官方 GitHub org（github.com/zhihu）另有发布向工具 ZhihuPublisher /
  zhihu-mediacloud-uploader，与本产品读取需求无关；PyPI `zhihu` 包为 2017 年
  社区项目，不用
- **对 Scout 的意义**：未来知乎接入两条路——(a) collector 以 subprocess 调用
  zhihu CLI（官方设计路径，输出机器可读）；(b) 直接调 HTTP API（Bearer +
  时间戳，端点已知）。共同前提：完成邀测申请拿到 Access Secret。V1 不写代码

### 1.3 现有代码地图（HEAD 4be9c55）
- Python 3.14 + uv，无打包（tool.uv package=false），依赖仅 lark-oapi、openai
- signalfeed/ 共 12 模块：__main__.py(CLI入口)、app.py(916行编排)、
  collector.py(1480行,5种collector,RSS在L227-302)、config.py(TOML强校验)、
  model.py(NewsItem/canonicalize_url)、filter.py(24行关键词)、storage.py(636行,
  SQLite 5表:delivered_items/source_state/baseline_items/chinese_summary_cache/
  active_failures)、notifier.py(飞书,lark SDK,L10-24 atexit websocket清理
  workaround不可删)、reader.py(Jina,待删)、summarizer.py(LLM摘要,待删)、
  datetime_utils.py(北京时间)
- 测试：unittest，tests/ 共约4000行122用例
- 质量门禁：`uv run --locked python -m unittest discover -s tests -v`（CI 中
  ResourceWarning 即失败）、`ruff check`、`ruff format --check`、`compileall`
- 已知：collector.py:1089、storage.py:483 两处 PEP 758 无括号 except 合法非 bug

## 2. 用户决策记录（本计划的依据）
1. 飞书保留；彻底改名为 scout
2. 旧 SQLite 删除；服务器定时任务停用（timer 与 CD 不再跑）
3. LLM 仅保留 API 调用入口：保留 models.toml 加载校验 + SCOUT_LLM_API_KEY
   解析 + 最小 client 工厂（沿用现有超时/store=false 设置）；删除 summarizer、
   摘要提示词与 JSON Schema、摘要缓存表等一切业务物。V1 数据流完全不调用 LLM，
   models.toml 与 key 变为可选（缺失可正常运行，存在则校验）
4. 知乎 V1 仅调研文档
5. 用户手工步骤（执行前提醒用户完成）：
   - 服务器：`sudo systemctl disable --now signalfeed.timer`
   - 删除旧库：服务器与本地 `data/signalfeed-zh.sqlite3` 及 `*.bak`
   - 可选：删除 GitHub Actions Secrets DEPLOY_*；GitHub 仓库改名 scout 后更新 remote

## 3. 目标形态

### 3.1 config.toml（重写）
```toml
[[sources]]
name = "juya-ai-daily"
url = "https://daily.juya.uk/rss.xml"
collector = "rss"        # 唯一合法值，保留枚举为将来 zhihu 等扩展
window_size = 7
max_age_days = 3

[network]
timeout_seconds = 15
max_bytes = 5242880
user_agent = "Scout/0.1"

[feishu]
max_payload_bytes = 28672
```
删除字段：transport、content_mode、allowed_hosts、per-source filter、[filter]。
models.example.toml 结构不变，仅 `api_key_env` 值改为 `SCOUT_LLM_API_KEY`，
文档注明 V1 可选。

### 3.2 环境/命名
- 包 signalfeed/ → scout/；CLI `python -m scout [--dry-run|--send|--config|--models-config]`
- 环境变量：SIGNALFEED_LLM_API_KEY → SCOUT_LLM_API_KEY（可选）；
  SIGNALFEED_DB_PATH → SCOUT_DB_PATH（默认 data/scout.sqlite3，全新库）
- FEISHU_* 四变量不变；.env.example、models.example.toml 同步更新
- pyproject name="scout"，描述改为个人自用 AI 侦察工具

### 3.3 数据流（app.py 重写为单源精简流）
采集(RSSCollector) → 批内去重 → 首轮建基线不补发 → storage.unseen 持久去重
→ max_age_days 门槛 → 解析日报(content:encoded → 概览结构) → 构建 1 条飞书富文本
→ dry-run 打印或发送 → record_delivered → 失败按 active_failures 一次性告警
→ 退出码：有失败为 1，否则 0；汇总输出 sent/failed/baseline/skipped
- dry-run 语义保持：真实拉取 feed、只读数据库、不要求飞书配置、不写任何状态
- 全程无 LLM 调用

### 3.4 飞书消息（每期一条）
- 标题：`YYYY-MM-DD · Scout · 橘鸦AI早报`
- 正文：解析 content:encoded 的"概览"段 → 每个分类一小节（h3 文本），分类下每条
  为"原文标题（蓝色链接到外链）"；文末附"查看全文"链接（issues URL）
- 消息须 ≤28KiB（概览约 1-3KB，天然满足，仍保留预检）
- 解析器（新模块 scout/digest.py，stdlib HTMLParser）输出结构：
  issue_date、overview: list[{category, headline, url, number}]、
  sections: list[{title, url, summary(blockquote), detail, related_links}]、
  page_url。V1 消息只用 overview+page_url；完整解析为将来按条个性化打基础，
  并用真实 feed 样本做 fixture 测试

### 3.5 LLM 入口（新模块 scout/llm.py，极薄）
- 从 config.py 移植 load_models_config 与 resolve_api_key 的校验逻辑，改为可选：
  --models-config 提供且 SCOUT_LLM_API_KEY 存在时才加载校验
- 提供唯一函数：按 models.toml 构造 AsyncOpenAI 客户端（沿用超时 60s、关闭 SDK
  重试、store=false），无提示词、无 Schema、无业务调用
- 删除 summarizer.py 与 tests/test_summarizer.py；storage.py 删除
  chinese_summary_cache 表及 cached_summary/cache_summary 方法与相关测试

## 4. 实施步骤（每步保持测试绿，独立提交）

### Step 1 清场
- 删 deploy/、.github/workflows/deploy.yml、docs/deploy-from-scratch.md、
  docs/product-vision.md、docs/multi-source-specification.md、
  docs/single-source-specification.md、signalfeed/reader.py、
  tests/test_reader.py、signalfeed/summarizer.py、tests/test_summarizer.py
- plan/ 下旧计划已删除，仅保留本文件

### Step 2 改名
- git mv signalfeed scout；全仓库替换 signalfeed→scout、SignalFeed→Scout、
  SIGNALFEED_→SCOUT_（含 pyproject、.env.example、models.example.toml、
  config.py 默认值、network UA、全部测试 import）
- 本地 .env 与 models.toml 同步改键名
- 验证：四条质量命令全绿

### Step 3 精简采集与配置
- collector.py 删除 MarkdownIndex/MarkdownChangelog/MarkdownCards/NextDataIndex
  四个 collector 及其 markdown/next_data 解析辅助函数、changelog_dedupe_key，
  COLLECTOR_REGISTRY 仅剩 rss；保留 _BaseCollector/_fetch_bytes（含瞬时错误
  重试一次）/clean_html/normalize_date/canonicalize_url
- config.py 校验改为 3.1 新 schema（删 transport/content_mode/allowed_hosts/
  filter 等字段校验）；config.toml 重写为单源；models 配置改可选（3.5）
- 同步删改测试：test_collector 仅留 RSS 部分，test_config 按新 schema 重写

### Step 4 日报解析与消息
- 新增 scout/digest.py + tests/test_digest.py（用 1.1 节结构做 fixture，至少
  覆盖：概览分类/链接/编号、blockquote 摘要、相关链接、图片剔除、坏 HTML 容错）
- notifier.py：新增 build_issue_digest（3.4 格式），删除旧逐篇 build_digests 与
  ChineseSummary 应用路径
- app.py 重写为 3.3 精简流（保留：批内/持久/次级身份去重、基线原子建立、
  age gate、失败隔离、一次性告警、退出码、dry-run 只读语义；删除：跨源优先级、
  Jina 正文、LLM 摘要、关键词接线、Semaphore 并发摘要）
- storage.py 删摘要缓存表与方法（若 Step 1 未覆盖）
- 删除 test_multisource_app.py、test_app.py 中多源/摘要相关用例，新写单源流
  测试（空库基线、二次去重、dry-run 零写入、单条失败不记账、告警一次/恢复）

### Step 5 过滤模块保持不接线
- filter.py 与 test_filter.py 原样保留（后置功能的独立纯函数，不接线）

### Step 6 LLM 入口
- 新增 scout/llm.py（3.5 极薄入口）+ 对应测试（构造参数、可选性、缺失不报错）
- app/__main__ 中仅做可选加载校验，不实例化业务调用

### Step 7 文档
- README.md 重写：Scout 定位（个人自用、AI 话题、规则过滤饭圈化的初衷）、
  单信源配置、运行方式（本地手动，无调度）、"当前非目标"（LLM 个性化/知乎
  接入/关键词过滤/定时部署）
- 新增 docs/scout-spec.md：目标形态、配置契约、数据流、消息格式、非目标
- 保留 plan/scout-rebuild-plan.md 即本文件

### Step 8 知乎调研文档
- 产出 docs/zhihu-research.md，以 1.2 节为素材组织：平台现状与邀测申请路径、
  HTTP API 鉴权与端点、Zhihu CLI 能力矩阵（搜索/热榜/直答/个人数据/知识库，
  及各自对"AI 话题 + 过滤饭圈化"场景的适配度）、凭证与安全边界、
  与 Scout 集成设计草案（未来 collector="zhihu_hot_list" 等，subprocess 调 CLI
  与直连 HTTP API 两条路线的取舍）、风险（邀测门槛、配额计费、单账号限制、
  内容质量）、下一步行动（邮件申请邀测）
- 不写任何生产代码

### Step 9 最终验证
```
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q scout tests
uv run --locked python -m scout --dry-run   # 无 models.toml 也应可运行
uv run --locked python -m scout --send      # 用户确认后执行一次真实推送
```

## 5. 明确范围外（V1 不做）
- LLM 个性化推荐与反馈学习（仅保留 scout/llm.py API 入口）
- 知乎代码接入（仅 Step 8 调研文档；邀测申请是用户手工事项）
- 关键词过滤接线（filter.py 保留待用）
- 定时调度、systemd、CD 重启（全部停用；将来如恢复另立计划）
