# Scout（侦察兵）

Scout 是个人自用的 AI 新闻工具，从[橘鸦 AI 早报](https://daily.juya.uk/) RSS
读取新闻，按偏好判断后发送飞书卡片。喜欢和不喜欢都通过卡片填写原因，供后续更新偏好。
也支持在飞书群里 @ 机器人描述话题，通过本机采集器搜索知乎；两种来源的偏好独立。

## 安装与配置

需要 uv、CPython 3.14，以及可使用配置模型的 ChatGPT 订阅。

```bash
uv python install 3.14
uv sync --locked
install -m 0600 .env.example .env
cp models.example.toml models.toml
```

编辑 `.env` 中的飞书应用凭据和目标群：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、
`FEISHU_RECEIVE_ID_TYPE=chat_id`、`FEISHU_RECEIVE_ID`。
数据库默认位于 `data/scout.sqlite3`，可用 `SCOUT_DB_PATH` 修改。
模型设置见 [models.example.toml](models.example.toml)，运行配置见 [config.toml](config.toml)。

```bash
uv run --locked python -m scout.auth login
uv run --locked python -m scout.auth status
```

Scout 使用专用认证目录，可用 `SCOUT_CODEX_HOME` 修改；该目录不能包含 `config.toml`。
模型在 `models.toml` 中配置，官方 SDK 随包提供运行时。

## 首次运行

飞书应用需启用机器人、发送权限、`im:message.reactions:write_only` 和 `im:message.group_at_msg:readonly`，
通过长连接接收 `card.action.trigger` 回调及 `im.message.receive_v1` 事件。
发布应用并将机器人加入目标群后，启动 listener：

```bash
uv run --locked python -m scout --listen-feedback
```

另开终端发送校准卡片，在群内完成至少两条喜欢和两条不喜欢的反馈，再开始推送：

```bash
uv run --locked python -m scout --calibrate
uv run --locked python -m scout --send
```

## 常用入口

```bash
# 预览：真实 RSS 和模型调用，不写业务 SQLite、不发送飞书
uv run --locked python -m scout --dry-run
# 查看偏好
uv run --locked python -m scout --profile-show
# 查看完整命令
uv run --locked python -m scout --help
uv run --locked python -m scout.auth --help
uv run --locked python -m scout.zhihu --help
```

知乎需要独立本机采集器；在 `.env` 设置 `ZHIHU_COLLECTOR_URL` 和
`ZHIHU_COLLECTOR_TOKEN`，令牌与采集器一致，健康接口需提供 `search_page` 和
`detail`。Cookie 仅保存在采集器项目。
在目标群使用真实 @ 提及发送话题；受理成功贴「收到」表情，受理失败用文字回复原因。
机器人搜索近 30 天内容，送达五条后暂停；
通过卡片继续、停止或填写反馈，也可用 `scout.zhihu status` 查看状态。
每篇使用 `[zhihu_content]` 的 `gpt-6.1-sol / low` 一次完成相关性、偏好筛选和摘要，
同时只评价一篇。“继续”在目标未满时补足，满额后再找五条。
同一篇只保存最新反馈。每次开始或继续固定当前有效偏好，学习失败保留上一有效版本。

已有数据库升级前先停止 listener、发送及偏好更新服务，运行
`uv run --locked python -m scripts.upgrade_zhihu_content`，再恢复服务。
升级会在数据目录备份数据库并终止旧扫描；旧卡片需重新发起搜索。

## 定时运行

用户服务和 timer 位于 [systemd/](systemd/)。安装到用户 systemd 目录前，
检查 unit 中的项目路径、环境文件及 uv 路径；完成登录和人工投递验收后再启用。
橘鸦推送在北京时间 09:30–12:30 每半小时检查，偏好每日 19:00 更新。

开发约束见 [AGENTS.md](AGENTS.md)。[handoff/](handoff/) 保存供后续 agent
审计和接手所需的脱敏信息，按主题维护；验收结果在回复中说明。
