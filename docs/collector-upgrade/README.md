# 本机知乎采集器部署与恢复

采集器是 `/home/dev/workspace/mediacrawler-scout` 中的独立 MediaCrawler 适配服务。Scout 通过
本机 HTTP 协议访问它，采集器负责知乎会话、搜索、同题分页和单篇正文，不负责模型判断或
飞书发送。当前实验保留已确认的真实采集任务、证据、游标与登录会话，Scout 保存查询计划、游标、完整正文与来源，按[当前规则](../zhihu-semantic-scan.md)逐批评价。

## 运行环境

采集器使用自己的 `adapter/uv.lock`、`adapter/.venv`、`runtime/node` 和浏览器目录；Scout
不安装或导入采集器源码。服务为 `mediacrawler-scout.service`，只监听 `127.0.0.1:8765`。

```bash
uv sync --frozen --project /home/dev/workspace/mediacrawler-scout/adapter --python 3.12
systemctl --user status mediacrawler-scout.service
```

服务的工作目录、环境文件和解释器必须指向采集器项目。修改依赖或部署脚本时以采集器自己的
锁文件和文档为准。启动前确认采集器 `.env` 中的 `COLLECTOR_TOKEN` 与 Scout 的
`ZHIHU_COLLECTOR_TOKEN` 一致，Scout 设置 `ZHIHU_COLLECTOR_URL=http://127.0.0.1:8765`。
令牌文件使用私有权限，不把 Cookie 写入 Scout 或命令参数。

任务库位于采集器 `data/scout/tasks.sqlite3`，会话位于 `data/scout/session`，证据位于
`data/scout/evidence`。它们不进入 Git，也不作为 Scout 本地数据库读取。

## 当前单页 HTTP 契约

接口和证据资源要求 `Authorization: Bearer <token>`。`GET /v1/health` 的 `capabilities`
须包含 `search_page`、`question_answers_page`，Scout 才执行新语义扫描。

| 接口 | 输入或返回 |
| --- | --- |
| `POST /v1/runs` 搜索页 | `request_uuid,kind=search_page,query,sort=general/latest,cursor?` |
| `POST /v1/runs` 同题页 | `request_uuid,kind=question_answers_page,question_id,sort=default,cursor?` |
| `POST /v1/runs` 详情 | `request_uuid,kind=detail,items=[...]` |
| `GET /v1/runs/{id}` | 任务身份、状态、错误、登录要求及覆盖情况 |
| `GET /v1/runs/{id}/records` | 归一化内容及分页结果；页结果包含 `next_cursor`、`is_end` |
| `GET /v1/login` | 当前会话状态，不能以旧登录结果判断当前有效性 |
| `GET /v1/evidence/{id}` | 经登记并核对哈希的受控证据资源 |

单页请求沿用采集器传输页大小。同题默认排序保持上游原始顺序，赞数排序由 Scout 对已发现
候选执行。后续页使用真实响应返回的位置，不能假定 offset 固定增加。采集器生成不透明
游标、保存对应搜索或问题绑定，不接受任意外部 URL。

每条回答或文章保存稳定类型和 ID、来源、时间、可用正文、完整性标记与可空 `voteup_count`。
赞数是非负整数或空值，续页仍保留该字段。默认列表可靠完整时可复用正文，搜索摘要不能
代替全文；无法确认完整性时保留候选并交给详情请求。

同一请求 UUID 返回原任务，参数变化复用返回冲突。任务记录和后续位置持久化，恢复沿用原
任务身份；错误、登录失效和缺少游标等情况不能伪装成实际结束。旧 `search`、`question_answers`
等任务接口保留代码兼容，当前实验使用单页能力并由 Scout 决定何时继续。

## 登录和恢复

Cookie 文件导入操作见 [登录说明](../zhihu-cookie-import.md)。账户以真实验证结果为准，
导入成功不等于搜索或正文已经成功。额外验证、限流、内容不可见和网络失败单独保存原因。
会话凭据不进入日志、Scout、飞书、报告或证据目录。

```bash
systemctl --user restart mediacrawler-scout.service
```

正常重启恢复当前数据库中的任务和已保存分页，已完成内容不重新采集。本次明确保留的任务
和游标继续可用，按用户后续继续或调整采集参数的操作推进。维护活跃任务库时使用 SQLite
backup API 核对完整性，不只复制可能仍在写入的主数据库文件。

## 核验

使用真实知乎、模型、SQLite 和飞书核验，不编写单元或模拟测试。完整规则和验证边界见
[知乎搜索与偏好规则](../zhihu-semantic-scan.md)。
