from __future__ import annotations

from random import randint

from nonebot import on_fullmatch, on_notice, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, NoticeEvent
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from .state import get_group_features, is_feature_enabled, set_group_feature, sign_in


HELP = "帮助"
MENU = "菜单"
CHECK_IN = "签到"
WELCOME = "欢迎"
FEATURE = "功能"
FEATURE_ON = "开启功能"
FEATURE_OFF = "关闭功能"
UNKNOWN_FEATURE = "未知功能，只能操作：帮助、签到、欢迎、roll、闲聊、头衔"

admin_permission = GROUP_ADMIN | GROUP_OWNER | SUPERUSER

help_cmd = on_fullmatch({HELP, MENU, "help"}, priority=10, block=True)
ping_cmd = on_fullmatch("ping", priority=10, block=True)
roll_cmd = on_regex(r"^roll(?:\s+\d+\s+\d+)?$", priority=10, block=True)
sign_cmd = on_fullmatch(CHECK_IN, priority=10, block=True)
feature_cmd = on_fullmatch(FEATURE, permission=admin_permission, priority=5, block=True)
feature_on_cmd = on_regex(r"^开启功能(?:\s+.+)?$", permission=admin_permission, priority=5, block=True)
feature_off_cmd = on_regex(r"^关闭功能(?:\s+.+)?$", permission=admin_permission, priority=5, block=True)
welcome_notice = on_notice(priority=20, block=False)


@help_cmd.handle()
async def handle_help(event: GroupMessageEvent) -> None:
    if not is_feature_enabled(event.group_id, HELP):
        await help_cmd.finish("这个群已经关闭了帮助功能。")

    features = get_group_features(event.group_id)
    lines = [
        "可用命令：",
        "1. 帮助 / 菜单",
        "2. ping",
        "3. 签到",
        "4. 掷骰子！roll <min> <max>",
        "5. 开启功能 <帮助|签到|欢迎|roll|闲聊|头衔>",
        "6. 关闭功能 <帮助|签到|欢迎|roll|闲聊|头衔>",
        "7. 早安 / 晚安 / 睡眠统计",
        "8. （xx）吃什么 / （xx）今天喝什么 / 添加菜单 / 查看菜单",
        "9. 杜若汀会聊天喔，@我开启闲聊",
        "10. 设置头衔 <内容> / 清除头衔",
        "11. 今日单词",
        "12. favor",
        "当前群功能状态：",
    ]
    lines.extend(f"- {name}: {'开启' if enabled else '关闭'}" for name, enabled in features.items())
    await help_cmd.finish("\n".join(lines))


@ping_cmd.handle()
async def handle_ping(event: GroupMessageEvent) -> None:
    await ping_cmd.finish(f"pong\n群号: {event.group_id}\n用户: {event.user_id}")


@roll_cmd.handle()
async def handle_roll(event: GroupMessageEvent) -> None:
    if not is_feature_enabled(event.group_id, "roll"):
        await roll_cmd.finish("这个群已经关闭了 roll 功能。")

    parts = event.get_plaintext().split()
    if len(parts) != 3:
        await roll_cmd.finish("用法：roll 1 100")

    try:
        start, end = int(parts[1]), int(parts[2])
    except ValueError:
        await roll_cmd.finish("roll 参数必须是整数。")

    if start > end:
        start, end = end, start

    if end - start > 10000:
        await roll_cmd.finish("范围太大了，换个小一点的区间吧。")

    value = randint(start, end)
    await roll_cmd.finish(f"你掷出了 {value}，范围 [{start}, {end}]。")


@sign_cmd.handle()
async def handle_sign(event: GroupMessageEvent) -> None:
    if not is_feature_enabled(event.group_id, CHECK_IN):
        await sign_cmd.finish("这个群已经关闭了签到功能。")

    created, streak = sign_in(event.user_id)
    if not created:
        await sign_cmd.finish(f"今天已经签过到了，当前连续签到 {streak} 天。")
    await sign_cmd.finish(f"签到成功，当前连续签到 {streak} 天。")


async def _toggle_feature(event: GroupMessageEvent, feature: str, enabled: bool) -> str:
    if not set_group_feature(event.group_id, feature, enabled):
        return UNKNOWN_FEATURE
    state = "开启" if enabled else "关闭"
    return f"已将 {feature} {state}。"


@feature_cmd.handle()
async def handle_feature_list(event: GroupMessageEvent) -> None:
    features = get_group_features(event.group_id)
    text = "\n".join(f"- {name}: {'开启' if enabled else '关闭'}" for name, enabled in features.items())
    await feature_cmd.finish(f"当前功能状态：\n{text}")


@feature_on_cmd.handle()
async def handle_feature_on(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    feature = args.extract_plain_text().strip().removeprefix(FEATURE_ON).strip()
    await feature_on_cmd.finish(await _toggle_feature(event, feature, True))


@feature_off_cmd.handle()
async def handle_feature_off(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    feature = args.extract_plain_text().strip().removeprefix(FEATURE_OFF).strip()
    await feature_off_cmd.finish(await _toggle_feature(event, feature, False))


@welcome_notice.handle()
async def handle_welcome(bot: Bot, event: NoticeEvent) -> None:
    notice_type = getattr(event, "notice_type", None)
    sub_type = getattr(event, "sub_type", None)
    group_id = getattr(event, "group_id", None)
    user_id = getattr(event, "user_id", None)

    if notice_type != "group_increase":
        return
    if group_id is None or user_id is None:
        return
    if sub_type not in {"approve", "invite", None}:
        return
    if not is_feature_enabled(group_id, WELCOME):
        return

    await bot.send_group_msg(
        group_id=group_id,
        message=(
            "欢迎来到小汀的茶馆喔！这里有的是沾着露水的鲜花、新沏的茶、美丽的故事和可爱的茶友\n"
            "桓衍有时候不在家\n"
            "有什么问题都可以和我说喔\n"
            "我是杜若汀！请多关照喔！"
        ),
    )
