# 2026-09-06 偏好增量归纳与定期重整验收

范围：现有真实历史反馈的全量重整、启用与飞书偏好通知，以及 2026-09-02 完整
新闻快照的只读评价。没有编写单元测试、伪造反馈或回调；没有推进其他日报日期。

## 备份、初始化与兼容

- 静态检查：`ruff check .`、`ruff format --check .`、`compileall -q scout`、
  `git diff --check` 通过。
- 13:18:06（北京时间）在 sender 写操作锁内，通过 SQLite backup API 保存
  `data/scout-before-profile-v2-20260906T131806.sqlite3`，然后增量初始化新表。
- 备份时有 24 条真实反馈修订、22 条有效反馈、偏好 v1–v8、50 条正文送达、
  70 条新闻快照、42 条评价缓存、1 张不推荐列表及 7 个成员。
- 原业务表在初始化前后的逐表内容哈希一致；`integrity_check=ok`，
  `foreign_key_check` 无异常。
- 旧格式 v8 正常 show，history 可以完整读取 v1–v8；自动更新路由识别为
  `rebuild / format_migration`。原 v8 共 9 条喜欢、8 条不喜欢、7 条权衡、
  9 条疑问，共 33 条，可读偏好正文 2,105 字符；原评价输入档案 JSON 为
  2,545 字符（含当时附带的证据和变化说明）。

## 真实模型预览

使用已有 `models.toml` 中的 `deepseek-v4-flash` / `https://api.deepseek.com`，
模型参数保持 `max_output_tokens=65536`、600 秒超时、无 SDK 重试、`store=false`。
初期使用端点默认推理档位；owner 随后要求改为 max，已在偏好归纳和新闻评价请求中
显式设置 `reasoning.effort=max`，并记录实际请求参数及响应 usage。

- 首次预览返回的格式未通过严格校验，命令失败；SQLite SHA-256 前后一致，
  活动版本仍为 v8，消费位置仍为 24。第二次预览多出顶层 evidence_refs，同样被
  拒绝，SQLite 哈希仍一致。最终输出契约简化为带 category 和逐条证据的 entries
  数组，顶层仅保留格式版本、条目数组和变化说明；没有放宽校验。
- 首次失败记录为 `data/profile-rollout-20260906/preview-invalid-format.txt`，
  对应哈希记录为 `preview-invalid-format-metrics.json`。
- 第二次失败记录为 `preview-extra-field.txt` 及 `preview-extra-field-metrics.json`。
- 内容审查曾发现继承已被替换的“豆包不感兴趣”结论、遗漏明确不用的产品，以及
  把仅以通用理由评价的新闻名称累积为长期对象；这些预览没有启用。提示词已明确
  要求重建结论时忽略旧偏好、保留明确对象关系、避免新闻案例进入对象清单。
- 还观察到空 output_text（仅 reasoning）以及再次混用字段的真实响应，均停止且
  没有 SQLite 写入。最终同时在模型输入中给出输出 schema，保留严格校验。
- 默认档诊断响应记录在 `preview-response.json`：输出 3,513 token，其中推理
  1,756 token，未耗尽 65,536 预算，但漏掉 change_summary 并混入多余顶层分类。
- 最终契约的全量输入包含 22 条当前有效反馈及完整原新闻上下文，输入 18,669 字符，
  包括 instructions、JSON 输入和输出 schema，不是 token 数。

## Pro + max 单次预览

owner 要求停止后，Flash + max 的请求在 156.38 秒被中断，未取得结果。随后按
owner 的“换成 deepseek pro、保持 max、再试一次”指令，将本地 `models.toml`
改为 `deepseek-v4-pro`，只执行一次相同提示词和反馈输入的只读全量预览。

- 用时 168.40 秒，响应 `completed`，实际请求 `reasoning.effort=max`。
- 输入 8,752 token；输出 12,362 token，其中推理 10,414 token。
- 格式及证据引用校验通过。临时 v9 包含 12 条判断规则、13 个具体对象、1 个专题
  兴趣、3 个重要疑问，共 29 条；全部条目都有有效引用。
- 明确使用/不使用的对象、投资标的、任职关系和模型破甲兴趣得到保留；删除了没有
  当前有效反馈支持的“豆包不感兴趣”。没有继续把 Runway、TimesFM 等案例列成
  长期对象。
- 同为不含证据及变化说明的偏好 JSON，可读字符数由旧 v8 的 2,301 降为 1,238，
  减少 46.2%。规则数超过 10 的软目标，程序未机械截断。
- 内容审查仍有未通过项：三个疑问仍在推演使用范围、DeepSeek 是否属于投资标的、
  顶尖模型的能力方向边界；尤其以“推荐强度”为由追问 DeepSeek 投资身份，并不
  符合本项目的三态判断，也不是当前推荐所必需的歧义。因此不能把格式通过视为
  内容完全验收通过。
- SQLite SHA-256 前后一致，活动档案仍为 v8（格式 v1），消费位置和最新修订
  均为 24。没有启用临时 v9，没有发送偏好通知，没有运行新闻流程。

本次记录在 `data/profile-rollout-20260906/pro-max/preview.txt` 与
`preview-metrics.json`。正式重整/通知以及 2026-09-02 只读评价尚未执行。

## 后续自然反馈待验收

以下场景只等待自然发生的真实反馈或失败，不为覆盖而生成反馈、修改原始数据或
推进其他日期：重复兴趣是否只印证已有判断；新兴趣是否保留；已处理反馈修改是否
直接触发全量；累计 20 条真实修订的阈值；模型调用期间新增/修改反馈留待下一轮；
新格式回滚后首次新反馈触发全量；飞书通知失败后的优先补发。

本地详细操作记录保存在 `data/profile-rollout-20260906/`，不提交到版本库。
观察期 timer 保持停用，listener 保持运行。
