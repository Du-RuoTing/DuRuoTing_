import json
from pathlib import Path

import nonebot
from nonebot import get_asgi, init, load_plugins
from nonebot.adapters.onebot.v11 import Adapter
from nonebot import load_plugin


def _cleanup_invalid_parser_bilibili_cookie() -> None:
    cookie_path = Path("config") / "nonebot_plugin_parser" / "bilibili_cookies.json"
    if not cookie_path.exists():
        return

    try:
        cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cookies = {}

    if not isinstance(cookies, dict):
        cookies = {}

    required_keys = ("SESSDATA", "bili_jct")
    if all(str(cookies.get(key, "")).strip() for key in required_keys):
        return

    backup_path = cookie_path.with_suffix(".invalid.json")
    try:
        cookie_path.replace(backup_path)
    except OSError:
        pass


# 初始化 NoneBot 配置和驱动环境。
init()
_cleanup_invalid_parser_bilibili_cookie()

# 注册 OneBot V11 适配器，并加载本地插件目录。
driver = nonebot.get_driver()
driver.register_adapter(Adapter)
load_plugins("src/plugins")
load_plugin("nonebot_plugin_withdraw")
load_plugin("nonebot_plugin_whateat_pic")
load_plugin("nonebot_plugin_today_waifu")
load_plugin("nonebot_plugin_wordsnorote")
load_plugin("nonebot_plugin_reboot")
load_plugin("nonebot_plugin_parser")
load_plugin("nonebot_plugin_duel")
load_plugin("nonebot_plugin_steam_info")
load_plugin("nonebot_plugin_maimaidx")
load_plugin("nonebot_plugin_pjsk_helper")
load_plugin("GenshinUID")


# 提供给 ASGI 服务器使用的应用对象。
app = get_asgi()


if __name__ == "__main__":
    # 直接运行当前机器人进程。
    nonebot.run()
