# 日常运行、备份与恢复

## 手动更新

从项目目录执行 `.\run.ps1 update`。Windows 计划任务运行相同的 Python 命令。一次更新在同一把本地锁内完成以下步骤。

1. 核对微信账号与好友身份，读取包含已提交 WAL 的只读快照。首次按配置试读，之后按时间水位和重叠窗口读取。消息 ID 用于去重，解析修订保存在 SQLite。
2. 提交本地批次、生成每日文件、校验资源。若开启 `upload_enabled`，同时同步每日归档与媒体上传队列。
3. 从 SQLite 构建阅读页面及检索正文，逐条验证 ID、时间、顺序与原文，再发布本地文档。
4. 将正式正文、00_START_HERE 和 01_CATALOG 同步到 Drive 的 01_Retrieval。上传后导出 Docs 回读校验，成功后保存远端 ID 和摘要。目录使用已知文档链接。重复执行时跳过相同内容。

默认的云端发布范围是正式检索 Google Docs。月度 HTML、原始媒体和连续截图属于本地档案。`upload_enabled` 专门控制额外的每日 Markdown、JSONL、Manifest、媒体和截图文件队列；`update` 与 `sync` 总会执行正式检索文档发布。`run --no-upload` 只做采集。提取媒体与截图覆盖分别计数，manual 截图策略允许保留没有截图的纯文本消息。

## 定时任务

任务名为 `ChatTrace-Archive`。schedule_time 使用北京时间（UTC+8）。每日 04:00 对应前一日 20:00 UTC，Windows 任务界面显示系统当地时间。任务使用当前用户、最低权限、仅在用户已登录时运行，工作目录为项目根目录。睡眠时允许唤醒，漏过计划时允许补跑；实际唤醒取决于设备、Windows 电源设置和账户会话。程序不会登录或解锁桌面。

| 操作 | PowerShell |
|---|---|
| 安装或首次启用 | `.\run.ps1 schedule install` |
| 立即通过任务计划运行 | `.\run.ps1 schedule run` |
| 查看系统的时间与结果 | `Get-ScheduledTaskInfo -TaskName ChatTrace-Archive` |
| 暂停 / 恢复 | `.\run.ps1 schedule pause` / `.\run.ps1 schedule resume` |
| 卸载任务 | `.\run.ps1 schedule remove` |

修改时间后，再执行 schedule install 原位更新任务。已运行的同一任务不会启动第二个实例。手动更新和后台更新也共享归档锁。失败时 Windows 每 15 分钟重试，最多两次，最长运行 3 小时。

## 查看和重试

每次 update 在 archive_root/update-runs/ 下记录一份 JSON。它包含开始/结束 UTC 时间、阶段、采集运行号、消息数、卷数和 Drive 变更计数。日志中不含聊天正文。查看最近一份记录可运行：

```powershell
$config = Get-Content config.json -Raw | ConvertFrom-Json
Get-ChildItem (Join-Path $config.archive_root 'update-runs') -Filter '*.json' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content
```

退出码 0 表示本次指定范围完成，3 表示媒体或策略要求的截图仍待补，2 表示执行失败。日志停在 running 时结合任务状态判断是否中断。`status` 查看采集历史，`coverage` 核查 SQLite、文件和媒体；`pending` 列出额外文件队列，正式 Docs 的差异用 `sync --dry-run` 查看。

- 微信未运行、账号不一致或版本不受支持时，检查 doctor，重新登录正确账号后重跑。仅在密钥失效时执行 wechat-auth。
- Drive 报 DRIVE_AUTH_REQUIRED 时运行 auth，并在浏览器重新授权。同一 Windows 用户才能解开 DPAPI 令牌。网络故障后可以运行 update 重试全部流程；已构建的正文也可单独 sync。
- 需要覆盖更早的本机历史时，运行 run --backfill --no-upload，再执行 update。常规更新不会每天扫描全部历史。
- 生成截图时打开该好友独立窗口，使用 capture-series 并核对实际消息范围。锁屏与窗口最小化会限制截图；不要将截图缺失记为正文漏采。
- 超大消息连同文档元数据超过硬上限时，检索构建会失败并保留本地原文。检查本地文档与错误码，不能将任务启动视为云端成功。

## 备份和迁移

先暂停任务并等待当前进程退出，再备份整个 archive_root、inbox、本地配置和凭证。保留 SQLite、关联媒体、截图和 Drive 映射的一致副本。在新 Windows 用户下重新执行 OAuth 和微信身份授权。恢复后先 validate 与 sync --dry-run，再恢复任务。更换电脑还需更新任务的 Python 路径。用户自行处置标为 pending_retirement 的旧远端文件。
补采历史表情时，程序优先查找消息所属月份的原件缓存。微信也会把重新同步的表情写入同步月份；此时按消息 MD5 在其他月份的同一原件路径查找，解码后核对 MD5 并验证图片格式。
