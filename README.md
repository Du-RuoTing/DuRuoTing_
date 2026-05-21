﻿# QQ Group Bot

用在QQ群聊的NoneBot2机器人。
A starter NoneBot2 QQ group bot powered by LLOneBot.

## Built-in Features



## Before Running

1. Start `LLOneBot`
2. Make sure reverse WebSocket points to `ws://127.0.0.1:8080/onebot/v11/ws`
3. Install dependencies, then run `python bot.py`

## Project Layout

```text
bot.py
src/plugins/
data/
```

### 需要手动补充的东西： 

##### .env
```text
DRIVER=~fastapi+~websockets
HOST=127.0.0.1
PORT= //
LOG_LEVEL=INFO
analysis_display_image=true
analysis_display_image_list=["video","bangumi","live","article","dynamic"]

SUPERUSERS=["242003347"]  # 替换为你的超级用户 QQ 号列表

ONEBOT_ACCESS_TOKEN=
LOCALSTORE_USE_CWD=true

DUEL__NICKNAME=杜若汀

# 平台可用 deepseek 或 packy；回复和摘要分别控制。
# 模型名和 API key 单独填写，避免平台选择、模型选择、密钥配置互相覆盖。
DU_RUO_TING_REPLY_SERVICE=deepseek
DU_RUO_TING_REPLY_MODEL=deepseek-v4-pro
DU_RUO_TING_SUMMARY_SERVICE=deepseek
DU_RUO_TING_SUMMARY_MODEL=deepseek-v4-flash
PACKY_API_KEY=//
DEEPSEEK_API_KEY=//
DU_RUO_TING_REQUEST_TIMEOUT_SECONDS=90
# Packy 常用模型：xxxx，需确认当前令牌分组可用。
# DeepSeek 常用模型：deepseek-v4-pro / deepseek-v4-flash。
