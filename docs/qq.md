# QQ 单好友归档

## 直接读取 QQNT

实测 Windows QQNT 9.9.36.53644。QQ 使用独立库、输入目录、来源标识、Drive 目录、上传台账和计划任务。例子假设已将 config.qq-db.example.json 复制为 config.qq.local.json。填写自己的数字 QQ 号、确认过的好友 QQ 号、备注及本机账号目录，再启用 identity_confirmed。该配置已被 Git 忽略。所有相对路径从配置文件所在目录解析。

```powershell
.\run.ps1 --config config.qq.local.json qq-auth
.\run.ps1 --config config.qq.local.json run --backfill --no-upload
.\run.ps1 --config config.qq.local.json update
.\run.ps1 --config config.qq.local.json stats
```

qq-auth 在当前用户权限下只读查找消息库与联系人库盐值对应的密钥，以第一页 HMAC 验证后写入 Windows DPAPI 文件。账号路径必须与配置吻合。密钥失效时重新执行 qq-auth。数据库页在内存中解密，应用已提交且通过校验的 WAL，再运行 quick_check。联系人表按数字 QQ 号查唯一 UID，私聊查询限制为此 UID；发送者按目标 UID 及本人 UID 集判定。页验证或身份核对失败时停止。库或 WAL 读取期间发生变化会重试；当前每次需读取当前数据库文件，不宣称物理 I/O 为增量。消息查询按时间水位、默认 72 小时重叠窗口和待补来源 ID 进行。run --backfill 扫描整个目标会话。归档使用来源消息 ID 去重；补采和普通更新共用 SQLite 提交及修订历史。

QQ 消息正文使用经核对的 protobuf 字段。组合消息保留元素顺序、正文和媒体索引；QQ 内置表情保留表情编号和显示文字。显式引用优先保留来源 ID，否则使用同一联系人的来源序号、时间和发送方号码唯一匹配；解析不全时记录来源摘要和待核信息。图片原件必须通过来源 MD5。来源记录指向本机可读预览时可保留预览；原件校验失败继续记为待补，后续重试。语音、视频、文件及其他未验证类型的完整解析与在线原件恢复仍有限。

## 截图及范围核验

QQ 主窗口可能包含其他会话。采集前需人工核对 QQ 号，并在本地配置 qq_panel 的 size（原图宽高）、rect（目标标题和消息区的左、上、右、下像素）、title（标题栏联系人显示名）和 window_title（Windows 窗口标题）。此设置与具体窗口尺寸对应。程序仅落盘标定区域，先检查尺寸、目标标题与至少两条消息 OCR 锚点；窗口最小化、锁屏或版式不符会拒绝抓图。直接 capture 尝试 Windows Graphics Capture。也可以使用下列方式核对用户提供的真实窗口截图：

```powershell
.\run.ps1 --config config.qq.local.json capture --qq-image "C:\path\qq.png"
.\run.ps1 --config config.qq.local.json capture-link S<截图摘要> M000001 M000002
```

原文、时间及发送者取自数据库；视觉范围需核对，截图边缘的残缺消息单独记录。截图显示客户端的界面时间；本地档案可按北京时间展示同一时刻。

编号型内置表情即使缺少显示文字也归为 emoji。此时 parts 中的 face_index 和 text_offset 分别记录表情编号和它在已保存正文中的字符位置（从 0 开始），正文不补写名称。QQ 消息类型 23、元素 26 的已验证“推荐联系人”卡片归为 system；按来源顺序保存标题和说明，未知卡片继续待核。普通更新按来源 ID 重读原有待核消息，保留修订历史。

完整回填后，每次增量运行还核对该好友早于重叠窗口的来源消息 ID。新同步到电脑的旧消息按 ID 补读，已入库的旧正文无需重复解析。首次限量试跑仍按配置范围采集。

## 同步与定时运行

首次在 config.qq.local.json 指定私有 drive_folder_id 和 OAuth 路径；本机可复用同一用户已授权的 Drive 根目录。update 生成本地阅读、统计及 Markdown，并发布到 QQ 联系人目录下的 01_Retrieval_Markdown。启用现有 LLM 时，对应的目标好友原文会发送给该供应商；配置和隐私说明见 README。原始媒体、预览和截图默认留在本机。确认部署后启用 live_acceptance_confirmed，再执行：

```powershell
.\run.ps1 --config config.qq.local.json schedule install
.\run.ps1 --config config.qq.local.json schedule run
Get-ScheduledTaskInfo -TaskName ChatTrace-Archive-QQ
```

北京时间每天 04:00。任务使用交互用户，部分媒体仍待补时会返回 synced_partial（退出码 3），即使检索文档已成功更新。普通微信任务是另一个任务名。

## 已有导出与上游

config.qq.example.json 用于 QQNT_Export 的 ChatLab 0.0.2 私聊 JSON。按导出的 meta.ownerId 和成员 platformId 核对双方身份。该 JSON 不包含独立媒体原件。详情参见 [导出器源码](https://github.com/Tealina28/QQNT_Export/blob/dev/exporters/chatlab_json.py)。不要混用两种读取器的归档与 inbox。

Windows 密钥匹配方法、消息字段及 MIT 来源见 [第三方声明](../chattrace/vendor/NOTICE.md)。已实测目标私聊记录、重复采集、隐私裁剪截图、原始媒体摘要、引用关系与 Markdown 管线。系统睡眠唤醒、其他 QQ 版本及手机全历史覆盖未验证。

### 自动与手动补回 QQ 媒体原件

普通更新及定时任务优先读取本地原件。图片或贴纸缺少原件时，读取器默认尝试消息中的腾讯下载路径及腾讯图片服务的 MD5 地址。同一地址每轮最多尝试一次。程序检查来源 MD5、已知文件大小和图像格式，将匹配的原件缓存到 qq-originals，再按正常流程入库。服务暂时不可用时保留待补状态。设 qq_media_download 为 false 可关闭网络补取。

将已取得的原件放入此 QQ 联系人的 `inbox/qq-originals/`，按来源 MD5（32 位小写十六进制）和真实文件扩展名命名。运行普通增量更新时，读取器会重试待补消息，按文件内容复核 MD5，再沿用现有资源去重和修订流程。校验失败的文件不作为预览或原件入库。该目录是本地恢复输入，不上传至 Drive。
