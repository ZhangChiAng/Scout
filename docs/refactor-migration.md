# 配置和知乎通知迁移

本次在隔离工作副本中交付，运行目录的代码、`.env`、`models.toml` 和真实数据库未切换。
先审阅补丁，再在维护窗口切换代码和配置并重启服务；本轮不执行上线或真实发送验收。

## 配置

把 `config.toml` 的 `[[sources]]` 改为 `[source]`，删除 `collector`。
保留 `name = "juya-ai-daily"` 以及原 URL、window_size、max_age_days；来源名称参与数据身份，
不能在迁移时改名。仅允许一个来源，网络和飞书卡片容量配置继续保留。

把本机 `models.toml` 的 `[[models]]` 改为 `[model]`，保留原 model/base_url，删除
protocol/api_key_env。协议固定 OpenAI Responses，密钥继续读取 `.env` 的 SCOUT_LLM_API_KEY。
示例见仓库 `models.example.toml`。旧数组配置将明确报错，必须与新代码一起迁移。

`config.zhihu.toml` 的查询和正文规则可直接沿用；max_results 默认 5，必须为不超过
max_unique 的正整数。自动扫描只接受 fields=["body"]。新建 scan 即启用自动通知，
目标为当时 FEISHU_RECEIVE_ID_TYPE=chat_id 对应的群；扫描保存后不随环境变量改变群。

## 命令替代

| 旧操作 | 新操作 |
| --- | --- |
| scan 后人工核对并 send-results | `python -m scout.zhihu scan --config config.zhihu.toml` 自动发送合格链接 |
| preview 正文卡片 | `python -m scout.zhihu score --input <collection.json> --config config.zhihu.toml` 查看评分报告 |
| send-test / send-results 重试 | `python -m scout.zhihu scan --run-id <新扫描UUID>` 恢复失败通知 |
| scout.zhihu_verify 旧命令转发 | 使用 `python -m scout.zhihu score` |

删除人工核对文件和正文卡片的生成/发送实现；已有档案和 SQLite 历史表保留。旧扫描不会
获得自动发送授权；对旧 UUID 执行 scan 也不会补发历史消息。需要按新规则扫描时创建新 UUID。
score 始终只生成报告；它不会建立扫描或触发通知。

## 数据与恢复

首次新建扫描或启动知乎后台线程时，在 sender 锁内检查新表。若新表不存在且数据库已有
数据，先调用 SQLite backup API 写入同目录 `scout-before-links-<纳秒时间>.sqlite3`，
检查备份 integrity_check=ok，成功后创建 zhihu_link_deliveries。备份失败即停止建表。
本轮仅做只读核对，未执行这次迁移备份或建表。

旧 rule_test_deliveries 的 delivered 记录参与全局去重；旧 pending 卡片不由新流程接管。
日报快照、偏好历史、反馈、评价缓存及其他历史表不删除。旧格式档案继续可查看和回滚。
恢复链接只读取固定标题、URL、群和 UUID；请求时不占数据库事务。最多自动尝试三次，
间隔至少 5 秒和 15 秒，耗尽暂停同一扫描的后续链接。scan --run-id 重置失败项次数，
继续使用原 UUID。远端成功但本地记账前中断，恢复可能受飞书幂等窗口限制。

## 验证边界

本轮执行 Ruff、格式检查、Python 编译、CLI 帮助与入口引用检查、git diff --check；
只读读取真实 SQLite 的历史档案、日报快照和知乎采集证据。不新增单元或模拟测试，
不访问模型或发送飞书消息。

上线后仍需真实橘鸦 RSS、模型端点、SQLite 和飞书群的端到端验收：日报与反馈闭环、
知乎新扫描自动链接通知、历史去重不占额度、断线恢复与三次失败暂停、显式恢复 UUID、
listener 重启后继续发送，以及首次新表迁移备份。上述项目本轮未执行。
