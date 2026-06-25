from __future__ import annotations

from io import BytesIO
from pathlib import Path
from random import randint
import re

from nonebot import get_driver, on_fullmatch, on_notice, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment, NoticeEvent
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor
from nonebot.permission import SUPERUSER
from PIL import Image, ImageDraw, ImageFont

from .state import get_group_features, is_feature_enabled, set_group_feature, sign_in


HELP = "帮助"
MENU = "菜单"
CHECK_IN = "签到"
WELCOME = "欢迎"
FEATURE = "功能"
FEATURE_ON = "开启功能"
FEATURE_OFF = "关闭功能"
UNKNOWN_FEATURE = "未知功能，只能操作：帮助、签到、欢迎、roll、闲聊、头衔、favor、今天吃什么、小汀报考"
CHAT_FEATURE = "闲聊"
FAVOR_FEATURE = "favor"
WHATEAT_FEATURE = "今天吃什么"

WAIFU_COMMAND_RE = re.compile(
    r"^\s*[/.]?(?:"
    r"今日老婆(?:信息|帮助)?|"
    r"换老婆|"
    r"(?:刷新|重置)今日老婆|"
    r"设置换老婆次数\s*\d+|"
    r"(?:开启|关闭)换老婆|"
    r"(?:开启|关闭)自动撤回|"
    r"设置自动撤回延迟\s*\d+|"
    r"(?:开启|关闭)自动设置对方老婆|"
    r"设置抽取模式\s*.+|"
    r"设置活跃天数\s*\d+"
    r")\s*$"
)
FAVOR_COMMAND_RE = re.compile(r"^\s*[/.]?favor(?:\s+.*)?$", re.I)
WHATEAT_COMMAND_RE = re.compile(
    r"^\s*[/.]?(?:"
    r"(?:今|明|后)?(?:天|日)?(?:早|中|晚|上|下)?(?:上|午|餐|饭|夜宵|宵夜|早|晚)?(?:吃|喝)(?:什么|啥|点啥)|"
    r"(?:今天吃什么|今天喝什么|全部菜单|查看菜单|查看菜品|添加菜单|删除菜单)(?:\s+.*)?"
    r")\s*$"
)

help_cmd = on_fullmatch({HELP, MENU, "help"}, priority=10, block=True)
ping_cmd = on_fullmatch("ping", priority=10, block=True)
roll_cmd = on_regex(r"^roll(?:\s+\d+\s+\d+)?$", priority=10, block=True)
sign_cmd = on_fullmatch(CHECK_IN, priority=10, block=True)
feature_cmd = on_fullmatch(FEATURE, permission=SUPERUSER, priority=5, block=True)
feature_on_cmd = on_regex(r"^开启功能(?:\s+.+)?$", permission=SUPERUSER, priority=5, block=True)
feature_off_cmd = on_regex(r"^关闭功能(?:\s+.+)?$", permission=SUPERUSER, priority=5, block=True)
chat_on_cmd = on_regex(r"^开启闲聊(?:\s+\d+)?$", permission=SUPERUSER, priority=4, block=True)
chat_off_cmd = on_regex(r"^关闭闲聊(?:\s+\d+)?$", permission=SUPERUSER, priority=4, block=True)
welcome_notice = on_notice(priority=20, block=False)


def _is_superuser(event: GroupMessageEvent) -> bool:
    return str(event.user_id) in {str(user_id) for user_id in get_driver().config.superusers}


def _blocked_feature_for_text(text: str) -> str | None:
    if WAIFU_COMMAND_RE.match(text):
        return FAVOR_FEATURE
    if FAVOR_COMMAND_RE.match(text):
        return FAVOR_FEATURE
    if WHATEAT_COMMAND_RE.match(text):
        return WHATEAT_FEATURE
    return None


@event_preprocessor
async def _block_disabled_external_features(event: GroupMessageEvent) -> None:
    feature = _blocked_feature_for_text(event.get_plaintext().strip())
    if feature and not is_feature_enabled(event.group_id, feature):
        raise IgnoredException(f"{feature} disabled in group {event.group_id}")


def _pick_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("fonts") / ("MiSans-Bold.ttf" if bold else "MiSans-Regular.ttf"),
        Path(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _build_help_image(group_id: int, features: dict[str, bool]) -> bytes:
    commands = [
        ("帮助 / 菜单", "查看这张菜单"),
        ("ping", "测试机器人是否在线"),
        ("签到", "记录每日签到"),
        ("roll <min> <max>", "掷一个指定范围的骰子"),
        ("开启功能 / 关闭功能 <功能名>", "superuser 管理群功能"),
        ("开启闲聊 [群号] / 关闭闲聊 [群号]", "superuser 选择哪些群开启闲聊"),
        ("早安 / 晚安 / 睡眠统计", "记录和查看睡眠"),
        ("吃什么 / 喝什么 / 添加菜单 / 查看菜单", "随机菜单相关功能"),
        ("abbr bupt", "查询高校英文简称对应的中文校名"),
        ("报考 物理 4874 新工科优先", "小汀报考：按位次生成报考参考"),
        ("@杜若汀", "在已开启闲聊的群里聊天"),
        ("设置头衔 <内容> / 清除头衔", "群头衔工具"),
        ("今日老婆 / favor / 今日单词", "群友抽取、好感和学习功能"),
    ]

    width = 920
    header_h = 156
    row_h = 72
    gap = 22
    feature_h = 136
    height = header_h + len(commands) * row_h + gap + feature_h + 54

    image = Image.new("RGB", (width, height), "#f7f1e8")
    draw = ImageDraw.Draw(image)
    title_font = _pick_font(42, bold=True)
    subtitle_font = _pick_font(22)
    command_font = _pick_font(24, bold=True)
    desc_font = _pick_font(21)
    small_font = _pick_font(18)

    draw.rounded_rectangle((28, 28, width - 28, height - 28), radius=26, fill="#fffaf3", outline="#e6d7c2", width=2)
    draw.text((58, 50), "杜若汀菜单", fill="#33281f", font=title_font)
    draw.text((60, 104), f"群号 {group_id} · 功能状态随本群设置变化", fill="#7b6958", font=subtitle_font)

    y = header_h
    for index, (command, desc) in enumerate(commands, start=1):
        fill = "#fbf3e8" if index % 2 else "#fff7ee"
        draw.rounded_rectangle((54, y - 6, width - 54, y + row_h - 12), radius=12, fill=fill)
        draw.text((78, y + 6), f"{index:02d}", fill="#a2754b", font=small_font)
        draw.text((132, y - 1), command, fill="#44352a", font=command_font)
        draw.text((132, y + 31), desc, fill="#7a6a5d", font=desc_font)
        y += row_h

    y += gap
    draw.text((60, y), "当前群功能", fill="#33281f", font=command_font)
    pill_x = 60
    pill_y = y + 42
    for name, enabled in features.items():
        text = f"{name} {'开' if enabled else '关'}"
        bbox = draw.textbbox((0, 0), text, font=small_font)
        pill_w = bbox[2] - bbox[0] + 34
        if pill_x + pill_w > width - 60:
            pill_x = 60
            pill_y += 40
        color = "#d9efe0" if enabled else "#eadfda"
        text_color = "#2f6c43" if enabled else "#8a6255"
        draw.rounded_rectangle((pill_x, pill_y, pill_x + pill_w, pill_y + 30), radius=15, fill=color)
        draw.text((pill_x + 17, pill_y + 4), text, fill=text_color, font=small_font)
        pill_x += pill_w + 10

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@help_cmd.handle()
async def handle_help(event: GroupMessageEvent) -> None:
    if not is_feature_enabled(event.group_id, HELP):
        await help_cmd.finish("这个群已经关闭了帮助功能。")

    features = get_group_features(event.group_id)
    await help_cmd.finish(MessageSegment.image(_build_help_image(event.group_id, features)))


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
    if feature == CHAT_FEATURE and not _is_superuser(event):
        return "闲聊开关只能由 superuser 操作。"
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
async def handle_feature_on(event: GroupMessageEvent) -> None:
    feature = event.get_plaintext().strip().removeprefix(FEATURE_ON).strip()
    await feature_on_cmd.finish(await _toggle_feature(event, feature, True))


@feature_off_cmd.handle()
async def handle_feature_off(event: GroupMessageEvent) -> None:
    feature = event.get_plaintext().strip().removeprefix(FEATURE_OFF).strip()
    await feature_off_cmd.finish(await _toggle_feature(event, feature, False))


def _extract_target_group(event: GroupMessageEvent, prefix: str) -> int:
    raw = event.get_plaintext().strip().removeprefix(prefix).strip()
    return int(raw) if raw else int(event.group_id)


@chat_on_cmd.handle()
async def handle_chat_on(event: GroupMessageEvent) -> None:
    group_id = _extract_target_group(event, "开启闲聊")
    set_group_feature(group_id, CHAT_FEATURE, True)
    await chat_on_cmd.finish(f"已开启 {group_id} 的闲聊。")


@chat_off_cmd.handle()
async def handle_chat_off(event: GroupMessageEvent) -> None:
    group_id = _extract_target_group(event, "关闭闲聊")
    set_group_feature(group_id, CHAT_FEATURE, False)
    await chat_off_cmd.finish(f"已关闭 {group_id} 的闲聊。")


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
