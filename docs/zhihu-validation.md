# 知乎本机采集、规则筛选与飞书交付

Scout 通过本机采集器取得知乎回答或专栏正文，按配置规则筛选，并按发现顺序向
飞书群发送最多 5 篇结果。当前发送入口要求正文讨论 GPT-6 模型且含字面“斩杀线”，
GPT-5.6 不算命中。`scan` 保存采集结果，执行 `send-results` 才发送结果。

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

完整查询及分页配置在 [config.zhihu.toml](../config.zhihu.toml)。先依次执行
`GPT-6 斩杀线`、`GPT6 斩杀线`、`GPT 6 斩杀线`，每项最多 3 页；不足 5 篇时继续
`GPT-6`、`GPT6`、`斩杀线`，每词依次综合、最新排序，各最多 3 页。没有发布日期限制。
搜索只发现候选，详情另发请求；跨查询按内容类型和 ID 去重，最多检查 200 篇唯一
候选，达到配置的合格数量后停止扩展。规则只做文本判断，最终仍核对模型语境。

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
| `deliveries.json` | 实际发送完成后的文章键、消息 ID 和 UUID；生成预览不产生成功记录 |

覆盖包括每个已执行查询的实际采集页信息、候选数、正文成功数、详情失败和终止
原因。`unique_candidates` 按首次发现归属计数，跨查询重复不会增加总候选数。
`max_unique`、`max_results`、查询耗尽以及采集错误分别记录；未请求的详情不能算
正文无命中。HTTP 200 或任务已创建也不表示采集成功。

`scan` 退出码 0 表示扫描完成，仍可能零命中；2 表示等待、未完成或部分失败；1 是
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

`score` 使用只包含 `[rules]` 的独立规则文件。将以下内容保存到自己的规则文件
（例如 `data/zhihu-rules.toml`），再通过 `--rules` 指定。带 `[scan]` 的完整采集配置
用于 `scan`。

```toml
[rules]
fields = ["body"]
regex = ['(?<![A-Za-z0-9])gpt[\s\-‐‑–—]*6(?![A-Za-z0-9]|\.\d)']
keywords = [{ text = "斩杀线", weight = 1 }]
threshold = 1
```

`fields` 可省略，默认标题、摘要和正文逐字段匹配；提供时必须是 `title`、`summary`、
`body` 的非空、无重复列表。正则忽略大小写，先召回再累计关键词权重；关键词为
忽略大小写的字面匹配，每篇只计一次，重复配置同一关键词会报错。达到阈值且
正文完整才有发送资格。失败记录分数为 null；仅摘要或部分正文不能替代完整正文。
非法规则在 HTTP 请求之前拒绝，配置不调用模型或偏好系统。

```bash
zhihu_run=data/zhihu-runs/<Scout扫描UUID>
uv run --locked python -m scout.zhihu score \
  --input "$zhihu_run/collection.json" \
  --rules data/zhihu-rules.toml --output-prefix rescored
uv run --locked python -m scout.zhihu preview \
  --run-dir "$zhihu_run" --report rescored.json \
  --article-key <zhihu:内容类型:内容ID>
```

修改规则后可反复评分同一正文，发现顺序不变。`score` 和 `preview` 不联系模型或
飞书，也不写发送记录；`send-results` 使用扫描保存的规则与已固定的发送快照，
不会因另一个本地评分报告被改写而改变待发送内容。

## 核对语境与真实发送

规则不能理解语义。执行者应查看实际正文里的 GPT-6 和“斩杀线”命中上下文，确认
GPT-6 指模型，并将事实核对与该正文 SHA-256 绑定，作为内容审核记录保存。

核对文件采用以下结构，尖括号内容必须替换为真实值，`notes` 写实际模型语境：

```json
{
  "articles": [
    {
      "article_key": "zhihu:article:<内容ID>",
      "body_sha256": "<完整正文UTF-8的SHA-256>",
      "gpt6_is_model": true,
      "notes": "<实际命中上下文与模型语境核对结论>"
    }
  ]
}
```

若 GPT-6 实际不指模型，应写 `gpt6_is_model=false` 并说明原因，不能为了命中
将结论写成 true。false 与正文哈希一起持久保存，该正文不进入发送范围；若此前
因达到 5 篇而停止扫描，排除后会在原查询与 200 篇上限内继续采集。命令此时返回
2，完成补采后对新增正文继续事实核对；已保存的核对无需重新填写。缺少核对的
候选仍明确报错，不把“未审核”当作 true。

核对文件与采集结果一起保存，例如 `model-context.json`，然后执行发送命令：

```bash
uv run --locked python -m scout.zhihu send-results \
  --run-id <Scout扫描UUID> \
  --review "$zhihu_run/model-context.json"
```

发送入口仍独立检查正文里的 GPT-6 词形与字面“斩杀线”，只发送完整且通过规则的
已核对内容。卡片包含标题、原文链接、正文摘录以及两项命中上下文；缩短正文摘录
不会删除固定证据区，整张卡按序列化 UTF-8 字节大小校验。

网络调用之前先在 `zhihu_send_campaigns`、`zhihu_campaign_articles` 固定本扫描的
目标群、最多 5 篇范围及内容快照和 SHA-256，并在独立 `rule_test_deliveries` 提交内容、规则、
证据、卡片、目标群和 UUID。成功后保存飞书消息 ID，已成功的文章默认跳过。
`send-results` 按固定范围发送，不启用后台自动发送。

发送中断后，用同一扫描 UUID 重试；已保存快照的恢复不重新评分或改变 UUID：

```bash
uv run --locked python -m scout.zhihu send-results --run-id <Scout扫描UUID>
```

已固定的内容、语境核对、规则与证据从 SQLite 恢复，不依赖知乎原文或运行目录
继续存在。已有单篇待发送记录直接使用 SQLite；尚未进入单篇表的固定快照用
临时文件交给发送入口，证据来自其嵌入副本。恢复保留原群，不因当前群配置改变
而重新定位；跨扫描的待发送快照不能混入本次范围。发送记录检查完整快照哈希、
正文完整性、事实核对、单次范围、文章键、群、UUID 及卡片大小。飞书发送和本地成功提交
不是跨系统原子事务；远端成功而本地未保存消息 ID 的情况只能按原 UUID 重试，
不能承诺超出飞书幂等窗口后绝不重复。

没有合格正文时，输出有明确覆盖范围的报告，不发送替代文章。没有消息 ID 和
对应 SQLite 成功记录就不能称为真实交付完成。
