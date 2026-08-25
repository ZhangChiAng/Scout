# 知乎接入调研（Scout 未来信源）

本文档汇总知乎数据开放平台与官方 Zhihu CLI 的调研结论，作为 Scout 未来接入
知乎的素材。V1 不写任何知乎相关代码，邀测申请是用户手工事项。

## 1. 平台现状与邀测申请

- 平台：developer.zhihu.com，当前处于**邀测阶段**，API 权限需要邮件申请。
- 申请路径：发送邮件至 openplatform@zhihu.com，说明使用场景与预估调用量，
  约 1 个工作日答复；计费为商务定制。
- 申请材料应说明 Scout 的场景：个人自用、AI 话题、读取真实开发者经验以
  过滤饭圈化讨论，仅读取公开内容。

## 2. HTTP API 鉴权与端点

- 鉴权头：
  - `Authorization: Bearer <access_secret>`（access_secret 在个人中心
    developer.zhihu.com/profile 生成）；
  - `X-Request-Timestamp`：秒级 Unix 时间戳；
  - `Content-Type: application/json`。
- 已知端点（均为 `GET https://developer.zhihu.com/api/v1/content/...`）：
  - `zhihu_search?Query=...`：知乎站内搜索；
  - `global_search`：全网搜索；
  - `hot_list`：知乎热榜。
- "直答 API"支持流式输出，搜索 API 不支持。

## 3. Zhihu CLI（官方 Agent 工具）

定位"让你的 Agent 读懂知乎"：Agent 用自然语言下达任务，CLI 负责取数并保留
原始内容链接，输出机器可读。

- 安装：把 skill 包发给 Agent——
  `https://developer-cdn.zhihu.com/zhihu-cli/releases/stable/skill/zhihu-cli-skill.zip`
- 能力矩阵与对 Scout 场景的适配度：

| 能力 | 说明 | 对"AI 话题 + 过滤饭圈化"的适配度 |
| --- | --- | --- |
| 搜索知乎 | 真实经验/观点/案例 | 高：可定向搜"DeepSeek API 调试"等开发体验帖 |
| 搜索全网 | 通用搜索 | 中：可作为补充信源，噪声较大 |
| 知乎热榜 | 热榜条目 | 中：热榜本身偏流量，需规则过滤后才可用 |
| 知乎直答 | 问答生成 | 低：生成内容，非原始开发经验 |
| 我的创作 | 个人创作读取 | 低：仅凭证所属账号 |
| 我的关注/收藏 | 个人数据 | 低：个人范围 |
| 知识库 | 查看/检索/上传单文件 | 中：上传调研文档后检索可行，但偏离信源定位 |

- 凭证：Access Secret 由 Agent 经标准输入交给 CLI；验证后存入 OS 凭证管理
  器（macOS Keychain / Windows Credential Manager / Linux Secret Service）；
  Linux SSH/CI/容器场景由宿主 Secret Store 经进程级环境变量注入。不写入
  skill 文件夹或普通配置文件。
- 边界：
  - 只查凭证所属账号，不接受 OAuth / 用户 ID / 代查；
  - 个人数据按需读取；摘要不等于原文；
  - 更新需征得同意，更新服务不接收 Secret/数据；
  - 第三方 Web 应用代表其他用户访问需另行接入知乎 OAuth，不得分发
    Access Secret。
- 行为：每 session 首次激活检查一次兼容性，不自动升级。

其他：官方 GitHub org（github.com/zhihu）另有发布向工具 ZhihuPublisher /
zhihu-mediacloud-uploader，与读取需求无关；PyPI `zhihu` 包为 2017 年社区
项目，不用。

## 4. 与 Scout 集成设计草案

未来接入两条路线，共同前提是完成邀测申请拿到 Access Secret：

- **路线 A：collector 以 subprocess 调用 Zhihu CLI**（官方设计路径）
  - 实现形态：新增 `zhihu_hot_list` / `zhihu_search` 等 collector 类型，
    通过 subprocess 下达自然语言任务、解析机器可读输出为 NewsItem，
    原始内容链接保留在条目里。
  - 优点：官方路径、凭证不经 Scout 代码（CLI 读 stdin 后交给 OS 凭证
    管理器）、能力边界由官方维护。
  - 缺点：依赖 CLI 安装与 session 环境；subprocess 输出契约需自建 fixture
    测试。
- **路线 B：直连 HTTP API**
  - 实现形态：collector 内直接发 `Bearer + X-Request-Timestamp` 请求，
    与现有 RSS collector 同构，复用 `_BaseCollector._fetch_bytes` 的
    超时/大小/重试设置。
  - 优点：无外部进程依赖，网络层与现有代码统一。
  - 缺点：Scout 需自行保管 Access Secret（环境变量），且 API 处于邀测、
    契约可能变化。

取舍建议：先走路线 A（官方设计、安全边界清晰）；当搜索/热榜端点稳定且
Scout 需要更细的查询参数控制时再评估路线 B。两条路线都只读取公开内容，
配合 `scout/filter.py` 的规则过滤实现"只留开发体验、不玩梗"的目标。

## 5. 风险

- **邀测门槛**：申请可能被拒或需商务谈判，接入时间不可控。
- **配额与计费**：商务定制，费用与 QPS 上限未公开。
- **单账号限制**：CLI 只查凭证所属账号，无法跨账号聚合社区样本。
- **内容质量**：知乎站内同样存在情绪化内容，规则过滤效果需真实样本验证；
  热榜天然偏流量，建议优先用搜索能力按关键词定向取数。
- **API 稳定性**：邀测阶段端点与鉴权细节可能变化。

## 6. 下一步行动

1. 用户手工：向 openplatform@zhihu.com 邮件申请邀测（说明个人自用、AI
   话题、读取公开开发经验、预估调用量）。
2. 拿到 Access Secret 后：安装 Zhihu CLI skill 包，验证搜索/热榜输出。
3. 评估路线 A：用少量真实样本设计 `zhihu_hot_list` collector 与过滤规则
  的 fixture；届时另立实施计划。
