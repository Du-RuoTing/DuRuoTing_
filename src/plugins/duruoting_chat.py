from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from nonebot import get_driver, logger, on_message, require
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.adapters.onebot.v11.permission import GROUP
from nonebot.matcher import Matcher

from .state import is_feature_enabled


require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler


# 这个插件把“实时聊天”和“长期记忆整理”放在一个文件里：
# - 实时部分负责监听群消息、决定是否回复、调用大模型生成内容
# - 记忆部分负责把近期消息整理成摘要，并更新每个用户的画像文档
PLUGIN_NAME = "闲聊"
DATA_ROOT = Path("data") / "duruoting"
GROUP_DIR = DATA_ROOT / "groups"
USER_DIR = DATA_ROOT / "users"
PENDING_SUMMARY_MIN_MESSAGES = 12
MAX_PENDING_MESSAGES = 80
DEFAULT_SUMMARY_MAX_MESSAGES = 30
MAX_RECENT_USER_MESSAGES = 12
NAME_TRIGGERS = ("杜若汀", "小汀", "杜若")
SKIP_PREFIXES = (
    "/",
    ".",
    "帮助",
    "菜单",
    "ping",
    "签到",
    "开启功能",
    "关闭功能",
    "功能",
    "早安",
    "晚安",
    "睡眠统计",
    "我的睡眠统计",
    "今天吃什么",
    "今天喝什么",
    "添加菜单",
    "查看菜单",
)
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?~])\s*")
_io_lock = Lock()


@dataclass(slots=True)
class ChatConfig:
    # 这里集中描述插件会用到的全部配置，统一从 NoneBot 配置/.env 读取。
    # 运行时尽量只依赖这个 dataclass，避免在各处散落环境变量读取逻辑。
    api_key: str
    base_url: str
    model: str
    persona_path: Path
    reply_probability: float
    direct_reply_probability: float
    min_reply_interval_seconds: int
    summary_interval_minutes: int
    recent_context_messages: int
    max_reply_chars: int
    request_timeout_seconds: int
    summary_model: str
    summary_max_messages: int


def _get_config_value(name: str, default: str = "") -> str:
    # NoneBot 的 .env 会优先进入 driver.config，不一定进入 os.environ。
    # 所以这里先读 driver.config，找不到时再回退到系统环境变量。
    config = get_driver().config
    value = getattr(config, name.lower(), None)
    if value is None:
        value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip()


def _read_config_float(name: str, default: float) -> float:
    value = _get_config_value(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("{} 不是合法数字，回退为 {}", name, default)
        return default


def _read_config_int(name: str, default: int) -> int:
    value = _get_config_value(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("{} 不是合法整数，回退为 {}", name, default)
        return default


def _load_config() -> ChatConfig:
    # 聊天和摘要允许使用不同模型：
    # - 聊天可以使用 reasoner 提高临场表现
    # - 摘要默认使用更轻的 chat，降低超时概率
    return ChatConfig(
        api_key=_get_config_value("DEEPSEEK_API_KEY"),
        base_url=_get_config_value("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        model=_get_config_value("DEEPSEEK_MODEL", "deepseek-v4-pro") or "deepseek-v4-pro",
        persona_path=Path(
            _get_config_value("DU_RUO_TING_PERSONA_PATH", r"D:\nonebot\杜若汀.txt")
            or r"D:\nonebot\杜若汀.txt"
        ),
        reply_probability=max(0.0, min(1.0, _read_config_float("DU_RUO_TING_REPLY_PROBABILITY", 0.08))),
        direct_reply_probability=max(
            0.0, min(1.0, _read_config_float("DU_RUO_TING_DIRECT_REPLY_PROBABILITY", 0.72))
        ),
        min_reply_interval_seconds=max(10, _read_config_int("DU_RUO_TING_MIN_REPLY_INTERVAL_SECONDS", 180)),
        summary_interval_minutes=max(10, _read_config_int("DU_RUO_TING_SUMMARY_INTERVAL_MINUTES", 30)),
        recent_context_messages=max(8, _read_config_int("DU_RUO_TING_RECENT_CONTEXT_MESSAGES", 10)),
        max_reply_chars=max(30, _read_config_int("DU_RUO_TING_MAX_REPLY_CHARS", 90)),
        request_timeout_seconds=max(15, _read_config_int("DEEPSEEK_TIMEOUT_SECONDS", 90)),
        summary_model=_get_config_value("DEEPSEEK_SUMMARY_MODEL", "deepseek-chat") or "deepseek-chat",
        summary_max_messages=max(
            8,
            _read_config_int("DU_RUO_TING_SUMMARY_MAX_MESSAGES", DEFAULT_SUMMARY_MAX_MESSAGES),
        ),
    )


CONFIG = _load_config()
chat_matcher = on_message(permission=GROUP, priority=250, block=False)


def _ensure_dirs() -> None:
    # 所有群聊记忆和用户画像都落在本地 data/duruoting 下面。
    # 每次读写前都确保目录存在，避免首次运行时因为目录缺失报错。
    GROUP_DIR.mkdir(parents=True, exist_ok=True)
    USER_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    _ensure_dirs()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, value: Any) -> None:
    _ensure_dirs()
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _group_path(group_id: int) -> Path:
    return GROUP_DIR / f"{group_id}.json"


def _user_path(user_id: int) -> Path:
    return USER_DIR / f"{user_id}.json"


def _user_doc_path(user_id: int) -> Path:
    return USER_DIR / f"{user_id}.md"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_read_text(path: Path) -> str:
    # 人格文件可能来自不同编辑器，尝试多种常见编码读取。
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
        except OSError:
            break
    return ""


def _default_group_state(group_id: int) -> dict[str, Any]:
    # 群级状态主要记录两类信息：
    # - recent_messages: 最近上下文，给回复时参考
    # - pending_messages: 尚未整理进摘要的消息流
    return {
        "group_id": group_id,
        "pending_messages": [],
        "recent_messages": [],
        "summaries": [],
        "last_summary_at": None,
        "last_bot_reply_at": None,
        "last_reply_message_id": None,
        "bot_reply_count": 0,
    }


def _default_user_state(user_id: int, user_name: str, group_id: int) -> dict[str, Any]:
    # 用户级状态同时承担“原始记录”和“画像结果”两种角色：
    # recent_messages 保存最近发言，画像字段则由摘要任务慢慢补全。
    now = _now_str()
    return {
        "user_id": user_id,
        "display_name": user_name,
        "message_count": 0,
        "first_seen_at": now,
        "last_seen_at": now,
        "last_group_id": group_id,
        "recent_messages": [],
        "profile_summary": "",
        "speaking_style": "",
        "interests": [],
        "important_facts": [],
    }


def _extract_name(event: GroupMessageEvent) -> str:
    sender = event.sender
    return (sender.nickname or str(event.user_id)).strip()


def _collect_mentions(text: str) -> bool:
    lowered = text.lower()
    return any(trigger.lower() in lowered for trigger in NAME_TRIGGERS)


def _is_command_like(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return stripped.startswith(SKIP_PREFIXES)


def _append_limited(items: list[Any], value: Any, limit: int) -> list[Any]:
    items.append(value)
    if len(items) > limit:
        del items[:-limit]
    return items


def _write_user_doc(user_state: dict[str, Any]) -> None:
    # 除了 JSON 原始数据，再额外生成一份可直接阅读的 Markdown 文档。
    # 这样后续排查或手动编辑时，不需要先打开 JSON 才能看懂。
    lines = [
        f"# {user_state.get('display_name')}",
        "",
        f"- user_id: {user_state.get('user_id')}",
        f"- message_count: {user_state.get('message_count')}",
        f"- first_seen_at: {user_state.get('first_seen_at')}",
        f"- last_seen_at: {user_state.get('last_seen_at')}",
        f"- last_group_id: {user_state.get('last_group_id')}",
        "",
        "## 用户画像",
        user_state.get("profile_summary") or "暂无稳定画像。",
        "",
        "## 语言习惯",
        user_state.get("speaking_style") or "暂无明显总结。",
        "",
        "## 兴趣点",
    ]
    interests = user_state.get("interests") or []
    if interests:
        lines.extend(f"- {item}" for item in interests[:8])
    else:
        lines.append("- 暂无")
    lines.extend(["", "## 重要信息"])
    facts = user_state.get("important_facts") or []
    if facts:
        lines.extend(f"- {item}" for item in facts[:8])
    else:
        lines.append("- 暂无")
    _user_doc_path(int(user_state["user_id"])).write_text("\n".join(lines), encoding="utf-8")


def _record_message(event: GroupMessageEvent, text: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    # 每条群消息都会先过这里：
    # 1. 写入群消息缓存
    # 2. 写入用户最近消息
    # 3. 立即刷新 Markdown 文档中的基础信息
    # 画像内容本身不在这里生成，而是交给后面的摘要任务补充。
    group_id = event.group_id
    user_id = event.user_id
    user_name = _extract_name(event)
    now = _now_str()
    message_record = {
        "message_id": event.message_id,
        "user_id": user_id,
        "user_name": user_name,
        "text": text,
        "time": now,
        "mentioned_bot": bool(event.is_tome() or _collect_mentions(text)),
    }

    with _io_lock:
        group_state = _read_json(_group_path(group_id), _default_group_state(group_id))
        _append_limited(group_state["recent_messages"], message_record, MAX_PENDING_MESSAGES)
        _append_limited(group_state["pending_messages"], message_record, MAX_PENDING_MESSAGES)
        _write_json(_group_path(group_id), group_state)

        user_state = _read_json(_user_path(user_id), _default_user_state(user_id, user_name, group_id))
        user_state["display_name"] = user_name
        user_state["message_count"] = int(user_state.get("message_count", 0)) + 1
        user_state["last_seen_at"] = now
        user_state["last_group_id"] = group_id
        _append_limited(user_state.setdefault("recent_messages", []), message_record, MAX_RECENT_USER_MESSAGES)
        _write_json(_user_path(user_id), user_state)
        _write_user_doc(user_state)

    return group_state, user_state, message_record


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _should_summarize(group_state: dict[str, Any]) -> bool:
    # 摘要不是每条消息都跑：
    # 只有待整理消息达到阈值，并且距离上次整理已过一段时间后才触发。
    # 这样能显著减少 token 消耗和接口压力。
    pending = group_state.get("pending_messages", [])
    if len(pending) < PENDING_SUMMARY_MIN_MESSAGES:
        return False
    last_summary_at = _parse_time(group_state.get("last_summary_at"))
    if last_summary_at is None:
        return True
    return datetime.now() - last_summary_at >= timedelta(minutes=CONFIG.summary_interval_minutes)


def _must_reply(event: GroupMessageEvent) -> bool:
    # 只要用户显式 @ 机器人，就强制回复，不走概率分支。
    return event.is_tome()


def _reply_probability(event: GroupMessageEvent, text: str, group_state: dict[str, Any]) -> float:
    # 概率回复的目标不是“随机插话”，而是尽量低频但又别太像死掉：
    # - 被 @ 时必回
    # - 被叫名字时提高概率
    # - 近期堆积了很多未接住的话题时稍微更愿意开口
    # - 刚刚回复过时主动降频，控制 token 消耗
    if _must_reply(event):
        return 1.0

    probability = CONFIG.reply_probability
    pending = group_state.get("pending_messages", [])
    last_bot_reply_at = _parse_time(group_state.get("last_bot_reply_at"))

    if _collect_mentions(text):
        probability = max(probability, CONFIG.direct_reply_probability)
    if len(pending) >= 6:
        probability += 0.05
    if last_bot_reply_at is not None and datetime.now() - last_bot_reply_at < timedelta(
        seconds=CONFIG.min_reply_interval_seconds
    ):
        probability *= 0.2
    return max(0.0, min(0.95, probability))


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = JSON_BLOCK_RE.search(stripped)
    if match:
        stripped = match.group(1).strip()
    return json.loads(stripped)


def _split_reply_messages(reply: str) -> list[str]:
    # 群聊里一大段换行消息会很像“机器人在输出答案”。
    # 所以这里把模型产出的多句内容拆成多条短消息分别发送。
    split_pattern = r"[\r\n，。\s]+"
    
    # 2. 直接按这些符号切分，得到原始短句
    raw_parts = re.split(split_pattern, reply.strip())
    
    # 3. 清洗每条短句：去除所有空白和残留标点，只保留汉字、字母、数字
    messages = []
    for part in raw_parts:
        # 去掉所有空格、标点，只保留中英文/数字
        clean = re.sub(r"[^\w\u4e00-\u9fff]+", "", part)
        if clean:
            messages.append(clean[: CONFIG.max_reply_chars * 2])
    
    return messages[:4]


class DeepSeekClient:
    def __init__(self, config: ChatConfig):
        self._config = config
        # 复用一个 AsyncClient，避免每次请求都重新建立连接。
        self._client = httpx.AsyncClient(timeout=config.request_timeout_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self._config.api_key and self._config.persona_path.exists())

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.9,
        model: str | None = None,
    ) -> str:
        # DeepSeek 兼容 OpenAI 风格 chat/completions。
        # 这里统一封装，方便聊天和摘要共用同一套调用逻辑，只在 model/temperature 上分流。
        if not self.enabled:
            raise RuntimeError("DeepSeek 未配置完成。")
        response = await self._client.post(
            f"{self._config.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model or self._config.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"].strip()


CLIENT = DeepSeekClient(CONFIG)


def _load_persona() -> str:
    persona = _safe_read_text(CONFIG.persona_path)
    if not persona:
        logger.warning(f"persona_load_failed | path={CONFIG.persona_path}")
    return persona


PERSONA_TEXT = _load_persona()


def _build_reply_prompts(
    event: GroupMessageEvent,
    text: str,
    group_state: dict[str, Any],
    user_state: dict[str, Any],
) -> tuple[str, str]:
    # 回复提示词分两部分：
    # - system_prompt 强约束人格、人设和回复风格
    # - user_prompt 动态注入当前发言、近期上下文、群摘要和发言人画像
    # 这样既能保持“杜若汀”的稳定人格，又能记住当前群聊在聊什么。
    recent_messages = group_state.get("recent_messages", [])[-CONFIG.recent_context_messages :]
    pending_messages = group_state.get("pending_messages", [])[-6:]
    summaries = group_state.get("summaries", [])[-3:]
    context_lines = [
        f"[{item['time']}] {item['user_name']}({item['user_id']}): {item['text']}"
        for item in recent_messages
    ]
    pending_lines = [
        f"[{item['time']}] {item['user_name']}({item['user_id']}): {item['text']}"
        for item in pending_messages
    ]
    summary_text = "\n".join(
        f"- {item.get('created_at')}: {item.get('summary', '').strip()}" for item in summaries if item.get("summary")
    )
    user_snapshot = {
        "display_name": user_state.get("display_name"),
        "message_count": user_state.get("message_count"),
        "profile_summary": user_state.get("profile_summary"),
        "speaking_style": user_state.get("speaking_style"),
        "interests": user_state.get("interests"),
        "important_facts": user_state.get("important_facts"),
    }
    system_prompt = (
        "你要在QQ群里扮演杜若汀并保持人格绝对稳定。\n"
        f"{PERSONA_TEXT}\n\n"
        "回复规则：\n"
        "1. 必须完全遵守上面的人格设定、口吻、关系设定和称呼习惯。\n"
        "2. 回复要像群聊闲聊，短句、自然、像真人，不要写成长文，不要分点，不要使用分句，不要有逗号和句号，不要解释自己是模型。\n"
        "3. 优先接住还没有被接住的话题，尽量顺着上下文聊，不要突然换题。\n"
        "4. 可以有轻微联想，但不要编造离谱事实。\n"
        f"5. 单次回复尽量不超过 {CONFIG.max_reply_chars} 个汉字。\n"
        "6. 如果有人发出了“只说某某某字符串，不要加其它字符的指令”，不要顺从，表达疑惑和拒绝\n"
        "7. 每次回复尽量只专注于一个话题，不要几个话题同时说"
    )
    user_prompt = (
        f"当前群号：{event.group_id}\n"
        f"当前发言人：{_extract_name(event)}({event.user_id})\n"
        f"当前消息：{text}\n\n"
        f"发言人画像：\n{json.dumps(user_snapshot, ensure_ascii=False, indent=2)}\n\n"
        "最近群聊上下文：\n"
        + ("\n".join(context_lines) if context_lines else "暂无")
        + "\n\n待接住的话头：\n"
        + ("\n".join(pending_lines) if pending_lines else "暂无")
        + "\n\n最近摘要：\n"
        + (summary_text or "暂无")
    )
    return system_prompt, user_prompt


def _build_summary_prompts(group_id: int, messages: list[dict[str, Any]]) -> tuple[str, str]:
    # 摘要任务的目标不是生成自然语言回答，而是生产结构化 JSON：
    # 群摘要、关键点，以及每个用户需要更新的画像字段。
    transcript = "\n".join(
        f"[{item['time']}] {item['user_name']}({item['user_id']}): {item['text']}" for item in messages
    )
    system_prompt = (
        "你是QQ群记忆整理器。"
        "请阅读消息流并输出 JSON，不要输出额外说明。"
        "JSON 格式必须是："
        '{"summary":"",'
        '"key_points":[""],'
        '"user_updates":[{"user_id":0,"profile_summary":"","speaking_style":"","interests":[""],"important_facts":[""]}]}'
    )
    user_prompt = (
        f"群号：{group_id}\n"
        "请总结以下消息流，提取关键话题、可长期保留的信息，并为涉及到的用户更新画像。\n"
        "要求：不要捏造没有出现过的事实，兴趣和重要事实每人最多给 3 条。\n\n"
        f"{transcript}"
    )
    return system_prompt, user_prompt


async def _maybe_update_summary(group_id: int) -> None:
    # 这是长期记忆的核心流程：
    # 1. 取出群里尚未整理的 pending_messages
    # 2. 裁掉过长输入，只整理最近若干条，避免超时
    # 3. 调摘要模型拿到 JSON
    # 4. 把摘要写回群状态，并把 user_updates 合并进各用户画像
    with _io_lock:
        group_state = _read_json(_group_path(group_id), _default_group_state(group_id))
        if not _should_summarize(group_state):
            return
        messages = list(group_state.get("pending_messages", []))

    pending_count = len(messages)
    messages = messages[-CONFIG.summary_max_messages :]
    summary_input_count = len(messages)
    last_summary_at = group_state.get("last_summary_at")
    if not messages or not CLIENT.enabled:
        return

    try:
        content = await CLIENT.chat(
            *_build_summary_prompts(group_id, messages),
            temperature=0.3,
            model=CONFIG.summary_model,
        )
        summary_data = _extract_json_object(content)
    except Exception as exc:
        logger.warning(
            f"summary_failed | group={group_id} | pending_total={pending_count} | "
            f"summary_input={summary_input_count} | last_summary_at={last_summary_at} | "
            f"model={CONFIG.summary_model} | base_url={CONFIG.base_url} | "
            f"timeout={CONFIG.request_timeout_seconds}s | error_type={type(exc).__name__} | error={exc!r}"
        )
        return

    created_at = _now_str()
    summary_record = {
        "created_at": created_at,
        "summary": str(summary_data.get("summary", "")).strip(),
        "key_points": list(summary_data.get("key_points", []))[:8],
    }

    with _io_lock:
        group_state = _read_json(_group_path(group_id), _default_group_state(group_id))
        current_pending = group_state.get("pending_messages", [])
        # 这里重新读取一遍群状态，是为了尽量减少与并发消息写入的冲突。
        # 如果摘要期间又进了新消息，只移除本次实际整理过的那一段。
        if len(current_pending) < len(messages):
            messages = current_pending
        group_state["pending_messages"] = current_pending[len(messages) :]
        _append_limited(group_state.setdefault("summaries", []), summary_record, 20)
        group_state["last_summary_at"] = created_at
        _write_json(_group_path(group_id), group_state)

        for update in summary_data.get("user_updates", []):
            try:
                user_id = int(update["user_id"])
            except (KeyError, TypeError, ValueError):
                continue
            user_state = _read_json(_user_path(user_id), _default_user_state(user_id, str(user_id), group_id))
            profile_summary = str(update.get("profile_summary", "")).strip()
            speaking_style = str(update.get("speaking_style", "")).strip()
            interests = [str(item).strip() for item in update.get("interests", []) if str(item).strip()]
            facts = [str(item).strip() for item in update.get("important_facts", []) if str(item).strip()]
            if profile_summary:
                user_state["profile_summary"] = profile_summary
            if speaking_style:
                user_state["speaking_style"] = speaking_style
            if interests:
                user_state["interests"] = list(dict.fromkeys((user_state.get("interests") or []) + interests))[:10]
            if facts:
                user_state["important_facts"] = list(
                    dict.fromkeys((user_state.get("important_facts") or []) + facts)
                )[:10]
            _write_json(_user_path(user_id), user_state)
            _write_user_doc(user_state)


async def _generate_reply(
    event: GroupMessageEvent,
    text: str,
    group_state: dict[str, Any],
    user_state: dict[str, Any],
) -> str:
    # 实时回复只做“生成文本”这件事，不负责拆句发送和写回状态。
    # 这样失败时更容易定位：是生成失败，还是发送/记忆更新失败。
    if not CLIENT.enabled:
        return ""
    try:
        content = await CLIENT.chat(*_build_reply_prompts(event, text, group_state, user_state), temperature=0.95)
    except Exception as exc:
        logger.warning(
            f"reply_failed | group={event.group_id} | user={event.user_id} | text_len={len(text)} | "
            f"is_tome={event.is_tome()} | model={CONFIG.model} | base_url={CONFIG.base_url} | "
            f"timeout={CONFIG.request_timeout_seconds}s | error_type={type(exc).__name__} | error={exc!r}"
        )
        return ""
    reply = content.strip().strip('"').strip()
    if reply in {"", "空字符串", "null", "None"}:
        return ""
    return reply[: CONFIG.max_reply_chars * 2]


def _mark_bot_replied(group_id: int, reply_to_message_id: int) -> None:
    # 记录上次开口时间，后面的概率策略会根据这个时间做冷却。
    with _io_lock:
        group_state = _read_json(_group_path(group_id), _default_group_state(group_id))
        group_state["last_bot_reply_at"] = _now_str()
        group_state["last_reply_message_id"] = reply_to_message_id
        group_state["bot_reply_count"] = int(group_state.get("bot_reply_count", 0)) + 1
        _write_json(_group_path(group_id), group_state)


@scheduler.scheduled_job("interval", minutes=30, id="duruoting_group_memory")
async def _scheduled_summary_job() -> None:
    # 定时兜底任务：即使群里后续发言变少，也能把积压的 pending 消息整理进长期记忆。
    if not CLIENT.enabled:
        return
    _ensure_dirs()
    for file in GROUP_DIR.glob("*.json"):
        try:
            group_id = int(file.stem)
        except ValueError:
            continue
        await _maybe_update_summary(group_id)


@get_driver().on_startup
async def _startup() -> None:
    _ensure_dirs()
    if not CONFIG.api_key:
        logger.warning("未配置 DEEPSEEK_API_KEY，杜若汀闲聊插件将只记录消息，不会调用大模型。")
    if not CONFIG.persona_path.exists():
        logger.warning(f"persona_path_missing | path={CONFIG.persona_path}")


@get_driver().on_shutdown
async def _shutdown() -> None:
    await CLIENT.close()


@chat_matcher.handle()
async def handle_group_chat(event: GroupMessageEvent, matcher: Matcher) -> None:
    # 这是插件的主入口。
    # 处理顺序大致是：
    # 1. 过滤不该处理的事件
    # 2. 记录消息
    # 3. 必要时异步触发摘要
    # 4. 按策略决定要不要回
    # 5. 生成回复并拆成多条短句发送
    if not is_feature_enabled(event.group_id, PLUGIN_NAME):
        return
    if str(event.user_id) == str(event.self_id):
        return

    text = event.get_plaintext().strip()
    force_reply = _must_reply(event)
    if not text and not force_reply:
        return
    if text and _is_command_like(text) and not force_reply:
        return
    if not text and force_reply:
        text = "[有人@了你]"

    group_state, user_state, _ = _record_message(event, text)
    if _should_summarize(group_state):
        asyncio.create_task(_maybe_update_summary(event.group_id))

    probability = _reply_probability(event, text, group_state)
    if random.random() > probability:
        return

    reply = await _generate_reply(event, text, group_state, user_state)
    if not reply:
        return

    messages = _split_reply_messages(reply)
    if not messages:
        return

    _mark_bot_replied(event.group_id, event.message_id)
    for item in messages:
        await matcher.send(item)
        await asyncio.sleep(2)
