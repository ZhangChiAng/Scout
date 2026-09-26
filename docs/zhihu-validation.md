# 知乎本机采集、规则筛选与飞书交付

Scout 采集完整正文，按配置筛选，再按发现顺序自动向飞书群发送每篇的标题和原文 URL。
默认主题为 GPT-6 与“斩杀线”，可修改配置；规则不做语义判断。

## 两个项目与运行状态

| 项目 | 部署与持久数据 |
| --- | --- |
| Scout | `/home/dev/workspace/scout`；自身 `.venv`、`uv.lock`；既有 `data/scout.sqlite3`；`data/zhihu-runs/<Scout UUID>/` 保存采集证据副本与报告 |
| MediaCrawler 适配 | `/home/dev/workspace/mediacrawler-scout`；独立分支 `scout/local-zhihu-api`，基于上游 `380b426000aac3d612837ed72c99808347dc94c9`；`adapter/.venv` 与 `adapter/uv.lock` |
| 采集器运行资源 | 独立 Node.js 在 `runtime/node`，Playwright 浏览器在 `runtime/browsers`；任务库 `data/scout/tasks.sqlite3`，原始证据 `data/scout/evidence`，浏览器会话 `data/scout/session` |
| 用户服务 | `mediacrawler-scout.service` 只监听 `127.0.0.1:8765`；`scout-feedback.service` 运行独立的后台扫描线程，推进未完成扫描 |

Scout 通过带访问令牌的 HTTP/JSON 调用采集器，不安装或导入采集器依赖，也不读取
采集器内部 SQLite。Cookie 仅留在采集器会话目录；Scout 配置只保存服务 URL 和
访问令牌。采集采用单次任务，没有周期采集 timer。

采集器自身的部署和运行时锁定步骤见
[独立采集器部署说明](../../mediacrawler-scout/deploy/README.md)。部署使用包含
`adapter/` 和 `deploy/` 的适配提交；仅检出上游固定提交不会包含 Scout 的 HTTP 接口。
采集器协议版本为 `1.0`，部署版本与依赖按独立采集器的部署说明锁定。
两个仓库分别记录 `git rev-parse HEAD`，升级或回退时分别保留版本与数据备份。

## 部署与配置

在采集器项目同步其独立锁文件并安装用户服务；独立 Node、Chromium 的安装及
版本校验按采集器部署说明执行：

```bash
cd /home/dev/workspace/mediacrawler-scout
uv sync --project adapter --locked --python 3.12
install -Dm0644 deploy/mediacrawler-scout.service \
  "$HOME/.config/systemd/user/mediacrawler-scout.service"
systemctl --user daemon-reload
systemctl --user enable --now mediacrawler-scout.service
systemctl --user status mediacrawler-scout.service
```

采集器根目录的 `.env` 保存 `COLLECTOR_TOKEN` 和请求间隔 `COLLECTOR_INTERVAL`，
权限应为 `0600`。Scout 的 `.env` 使用同一访问令牌，并保留现有飞书配置：

```dotenv
ZHIHU_COLLECTOR_URL=http://127.0.0.1:8765
ZHIHU_COLLECTOR_TOKEN=<与采集器相同的访问令牌>
```

这些是配置占位符，不是可用凭据。不要把 Cookie、令牌或飞书密钥写进报告、命令
输出或版本化文件。配置更新后重新启动现有 listener，使后台任务读取新配置：

```bash
cd /home/dev/workspace/scout
uv sync --locked
systemctl --user restart scout-feedback.service
uv run --locked python -m scout.zhihu status
```

常驻运行还需要该用户的 systemd manager 在离开 SSH 后继续运行；可用
`loginctl show-user dev -p Linger` 检查宿主机设置是否为 `Linger=yes`。服务日志分别查看：

```bash
journalctl --user -u mediacrawler-scout.service --since today
journalctl --user -u scout-feedback.service --since today
```

## 单次扫描与覆盖范围

查询、排序、页数、详情批量和数量均来自 [config.zhihu.toml](../config.zhihu.toml)。
默认查询覆盖 GPT-6 各种写法与“斩杀线”，不限定发布日期。每个查询最多三页，
`max_unique` 为 1–200；`max_results` 默认 5，允许任意不超过 `max_unique` 的正整数。
搜索仅发现候选，详情请求才确认正文；按内容类型及 ID 去重。旧送达文章和其他扫描
已登记的文章不占本次额度；达到新链接额度、候选上限或查询耗尽时停止扩展。
`scan` 新建任务即启用自动发送，固定保存当前群和规则。`--wait-seconds 0` 只提交，
之后由 listener 后台线程推进采集及发送。

```bash
uv run --locked python -m scout.zhihu scan --config config.zhihu.toml
uv run --locked python -m scout.zhihu status --run-id <Scout扫描UUID>
```

`scan` 返回 Scout 扫描 UUID 和报告目录。命令默认最多等待 60 秒；退出后，已配置的
`scout-feedback.service` 继续推进已保存任务。需要在终端继续等待时：

```bash
uv run --locked python -m scout.zhihu scan \
  --run-id <Scout扫描UUID> --wait-seconds 60
```

首次提交可传 `--request-uuid <UUID>`；相同 UUID 返回原扫描及原配置，避免进程中断
后重复创建任务。Scout 扫描 UUID 与采集器任务 UUID 不相同，一个扫描会串行产生
多个搜索和详情任务；`status` 显示当前 `collector_run_id`。

报告位于 `data/zhihu-runs/<Scout UUID>/`：

| 文件 | 内容 |
| --- | --- |
| `scan-config.json` | 本次固定的搜索限制与规则 |
| `collection.json` | 正文、摘要、作者、日期、发现位置、获取时间、正文状态与证据引用 |
| `raw/<SHA256>.json` | 通过受控接口取得并验证哈希的内容证据副本 |
| `scores.json`、`scores.md` | 字段命中上下文、分数、完整正文资格、查询覆盖和失败原因 |
| SQLite `zhihu_link_deliveries` | 固定消息、群、UUID、尝试次数与消息 ID；`status` 显示通知状态计数 |

覆盖包括每个已执行查询的实际采集页信息、候选数、正文成功数、详情失败和终止
原因。`unique_candidates` 按首次发现归属计数，跨查询重复不会增加总候选数。
`max_unique`、`max_results`、查询耗尽以及采集错误分别记录；未请求的详情不能算
正文无命中。HTTP 200 或任务已创建也不表示采集成功。

`scan` 退出码 0 表示扫描及通知完成，仍可能零命中；2 表示等待、未完成、部分失败或通知暂停；1 是
配置或执行错误。阅读状态和覆盖后才能判断“没有合格正文”还是“采集未完成”。

## Cookie 文件导入

独立采集器的 Cookie 导入入口支持完整请求头文本和 JSON 数组；操作及凭据边界
见 [Cookie 导入说明](zhihu-cookie-import.md)。导入不创建新扫描，
原任务保持等待，直到真实账户验证通过。

## Cookie 登录与任务恢复

缺少会话或账户验证确认失效时，Scout 保留原采集器任务 ID，扫描状态为
`waiting_login`。按 [Cookie 导入说明](zhihu-cookie-import.md) 导入有效 Cookie，
采集器通过账户接口验证后继续原任务。使用以下命令查看登录状态及扫描进度：

```bash
uv run --locked python -m scout.zhihu status --run-id <Scout扫描UUID>
```

输出的 `login` 包含采集器当前 `status`、`auth_method`、`verified_at` 和 `error`；
`scan.collector_run_id` 是 Cookie 导入命令使用的采集器任务 ID。
现有 `scout-feedback.service` 中的后台扫描线程每 5 秒推进一次已保存任务，
终端命令退出后仍可恢复。网络错误、限流、内容不可见和额外验证分别报告，
不会把任意 403 或超时直接当作会话失效并清除 Cookie。

分别重启时保留两个项目的数据目录和数据库：

```bash
systemctl --user restart scout-feedback.service
uv run --locked python -m scout.zhihu status --run-id <Scout扫描UUID>
systemctl --user restart mediacrawler-scout.service
uv run --locked python -m scout.zhihu status --run-id <Scout扫描UUID>
```

Scout 从 `zhihu_scans` 中保存的扫描状态及任务关联恢复协调；采集器从自己的任务库恢复
尚未完成的搜索或详情，浏览器会话仍需账户接口确认有效。采集器的任务请求 UUID
在 POST 前已保存，未知结果重试复用原 UUID。额外验证不能自动绕过，报告应明确
停在哪里。暂时连接错误有限重试，协议校验失败或重试耗尽会记录终态及失败原因。
遇到平台额外验证时，服务保留任务并暂停采集，待账户验证通过后继续原任务。

## 规则与同正文重新评分

自动扫描要求 `rules.fields = ["body"]`。正则忽略大小写，先召回，再累计字面关键词
权重；每个关键词每篇只计一次。完整性依据必须确认目标 ID、类型、正文字段存在，
且没有未解决的限制。采集证据下载后校验 SHA-256；登记发送前再次校验。部分正文、
摘要、失败记录不会发送，失败不能算零命中。主题匹配只由配置决定。

```bash
uv run --locked python -m scout.zhihu score \
  --input data/zhihu-runs/<Scout扫描UUID>/collection.json \
  --config config.zhihu.toml --output-prefix rescored
```

也可用 `--rules <只含[rules]的TOML>` 做本地诊断评分，该文件允许选择 title、summary、body。
`score` 只生成 JSON 和 Markdown 报告，不联系模型、采集器、飞书，也不写 SQLite。
重新评分不会更改扫描规则或固定消息；扫描使用创建时保存的配置。

## 自动通知与恢复

每批正文保存后，按首次发现顺序登记合格新链接。每篇发送一条文本消息，内容恰为
标题、换行和原文 URL。SQLite 在发送前保存文章键、扫描归属、标题、URL、目标群、
固定 UUID 和状态；成功后记录消息 ID。网络请求不持有 SQLite 事务。

CLI 与 listener 共用扫描锁、sender 锁及恢复流程。扫描终止后仍恢复待发送链接。
每条最多自动尝试三次，失败后分别至少等待 5 秒、15 秒。进程在请求期间中断也会
消耗一次尝试，并沿用原 UUID；第三次失败暂停该扫描后续消息。其他扫描独立推进。

```bash
uv run --locked python -m scout.zhihu scan --run-id <Scout扫描UUID>
```

显式恢复重置失败消息的尝试次数，保留消息和 UUID。已送达记录不会重发。恢复发送
只依赖数据库中的固定消息，不依赖原文或采集目录仍然存在；继续采集和生成报告仍需证据目录。
飞书成功与本地记账不是跨系统原子事务，超出远端 UUID 幂等窗口的恢复可能重复。

升级不会为旧扫描添加自动通知标记，不会自动发送旧待发送卡片。旧
`rule_test_deliveries` 的成功记录参与新扫描去重；历史表和证据保留。
移除的 `preview`、`send-test`、`send-results` 和旧模块入口替代方式见
[迁移说明](refactor-migration.md)。
