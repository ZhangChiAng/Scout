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

需要 [uv](https://docs.astral.sh/uv/)、CPython 3.14，以及可使用 Codex 和 GPT-6 Sol
的 ChatGPT 订阅。官方 `openai-codex` Python SDK 随包安装固定版本的 Codex
运行时，不需要单独安装 Codex CLI 或 Node：

```bash
uv python install 3.14
uv sync --locked
install -m 0600 .env.example .env
cp models.example.toml models.toml
```

编辑 `.env`：

- `FEISHU_APP_ID`、`FEISHU_APP_SECRET`：listener、发送与校准必需；
- `FEISHU_RECEIVE_ID_TYPE=chat_id`、`FEISHU_RECEIVE_ID`：发送、listener 与校准的群目标；
- `SCOUT_DB_PATH`：可选，默认 `data/scout.sqlite3`；
- `SCOUT_CODEX_HOME`：可选，默认 `~/.local/share/scout/codex`，保存 Scout 专用认证及运行状态。

`models.toml` 默认使用 GPT-6 Sol、medium 推理档位和 Fast 速度：

```toml
[model]
model = "gpt-6-sol"
reasoning_effort = "medium"
```

`model` 必填，`reasoning_effort` 省略时为 `medium`。旧配置中的 `base_url`
必须删除，`.env` 不再需要 `SCOUT_LLM_API_KEY`。模型不可用或认证失败会明确报错，
不会自动切换模型或回退到 API Key。

首次使用前登录 ChatGPT：

```bash
# 默认设备码登录：按终端显示的地址和验证码操作，最多等待 15 分钟
uv run --locked python -m scout.auth login

# 也可使用浏览器登录；设备码登录未启用时可选此方式
uv run --locked python -m scout.auth login --browser

# 查看订阅档位、SDK/运行时版本和状态目录；--refresh 请求刷新凭据
uv run --locked python -m scout.auth status
uv run --locked python -m scout.auth status --refresh

# 只注销 Scout 专用目录中的登录
uv run --locked python -m scout.auth logout
```

认证命令不要求飞书或数据库配置。SDK 管理登录、凭据保存和令牌刷新，不复用日常
`~/.codex` 状态。`SCOUT_CODEX_HOME` 必须是没有 `config.toml` 的专用目录，模型
只在 Scout 的 `models.toml` 中配置；继承的 MCP 或自定义 OpenAI 端点配置会报错。
凭据目录仅当前用户可访问；同一目录的认证操作和模型进程使用
独占锁，正在运行时再次使用会报忙。定时任务遇到未登录状态时提示人工登录，
不会自行启动浏览器。

Scout 按官方 [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) 和
[可信私有自动化认证](https://learn.chatgpt.com/docs/auth/ci-cd-auth) 文档接入，
用作单一所有者的个人工具。订阅仍受账户可用模型、额度及使用条款约束，不等于
通用 API 额度，也不代表对任意用途的合规保证。

偏好归纳和评价各自创建独立临时线程，使用 JSON Schema 结构化输出并继续验证业务
字段。运行时采用独立空工作目录、只读沙箱和禁止批准模式，不给线程分配执行环境、
动态工具或能力目录，关闭 shell、联网搜索、浏览器、插件、应用及子代理等能力，
不加载项目指令和记忆；非预期工具调用判为失败。
每次请求总截止时间为 600 秒，超时后中断并关闭运行时。Scout 不自动重试模型请求；
官方运行时仍可刷新认证并执行内部恢复。SDK 未公开输出 token 上限参数。

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
第一次有效提交原子绑定当前飞书 `open_id` 为唯一 owner。相同重试幂等，每张卡片只保存一条当前反馈；修改直接覆盖倾向、原因和更新时间，
不保留修改前的内容。卡片只显示当前倾向、原因和修改入口。

至少完成 2 条喜欢和 2 条不喜欢后运行：

```bash
uv run --locked python -m scout --send
```

发送前会生成并激活偏好档案 v1，先发送一张档案变化卡，再评价新条目。偏好更新
失败时本轮不会沿用旧档案；每批最多评价 8 条，普通失败后的批次可继续评价并缓存，
正文发送在首个缺项处暂停，以保持原顺序。补齐评价和正文后才发末尾列表。
认证失效、限流或额度耗尽时停止本轮剩余模型请求，保留已完成缓存，等待重新登录
或后续调度。

## 日常命令

```bash
# 真实拉 RSS 和调用模型，Scout 业务 SQLite 零写入，也不联系飞书
uv run --locked python -m scout --dry-run

# 人工更新档案，按日期从早到晚补齐有效期内未推送的日报
uv run --locked python -m scout --send

# 定时入口：仅在北京 09:30–12:30 启动，使用最近一次已保存的偏好
uv run --locked python -m scout --send --scheduled

# 每日 19:00 的偏好更新入口；处理任务开始时的当前反馈
uv run --locked python -m scout --profile-update

# 精确补发一期，忽略年龄和首轮基线；不会重发已送达正文或已呈现标题
uv run --locked python -m scout --send --issue-date 2026-09-02
uv run --locked python -m scout --dry-run --issue-date 2026-09-02

# 查看当前档案和版本历史；二者只读且不需要模型或飞书
uv run --locked python -m scout --profile-show
uv run --locked python -m scout --profile-history

# 真实模型全量重整预览：显示完整结果及输入字符数，业务 SQLite 零写入，不发送消息
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

两个预览命令的只读保证针对 Scout 业务 SQLite 和飞书消息；官方 SDK 仍可能
更新专用目录中的认证、缓存和运行状态，并使用独立的 Codex 进程锁。

## 偏好增量归纳与全量重整

格式 v2 分为“判断规则、具体对象、专题兴趣、重要疑问”。规则表达跨新闻适用的
条件与例外；具体对象记录产品、公司与 owner 的关系；专题保留不能被通用规则
充分表达的明确兴趣；疑问只保留确实影响推荐且现有反馈不能解决的歧义。
判断规则争取不超过 10 条、重要疑问争取不超过 3 条，不机械截断。

归纳在同一次模型请求中先确定有效含义，再合并重复表达，最后核对事实、范围、
否定、期限和例外。合并必须保留判断所需的具体背景，不能把已知工作领域或兴趣
边界缩成“与自己相关”等泛泛条件，也不能为不同理由杜撰共同解释。同类对象按
相同关系、范围和期限合并列出，保留全部名称及依据；不同关系或例外分别描述，
新增反馈只改变相关对象。对象主要记录关系，通用判断只表达一次。
明确的必要条件保留原有强度，不弱化成优先倾向；关注理由的主次比较不自动确认
或否定对象关系，关系须有明确依据。
疑问须对应无法确定的具体推荐判断，尚未收集完整使用产品名单不构成疑问。

日常更新输入当前精简偏好及上次成功更新后新增的有效反馈（倾向、原因和原新闻
标题、摘要、详情，首期保留完整上下文）。模型输出完整新偏好，可合并、改写和
删除；重复反馈只用于印证，不自动增加规则，不复述案例或从单篇评价推断整个领域
的喜恶。明确表达的对象、关系和兴趣应保留，不为每个兴趣推演未知边界。
单纯印证已有含义的反馈主要补充依据；合并或拆分旧条目时检查仍有效的信息是否
保留。较新的明确偏好更改替代同一范围内的旧判断，全量重整也不恢复已更改的
旧偏好；单篇倾向不同不自动推翻其他范围的偏好。
即使内容保持不变，也保存版本、更新说明及已消费进度。

首次达到两条喜欢、两条不喜欢时全量归纳；旧格式切换、自上次全量后累计 20 次
真实反馈变更、修改已处理反馈、手动重整、回滚后首次有反馈变更，都直接全量重整。
新增反馈通常增量更新；同一条未处理反馈的多次修改只输入最终内容。相同内容重复
提交不增加变更计数。回调只覆盖保存反馈，下一次偏好更新处理变化。

自动偏好更新在北京时间每日 19:00 启动，读取任务实际开始时的当前反馈；模型调用
期间的新增和修改留待下一轮。白天使用最近保存的规则，以及当前仍未被修改的已处理
反馈；已被覆盖的反馈不会按旧截止位置恢复。人工 `--send` 与重整入口可主动更新偏好。

全量只使用当前反馈及原新闻上下文，旧规则仅用于变化说明。每条结论关联稳定反馈
ID 和生成时的变更序号；增量还可引用上版条目，程序检查依据是否仍与当前反馈一致。
规则版本历史可展示和回滚，回滚不会恢复旧反馈正文。`outdated_evidence_count` 表示
依据所指的反馈已更新、生成时的原文不再保留的数量。

每轮用只读事务固定反馈及全局变更计数，释放事务后调用模型。新规则、依据标识和
处理位置在一个短事务内提交，期间发生的修改仍待处理。计数器每次实际覆盖或新增
成功提交才加一，不保存事件明细。模型或保存失败不推进进度；通知失败保留待发状态。
19:00 更新失败时保留原规则供白天使用，并在下一轮重试。没有可用规则时只保存日报。
输入超出模型容量时明确失败，不静默删减反馈。

命令日志及 `--profile-show/--profile-history` 显示更新方式、输入反馈数、变更次数、
规则条数、可读字符数和 `input_chars`。后者统计 instructions、JSON 输入和输出
schema 的字符数，**不是 token 数**。`last_feedback_change_seq` 是已处理位置，
`changes_since_rebuild` 是本轮开始时距上次全量的变更次数，
`last_full_feedback_change_seq` 保存成功全量的处理位置。

本机已将最新精简结果设为 V1（21 条偏好），反馈合并为 44 条当前值。旧规则版本、
旧反馈内容及其本地副本已清理，之后正常保存 V2、V3 等规则历史。历史筛选列表中的
负版本号表示重置前已删除的规则，避免与新版本序列混淆。

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
先以运行服务的同一用户完成 ChatGPT 登录；如自定义 `SCOUT_CODEX_HOME`，人工命令
和 systemd 服务须使用同一路径。SDK 按任务启动、关闭，无须新增常驻模型服务。

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

# ChatGPT 登录、目标模型、校准和一次人工 --send 验收通过后再启用 timer
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

运行日志包括实际模型、推理档位、请求耗时、可用的 token 用量和错误类别，不记录凭据。
同时记录 RSS 请求起止、发布时间、返回期次、首次新闻快照保存时间、跳过
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

项目不编写单元测试；需要验收时只使用真实 RSS、官方 SDK 的真实模型调用、真实 SQLite 和
真实飞书群做端到端验收。不涉及飞书端到端验收的改动不新增测试门禁。可执行的
本地静态检查只有：

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q scout
git diff --check
```

模型接入迁移须验证真实登录、状态查询、刷新、进程重启后的认证复用，以及
GPT-6 Sol 的 medium 推理和 Fast 速度。使用真实未缓存新闻执行预览，再在真实群
完成投递和反馈后的画像更新；不清空成功缓存或重放已完成日报来制造验收数据。
目标模型或结构化输出不可用时停止切换定时任务。认证失败可用临时独立状态目录
检查，不注销正式凭据；限流等未自然出现的场景如实记录为未验证，不故意耗尽额度。

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
