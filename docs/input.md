# 单会话导入契约

这是 ChatTrace 的内部批次格式，供人工 import 与已实现的 wechat-db 读取器共用；不是微信官方导出格式。

将 `examples/batch.template.json` 复制到配置的 inbox，填写已确认的账号、好友 ID、真实采集时刻。每个批次 JSON 只含这一会话。媒体与 PNG/JPEG 截图位于同一目录或其子目录。禁止混入侧栏其他聊天、整份数据库或登录凭证；请人工核对截图范围。处理后保留输入不修改，后续修订使用新的批次文件。

## 消息字段（messages 数组的每个对象）

|字段|类型/含义|
|---|---|
|source_id|string 或 null；从来源获得的稳定 ID，不能把 UI RuntimeId 当长期 ID|
|timestamp|带 UTC offset 的 ISO 时间或 null；不允许无时区时间|
|precision|second / minute / day / unknown；null 时间必须为 unknown|
|sender|self / friend / system / unknown|
|type|text / image / sticker / voice / video / file / quote / system / recalled；其他值保留并 needs_review|
|text|string，原始文字，不能 OCR 改写；无文字时空字符串|
|sequence|整数，来源顺序；同时间消息按此排序|
|occurrence|整数，来自完整来源的稳定重复序号，不能随分页或批次重置|
|media|数组，每项 kind（媒体类型）和 path（批次相对路径）|
|screenshots|数组，引用批次 screenshots 中的 id|
|reply_to|string 或 null，被引用来源消息 ID；当前保存来源引用，不解析跨来源关系|

批次 screenshots 数组每项为 id 和 path。必须提供真实 PNG/JPEG，扩展名与文件签名一致；保留原始字节，不绘制微信界面。消息可以共享同一张上下文截图。manifest 列出其所有关联消息，不把中间的未知消息假定为已覆盖。

同一账号/联系人/source/source_id 的变化保留为修订。没有 source_id 时，组合时间、精度、发送者、类型、原文和 occurrence 去重；输入不足以区分两条相同消息时拒绝同批歧义。无 ID 的内容变化视为新消息，程序不能自动识别撤回对应哪条记录。系统事件必须指定 system，程序不会猜测发送者。

0.3补充：重复采集的媒体失败不会清除已经归档的同类型媒体引用，source_metadata.media_retry_status保留新失败状态；仍可继续重试。截图完整覆盖要求文件存在、range_verification=visual_confirmation且当前M编号不在partially_visible_message_ids中；其他连续截图可独立补齐该消息，不能根据两个端点编号推断中间消息。截图级partially_visible_message_ids会反向关联本库已存在消息，即使消息自身没有截图链接；它表示该图中的部分可见，不表示未入库。当前试验没有选择某消息，也不等于正式归档没有该消息。详细状态定义见retrieval.md。

只访问 inbox 顶层 JSON。文件 size/mtime 未变时跳过读取；新批次再用 SHA-256 检查。文件和附件应视为不可变输入；更正必须另建批次，且附上需保留的媒体。批次内容改变但附件同名时，以新批次中的实际字节生成新媒体编号。不会自动删除旧输入或修订。

## 覆盖范围

wechat-ui 文本读取入口保持禁用；正式文字来源是数据库。wechat-db 使用72小时重叠窗口和待补媒体ID查询，不能确认手机或服务端尚未同步到本机的消息。import 能接收任意较早日期的新批次，不使用时间水位丢弃它们。截图分段已经实现，消息范围核对仍需人工；截图 metadata 保存原始尺寸、裁剪范围、原图摘要和部分可见消息。

## 已实现的数据库批次扩展

wechat-db 使用同一 schema=1；source 固定为 wechat-db-v4。source_id 优先为服务端 ID，缺失时为分片名和 local_id。新增 source_metadata 保存分片、local_id、local_type、状态、实际发送者、媒体获取状态；raw_content 仅用于非文本的原始结构，输出前去除媒体 AES 密钥、授权字段和资源 URL。原始聊天文本不做 strip、纠错或 XML 重写。message_content 是 zstd 时只解压并严格 UTF-8 解码，失败会保留二进制的 Base64 并标记待处理。

图片从目标联系人的 attach 子目录按 protobuf 中的文件名定位；本地只有缩略图时必须记录 thumbnail，不能声称取得原图。表情可从消息对应的腾讯 CDN 下载，校验媒体 MD5 或图片解析结果，不上传任何数据；HTTP 错误仅记录错误代码，不打印 URL、令牌或正文。原始截图仍是独立来源，本模式不伪造界面截图。
