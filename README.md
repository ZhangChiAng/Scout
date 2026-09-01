# Scout（侦察兵）

Scout 是单一 owner 自用的 AI 新闻个性化工具。它从
[橘鸦 AI 早报](https://daily.juya.uk/) RSS 读取每期“概览”，把每条新闻与对应
人工摘要、详情和相关链接信息规范化后，按原顺序交给 LLM 判断，再逐条发送飞书
交互卡片。第一版不按判断过滤或排序：`推荐 / 不推荐 / 不确定` 都会发送，并附
简短、具体的个性化理由。

Scout 不抓新闻原文，不接知乎或其他信源，不使用 embedding、向量数据库、数值
兴趣分，也不做多用户产品设计。权威行为见 [Scout 规格](docs/scout-spec.md)。

## 准备

需要 [uv](https://docs.astral.sh/uv/) 和 CPython 3.14：

```bash
uv python install 3.14
uv sync --locked
install -m 0600 .env.example .env
cp models.example.toml models.toml
```

编辑 `.env`：

- `SCOUT_LLM_API_KEY`：个性化 `--send`、`--dry-run` 必需；
- `FEISHU_APP_ID`、`FEISHU_APP_SECRET`：listener、发送与校准必需；
- `FEISHU_RECEIVE_ID_TYPE`、`FEISHU_RECEIVE_ID`：发送与校准目标；
- `SCOUT_DB_PATH`：可选，默认 `data/scout.sqlite3`。

编辑 `models.toml`，配置唯一一个支持 OpenAI Responses 契约的模型端点。协议固定
为 `openai_responses`，环境变量名固定为 `SCOUT_LLM_API_KEY`。端点必须支持
`text.format` JSON Schema、`store=false`、`output_text`、`status` 与
`incomplete_details`；Scout 不降级成自由文本解析。

`config.toml` 只配置橘鸦 RSS、网络限制和飞书卡片最大字节数。来源名是 SQLite
持久化身份，建立基线后不要修改。

## 飞书应用

使用一个已发布的企业自建应用：

1. 启用机器人能力，并授予机器人发送消息所需权限；
2. 在事件与回调配置中选择“使用长连接接收回调”；
3. 添加 `card.action.trigger` 回调，保存并发布应用版本；
4. 把机器人加入 `FEISHU_RECEIVE_ID` 对应的真实群聊；
5. 启动 listener，再执行校准。

长连接不需要公网回调地址。listener 只在回调中校验并写短 SQLite 事务，不调用
LLM；当前 `lark-oapi` 对 CARD 帧的兼容处理已包含在项目中。

## 首次校准

先常驻启动反馈 listener：

```bash
uv run --locked python -m scout --listen-feedback
```

另开终端，把最新一期全部概览条目各发一张校准卡片：

```bash
uv run --locked python -m scout --calibrate
```

`--calibrate` 不需要模型，忽略旧的整期送达状态；同一期重复执行只补发尚未成功
记账的校准卡片。每张卡片先选“喜欢”或“不喜欢”，再填写必填的 1–500 字原因。
选择、校验和提交反馈时都会保留原新闻正文，只切换卡片底部的反馈区。
第一次有效提交原子绑定当前飞书 `open_id` 为唯一 owner。相同重试幂等，修改反馈
会新增不可变修订。

至少完成 2 条喜欢和 2 条不喜欢后运行：

```bash
uv run --locked python -m scout --send
```

发送前会生成并激活偏好档案 v1，先发送一张档案变化卡，再评价新条目。偏好更新
失败时本轮不会沿用旧档案；某个 8 条评价批次失败只隔离该批，其他批次继续。

## 日常命令

```bash
# 真实拉 RSS 和调用模型，但 SQLite 完全零写入，也不联系飞书
uv run --locked python -m scout --dry-run

# 更新档案、发送档案通知和逐条新闻卡片
uv run --locked python -m scout --send

# 查看当前档案和版本历史；二者只读且不需要模型或飞书
uv run --locked python -m scout --profile-show
uv run --locked python -m scout --profile-history

# 复制旧版本为新的活动版本；现有反馈会标记已处理，避免立即反弹
uv run --locked python -m scout --profile-rollback 1
```

条目成功发送后立即保存 `message_id/chat_id`。只有全部条目已成功发送或已跨期
送达时才记录整期完成，因此局部失败下次只补缺项。sender 使用进程锁防止 timer
与人工运行重叠；listener 与 sender 使用 5 秒 SQLite busy timeout 和短事务。

## systemd 用户服务

仓库位于默认的 `%h/workspace/signal-feed` 时，可以直接安装；如果路径不同，先
修改三个 unit 的 `WorkingDirectory`、`EnvironmentFile` 和 `ExecStart`。

```bash
mkdir -p ~/.config/systemd/user
ln -sf "$PWD/systemd/scout-feedback.service" ~/.config/systemd/user/
ln -sf "$PWD/systemd/scout-send.service" ~/.config/systemd/user/
ln -sf "$PWD/systemd/scout-send.timer" ~/.config/systemd/user/
systemctl --user daemon-reload

# 先启动 listener，并完成飞书回调发布与真实校准
systemctl --user enable --now scout-feedback.service
journalctl --user -u scout-feedback.service -f

# 校准和一次人工 --send 验收通过后再启用 timer
systemctl --user enable --now scout-send.timer
systemctl --user list-timers scout-send.timer
```

timer 使用 `Asia/Shanghai` 的 `10:00 / 12:30 / 21:00`，并设置
`Persistent=true`。需要退出登录后仍常驻时，由系统管理员为该用户启用 linger。

## 工程检查与已知边界

项目不编写单元测试；需要验收时只使用真实 RSS、当前模型端点、真实 SQLite 和
真实飞书群做端到端验收。不涉及飞书端到端验收的改动不新增测试门禁。可执行的
本地静态检查只有：

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q scout
```

飞书成功与 SQLite 提交无法形成跨系统原子事务，进程恰好在两者之间崩溃时仍有
可能重复发送一次；这是保留的已知限制。
