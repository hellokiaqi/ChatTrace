# 正式检索视图

ChatGPT / Gemini 定位句：请调用 Google Drive，读取 ChatTrace 的 01_Retrieval，在 01_CATALOG 定位正式正文，引用原始聊天里的时间、发送者与消息 ID。
Message → Conversation Block → Topic Segment → Retrieval Document。SQLite 提供时间、身份、正文、媒体与来源引用。读取器按时间、来源顺序和稳定 ID 排序。Human Archive 继续使用既有月度 HTML。

模型接收连续原文、时间差、真实引用、前后文与前一话题。90 分钟以内优先连续，90 分钟至 6 小时提示候选间隔，超过 6 小时优先检查是否重新开始。跨午夜、沉默和话题改变本身均不强制结束 Block。独立检索问题决定 Segment；短回应与自然收尾并入相邻话题。模型只建议边界和导航词。阶段缓存包含输入、提示、温度、端点和模型。模型不可用或返回不合法边界时采用确定性分组并报告回退。默认沿用本机 .env 中配置的 DeepSeek；离线使用 --no-llm。

程序按完整文档的 Unicode 字符数装箱，包括导航、正文和换行。软阈值 8,000，优选 8,000–12,000，硬上限 16,000。优先在话题边界结束一卷；独立话题过长时优先采用语义子边界，然后在消息之间续卷。单条消息连同必要格式超过上限时拒绝发布，保留原文。超过七天只记提示。显示名含日期、卷号及话题；逻辑 ID 取决于 namespace 和首条消息身份。

运行 `main.py build --preview` 生成独立预览，`main.py validate --preview` 对照 SQLite 校验。正式使用 `main.py promote` 发布已验证预览；`main.py build` 将上述步骤与既有人类阅读视图生成串联。原始检索区保留历史备份。入口 00_START_HERE 说明证据范围，01_CATALOG 提供正式文件定位。Google Drive 使用 `main.py sync --dry-run` 预检、`main.py sync` 手动发布。失效映射只列入 pending_retirement。授权失效报告 DRIVE_AUTH_REQUIRED。详见 [验证方法](verification.md)。

预览位于 archive_root/retrieval/v4_preview。正式包位于 archive_root/v4/retrieval。每个包内含 manifest.json。替换前一代移入 archive_root/retrieval-history；v4/drive-state.json 独立保留。原生 Docs 的更新按逻辑 ID 定位；已有成功台账且内容摘要与标题一致时，sync 核验远端身份后跳过，未检查用户手改的正文。写入后导出文本并校验，接受 Google 导出时的空行转换。首次 OAuth 配置沿用 README，重新授权使用 main.py auth。

配置项 v4_soft_chars 和 v4_max_chars 的默认值分别为 8000、16000。装箱低于软阈值时继续加入话题，硬上限优先。从软阈值开始优先选择 Block 边界，达到优选长度上沿后可在下一个话题边界换卷。末卷较短且紧接前卷时，重新测量合并后的完整文本，能容纳则合卷。日期跨度以实际首末消息计算。SQLite 的修订保留在原管线；检索文档导出当前版本。

复制 .env.example 为 .env 并配置 CHATTRACE_LLM_BASE_URL、CHATTRACE_LLM_MODEL、CHATTRACE_LLM_API_KEY。环境变量优先。本机选择 deepseek-flash；启用模型时，原文窗口将发送给配置的供应商。使用 --refresh-metadata 可重算语义建议。相邻窗口按顺序处理，提供前一话题与两侧上下文；另一次语义检查决定哪些话题边界实际开启新的 Block。完整消息 ID 包含会话 namespace。本地 Human Archive 的入口仍为 v4/human/index.html。
