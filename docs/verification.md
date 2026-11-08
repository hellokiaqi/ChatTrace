# 验证与维护

## 可重复检查

从干净的源码目录建立 Python 3.12 环境并安装 requirements.lock.txt。执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m pip check
.\run.ps1 doctor
```

测试使用临时 SQLite 和合成消息。覆盖首次导入、重跑、同秒排序、时间重叠、晚到记录、修订历史、快照/WAL、单联系人身份绑定、媒体缓存、截图范围、数据损坏、上传失败恢复、预览隔离、分卷字符上限、目录变更、远端文档回读和任务 XML。

## 部署验收

1. 运行 update。打开本地 HTML 查一条文字和一条媒体消息。用 search 或每日 JSONL 核对消息 ID，再用 validate 检查分卷中每条当前消息只出现一次。
2. 打开 Google Drive 正式检索目录的 01_CATALOG。检查链接指向本账号的正式文档；在原始聊天区核对时间、发送者、消息 ID 和原文。
3. 再执行一次 update。在来源未改变时，运行记录中的 changed_documents 应为 0。更新已登记的文档应保持原文件 ID。
4. 运行 schedule run，从 Get-ScheduledTaskInfo 检查 LastTaskResult、LastRunTime、NextRunTime，并核对 update-runs 的 ended、status 和 stage。
5. 在 ChatGPT 或 Gemini 中调用 Drive，用一句实际原话、一个日期和一条消息 ID 分别检索。按 Google Docs 原始聊天区核对引用。

初始版在 Windows 当前用户的真实计划任务和手动命令入口均完成过正式更新。核对方式包含原生 Docs 文本导出、归档消息块逐条匹配及重复运行跳过写入。实际生产数据、文件 ID 和部署日志仅保存在用户本机。系统睡眠时能否按时唤醒、锁屏截图、手机或备份历史完整性需在对应设备上单独验证。

## 源码发布范围

Git 包含 Python 源码、合成测试、配置模板、空档案示例、安装与运维文档、依赖清单及第三方声明。提交前用 git diff --cached --name-only 检查索引。实际配置、API key、OAuth 客户端与令牌、数据库、聊天正文、截图、个人报告、模型缓存和运行日志应位于忽略范围内。保留 chattrace/vendor 中的第三方许可证。

北京时间调度与统计验证：任务 XML 带明确的 UTC+8 偏移，实际注册后通过 Windows 任务计划程序回读下一触发时刻。统计测试覆盖 UTC 跨北京午夜、跨年自然周、闰年月份、自定义区间起止、空结果、同分钟去重及媒体/引用正文计数。统计页面使用的 JavaScript 已执行测试，Chrome 离线加载真实页面的双方条数与字数和 SQLite 独立汇总一致。
