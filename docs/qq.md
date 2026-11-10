# QQ 单好友归档

当前适配器读取 QQNT_Export 的 ChatLab 0.0.2 私聊 JSON。输入来自已导出的文件；导出步骤需由可用且已获授权的工具完成。上游 [JSON 生成器](https://github.com/Tealina28/QQNT_Export/blob/dev/exporters/chatlab_json.py) 提供消息 ID、成员 ID、秒级时间、类型和引用目标。其 JSON 导出不打包独立媒体。

复制 config.qq.example.json 为 private/qq-config.json 时注意所有相对路径以配置文件所在目录解析。建议直接在项目根目录另存为 config.qq.local.json。填写导出中的 meta.ownerId、目标成员 platformId、备注与导出文件路径，核对身份后设 identity_confirmed=true。仅当 ownerId 与账号相符、成员集合恰好为这两人、每条发送者均匹配时才导入。数据分开入库，不按备注推断两个人为同一身份。

运行 `.\run.ps1 --config config.qq.local.json run --no-upload` 做首次导入。再用同一配置运行 build --no-llm、stats、validate 或 update。定时 update 可以读取外部程序更新的同一路径，按来源消息 ID 去重；它不会主动刷新 QQ 客户端。独立的 QQ 任务名避免覆盖微信任务。

处理范围为已导出的正文、来源身份、时间、类型、引用 ID。媒体占位保留其导出表示，缺原件标记待补；未知类型保留编码。QQ 原生窗口截图、直接读 QQNT 库，以及从真实 QQ 私聊至 Drive 的闭环未经验证。客户端实时接入另需验证版本并确认数据访问方法。
本机核对记录：Windows QQNT 9.9.36.53644 正在运行，发现账号目录和 nt_msg.db，文件头不是标准 SQLite。使用合成 ChatLab 输入实测首次导入、重复导入、同秒新增引用消息、错误账号和群聊拒绝、Markdown 构建及同步预检；原有微信正式检索文档独立校验通过。此项未执行 QQ 私聊实际上传。
