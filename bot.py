import nonebot
from nonebot import get_asgi, init, load_plugins
from nonebot.adapters.onebot.v11 import Adapter
from nonebot import load_plugin


# 初始化 NoneBot 配置和驱动环境。
init()

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


# 提供给 ASGI 服务器使用的应用对象。
app = get_asgi()


if __name__ == "__main__":
    # 直接运行当前机器人进程。
    nonebot.run()
