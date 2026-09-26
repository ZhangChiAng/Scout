# 本机知乎采集器部署与恢复

此目录属于独立 MediaCrawler 检出 `/home/dev/workspace/mediacrawler-scout`。上游固定为
`380b426000aac3d612837ed72c99808347dc94c9`，适配分支为 `scout/local-zhihu-api`，HTTP 协议为 `1.0`。

`adapter/collector` 直接使用该固定版本的 `libs/zhihu.js` 签名，搜索参数来自
`media_platform/zhihu/client.py:get_note_by_keyword`，正文 JSON 实体格式来自
`media_platform/zhihu/help.py`。适配层不启动上游 WebUI，也不继承其中按首个实体取正文、
打印完整返回体或将任意网络错误当作登录失效的行为。上游代码和原始 `uv.lock` 保留，
适配器只安装 `adapter/uv.lock` 中的依赖。协议实现不依赖 Scout、LLM、Agent 会话或付费服务。

## 环境与版本

- Python 3.12.13；独立虚拟环境 `adapter/.venv`，依赖锁为 `adapter/uv.lock`。
- Node 24.18.0 独立安装在 `runtime/node`。官方归档 SHA256 为
  `55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742`；下载来源见
  `node-runtime.txt`，该版本官方校验列表见 `node-SHASUMS256.txt`。
- Playwright 1.61.0；其 Chromium 149.0.7827.55、浏览器修订 1228 位于 `runtime/browsers`。
- 用户服务 `mediacrawler-scout.service`；监听 `127.0.0.1:8765`，不暴露 CDP。
- `.env` 只含采集器 Bearer token 与请求间隔。Scout 的 `.env` 只保存
  `ZHIHU_COLLECTOR_URL` 和 `ZHIHU_COLLECTOR_TOKEN`；Cookie 不进入 Scout。
- 持久任务 SQLite：`data/scout/tasks.sqlite3`；会话：`data/scout/session`；
  受控证据：`data/scout/evidence`。这些路径均不纳入 Git。

## 可重复部署

先将此适配分支检出到固定项目目录。使用已安装的 uv 与 Python 3.12 运行：

```sh
uv sync --frozen --project /home/dev/workspace/mediacrawler-scout/adapter --python 3.12
```

从 `https://nodejs.org/dist/v24.18.0/node-v24.18.0-linux-x64.tar.xz` 下载归档，
使用上面的固定 SHA256 校验后解压，令 `runtime/node/bin/node --version` 返回 `v24.18.0`。
浏览器使用采集器自身的解释器安装：

```sh
PLAYWRIGHT_BROWSERS_PATH=/home/dev/workspace/mediacrawler-scout/runtime/browsers \
  /home/dev/workspace/mediacrawler-scout/adapter/.venv/bin/python -m playwright install chromium
```

采集器 `.env` 应以权限 `0600` 保存 `COLLECTOR_TOKEN=<至少32字符的随机令牌>` 和
`COLLECTOR_INTERVAL=3`。不要把 Cookie 加入 `.env`。Scout 接入地址为
`http://127.0.0.1:8765`，使用相同的独立访问令牌。部署文件中的路径与本机项目固定路径一致。

```sh
install -m 0644 deploy/mediacrawler-scout.service /home/dev/.config/systemd/user/mediacrawler-scout.service
systemctl --user daemon-reload
systemctl --user enable --now mediacrawler-scout.service
systemctl --user is-active mediacrawler-scout.service
loginctl show-user dev -p Linger
```

本机已实查 `Linger=yes`，服务已 enabled、active。因此用户服务可脱离 SSH 会话运行。
首次部署其他机器时需按机器自身的用户服务设置确认 linger，不能把本文的实查结论移用于其他机器。

## HTTP 契约

所有接口和证据资源均要求 `Authorization: Bearer <token>`，接口返回值没有 `data` 包装。

| 路径 | 请求/返回 |
| --- | --- |
| `GET /v1/health` | `protocol_version,upstream_version,status,headless,cdp,pending_runs` |
| `POST /v1/runs` | `request_uuid,kind=search,query,sort=general/latest,max_pages=1..3`，或 `request_uuid,kind=detail,items=[...]` |
| `GET /v1/runs/{id}` | `id,request_uuid,status,login_required,error,coverage` |
| `GET /v1/runs/{id}/records` | `records,coverage` |
| `POST /v1/login` | `request_uuid,run_id?`；立即提交后台登录流程 |
| `GET /v1/login` | `login_id,status,qr,verified_at,error` |
| `GET /v1/evidence/{id}` | 经注册且哈希一致的原始响应、诊断证据或有效二维码图片 |

任务状态为 `running/waiting_login/completed/partial_failed/failed`。HTTP 200 只表示请求交互成功。
同一请求 UUID 返回原任务，进程重启保留 ID、分页进度与已完成记录。详情列表按顺序处理，
同任务内以内容类型和 ID 去重。Scout 限制每话题跨查询最多接纳 200 个搜索种子，同题扩展
另行计数；全部候选采集和筛选后，按学习分数排序，最多投递 10 篇新内容。

搜索结果始终仅作候选，正文不会使用搜索片段代替。详情优先读取原文页内指定 ID 的
`initialState.entities.answers/articles`，缺少实体正文时最多补读一次该 ID 的详情 API。
`content` 字段经目标 ID/type 核对、付费和截断标记检查后才可标为 `body`；无法取得该字段
则保留摘要或失败状态。`completeness` 记录使用的字段来源、ID 核对和限制，不代表语义理解。
纯文本只来自该实体的正文，保留链接文字和图片替代文字，导航、评论、推荐及其他回答不会拼入正文。

记录字段为 `content_type,content_id,title,url,author,author_url,published_at,updated_at,
summary,body,status,read_error,first_discovery,fetched_at,evidence,completeness`。
`status` 为 `body/partial_body/summary_only/failed`。`evidence` 每项含受控相对 HTTP 路径和 SHA256；
Scout 下载证据后自行加入其本地 `raw_files`，不直接读取采集器数据库。

## 登录与重启恢复

没有已保存会话时，任务进入 `waiting_login`，采集器不生成二维码。Scout owner 在飞书点击登录后，
Scout 才调用 `POST /v1/login`。有效二维码保留至页面提示失效或保守的 110 秒有效期。
二维码过期/登录完成即删除对应图片资源。相同请求 UUID 幂等返回原登录过程；过期后新点击用新 UUID。

扫描成功必须通过真实 `/api/v4/me` 账户接口核验，单独出现 `z_c0` 不算成功。
登录 API 不返回账户数据或 Cookie，原始账户返回不保存。验证成功后自动续跑原 `waiting_login` 任务。
重复点击复用当前有效登录过程。网络错误、限流、不可见内容与登录失效有独立错误分类；
额外验证进入等待状态，不清理会话或继续扩展采集。

```sh
systemctl --user restart mediacrawler-scout.service
```

重启后已完成任务不重做，运行中任务从最后已持久化的页面/详情进度继续；待登录任务最多进行一次
已保存会话复核，避免失效 Cookie 导致无限请求。正在扫描的二维码无法跨浏览器重启复用，状态变为
`expired` 并清除图片，飞书可重新生成。上次 `verified` 在重启后先降为 `idle`，当前有效性须重新实查。

SQLite 一致性备份必须使用 SQLite backup API 或 `.backup`，不要只复制正在写入的主数据库文件。
恢复时先停止此服务，恢复独立任务库、证据目录及会话目录，再启动服务；不要把这些目录复制进 Scout。
回退适配版本时先停止服务，检出已保存的旧适配提交，再按其 `adapter/uv.lock` 同步环境并启动。
`data/scout` 不随代码切换删除。没有新增周期采集 timer。

## 验收边界

静态检查、独立 HTTP 健康、无会话等待、UUID 幂等与重启后同任务 ID 保留的实际记录见
`ACCEPTANCE.md`。这些结果不能证明已完成知乎扫码、正文采集或真实飞书交付；只有对应真实证据出现后
才能追加相应验收结论。


## Cookie 文件导入

独立 CLI：在 `adapter` 目录执行 `.venv/bin/python -m collector.import_session
--file ../data/scout/session/zhihu-cookie.txt --run-id <原采集器任务UUID>`（实际命令写成一行）。
完整 Cookie 请求头文本和 Cookie JSON 数组均支持，须含 `z_c0`、`d_c0`。
输入文件必须直接位于私有 `data/scout/session` 目录，当前用户所有且权限 `0600`；
保留原文件，服务完成后删除 `session/imports` 下对应暂存副本。
不要把 Cookie 值放进参数、聊天、Git 或 Scout。`session/cookies.json` 为受管快照，
不能作为输入文件。

CLI 只向鉴权 `POST /v1/login/import` 发送 UUID、原任务 ID 和暂存文件名；
`GET /v1/login/import/<UUID>` 查询持久结果。使用 `--request-uuid` 重试同一请求，
不会重复执行；运行中重启的导入标记 `interrupted`，需用新 UUID 重试。
服务在原浏览器锁内创建隔离上下文验证账户，成功后替换会话并保存 Cookie 更新。
账户验证不等于真实搜索成功，当前状态见 `GET /v1/login`。服务重启仍需重新验证。

输入格式、原任务 ID、Scout 状态与验收覆盖见
[Cookie 导入说明](../../scout/docs/zhihu-cookie-import.md)。2026-09-26 已使用真实 Cookie 验证账户、首查询 3 页 60 个候选、两个服务分别重启、
同 UUID 导入幂等及 3 篇目标结果发送。失效回滚、过期及中断仍未覆盖；扫码未成功。

## 默认排序的同题回答扩展

`GET /v1/health` 的 `capabilities` 包含 `question_answers` 后，Scout 才可启用同题扩展。
新增提交请求：`request_uuid,kind=question_answers,question_id="数字问题ID",limit=20,sort=default`。
现有 `search/detail` 请求和协议版本保持兼容；回答记录补充 `question_id`。
同一 UUID 的不同请求参数返回 409，避免错误复用其他问题的结果。

适配器调用 `/api/v4/questions/{question_id}/answers` 并指定 `sort_by=default`，
按每页原始顺序收集前 20 个不同回答 ID，使用上游返回的默认排序游标继续分页。
每条记录保存 `list_rank`、`question_id`、`discovery_source=question_default`、采集时间、
页号和证据引用。列表不按日期、赞同数重新排序，时间过滤与送达过滤由 Scout 在列表固定后执行。
每页新记录及下一游标在同一 SQLite 事务提交；重启按原任务 UUID 和游标恢复。
数据库结构不变，已有搜索/详情任务继续使用原快照。

`records` 仍返回 `{records,coverage}`。扩展 coverage 包含 `question_id,sort,limit,pages,
raw_count,candidates,actual_count,complete,stop_reason,next_uri,next_offset,fetched_at,evidence`。
成功的终止原因只有 `limit_reached`（20 个不同回答）或 `end_of_list`（上游 `is_end=true`）。
缺少游标、循环游标、畸形响应、登录或网络失败均显式报告失败及 `complete=false`，
不会把不完整覆盖伪装为不足 20 个。单任务异常超过 100 页仍未得到 20 个答案时明确失败。
使用真实登录会话的验证记录见 `QUESTION_ANSWERS_ACCEPTANCE.md`。

### 默认列表正文复用与分页效率

真实验证后，默认列表首请求改用 `limit=20` 并请求 `content`、付费和截断相关字段。
上游若返回不足 20 个且未结束，仍严格按原始 next 游标继续，不自行重排或补选名单。
只有 `answer_type=normal`、`is_normal=true`、`content_need_truncated=false` 都明确存在，
且现有 `detail_record` 完整性检查通过时，记录才返回 `status=body` 和
`completeness.basis=question_answers_api.v4.answers:target_entity.content`。
Scout 可复用这些全文；其他记录继续走既有单篇 detail，不因上述标志直接筛除候选。
已有任务的持久化 next_uri 保持原值，重启不改变其已固定的列表或记录身份。

5 篇列表正文与独立原文 HTML 正文逐字符一致；20 条单页列表顺序与原 4 页列表逐项一致，
生产 HTTP 已确认一次取得 5/20 条完整正文。付费样本和 `content_need_truncated=true` 的
实际含义仍未覆盖，详细边界见 `QUESTION_ANSWERS_ACCEPTANCE.md`。
