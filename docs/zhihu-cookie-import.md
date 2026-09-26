# 知乎 Cookie 文件导入

采集器支持完整 Cookie 请求头文本和 Cookie JSON 数组。必须包含当前请求签名使用
的 `z_c0`、`d_c0`；账户有效性以真实 `/api/v4/me` 响应为准，导入不能保证解除平台
额外验证。导入成功表示账户验证通过，搜索和正文采集进度通过 Scout 的 `status` 查看。

## 输入文件

推荐将已登录知乎请求的完整 Cookie 请求头值存为 UTF-8 单行文本，可带 `Cookie:`
前缀。以下仅为格式示意，不能用于登录：

```text
z_c0=<实际值>; d_c0=<实际值>; <其他名称>=<实际值>
```

也可使用浏览器导出的 JSON 数组，每项含 `name`、`value`，可带 `domain`、`path`、
`expires` 或 `expirationDate`、`secure`、`httpOnly`、`sameSite`。域名仅接受知乎主域、
www 和 zhuanlan；缺省域名为 `.zhihu.com`，路径为 `/`。不接受任意 session ID。

文件必须直接放在 `/home/dev/workspace/mediacrawler-scout/data/scout/session/` 内，
由当前用户所有，权限为 `0600`，不能是符号链接或有额外硬链接，大小不超过 256 KiB。
建议命名 `zhihu-cookie.txt`；`cookies.json` 是采集器管理的会话快照，不能作为输入。
不要将文件放进 Scout、Git、聊天、飞书或证据目录，不要将 Cookie 值放入命令行。

```bash
chmod 600 /home/dev/workspace/mediacrawler-scout/data/scout/session/zhihu-cookie.txt
cd /home/dev/workspace/mediacrawler-scout/adapter
.venv/bin/python -m collector.import_session \
  --file ../data/scout/session/zhihu-cookie.txt \
  --run-id <采集器任务UUID>
```

`--run-id` 使用 Scout 的 `status` 输出中的 `collector_run_id`，不能传 Scout 扫描 UUID。
CLI 先输出请求 UUID，再等待最多 180 秒；`--wait-seconds` 可调整等待时长。
退出码 0 仅表示账户验证通过，2 表示尚未通过或仍在处理，1 表示本机提交/查询失败。
未知提交结果时使用相同 `--request-uuid <UUID>` 重试，先查询已保存结果，不重启浏览器
或重复执行已存在的导入。若服务中途退出，正在校验的请求标记 `interrupted`；检查状态后
用新 UUID 重试，不将不确定的导入误报成功。

## 服务与凭据边界

CLI 从采集器环境或其 `.env` 读取访问令牌，将规范化 Cookie 写入会话目录下的
`imports/import-<UUID去连字符>.json` 私有暂存文件。鉴权接口
`POST /v1/login/import` 只接收 `request_uuid`、原 `run_id`、暂存 `filename`，
`GET /v1/login/import/<UUID>` 查询持久导入结果。HTTP 不传输 Cookie 内容；请求参数
校验错误也不回显输入。接口只监听原本机服务地址。

现有服务持有浏览器锁，在独立浏览器上下文注入 Cookie 并实际验证账户。验证失败
保留原会话；成功后替换持久会话并原子写入 `session/cookies.json`，权限为 `0600`。
后续请求保存服务端更新的 Cookie，重启时重新检查账户，不凭快照文件存在判为已登录。
账户响应不进入日志或证据。处理结束删除暂存副本，用户输入原文件保留。

导入结果记录是历史结果；当前会话状态以 `GET /v1/login` 为准。原任务按已保存分页
位置继续，不新建扫描。额外验证、限流和网络问题保留具体分类。通过
`python -m scout.zhihu status` 查看 Cookie 登录状态及原任务进度。
真实搜索、正文筛选和自动链接通知见[采集流程](zhihu-validation.md)。
