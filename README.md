# DuRuoTing QQ Bot

一个运行在 NoneBot2 + OneBot V11 上的 QQ 群机器人，当前通过 LLOneBot 接入 QQ。

项目包含群聊与私聊人格对话、长期记忆、可切换的本地 TTS、高考数据查询、报考参考、棋类游戏、睡眠记录、群功能管理等功能。

## 主要功能

- `duruoting_chat`：群聊/私聊人格对话、上下文、摘要和用户画像
- 本地 TTS：通过 CosyVoice 克隆参考声线，支持全局和单会话切换
- 高考录取查询与“小汀报考”参考数据
- 国际象棋和中国象棋图片对局
- 群签到、睡眠记录、群头衔和图片帮助菜单
- 学校英文简称查询
- GenshinUID、链接解析、舞萌等外部插件集成
- 群功能开关和自定义 admin

## 环境要求

- Windows 10/11
- Python `>=3.11,<3.15`
- LLOneBot 和 OneBot V11
- NVIDIA GPU、Docker Desktop 与 WSL2，仅在启用本地 CosyVoice TTS 时需要

## 安装与启动

1. 创建虚拟环境并安装项目依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

2. 创建 `.env`，至少填写监听地址、superuser 和聊天模型配置。

3. 在 LLOneBot 对应账号的反向 WebSocket 配置中填写：

```text
ws://127.0.0.1:18080/onebot/v11/ws
```

端口必须和 `.env` 中的 `PORT` 一致。`18080` 只是当前部署使用的端口，可以换成其他未占用端口。

4. 先启动 LLOneBot，再启动机器人：

```powershell
.\start-bot.bat
```

也可以直接运行：

```powershell
.\.venv\Scripts\python.exe bot.py
```

不要同时启动多个 NoneBot 实例，否则会出现 WinError 10048 端口占用错误。

## 基础配置

下面仅列出核心配置。API key、QQ 登录数据、人格文档和本地声纹文件不要提交到 Git。

```dotenv
DRIVER=~fastapi+~websockets
HOST=127.0.0.1
PORT=18080
LOG_LEVEL=INFO

SUPERUSERS=["你的 QQ 号"]
ONEBOT_ACCESS_TOKEN=
LOCALSTORE_USE_CWD=true

DU_RUO_TING_BOT_NAME=杜若汀
DU_RUO_TING_PERSONA_PATH=D:\nonebot\杜若汀.txt
DU_RUO_TING_PRIVATE_PERSONA_PATH=D:\nonebot\杜若汀私聊.txt
DU_RUO_TING_GROUP_PERSONA_PATHS={"1084401296":"D:\\nonebot\\杜若汀.txt"}

DU_RUO_TING_REPLY_SERVICE=huanyan
DU_RUO_TING_REPLY_MODEL=gpt-5.5
DU_RUO_TING_SUMMARY_SERVICE=huanyan
DU_RUO_TING_SUMMARY_MODEL=gpt-5.5
HUANYAN_API_KEY=填写你的密钥

DU_RUO_TING_REQUEST_TIMEOUT_SECONDS=90
DU_RUO_TING_MAX_REPLY_CHARS=200
```

可用聊天服务及对应密钥变量以 `src/plugins/duruoting_chat.py` 中的 `LLM_SERVICES` 为准。

## CosyVoice TTS

`duruoting_chat` 使用兼容 CosyVoice WebUI 的本地 HTTP 接口：

```text
POST http://127.0.0.1:50000/inference_zero_shot
```

请求包含 `tts_text`、`prompt_text` 和 `prompt_wav`，响应为 24 kHz、单声道、16-bit PCM。插件会在内存中封装为 WAV，再交给 LLOneBot 转换为 Silk 语音。

推荐将 CosyVoice 作为独立 Docker 服务部署，不要把模型、第三方源码和个人参考音频提交到本仓库。参考音频和对应文本必须内容一致，路径示例：

```text
voices/duruoting/reference.wav
voices/duruoting/prompt.txt
```

TTS 配置：

```dotenv
DU_RUO_TING_TTS_ENABLED=true
DU_RUO_TING_TTS_BASE_URL=http://127.0.0.1:50000
DU_RUO_TING_TTS_REFERENCE_WAV=D:\nonebot\voices\duruoting\reference.wav
DU_RUO_TING_TTS_PROMPT_TEXT_PATH=D:\nonebot\voices\duruoting\prompt.txt
DU_RUO_TING_TTS_TIMEOUT_SECONDS=120
DU_RUO_TING_TTS_TEXT_FALLBACK=true
```

TTS 管理指令仅限 superuser，不区分大小写：

| 指令 | 作用 |
| --- | --- |
| `tts global voice` | 所有会话使用语音，并清空单会话覆盖 |
| `tts global text` | 所有会话使用文字，并清空单会话覆盖 |
| `tts here voice` | 当前群聊或当前私聊单独使用语音 |
| `tts here text` | 当前群聊或当前私聊单独使用文字 |

语音模式会把一次模型回复合成为一条音频。文字模式会恢复短句发送，只按全角逗号 `，` 和全角句号 `。` 分割，忽略空片段。

模式设置保存在 `data/duruoting/tts_settings.json`。单会话设置优先于全局设置；再次执行全局指令会清空所有单会话覆盖。

## 常用管理指令

| 指令 | 权限与作用 |
| --- | --- |
| `功能` | 查看当前群功能状态 |
| `开启功能 <功能名>` | admin 或 superuser 开启群功能 |
| `关闭功能 <功能名>` | admin 或 superuser 关闭群功能 |
| `开启闲聊 [群号]` | admin 或 superuser 开启闲聊 |
| `关闭闲聊 [群号]` | admin 或 superuser 关闭闲聊 |
| `admin add <QQ号>` | superuser 添加当前群 admin |
| `admin remove <QQ号>` | superuser 移除当前群 admin |
| `admin list [群号]` | superuser 查看群 admin |

完整用户指令请在群内发送 `帮助` 查看图片菜单。高考查询使用 `gaokaohelp`，GitHub 通知功能当前处于停用状态。

## 项目结构

```text
bot.py                         NoneBot 启动入口
src/plugins/                   本地插件
data/                          运行状态、聊天记忆和数据库，默认不提交
voices/                        本地参考声纹与提示文本，默认不提交
services/cosyvoice/            本地 CosyVoice 服务源码和模型，默认不提交
fonts/                         本地渲染字体，默认不提交
```

## 提交前检查

建议只选择需要的源码和文档，不要直接提交 `.env`、`data/`、`voices/`、`llonebot/` 或模型文件：

```powershell
git status --short
git diff --check
git add src/plugins/duruoting_chat.py src/plugins/basic.py README.md .gitignore
git commit -m "Add configurable CosyVoice replies"
git push origin main
```

## 许可证

- 功能源代码采用 MIT License，详见 [LICENSE](./LICENSE)。
- 人格文本、原创设定和数据内容不自动随 MIT License 授权；使用时请遵守对应内容的许可与署名要求。
