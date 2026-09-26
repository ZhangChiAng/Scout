# 同题回答扩展的真实采集验收

2026-09-26，先在私有 `/tmp/scout-collector-validation` 中使用已有登录会话、真实 SQLite
验证暂存版本。用户明确授权“升级采集器并重启服务”后，已备份并升级生产采集器。
`mediacrawler-scout.service` 重启后 active，健康接口声明 `question_answers`，真实账户复核 verified。
没有编写单元或模拟测试；本文只记录采集器验收，不替代人工反馈或飞书集成验收。

## 已完成的真实验证

| 场景 | 实际结果 |
| --- | --- |
| 有效会话 | 现有 `/v1/login` 返回 verified；每个验证任务通过真实 `/api/v4/me` 核验 |
| 不足 20 个回答 | 问题 `2085925368972698066` 实际返回 5 个，`paging.is_end=true`，终止原因 `end_of_list` |
| 默认列表前 20 个 | 问题 `2079174485035282663` 返回总数 53；沿上游游标读取 4 页，取得 20 个不同回答，终止原因 `limit_reached` |
| 顺序与身份 | 两个问题及恢复任务的候选 ID 顺序逐项等于原始响应首次出现顺序，排名连续，所有 question_id 正确，证据 SHA256 全部匹配 |
| 真实搜索种子在前 20 个内 | 现有真实搜索“GPT-6 斩杀线”的回答 `2079541679607002474` 位于上述默认列表中 |
| 分页中断恢复 | 临时进程在第 1 页 5 个记录提交后收到 SIGTERM；新进程以原 UUID 从 offset=5 恢复，得到 20 个，第一页面证据未重取 |
| 静态检查 | 全量 `ruff check collector` 和 Python AST 解析通过；部署后 `ruff check --no-cache adapter/collector` 通过 |
| 生产 HTTP 提交 | 真实 POST /v1/runs 分别取得 5 条与 20 条默认列表，结果顺序、排名、问题 ID、证据哈希全部核对一致 |
| 请求幂等 | 每个任务在运行中及完成后重复提交原 UUID 均返回同一任务；换问题复用 UUID 返回 409 |
| 升级与存量保存 | SQLite backup 和生产数据库 integrity_check 均为 ok；升级前 20 个存量任务快照及全部记录集合逐项不变 |

历史接口线索来自 [ZhihuHelp archived issue 89](https://github.com/YaoZeyuan/ZhihuHelp_archived/issues/89)。
实际请求明确包含 `sort_by=default`，后续分页严格采用真实响应的 next URL。
一次响应的 offset 从 10 跳到 16，因此不能假设 offset 必然按固定步长递增。
列表记录只保留原顺序，不依据赞同数、时间或过滤结果重选。

## 可追溯记录

- 不足 20 个：运行 `1981d64d-b6a7-452f-9f5c-e449e33d8c38`。
- 满 20 个：运行 `50309aef-66c1-400e-8de6-ba81c170357f`。
- 中断恢复：运行 `69fc49f8-14e3-413a-8ad4-e8a3a9e15d49`；请求 UUID `05ee4a50-f788-4ad3-9558-fbe395210f3d`。
- `question-answers-evidence.json` 包含有序回答 ID、原始 paging、证据 SHA256 与逐项核对结果。
- 原始响应、SQLite 和恢复前后记录分别位于 `/tmp/scout-collector-validation/data/scout`、
  `/tmp/scout-collector-validation/real-results.json`、`/tmp/scout-collector-validation/resume-results.json`。
- 上述临时验收数据已持久化到 `/home/dev/workspace/scout/data/zhihu-collector-validation-20260926`。
  `tasks.sqlite3` 使用 SQLite backup API 复制，`evidence/` 保留 16 份原始响应；
  5 份结果 JSON 同目录保存。数据库 integrity_check 和全部登记证据 SHA256 核对通过。
  归档含 4 个真实任务、50 条记录，目录权限 0700，文件权限 0600；未复制 session、Cookie 或 imports。
  归档目录由 Git 忽略，清单见该目录 `archive-manifest.json`。已确认没有本次启动的临时验证进程遗留。

## 生产部署记录

SQLite 和原始源码备份目录：
`/home/dev/workspace/mediacrawler-scout/data/scout/backups/20260926T121527Z-question-answers`。
补丁原地应用，已有 Cookie 导入相关未提交修改保留。

- 生产 5 条任务：`a89ec91c-0187-4cfc-b185-065896eb7f8d`，请求 `2d05ec2a-e519-5a37-bdc4-3375a2bca0f8`。
- 生产 20 条任务：`b198a20a-90e4-44fc-93a0-74bedb31f881`，请求 `407feadd-2fae-59f4-ad7d-11963fec0893`。
- `production-http-results.json` 保存生产能力声明、UUID 幂等结果、分页、原始顺序和证据 SHA256。
- `legacy-restart-results.json` 保存 20 个存量任务及记录的升级前后逐项比对。

## 默认列表全文与单页 20 条优化

在私有临时实例读取同一真实问题 `2085925368972698066`，增加 `content,paid_info` 等字段后，
5 篇回答正文与分别读取的原文页 `initialState.entities` 正文逐字符和 SHA256 相同，
纯文本长度分别为 1193、58、209、1293、19 字符。默认答案顺序及 paging 未改变。

`2079174485035282663` 的首请求使用 `limit=20`，一次返回 20 条，顺序逐项等于此前
按 5 条取得的 4 页结果。实际 next 指向 offset=21，保持使用服务端游标。

以上 25 条均明确返回 `answer_type=normal,is_normal=true,content_need_truncated=false`。
即使显式请求，正常回答仍未返回 paid_info 等付费字段；不能据此声称已验收费回答。
未取得 `content_need_truncated=true` 的真实样本，不能从字段名认定它代表服务器截断或客户端折叠。
实现仅对以上三个正常条件均成立、正文通过既有 `detail_record` 验证的列表记录提供 `body`。
其他类型、真值或缺失的标志保留原候选并交给独立 detail，不按该标志筛除文章。

在再次备份 SQLite 并完成 integrity_check 后部署，备份目录：
`/home/dev/workspace/mediacrawler-scout/data/scout/backups/20260926T122321Z-list-body-reuse`。
重启瞬间无运行中采集任务；历史任务、请求 UUID、已提交分页游标均保留。
新的生产 HTTP 任务实际一次返回 5/20 个经确认的 body，完整性来源为
`question_answers_api.v4.answers:target_entity.content`；顺序、排名、身份、证据哈希和 UUID 幂等均通过。

- `full-body-probe-results.json`：5 篇独立正文比对和字段证据。
- `page20-probe-results.json`：一次 20 条与原 4 页顺序比对。
- `production-body-results.json`：部署后的真实 HTTP、单页覆盖、body 状态及正文哈希。

## 尚未覆盖

尚未实测生产服务中运行中的旧搜索/详情任务跨重启恢复、种子在前 20 个之外、真实上游页面重复
回答、排名变化、真实登录失效或扩展失败。未通过页面 UI 逐项交叉核对默认列表。
Scout 的跨话题去重、时间和偏好筛除后不补位、反馈训练以及真实飞书群交付属于集成验收，
这里的采集器结果不代表这些场景已完成。
