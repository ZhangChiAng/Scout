# Scout（侦察兵）

Scout 是个人自用的信息采集工具，当前只服务 AI 话题。简体中文社区关于
DeepSeek 等 AI 话题的讨论越来越饭圈化、情绪化，Scout 的初衷是用规则过滤
低质样本，只保留一线开发体验、不玩梗的内容。

V1 只接入一个经人工策展的优质信源——[橘鸦AI早报](https://daily.juya.uk/)的
RSS：每天一期，Scout 解析当日"概览"并推送一条飞书富文本消息。内容本身已是
人工策展的中文日报，因此**不抓原文、不调用 LLM**。未来会接入更多优质信源。

## 项目文档

- [目标形态与规格](docs/scout-spec.md)
- [知乎接入调研](docs/zhihu-research.md)
- [重建计划](plan/scout-rebuild-plan.md)

## 准备

需要 [uv](https://docs.astral.sh/uv/) 和 CPython 3.14：

```bash
uv python install 3.14
uv sync --locked
```

在项目根目录创建权限为 `0600` 的 `.env`，再替换其中的占位值。进程中已存在
的同名环境变量优先，文件不会覆盖它们：

```bash
install -m 0600 .env.example .env
```

`--send` 需要以下四项飞书企业自建应用配置：

- `FEISHU_APP_ID`：应用凭证中的 App ID；
- `FEISHU_APP_SECRET`：应用凭证中的 App Secret；
- `FEISHU_RECEIVE_ID_TYPE`：接收者 ID 类型，允许 `chat_id`、`open_id`、`union_id`、`user_id` 或 `email`；
- `FEISHU_RECEIVE_ID`：与上述类型对应的群聊或用户 ID。

`SCOUT_DB_PATH` 可选，默认 `data/scout.sqlite3`。`.env`、`models.toml` 和
`data/` 都不会被 Git 跟踪。应用错误不会回显模型 API Key、App Secret、接收者
ID 或带凭据的 URL。

## 配置

`config.toml` 是唯一的业务配置。V1 只有一个 `[[sources]]`：

```toml
[[sources]]
name = "juya-ai-daily"
url = "https://daily.juya.uk/rss.xml"
collector = "rss"        # 唯一合法值，枚举保留为将来 zhihu 等扩展
window_size = 7
max_age_days = 3

[network]
timeout_seconds = 15
max_bytes = 5242880
user_agent = "Scout/0.1"

[feishu]
max_payload_bytes = 28672
```

`name` 同时是 SQLite 持久化身份，建立基线后不要随意改名。`collector` 目前
只接受 `rss`；配置校验是快速失败，未知字段、非法枚举、越界数值都会在启动
时直接报错。`max_age_days` 在去重之后生效，发布日期超过该天数的新一期不再
推送，只计入跳过并发送一次性告警；无法解析的发布时间保守放行。

文章身份以规范化 URL 为主键，另以（来源、条目 id）与（来源、URL）作为送达
记录和首次基线共同的次级身份，防止信源改版更换 URL 形态后重推历史条目。

## 运行

V1 全部本地手动运行，没有定时调度，也没有部署：

```bash
# 预览：真实拉取 feed、只读数据库、不要求飞书配置、不写任何状态
uv run --locked python -m scout --dry-run

# 发送：构建并推送一条当日日报消息
uv run --locked python -m scout --send
```

`--send` 会在采集前完整校验四项飞书环境变量，缺失或接收者类型非法时立即
失败。数据库为空时首次运行只建立窗口基线、不补发历史；`--dry-run` 只预览
基线。结束输出 sent/failed/baseline/skipped 汇总，本轮存在任何失败时退出
码为 `1`，全部成功为 `0`。

来源或单条消息失败时，会向同一飞书目标发送包含来源、文章和失败阶段的小型
告警；同一故障事件在恢复前不重复提醒，成功送达或来源恢复采集会清除活动
故障。告警自身失败不递归告警，下轮继续尝试。

## 消息格式

每期一条消息，标题 `YYYY-MM-DD · Scout · {来源}`。正文解析日报 HTML 的
"概览"段：每个分类一小节，分类下每条为蓝色原文链接，文末附"查看全文"链接。
概览约 1–3 KB，天然满足 28 KiB 预检上限。

## LLM 与 models.toml（V1 可选）

`models.toml` 在 V1 完全不参与数据流。仅当 `models.toml` 存在**且**
`SCOUT_LLM_API_KEY` 已设置时才会加载校验；两者任一缺失都可正常运行。文件
存在但内容非法则快速失败。协议固定 `openai_responses`、`api_key_env` 固定
`SCOUT_LLM_API_KEY`。`scout/llm.py` 只保留构造客户端的入口（60 秒超时、
关闭 SDK 重试、`store=false`），无提示词、无 Schema、无业务调用。

## 测试

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q scout tests
```

## 当前非目标

- LLM 个性化推荐与反馈学习（仅保留 `scout/llm.py` API 入口）；
- 知乎代码接入（仅完成[调研文档](docs/zhihu-research.md)，邀测申请是手工事项）；
- 关键词过滤接线（`scout/filter.py` 作为后置功能的独立纯函数保留待用）；
- 定时调度、systemd、CI 部署（全部停用，将来如恢复另立计划）；
- 飞书成功与本地 SQLite 提交无法形成跨系统原子事务，极端情况下可能重复
  发送一次。
