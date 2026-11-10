# 安装与首次配置

## 运行环境

使用 Windows x64、Python 3.12 和受支持的微信桌面客户端。下列命令在项目根目录的 PowerShell 中执行。使用一般用户账户即可；首次装机需允许安装项目依赖。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip check
.\run.ps1 doctor
```

启动任务时使用注册当时的 Python 绝对路径。移动项目、替换 Python 或重建虚拟环境后，按运行手册重新注册任务。

## 绑定一个联系人

在新部署中将 `config.example.json` 复制为 `config.json`。先在微信资料页核实好友的唯一微信号，填写 `contact_id` 和备注名 `contact_name`，再把 `identity_confirmed` 设为 true。首次绑定保留模板中的两个 `BIND_ON_SETUP`。使用已登录且存有目标聊天记录的 Windows 账户执行：

```powershell
.\run.ps1 wechat-auth
.\run.ps1 run --no-upload
.\run.ps1 coverage
```

wechat-auth 使用项目的只读账号适配器获取本地密钥，写入当前用户 DPAPI 保护的 `wechat_secret_path`。模板的首次绑定用精确微信号定位唯一的一对一联系人，将实际账号和联系人内部标识记入本地配置。后续采集会核对账号及联系人。请使用新的空归档目录。已有部署应继续使用原配置、状态库及上传台账。

默认首次试读最新 10 条。查看日期目录内的 conversation.md、messages.jsonl、manifest.json，核对身份及原文。历史补采使用 `run --backfill --no-upload`。之后的普通采集采用 72 小时重叠窗口，媒体未齐时保留待处理状态。采集范围以电脑已同步的数据库为准。

人工导入可以从 `init` 建立独立的 import 配置，再按照 [批次格式](input.md) 放入消息和附件。

## Google Drive

使用已启用 Drive API 的 Google Cloud 项目。创建桌面应用 OAuth 客户端，将下载的 JSON 放入本机配置指定的 `client_secret_path`。处于测试状态的应用需为当前账号开放测试访问。指定一个归本人所有、可写且私有的目录 ID，或让程序创建私有根目录。

执行 `.\run.ps1 auth`，在浏览器中完成授权。程序申请 drive.file 权限，并将令牌用 DPAPI 保存到 token_path。此授权用于后台任务；网页端 ChatGPT/Gemini 的 Drive 连接单独配置。

## 语义分段与启用

复制 `.env.example` 为 `.env`。配置启用标志、OpenAI-compatible URL、模型名和密钥，或将 CHATTRACE_LLM_ENABLED 设为 false 使用本地规则。所有模型字段仅用于检索导航。

执行 `.\run.ps1 update` 后查看本地阅读入口以及云端 01_Retrieval_Markdown。确认部署结果后，在配置中将 `live_acceptance_confirmed` 设为 true，选择 `schedule_time`（默认北京时间 04:00），执行 `.\run.ps1 schedule install`。
