# 2026-09-06 定时推送恢复记录

## 已上线

北京时间约 14:45 安装并启用 `scout-send.timer` 与
`scout-profile-update.timer`，两个 timer 均为 active、`Persistent=no`。
用户 `Linger=yes`，原飞书反馈 listener 保持 active，未重启。

- 新闻检查：每天 09:30、10:00、10:30、11:00、11:30、12:00、12:30。
  systemd 返回下次执行时间为 **2026-09-07 09:30:00 CST**。
- 偏好更新：每天 19:00，处理不晚于当天 19:00 的已保存反馈。
  systemd 返回下次执行时间为 **2026-09-06 19:00:00 CST**。
- 两个 timer 显式指定 `Asia/Shanghai`，没有启用时补触发；白天入口在窗口外退出。
- 当天完整快照已保存后停止 RSS 请求；按原年龄边界与去重规则升序补齐待发送期次。
  重试使用 SQLite 快照、评价缓存、正文和列表送达状态。
- 自动偏好更新独立运行，白天只使用已保存偏好及该版本已消费的反馈。
  模型失败保留原偏好及未消费反馈；通知失败保留新版本及待通知状态。

## 已完成的检查

- 修改前在发送锁内通过 SQLite backup API 备份真实数据库：
  `data/scout-before-schedule-20260906T144113.sqlite3`，权限 0600，完整性检查为 ok。
- `uv run --locked ruff check .`、`ruff format --check .`、
  `python -m compileall -q scout`、`git diff --check` 均通过。
- `systemd-analyze calendar --iterations=8` 核对 09:30、10:00–12:30
  六个半小时时点及每日 19:00；`systemd-analyze --user verify` 校验四个发送／
  偏好 unit 通过。
- 在真实当前时间 14:44 执行新入口：`--send --scheduled` 因窗口外退出；
  `--profile-update` 因尚未到 19:00 退出。没有模型请求、数据库业务写入或飞书发送。
- 只读请求真实橘鸦 RSS，返回 7 期，收集警告为 0，全部可解析为规范化条目。
  9 月 6 日发布时间为 09:16:27，包含 8 条；前六期对应 9 月 5 日至 8 月 31 日。
- 真实库中 9 月 2–6 日五期完整快照均已有整期完成记录。当前为偏好 v11，
  已消费修订 27，最新修订 31；共有 29 条当前有效反馈，其中 v11 截止位置的
  有效反馈为 25 条。此次没有提前消费剩余反馈。
- 上线后真实库 `integrity_check=ok`、`foreign_key_check` 为空；共 117 条新闻
  快照、72 条正文送达、5 张列表、104 条评价缓存和 11 个偏好版本。
  本次没有新闻或档案发送，也没有为了验收重放已完成期次。

日志中的 `first_saved_at` 读取该日报最早的 `article_snapshots.captured_at`，
因此也适用于上线前保存的真实快照，不为旧记录编造抓取时间。

## 待实际触发观察

上线时尚未到首个 19:00，也未到下一次白天窗口。以下结果尚未验收，不将静态检查
或已有送达记录当作新调度端到端通过：

1. 9 月 6 日 19:00 的真实模型偏好更新、SQLite 消费进度与飞书变化通知；
   19:00 后新增或修改的反馈留到次日。
2. 9 月 7 日的新闻任务使用真实 RSS、当前模型端点、SQLite 和飞书群，首次发现
   当天日报后保存并推送；后续检查点出现 `RSS skipped`，完成后出现
   `No pending issues`，没有重复模型调用或重复卡片。
3. 12:30 后发布、有效期内补更、模型／发送失败重试及跨日重启恢复，等待自然
   真实场景；不伪造数据、不调整系统时间、不重发已送达内容。

观察命令：

```bash
systemctl --user list-timers scout-send.timer scout-profile-update.timer
journalctl --user -u scout-profile-update.service --since '2026-09-06 18:59:00'
journalctl --user -u scout-send.service --since '2026-09-07 09:29:00'
uv run --locked python -m scout --profile-show
uv run --locked python -m scout --profile-history
```

没有编写单元测试或模拟测试。
