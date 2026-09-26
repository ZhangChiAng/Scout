# Scout（侦察兵）

Scout 是单一 owner 自用的 AI 新闻个性化工具。它从
[橘鸦 AI 早报](https://daily.juya.uk/) RSS 读取每期“概览”，把每条新闻与对应
人工摘要、详情和相关链接信息规范化后，按原顺序交给 LLM 判断。`推荐 / 不确定`
按原顺序发送完整飞书卡片；`不推荐` 收在当期末尾的标题勾选列表中。勾选多条后
点击“推送所选正文”，即可在同一个群按原顺序收到正文和当时的判断理由。

橘鸦业务只使用 RSS 内容，不使用 embedding、向量数据库、数值兴趣分，也不做
多用户产品设计。另有独立的[知乎本机采集](docs/zhihu-validation.md)：通过本机
HTTP 采集正文，按配置筛选，自动发送标题和原文链接。权威行为见 [Scout 规格](docs/scout-spec.md)。

## 准备

需要 [uv](https://docs.astral.sh/uv/) 和 CPython 3.14：

```bash
uv python install 3.14
uv sync --locked
install -m 0600 .env.example .env
cp models.example.toml models.toml
```

编辑 `.env`：

- `SCOUT_LLM_API_KEY`：个性化发送、只读评价、偏好更新及重整命令必需；
- `FEISHU_APP_ID`、`FEISHU_APP_SECRET`：listener、发送与校准必需；
- `FEISHU_RECEIVE_ID_TYPE=chat_id`、`FEISHU_RECEIVE_ID`：发送、listener 与校准的群目标；
- `SCOUT_DB_PATH`：可选，默认 `data/scout.sqlite3`。

编辑 `models.toml`，在 `[model]` 下只设置 `model` 和 `base_url`。模型端点支持 OpenAI Responses 契约。协议固定
为 `openai_responses`，环境变量名固定为 `SCOUT_LLM_API_KEY`。端点必须支持
`text.format` JSON Schema、`store=false`、`output_text`、`status` 与
`incomplete_details`；Scout 不降级成自由文本解析。
偏好归纳和评价均设置 `reasoning.effort=max`，保持 `max_output_tokens=65536`、
600 秒超时，关闭 SDK 自动重试。

`config.toml` 使用单个 `[source]` 配置橘鸦 RSS、网络限制和飞书卡片最大字节数。来源名是 SQLite
持久化身份，建立基线后不要修改。

## 飞书应用

使用一个已发布的企业自建应用：

1. 启用机器人能力，并授予机器人发送消息所需权限；
2. 在事件与回调配置中选择“使用长连接接收回调”；
3. 添加 `card.action.trigger` 回调，保存并发布应用版本；
4. 把机器人加入 `FEISHU_RECEIVE_ID` 对应的真实群聊；
5. 启动 listener，再执行校准。

长连接不需要公网回调地址。listener 只在回调中校验并写短 SQLite 事务，不调用
LLM，也不在回调中发送正文。listener 内的单个后台线程立即处理持久化正文请求；
当前 `lark-oapi` 对 CARD 帧的兼容处理已包含在项目中。

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
失败时本轮不会沿用旧档案；每批最多评价 8 条，失败后的批次可继续评价并缓存，
正文发送在首个缺项处暂停，以保持原顺序。补齐评价和正文后才发末尾列表。

## 日常命令

```bash
# 真实拉 RSS 和调用模型，但 SQLite 完全零写入，也不联系飞书
uv run --locked python -m scout --dry-run

# 人工更新档案，按日期从早到晚补齐有效期内未推送的日报
uv run --locked python -m scout --send

# 定时入口：仅在北京 09:30–12:30 启动，使用最近一次已保存的偏好
uv run --locked python -m scout --send --scheduled

# 每日 19:00 的偏好更新入口；只处理当天 19:00 前已保存的反馈
uv run --locked python -m scout --profile-update

# 精确补发一期，忽略年龄和首轮基线；不会重发已送达正文或已呈现标题
uv run --locked python -m scout --send --issue-date 2026-09-02
uv run --locked python -m scout --dry-run --issue-date 2026-09-02

# 查看当前档案和版本历史；二者只读且不需要模型或飞书
uv run --locked python -m scout --profile-show
uv run --locked python -m scout --profile-history

# 真实模型全量重整预览：显示完整结果及输入字符数，SQLite 零写入，不发送消息
uv run --locked python -m scout --profile-rebuild-preview

# 全量重整、启用新版本并通知飞书群；只处理偏好，不进入新闻推送流程
uv run --locked python -m scout --profile-rebuild

# 复制旧版本为新的活动版本；现有反馈会标记已处理，避免立即反弹
uv run --locked python -m scout --profile-rollback 1
```

`--issue-date` 只可搭配 `--send` 或 `--dry-run`，必须为 `YYYY-MM-DD`。优先读取
RSS；该日期已退出 RSS 或 RSS 暂时不可达时，使用 SQLite 中已有的完整日报快照。
没有完整快照会明确报错，不会换成其他日期。有效期外的历史日报需逐期明确指定。
`--scheduled` 只可搭配 `--send`，不能同时使用 `--issue-date`。
所有模式互斥；两个重整命令均不接受 `--issue-date`。正式重整、sender、校准和
回滚、每日偏好更新共用写操作锁。重整通知失败后新档案仍然生效且保留待通知状态；重试优先补发
已生成版本，没有新反馈时不会再次调用模型生成。已通知版本再次手动重整仍会强制
生成新版本。

## 偏好增量归纳与全量重整

格式 v2 分为“判断规则、具体对象、专题兴趣、重要疑问”。规则表达跨新闻适用的
条件与例外；具体对象记录产品、公司与 owner 的关系；专题保留不能被通用规则
充分表达的明确兴趣；疑问只保留确实影响推荐且现有反馈不能解决的歧义。
判断规则争取不超过 10 条、重要疑问争取不超过 3 条，不机械截断。

日常更新输入当前精简偏好及上次成功更新后新增的有效反馈（倾向、原因和原新闻
标题、摘要、详情，首期保留完整上下文）。模型输出完整新偏好，可合并、改写和
删除；重复反馈只用于印证，不自动增加规则，不复述案例或从单篇评价推断整个领域
的喜恶。明确表达的对象、关系和兴趣应保留，不为每个兴趣推演未知边界。
即使内容保持不变，也保存版本、更新说明及已消费进度。

首次达到两条喜欢、两条不喜欢时全量归纳；旧格式切换、自上次全量后累计 20 条
真实反馈修订、修改已处理反馈、手动重整、回滚后首次有新反馈，都直接进行全量
重整，不先发一次增量请求。回调仍只记录反馈，在下一次偏好更新时应用这些触发
条件。自动偏好更新仅在北京时间每日 19:00 执行，之后提交的反馈留到次日；
白天定时发送使用最近一次保存的偏好和该版本已消费的反馈，不触发偏好更新。
人工 `--send` 与重整入口仍可主动更新偏好。

全量只使用每张卡片当前有效的最新版反馈及原新闻上下文，旧偏好仅用于变化说明。
每条结论必须关联有效依据，不要求每条反馈都形成结论。增量可引用本轮反馈或上版
条目，程序将条目引用展开为 SQLite 中的反馈依据；不存在或截止位置已被替换的
依据会被拒绝。新闻评价模型与飞书偏好卡仅收到可读内容，不携带历史证据 ID 集合。
旧版本仍可展示和回滚，完整历史及逐条证据保留在数据库中。

每轮先在一致的只读事务中固定反馈截止位置，随即关闭事务再调用模型；期间新到
反馈留待下一轮。新偏好、逐条依据、活动版本、更新方式和消费进度在一个短事务中
提交。修订数使用真实行数，不能用 ID 差值代替；模型或校验失败不推进进度，发送
流程停止。19:00 更新失败保留原偏好和未消费反馈，次日 19:00 重试；
白天仍使用原偏好。没有可用偏好时只保存日报，不发送新闻。首期不分批归纳历史，超出模型容量时明确失败，不静默删减历史。

命令日志及 `--profile-show/--profile-history` 显示更新方式、输入反馈条数、
真实修订条数、判断规则条数、可读偏好字符数及输入字符数。`input_chars` 是
instructions、JSON 输入和输出 schema 的字符数总和，**不是 token 数**；
`revisions_since_rebuild` 记录本轮开始时距前次全量的修订数，
`last_full_feedback_revision_id` 保存成功全量的截止位置。

条目成功发送后立即保存 `message_id/chat_id`。“已在列表呈现”也计入本期完成和
自动去重，但不等于“正文已送达”。没有不推荐条目时不发空列表；全是不推荐时只发
列表；长列表按容量拆成编号卡片，保留完整标题。列表与评价在生成后固定，偏好
变化不会改写旧列表。查看正文不会新增喜欢记录，也不会改变偏好。

只有已有的唯一 owner 可以请求正文。未选条目可随时追加，发送中和已送达条目禁用
重复选择。每次选择中的失败项最多自动尝试三次，之后在列表恢复勾选重试；重启
listener 会恢复尚未完成的请求。正文仍提供“喜欢／不喜欢＋原因”和修改反馈。
只有实际提交的反馈才会进入下一次偏好更新。

sender 和 listener 各有独立进程锁。SQLite 使用短事务及 5 秒 busy timeout；
新增正文请求回调的锁等待缩短至 500 毫秒，避免超过飞书回调时限。列表刷新失败
也独立重试，正文成功后刷新失败不会导致正文重发；再次提交或重启会重试刷新。

## systemd 用户服务

仓库位于默认的 `%h/workspace/scout` 时，可以直接安装；如果路径不同，先
修改 service unit 的 `WorkingDirectory`、`EnvironmentFile` 和 `ExecStart`。

```bash
mkdir -p ~/.config/systemd/user
ln -sf "$PWD/systemd/scout-feedback.service" ~/.config/systemd/user/
ln -sf "$PWD/systemd/scout-send.service" ~/.config/systemd/user/
ln -sf "$PWD/systemd/scout-send.timer" ~/.config/systemd/user/
ln -sf "$PWD/systemd/scout-profile-update.service" ~/.config/systemd/user/
ln -sf "$PWD/systemd/scout-profile-update.timer" ~/.config/systemd/user/
systemctl --user daemon-reload

# 先启动 listener，并完成飞书回调发布与真实校准
systemctl --user enable --now scout-feedback.service
journalctl --user -u scout-feedback.service -f

# 校准和一次人工 --send 验收通过后再启用 timer
systemctl --user enable --now scout-send.timer scout-profile-update.timer
systemctl --user list-timers scout-send.timer scout-profile-update.timer
```

两个 timer 均使用 `Asia/Shanghai` 和 `Persistent=false`，不会在重启时补触发。
新闻每天 **09:30、10:00、10:30、11:00、11:30、12:00、12:30** 检查；
偏好每天 **19:00** 更新并通知变化。12:30 启动的任务允许运行至完成。
需要退出登录后仍常驻时，由系统管理员为该用户启用 linger。

白天先检查 `issue_snapshots`：当天已有完整快照就不再请求 RSS，重启后仍有效。
没有当天快照时抓取 RSS，并在模型调用前保存有效期内的完整日报；解析或保存失败
不算抓取成功。合并 RSS 与本地快照后按日期从早到晚处理，保留原基线和去重规则。
`max_age_days=3` 仅跳过发布时间的北京日期早于“今天减 3 天”的日报。
发送失败从 SQLite 重试未完成部分，较早一期未完成时暂停后续日期；全已完成时
跳过模型与飞书阶段。12:30 后才发布的内容次日补抓。

运行日志包括 RSS 请求起止、发布时间、返回期次、首次新闻快照保存时间、跳过
抓取原因，以及偏好、评价缓存、正文、列表和整期完成状态。`first_saved_at` 来自
该日报最早条目快照的保存时间，不代表每次网络抓取时间。

查看当前服务状态与日志：

```bash
systemctl --user list-timers scout-send.timer scout-profile-update.timer
systemctl --user status scout-feedback.service scout-send.service scout-profile-update.service
journalctl --user -u scout-feedback.service -u scout-send.service -u scout-profile-update.service --since today
uv run --locked python -m scout --profile-show
```

修改数据库结构或修复数据前，在 sender 写操作锁内使用 SQLite backup API 备份
真实库，并检查备份的完整性。不要通过复制正在写入的数据库文件替代备份。

## 工程检查与已知边界

项目不编写单元测试；需要验收时只使用真实 RSS、当前模型端点、真实 SQLite 和
真实飞书群做端到端验收。不涉及飞书端到端验收的改动不新增测试门禁。可执行的
本地静态检查只有：

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q scout
git diff --check
```

列表及按需正文在发送前持久化 UUID，并使用 SQLite 唯一约束。飞书对相同 UUID
提供 [1 小时发送去重](https://open.feishu.cn/document/server-docs/im-v1/message/create)，
可减少远端成功、本地提交前崩溃导致的重复。跨系统仍非原子事务，超过飞书去重
窗口的未知结果重试仍可能重复；普通新闻及档案通知在远端成功后才本地记账，
这一间隙中断也可能导致重试重复。真实验收要求见
[规格](docs/scout-spec.md#9-工程检查与验证边界)。

## 知乎本机采集

独立入口 `python -m scout.zhihu` 提供 `status`、`scan`、`score`、`preview`
和 `send-results`。本机 MediaCrawler 采集器以独立项目、环境和用户服务运行，
Scout 只通过 HTTP 接入；登录使用采集器的 Cookie 文件导入，`scan` 保存证据和报告，
现有 listener 的后台扫描线程负责任务恢复。

按配置搜索知乎完整正文，当前发送入口要求正文讨论 GPT-6 模型且含字面“斩杀线”，
最多按发现顺序推送 5 篇。`send-results` 前保存正文语境核对，随后固定内容、规则、
证据、目标群和 UUID，成功文章重复执行跳过。采集采用单次任务，没有周期采集 timer。

部署、命令和恢复说明见[知乎本机采集](docs/zhihu-validation.md)，Cookie 登录操作见
[Cookie 文件导入](docs/zhihu-cookie-import.md)。

配置格式与知乎旧命令的迁移步骤见 [重构迁移说明](docs/refactor-migration.md)。
