from __future__ import annotations

import re

from nonebot import on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from .state import is_feature_enabled


TITLE_FEATURE = "头衔"
TITLE_MAX_LEN = 18

set_title_cmd = on_regex(r"^(?:设置头衔|群头衔|我的头衔)\s*(.*)$", priority=8, block=True)
clear_title_cmd = on_regex(r"^(?:清除头衔|删除头衔|取消头衔)$", priority=8, block=True)


def _clean_title(raw: str) -> str:
    title = raw.strip()
    title = re.sub(r"[\r\n\t]+", " ", title)
    return re.sub(r"\s{2,}", " ", title)


def _extract_title(text: str) -> str:
    for prefix in ("设置头衔", "群头衔", "我的头衔"):
        if text.startswith(prefix):
            return _clean_title(text.removeprefix(prefix))
    return ""


async def _ensure_owner_bot(bot: Bot, event: GroupMessageEvent) -> bool:
    try:
        info = await bot.get_group_member_info(
            group_id=event.group_id,
            user_id=int(bot.self_id),
            no_cache=True,
        )
    except Exception:
        return False
    return info.get("role") == "owner"


async def _set_group_title(bot: Bot, event: GroupMessageEvent, title: str) -> None:
    await bot.call_api(
        "set_group_special_title",
        group_id=event.group_id,
        user_id=event.user_id,
        special_title=title,
        duration=-1,
    )


@set_title_cmd.handle()
async def handle_set_title(bot: Bot, event: GroupMessageEvent) -> None:
    if not is_feature_enabled(event.group_id, TITLE_FEATURE):
        await set_title_cmd.finish("这个群已经关闭了头衔功能。")

    title = _extract_title(event.get_plaintext())
    if not title:
        await set_title_cmd.finish("用法：设置头衔 你的头衔")
    if len(title) > TITLE_MAX_LEN:
        await set_title_cmd.finish(f"头衔最长 {TITLE_MAX_LEN} 个字，稍微短一点点。")

    if not await _ensure_owner_bot(bot, event):
        await set_title_cmd.finish("设置群头衔需要当前登录号是群主。现在这个账号不是群主，所以发包会被 QQ 拒绝。")

    try:
        await _set_group_title(bot, event, title)
    except Exception as exc:
        await set_title_cmd.finish(f"设置失败：{type(exc).__name__}: {exc}")

    await set_title_cmd.finish(f"已把你的群头衔设置为：{title}")


@clear_title_cmd.handle()
async def handle_clear_title(bot: Bot, event: GroupMessageEvent) -> None:
    if not is_feature_enabled(event.group_id, TITLE_FEATURE):
        await clear_title_cmd.finish("这个群已经关闭了头衔功能。")

    if not await _ensure_owner_bot(bot, event):
        await clear_title_cmd.finish("清除群头衔需要当前登录号是群主。现在这个账号不是群主，所以发包会被 QQ 拒绝。")

    try:
        await _set_group_title(bot, event, "")
    except Exception as exc:
        await clear_title_cmd.finish(f"清除失败：{type(exc).__name__}: {exc}")

    await clear_title_cmd.finish("已清除你的群头衔。")
