# Scout 规格（V1）

本文档是侦察兵（Scout）V1 的目标形态、配置契约、数据流、消息格式与非目标
的权威说明。实现与测试以此为准。

## 1. 定位

- 个人自用的信息采集工具，当前只服务 AI 话题。
- 动机：简体中文社区关于 DeepSeek 等 AI 话题的讨论饭圈化、情绪化，用规则
  过滤低质样本，只保留一线开发体验、不玩梗的内容。
- V1 只接入一个经人工策展的 RSS 信源（橘鸦AI早报）并推送飞书；未来接入
  更多优质信源与知乎。

## 2. 配置契约

### 2.1 config.toml

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

校验规则（快速失败）：

- 顶层只允许 `sources`、`network`、`feishu` 三个键；旧 `[source]` 单表与
  旧 `[filter]` 表一律拒绝。
- 每个 `[[sources]]` 必须包含 `name`、`url`、`collector`、`window_size`，
  可选 `max_age_days`（正整数）；未知字段拒绝。
- `collector` 枚举目前只允许 `rss`，注册表保留扩展位。
- `network`：`timeout_seconds` 为正数、`max_bytes` 为正整数、`user_agent`
  非空；`feishu` 只允许 `max_payload_bytes` 且不超过 30720（飞书 30 KiB
  富文本限制，为 OpenAPI 封装预留约 2 KiB）。
- URL 必须是无凭据的 HTTP(S) 地址；源名大小写不敏感唯一，且是 SQLite
  持久化身份，建立基线后不应改名。
- 已删除字段：`transport`、`content_mode`、`allowed_hosts`、逐源 `filter`
  与全局 `[filter]`。

### 2.2 环境变量

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `SCOUT_DB_PATH` | 否 | SQLite 路径，默认 `data/scout.sqlite3`（全新库） |
| `SCOUT_LLM_API_KEY` | 否 | V1 不调用 LLM；仅当与 models.toml 同时存在时校验 |
| `FEISHU_APP_ID` | `--send` | 飞书企业自建应用凭证 |
| `FEISHU_APP_SECRET` | `--send` | 同上 |
| `FEISHU_RECEIVE_ID_TYPE` | `--send` | chat_id/open_id/union_id/user_id/email |
| `FEISHU_RECEIVE_ID` | `--send` | 对应的群聊或用户 ID |

### 2.3 models.toml（可选）

`models.example.toml` 结构不变，仅 `api_key_env` 固定为
`SCOUT_LLM_API_KEY`。V1 数据流完全不调用 LLM：只有 `models.toml` 存在且
`SCOUT_LLM_API_KEY` 已设置时才加载校验，任一缺失可正常运行，存在则严格
校验（恰一个模型、四个固定字段、`openai_responses` 协议、无凭据端点）。

## 3. 数据流

```
采集(RSSCollector) → 批内去重 → 首轮建基线不补发 → storage.unseen 持久去重
→ max_age_days 门槛 → 解析日报(content:encoded → 概览结构) → 构建 1 条飞书
富文本 → dry-run 打印或发送 → record_delivered → 失败按 active_failures
一次性告警 → 退出码
```

保留语义：

- 批内去重：同一批次内相同 dedupe key 只取首个，其余计 skipped。
- 持久去重：`unseen()` 同时比对送达记录与首次基线的 dedupe key、
  （来源、条目 id）与（来源、URL）次级身份，URL 形态变化不重推。
- 基线：来源未初始化时原子建立（`source_state` + `baseline_items`），不
  补发历史；存在解析 issue 时基线推迟，直到该批无 issue。
- 年龄门槛：`max_age_days` 在去重后生效，超龄条目跳过并聚合告警一次；
  无超龄条目的轮次清除该活动告警。
- 失败隔离：解析（digest）、构建（message）、发送（send）、记账（record）
  任一失败只影响当前条目，后续条目继续；来源级失败只影响该源。
- 一次性告警：同一 `(source, item_key)` 事件在恢复前只告警一次；送达、
  基线建立或来源恢复采集清除活动故障。
- 退出码：本轮有任一失败为 `1`，否则 `0`；结束打印
  `sent/failed/baseline/skipped/previewed` 汇总。
- dry-run：真实拉取 feed、只读数据库、不要求飞书配置、不写任何状态
  （不建目录、不建库、不写表）。

已删除：跨源优先级、Jina 正文抓取、LLM 摘要与中文缓存、关键词接线、
Semaphore 并发摘要。

## 4. 日报解析（scout/digest.py）

输入为 RSS `content:encoded` 的完整 HTML，stdlib `HTMLParser` 解析，坏
HTML 不抛异常只产生更少条目。输出结构：

```
issue_date                       # 从 <h1>AI 早报 YYYY-MM-DD</h1> 提取
overview: [(category, headline, url, number), ...]
sections: [(title, url, summary, detail, related_links), ...]
page_url                         # 参数传入的 issues URL，或"查看网页全文"链接
```

- 概览段：`<h2>概览</h2>` 下按 `<h3>分类</h3>` 分组，
  `<li>标题 <a>↗</a> <code>#N</code></li>` 提取标题、链接与编号。
- 正文段：`<hr>` 后的每条新闻 `<h3>`（常含原文链接）+ `<blockquote>`
  一句话摘要 + 首个非空 `<p>` 详情 + `<ul>` 相关链接；图片剔除。
- V1 消息只用 overview + page_url；sections 完整解析为将来按条个性化
  打基础。

## 5. 消息格式

- 每期一条飞书富文本（post）。
- 标题：`YYYY-MM-DD · Scout · {来源}`，日期取日报 issue_date，缺失时用
  运行日。
- 正文：每个概览分类一小节（分类名文本行），分类下每条为蓝色链接到外链
  的原文标题；文末附"查看全文"链接（issues URL）。
- 预检：编码后必须 ≤ `feishu.max_payload_bytes`（概览约 1–3 KB，天然
  满足）；空概览视为消息构建失败。

## 6. 非目标（V1 明确不做）

- LLM 个性化推荐与反馈学习（仅保留 `scout/llm.py` API 入口）。
- 知乎代码接入（仅调研文档；邀测申请是用户手工事项）。
- 关键词过滤接线（`scout/filter.py` 保留待用）。
- 定时调度、systemd、CI 部署（全部停用）。
