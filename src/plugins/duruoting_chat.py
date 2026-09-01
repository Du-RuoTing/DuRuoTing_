from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from nonebot import get_driver, logger, on_message, on_regex, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.permission import GROUP, PRIVATE
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER

from .state import is_feature_enabled


require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler


# 这个插件把“实时聊天”和“长期记忆整理”放在一个文件里：
# - 实时部分负责监听群消息、决定是否回复、调用大模型生成内容
# - 记忆部分负责把近期消息整理成摘要，并更新每个用户的画像文档
PLUGIN_NAME = "闲聊"
DATA_ROOT = Path("data") / "duruoting"
GROUP_DIR = DATA_ROOT / "groups"
PRIVATE_DIR = DATA_ROOT / "private"
USER_DIR = DATA_ROOT / "users"
BOT_LOG_DIR = DATA_ROOT / "bot_logs"
TTS_SETTINGS_PATH = DATA_ROOT / "tts_settings.json"
PENDING_SUMMARY_MIN_MESSAGES = 12
MAX_PENDING_MESSAGES = 80
DEFAULT_SUMMARY_MAX_MESSAGES = 24
DEFAULT_SUMMARY_FAILURE_COOLDOWN_SECONDS = 300
MAX_RECENT_USER_MESSAGES = 24
MAX_RECENT_BOT_MESSAGES = 24
DEFAULT_BOT_NAME = "杜若汀"
DEFAULT_PERSONA_DIR = Path(r"D:\nonebot")
DEFAULT_EXTRA_NAME_TRIGGERS = ("小汀", "杜若")
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
_summary_locks: dict[int, asyncio.Lock] = {}


LLM_SERVICES: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key_name": "DEEPSEEK_API_KEY",
        "default_reply_model": "deepseek-v4-pro",
        "default_summary_model": "deepseek-chat",
    },
    "packy": {
        "base_url": "https://www.packyapi.com/v1",
        "api_key_name": "PACKY_API_KEY",
        "default_reply_model": "gpt-5.2",
        "default_summary_model": "gpt-5.2",
    },
    "huanyan": {
        "base_url": "https://api.huanyan.ltd/v1",
        "api_key_name": "HUANYAN_API_KEY",
        "default_reply_model": "gpt-5.5",
        "default_summary_model": "gpt-5.5",
    }
}


@dataclass(slots=True)
class ChatConfig:
    # 这里集中描述插件会用到的全部配置，统一从 NoneBot 配置/.env 读取。
    # 运行时尽量只依赖这个 dataclass，避免在各处散落环境变量读取逻辑。
    provider: str
    api_key: str
    base_url: str
    model: str
    reply_fallback_provider: str
    reply_fallback_api_key: str
    reply_fallback_base_url: str
    reply_fallback_model: str
    summary_provider: str
    summary_api_key: str
    summary_base_url: str
    summary_fallback_provider: str
    summary_fallback_api_key: str
    summary_fallback_base_url: str
    summary_fallback_model: str
    bot_name: str
    persona_path: Path
    private_persona_path: Path
    group_persona_paths: dict[int, Path]
    reply_probability: float
    direct_reply_probability: float
    min_reply_interval_seconds: int
    summary_interval_minutes: int
    recent_context_messages: int
    max_reply_chars: int
    request_timeout_seconds: int
    summary_model: str
    summary_max_messages: int
    summary_failure_cooldown_seconds: int
    name_triggers: tuple[str, ...]
    tts_enabled: bool
    tts_base_url: str
    tts_reference_wav: Path
    tts_prompt_text_path: Path
    tts_timeout_seconds: int
    tts_text_fallback: bool


class LLMResponseError(RuntimeError):
    def __init__(self, message: str, *, detail: dict[str, Any]):
        super().__init__(message)
        self.detail = detail

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.args[0]!r}, detail={self.detail!r})"


def _read_service_name(name: str, default: str) -> str:
    service = (_get_config_value(name, default) or default).lower().strip()
    if service not in LLM_SERVICES:
        logger.warning(
            "{}={} 不存在，已回退到 {}。可选值：{}",
            name,
            service,
            default,
            ", ".join(LLM_SERVICES),
        )
        return default
    return service


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


def _read_config_bool(name: str, default: bool) -> bool:
    value = _get_config_value(name)
    if not value:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logger.warning("{} 不是合法布尔值，回退为 {}", name, default)
    return default


def _read_group_persona_paths() -> dict[int, Path]:
    raw = _get_config_value("DU_RUO_TING_GROUP_PERSONA_PATHS", "").strip()
    if not raw:
        return {}

    pairs: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("DU_RUO_TING_GROUP_PERSONA_PATHS is not valid JSON, ignored.")
            return {}
        if isinstance(payload, dict):
            pairs = {str(group_id): str(path) for group_id, path in payload.items()}
    else:
        for item in re.split(r"[;\n]+", raw):
            if not item.strip() or "=" not in item:
                continue
            group_id, path = item.split("=", 1)
            pairs[group_id.strip()] = path.strip()

    result: dict[int, Path] = {}
    for group_id, path in pairs.items():
        try:
            result[int(group_id)] = Path(path)
        except ValueError:
            logger.warning(f"group_persona_invalid_group_id | group={group_id} | path={path}")
    return result


def _load_config() -> ChatConfig:
    provider = _read_service_name("DU_RUO_TING_REPLY_SERVICE", "packy")
    summary_provider = _read_service_name("DU_RUO_TING_SUMMARY_SERVICE", provider)
    reply_fallback_provider = _read_service_name("DU_RUO_TING_REPLY_FALLBACK_SERVICE", provider)
    summary_fallback_provider = _read_service_name("DU_RUO_TING_SUMMARY_FALLBACK_SERVICE", summary_provider)
    reply_service = LLM_SERVICES[provider]
    summary_service = LLM_SERVICES[summary_provider]
    reply_fallback_service = LLM_SERVICES[reply_fallback_provider]
    summary_fallback_service = LLM_SERVICES[summary_fallback_provider]

    api_key = _get_config_value(reply_service["api_key_name"])
    summary_api_key = _get_config_value(summary_service["api_key_name"])
    reply_fallback_api_key = _get_config_value(reply_fallback_service["api_key_name"])
    summary_fallback_api_key = _get_config_value(summary_fallback_service["api_key_name"])
    base_url = reply_service["base_url"]
    summary_base_url = summary_service["base_url"]
    reply_fallback_base_url = reply_fallback_service["base_url"]
    summary_fallback_base_url = summary_fallback_service["base_url"]
    model = _get_config_value("DU_RUO_TING_REPLY_MODEL", reply_service["default_reply_model"])
    summary_model = _get_config_value("DU_RUO_TING_SUMMARY_MODEL", summary_service["default_summary_model"])
    reply_fallback_model = _get_config_value(
        "DU_RUO_TING_REPLY_FALLBACK_MODEL",
        reply_fallback_service["default_reply_model"],
    )
    summary_fallback_model = _get_config_value(
        "DU_RUO_TING_SUMMARY_FALLBACK_MODEL",
        summary_fallback_service["default_summary_model"],
    )
    bot_name = _get_config_value("DU_RUO_TING_BOT_NAME", DEFAULT_BOT_NAME) or DEFAULT_BOT_NAME
    default_persona_path = DEFAULT_PERSONA_DIR / f"{bot_name}.txt"
    extra_triggers_raw = _get_config_value("DU_RUO_TING_NAME_TRIGGERS", ",".join(DEFAULT_EXTRA_NAME_TRIGGERS))
    extra_triggers = tuple(item.strip() for item in re.split(r"[,，]", extra_triggers_raw) if item.strip())

    return ChatConfig(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        reply_fallback_provider=reply_fallback_provider,
        reply_fallback_api_key=reply_fallback_api_key,
        reply_fallback_base_url=reply_fallback_base_url,
        reply_fallback_model=reply_fallback_model,
        summary_provider=summary_provider,
        summary_api_key=summary_api_key,
        summary_base_url=summary_base_url,
        summary_fallback_provider=summary_fallback_provider,
        summary_fallback_api_key=summary_fallback_api_key,
        summary_fallback_base_url=summary_fallback_base_url,
        summary_fallback_model=summary_fallback_model,
        bot_name=bot_name,
        persona_path=Path(
            _get_config_value("DU_RUO_TING_PERSONA_PATH", str(default_persona_path))
            or str(default_persona_path)
        ),
        private_persona_path=Path(
            _get_config_value("DU_RUO_TING_PRIVATE_PERSONA_PATH", str(default_persona_path))
            or str(default_persona_path)
        ),
        group_persona_paths=_read_group_persona_paths(),
        reply_probability=max(0.0, min(1.0, _read_config_float("DU_RUO_TING_REPLY_PROBABILITY", 0.08))),
        direct_reply_probability=max(
            0.0, min(1.0, _read_config_float("DU_RUO_TING_DIRECT_REPLY_PROBABILITY", 0.72))
        ),
        min_reply_interval_seconds=max(10, _read_config_int("DU_RUO_TING_MIN_REPLY_INTERVAL_SECONDS", 180)),
        summary_interval_minutes=max(5, _read_config_int("DU_RUO_TING_SUMMARY_INTERVAL_MINUTES", 10)),
        recent_context_messages=max(8, _read_config_int("DU_RUO_TING_RECENT_CONTEXT_MESSAGES", 10)),
        max_reply_chars=max(30, _read_config_int("DU_RUO_TING_MAX_REPLY_CHARS", 90)),
        request_timeout_seconds=max(15, _read_config_int("DU_RUO_TING_REQUEST_TIMEOUT_SECONDS", 90)),
        summary_model=summary_model,
        summary_max_messages=max(
            8,
            _read_config_int("DU_RUO_TING_SUMMARY_MAX_MESSAGES", DEFAULT_SUMMARY_MAX_MESSAGES),
        ),
        summary_failure_cooldown_seconds=max(
            60,
            _read_config_int(
                "DU_RUO_TING_SUMMARY_FAILURE_COOLDOWN_SECONDS",
                DEFAULT_SUMMARY_FAILURE_COOLDOWN_SECONDS,
            ),
        ),
        name_triggers=(bot_name, *extra_triggers),
        tts_enabled=_read_config_bool("DU_RUO_TING_TTS_ENABLED", True),
        tts_base_url=_get_config_value("DU_RUO_TING_TTS_BASE_URL", "http://127.0.0.1:50000").rstrip("/"),
        tts_reference_wav=Path(
            _get_config_value(
                "DU_RUO_TING_TTS_REFERENCE_WAV",
                str(Path("voices") / "duruoting" / "reference.wav"),
            )
        ),
        tts_prompt_text_path=Path(
            _get_config_value(
                "DU_RUO_TING_TTS_PROMPT_TEXT_PATH",
                str(Path("voices") / "duruoting" / "prompt.txt"),
            )
        ),
        tts_timeout_seconds=max(15, _read_config_int("DU_RUO_TING_TTS_TIMEOUT_SECONDS", 120)),
        tts_text_fallback=_read_config_bool("DU_RUO_TING_TTS_TEXT_FALLBACK", True),
    )


CONFIG = _load_config()
chat_matcher = on_message(permission=GROUP, priority=250, block=False)
private_chat_matcher = on_message(permission=PRIVATE, priority=250, block=False)
global_voice_cmd = on_regex(r"(?i)^tts\s+global\s+voice$", permission=SUPERUSER, priority=4, block=True)
global_text_cmd = on_regex(r"(?i)^tts\s+global\s+text$", permission=SUPERUSER, priority=4, block=True)
session_voice_cmd = on_regex(r"(?i)^tts\s+here\s+voice$", permission=SUPERUSER, priority=4, block=True)
session_text_cmd = on_regex(r"(?i)^tts\s+here\s+text$", permission=SUPERUSER, priority=4, block=True)


def _ensure_dirs() -> None:
    # 所有群聊记忆和用户画像都落在本地 data/duruoting 下面。
    GROUP_DIR.mkdir(parents=True, exist_ok=True)
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    USER_DIR.mkdir(parents=True, exist_ok=True)
    BOT_LOG_DIR.mkdir(parents=True, exist_ok=True)


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


def _default_tts_settings() -> dict[str, Any]:
    return {
        "global_mode": "voice" if CONFIG.tts_enabled else "text",
        "conversations": {},
    }


def _read_tts_settings() -> dict[str, Any]:
    settings = _read_json(TTS_SETTINGS_PATH, _default_tts_settings())
    if not isinstance(settings, dict):
        return _default_tts_settings()
    global_mode = settings.get("global_mode")
    if global_mode not in {"voice", "text"}:
        global_mode = "voice" if CONFIG.tts_enabled else "text"
    raw_conversations = settings.get("conversations", {})
    conversations = (
        {str(key): value for key, value in raw_conversations.items() if value in {"voice", "text"}}
        if isinstance(raw_conversations, dict)
        else {}
    )
    return {"global_mode": global_mode, "conversations": conversations}


def _conversation_key(scope: str, target_id: int) -> str:
    return f"{scope}:{int(target_id)}"


def _set_global_tts_mode(mode: str) -> None:
    with _io_lock:
        _write_json(TTS_SETTINGS_PATH, {"global_mode": mode, "conversations": {}})


def _set_conversation_tts_mode(scope: str, target_id: int, mode: str) -> None:
    with _io_lock:
        settings = _read_tts_settings()
        settings["conversations"][_conversation_key(scope, target_id)] = mode
        _write_json(TTS_SETTINGS_PATH, settings)


def _tts_mode_for(scope: str, target_id: int) -> str:
    with _io_lock:
        settings = _read_tts_settings()
    return settings["conversations"].get(
        _conversation_key(scope, target_id),
        settings["global_mode"],
    )


def _event_conversation(event: GroupMessageEvent | PrivateMessageEvent) -> tuple[str, int, str]:
    if isinstance(event, GroupMessageEvent):
        return "group", int(event.group_id), f"群 {event.group_id}"
    return "private", int(event.user_id), f"私聊 {event.user_id}"


def _group_path(group_id: int) -> Path:
    return GROUP_DIR / f"{group_id}.json"


def _private_path(user_id: int) -> Path:
    return PRIVATE_DIR / f"{user_id}.json"


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
        "bot_messages": [],
        "summaries": [],
        "last_summary_at": None,
        "last_summary_failed_at": None,
        "last_summary_error": None,
        "last_bot_reply_at": None,
        "last_reply_message_id": None,
        "bot_reply_count": 0,
    }


def _default_private_state(user_id: int, user_name: str) -> dict[str, Any]:
    now = _now_str()
    return {
        "user_id": user_id,
        "display_name": user_name,
        "recent_messages": [],
        "bot_messages": [],
        "first_seen_at": now,
        "last_seen_at": now,
        "message_count": 0,
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


def _extract_private_name(event: PrivateMessageEvent) -> str:
    sender = event.sender
    return (sender.nickname or str(event.user_id)).strip()


def _name_triggers() -> tuple[str, ...]:
    return CONFIG.name_triggers


def _collect_mentions(text: str) -> bool:
    lowered = text.lower()
    return any(trigger.lower() in lowered for trigger in _name_triggers())


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


def _normalize_memory_item(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[\s，。！？、；：,.!?;:「」“”\"'（）()【】\[\]<>《》]+", "", value)
    return value


def _merge_unique_texts(old_items: list[Any], new_items: list[Any], limit: int = 10) -> list[str]:
    merged: list[str] = []
    seen: list[str] = []
    for raw in [*old_items, *new_items]:
        item = str(raw).strip()
        if not item:
            continue
        key = _normalize_memory_item(item)
        if not key:
            continue
        if any(key == old_key or key in old_key or old_key in key for old_key in seen):
            continue
        merged.append(item)
        seen.append(key)
    return merged[-limit:]


def _merge_profile_text(old_text: str, new_text: str, max_chars: int = 180) -> str:
    old_text = old_text.strip()
    new_text = new_text.strip()
    if not new_text:
        return old_text
    if not old_text:
        return new_text[:max_chars]

    old_key = _normalize_memory_item(old_text)
    new_key = _normalize_memory_item(new_text)
    if new_key in old_key:
        return old_text[:max_chars]
    if old_key in new_key:
        return new_text[:max_chars]
    return f"{old_text}；{new_text}"[:max_chars]


def _bot_log_path(group_id: int) -> Path:
    return BOT_LOG_DIR / f"{group_id}.jsonl"


def _record_bot_reply(
    group_id: int,
    reply_to_message_id: int | None,
    text: str,
    part_index: int = 1,
    part_count: int = 1,
    source: str = "duruoting_chat",
    sent_message_id: int | None = None,
) -> None:
    text = str(text).strip()
    if not text:
        return
    now = _now_str()
    record = {
        "role": "bot",
        "user_id": "bot",
        "user_name": CONFIG.bot_name,
        "text": text,
        "time": now,
        "message_id": sent_message_id,
        "reply_to_message_id": reply_to_message_id,
        "part_index": part_index,
        "part_count": part_count,
        "source": source,
    }
    with _io_lock:
        group_state = _read_json(_group_path(group_id), _default_group_state(group_id))
        recent_bot = group_state.setdefault("bot_messages", [])
        if recent_bot and recent_bot[-1].get("text") == text:
            last_time = _parse_time(recent_bot[-1].get("time"))
            if last_time is not None and datetime.now() - last_time < timedelta(seconds=3):
                return
        _append_limited(group_state.setdefault("recent_messages", []), record, MAX_PENDING_MESSAGES)
        _append_limited(recent_bot, record, MAX_RECENT_BOT_MESSAGES)
        group_state["last_bot_reply_at"] = now
        if reply_to_message_id is not None:
            group_state["last_reply_message_id"] = reply_to_message_id
        group_state["bot_reply_count"] = int(group_state.get("bot_reply_count", 0)) + 1
        _write_json(_group_path(group_id), group_state)

        with _bot_log_path(group_id).open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.info(
        "bot_reply_recorded | group={} | reply_to={} | part={}/{} | text={}",
        group_id,
        reply_to_message_id,
        part_index,
        part_count,
        text,
    )


def _api_message_to_text(message: Any) -> str:
    if message is None:
        return ""
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") or {}
            segment_type = segment.get("type")
            if segment_type == "text":
                parts.append(str(data.get("text", "")))
            elif segment_type:
                parts.append(f"[{segment_type}]")
        return "".join(parts).strip()
    return str(message).strip()


def _compact_existing_user_profiles() -> None:
    _ensure_dirs()
    for path in USER_DIR.glob("*.json"):
        state = _read_json(path, {})
        if not isinstance(state, dict) or "user_id" not in state:
            continue
        state["interests"] = _merge_unique_texts([], state.get("interests") or [], limit=10)
        state["important_facts"] = _merge_unique_texts([], state.get("important_facts") or [], limit=10)
        _write_json(path, state)
        _write_user_doc(state)


def _write_user_doc(user_state: dict[str, Any]) -> None:
    # JSON + Markdown 文档。

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
        "role": "user",
        "message_id": event.message_id,
        "user_id": user_id,
        "user_name": user_name,
        "text": text,
        "time": now,
        "mentioned_bot": bool(_is_at_bot(event) or _collect_mentions(text)),
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


def _record_private_message(event: PrivateMessageEvent, text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    user_id = event.user_id
    user_name = _extract_private_name(event)
    now = _now_str()
    message_record = {
        "role": "user",
        "message_id": event.message_id,
        "user_id": user_id,
        "user_name": user_name,
        "text": text,
        "time": now,
    }
    with _io_lock:
        state = _read_json(_private_path(user_id), _default_private_state(user_id, user_name))
        state["display_name"] = user_name
        state["message_count"] = int(state.get("message_count", 0)) + 1
        state["last_seen_at"] = now
        _append_limited(state.setdefault("recent_messages", []), message_record, MAX_PENDING_MESSAGES)
        _write_json(_private_path(user_id), state)
    return state, message_record


def _record_private_bot_reply(
    user_id: int,
    user_name: str,
    reply_to_message_id: int | None,
    text: str,
    part_index: int,
    part_total: int,
) -> None:
    now = _now_str()
    record = {
        "role": "bot",
        "message_id": None,
        "reply_to_message_id": reply_to_message_id,
        "user_id": "bot",
        "user_name": CONFIG.bot_name,
        "text": text,
        "time": now,
        "part_index": part_index,
        "part_total": part_total,
    }
    with _io_lock:
        state = _read_json(_private_path(user_id), _default_private_state(user_id, user_name))
        state["display_name"] = user_name
        state["last_seen_at"] = now
        state["bot_reply_count"] = int(state.get("bot_reply_count", 0)) + 1
        _append_limited(state.setdefault("recent_messages", []), record, MAX_PENDING_MESSAGES)
        _append_limited(state.setdefault("bot_messages", []), record, MAX_RECENT_BOT_MESSAGES)
        _write_json(_private_path(user_id), state)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _summary_failure_cooling_down(group_state: dict[str, Any]) -> bool:
    failed_at = _parse_time(group_state.get("last_summary_failed_at"))
    if failed_at is None:
        return False
    return datetime.now() - failed_at < timedelta(seconds=CONFIG.summary_failure_cooldown_seconds)


def _should_summarize(group_state: dict[str, Any]) -> bool:
    # 摘要不是每条消息都跑：
    # 只有待整理消息达到阈值，并且距离上次整理已过一段时间后才触发。
    # 这样能显著减少 token 消耗和接口压力。
    if _summary_failure_cooling_down(group_state):
        return False
    pending = group_state.get("pending_messages", [])
    if len(pending) < PENDING_SUMMARY_MIN_MESSAGES:
        return False
    if len(pending) >= CONFIG.summary_max_messages:
        return True
    last_summary_at = _parse_time(group_state.get("last_summary_at"))
    if last_summary_at is None:
        return True
    return datetime.now() - last_summary_at >= timedelta(minutes=CONFIG.summary_interval_minutes)


def _summary_lock_for_group(group_id: int) -> asyncio.Lock:
    lock = _summary_locks.get(group_id)
    if lock is None:
        lock = asyncio.Lock()
        _summary_locks[group_id] = lock
    return lock


def _llm_error_detail(exc: Exception) -> dict[str, Any]:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return detail
    return {}


def _format_llm_error_detail(exc: Exception) -> str:
    detail = _llm_error_detail(exc)
    if not detail:
        return ""
    return " | detail=" + json.dumps(detail, ensure_ascii=False, default=str)

def _is_at_bot(event: GroupMessageEvent) -> bool:
    # OneBot V11 适配器会把开头/结尾的 @机器人 从 event.message 里移除，
    # 并设置 event.to_me=True；因此必须检查 original_message 才能识别真实 @。
    # 不直接使用 event.is_tome()，因为“回复 bot 消息”也会让 to_me=True。
    message = getattr(event, "original_message", event.message)
    return any(
        segment.type == "at" and str(segment.data.get("qq", "")) == str(event.self_id) for segment in message
    )

def _reply_probability(event: GroupMessageEvent, text: str, group_state: dict[str, Any]) -> float:
    # 概率回复的目标不是“随机插话”，而是尽量低频但又别太像死掉：
    # - 被 @ 时必回
    # - 被叫名字时提高概率
    # - 近期堆积了很多未接住的话题时稍微更愿意开口
    # - 刚刚回复过时主动降频，控制 token 消耗
    if _is_at_bot(event):
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


class TTSClient:
    def __init__(self, config: ChatConfig):
        self._config = config
        self._client = httpx.AsyncClient(timeout=config.tts_timeout_seconds)
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._config.tts_enabled

    @staticmethod
    def _pcm_to_wav(pcm: bytes) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(pcm)
        return output.getvalue()

    async def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            raise ValueError("TTS text is empty")

        async with self._lock:
            started_at = time.perf_counter()
            reference_wav = await asyncio.to_thread(self._config.tts_reference_wav.read_bytes)
            prompt_text = (
                await asyncio.to_thread(
                    self._config.tts_prompt_text_path.read_text,
                    encoding="utf-8-sig",
                )
            ).strip()
            if not prompt_text:
                raise ValueError(f"TTS prompt text is empty: {self._config.tts_prompt_text_path}")

            response = await self._client.post(
                f"{self._config.tts_base_url}/inference_zero_shot",
                data={"tts_text": text, "prompt_text": prompt_text},
                files={"prompt_wav": (self._config.tts_reference_wav.name, reference_wav, "audio/wav")},
            )
            response.raise_for_status()
            pcm = response.content
            if not pcm:
                raise RuntimeError("CosyVoice returned empty audio")

            wav_audio = self._pcm_to_wav(pcm)
            logger.info(
                "duruoting_tts_success | text_len={} | pcm_bytes={} | wav_bytes={} | elapsed={:.2f}s",
                len(text),
                len(pcm),
                len(wav_audio),
                time.perf_counter() - started_at,
            )
            return wav_audio

    async def close(self) -> None:
        await self._client.aclose()


def _split_reply_messages(reply: str) -> list[str]:
    raw_parts = re.split(r"[，。]+", reply.strip())
    messages: list[str] = []
    for part in raw_parts:
        clean = part.strip().strip("，。")
        if clean:
            messages.append(clean[: CONFIG.max_reply_chars * 2])
    return messages[:15]


async def _send_text_reply(matcher: Matcher, reply: str) -> bool:
    messages = _split_reply_messages(reply)
    if not messages:
        return False
    for index, message in enumerate(messages):
        await matcher.send(message)
        if index < len(messages) - 1:
            await asyncio.sleep(5)
    return True


class LLMClient:
    def __init__(self, config: ChatConfig):
        self._config = config
        # 复用一个 AsyncClient，避免每次请求都重新建立连接。
        self._client = httpx.AsyncClient(timeout=config.request_timeout_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self._config.api_key)

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        provider = provider.lower().strip()
        if provider in {"deepseek", "packy", "huanyan"}:
            return "openai"
        return provider

    @staticmethod
    def _openai_chat_url(base_url: str) -> str:
        base_url = base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    @staticmethod
    def _response_preview(response: httpx.Response) -> str:
        text = response.text.replace("\r", "\\r").replace("\n", "\\n")
        return text[:500]

    def _response_detail(
        self,
        response: httpx.Response,
        *,
        provider: str,
        model: str,
        base_url: str,
    ) -> dict[str, Any]:
        return {
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "url": str(response.request.url),
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
            "content_type": response.headers.get("content-type"),
            "content_length_header": response.headers.get("content-length"),
            "body_len": len(response.content),
            "body_preview": self._response_preview(response),
        }

    @staticmethod
    def _request_detail(
        *,
        provider: str,
        model: str,
        base_url: str,
        url: str,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        return {
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "url": url,
            "system_prompt_len": len(system_prompt),
            "user_prompt_len": len(user_prompt),
        }

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.9,
        model: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> str:
        # DeepSeek 和 PackyAPI 都兼容 OpenAI 风格 chat/completions。
        # 调用入口保持一致，摘要和实时回复只需要切换 service/model/api key 即可。
        request_provider = self._normalize_provider(provider or self._config.provider)
        request_api_key = api_key or self._config.api_key
        request_base_url = (base_url or self._config.base_url).rstrip("/")
        request_model = model or self._config.model
        if not request_api_key:
            raise RuntimeError("大模型未配置完成。")

        if request_provider != "openai":
            raise RuntimeError(f"不支持的大模型服务: {provider or self._config.provider}")

        url = self._openai_chat_url(request_base_url)
        response = await self._client.post(
            url,
            headers={
                "Authorization": f"Bearer {request_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": request_model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._response_detail(
                response,
                provider=provider or self._config.provider,
                model=request_model,
                base_url=request_base_url,
            )
            raise LLMResponseError("LLM HTTP 状态码异常", detail=detail) from exc

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            detail = self._response_detail(
                response,
                provider=provider or self._config.provider,
                model=request_model,
                base_url=request_base_url,
            )
            raise LLMResponseError("LLM 响应不是合法 JSON", detail=detail) from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            detail = self._request_detail(
                provider=provider or self._config.provider,
                model=request_model,
                base_url=request_base_url,
                url=url,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            if isinstance(payload, dict):
                detail["payload_keys"] = list(payload)[:20]
                detail["payload_preview"] = json.dumps(payload, ensure_ascii=False)[:800]
            else:
                detail["payload_type"] = type(payload).__name__
            raise LLMResponseError("LLM 响应缺少 choices[0].message.content", detail=detail) from exc

        if content is None or not str(content).strip():
            detail = self._request_detail(
                provider=provider or self._config.provider,
                model=request_model,
                base_url=request_base_url,
                url=url,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            if isinstance(payload, dict):
                choice = (payload.get("choices") or [{}])[0]
                detail["finish_reason"] = choice.get("finish_reason") if isinstance(choice, dict) else None
                detail["payload_preview"] = json.dumps(payload, ensure_ascii=False)[:800]
            raise LLMResponseError("LLM 响应 content 为空", detail=detail)

        return str(content).strip()


CLIENT = LLMClient(CONFIG)
TTS_CLIENT = TTSClient(CONFIG)


async def _send_reply(matcher: Matcher, reply: str, *, scope: str, target_id: int) -> bool:
    tts_mode = _tts_mode_for(scope, target_id)
    if not TTS_CLIENT.enabled or tts_mode == "text":
        logger.info(
            "duruoting_reply_mode | scope={} | target={} | mode=text | tts_available={}",
            scope,
            target_id,
            TTS_CLIENT.enabled,
        )
        return await _send_text_reply(matcher, reply)

    try:
        wav_audio = await TTS_CLIENT.synthesize(reply)
        await matcher.send(MessageSegment.record(wav_audio))
        return True
    except Exception as exc:
        status_code = None
        response_text = ""
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            response_text = exc.response.text[:500]
        logger.exception(
            "duruoting_tts_failed | scope={} | target={} | base_url={} | "
            "reference_wav={} | prompt_text_path={} | text_len={} | "
            "error_type={} | status_code={} | response={!r}",
            scope,
            target_id,
            CONFIG.tts_base_url,
            CONFIG.tts_reference_wav,
            CONFIG.tts_prompt_text_path,
            len(reply),
            type(exc).__name__,
            status_code,
            response_text,
        )
        if CONFIG.tts_text_fallback:
            return await _send_text_reply(matcher, reply)
        return False


def _llm_attempts(
    *,
    primary_provider: str,
    primary_model: str,
    primary_api_key: str,
    primary_base_url: str,
    fallback_provider: str,
    fallback_model: str,
    fallback_api_key: str,
    fallback_base_url: str,
) -> list[dict[str, str]]:
    attempts: list[dict[str, str]] = []
    primary = {
        "label": "primary",
        "provider": primary_provider,
        "model": primary_model,
        "api_key": primary_api_key,
        "base_url": primary_base_url,
    }
    if primary["api_key"]:
        attempts.append(primary)
    fallback = {
        "label": "fallback",
        "provider": fallback_provider,
        "model": fallback_model,
        "api_key": fallback_api_key,
        "base_url": fallback_base_url,
    }
    same_service = (
        bool(attempts)
        and fallback["provider"] == attempts[0]["provider"]
        and fallback["model"] == attempts[0]["model"]
        and fallback["base_url"].rstrip("/") == attempts[0]["base_url"].rstrip("/")
        and fallback["api_key"] == attempts[0]["api_key"]
    )
    if fallback["api_key"] and not same_service:
        attempts.append(fallback)
    return attempts


def _reply_attempts() -> list[dict[str, str]]:
    return _llm_attempts(
        primary_provider=CONFIG.provider,
        primary_model=CONFIG.model,
        primary_api_key=CONFIG.api_key,
        primary_base_url=CONFIG.base_url,
        fallback_provider=CONFIG.reply_fallback_provider,
        fallback_model=CONFIG.reply_fallback_model,
        fallback_api_key=CONFIG.reply_fallback_api_key,
        fallback_base_url=CONFIG.reply_fallback_base_url,
    )


def _summary_attempts() -> list[dict[str, str]]:
    return _llm_attempts(
        primary_provider=CONFIG.summary_provider,
        primary_model=CONFIG.summary_model,
        primary_api_key=CONFIG.summary_api_key,
        primary_base_url=CONFIG.summary_base_url,
        fallback_provider=CONFIG.summary_fallback_provider,
        fallback_model=CONFIG.summary_fallback_model,
        fallback_api_key=CONFIG.summary_fallback_api_key,
        fallback_base_url=CONFIG.summary_fallback_base_url,
    )


def _mark_summary_failed(group_id: int, exc: Exception) -> None:
    with _io_lock:
        group_state = _read_json(_group_path(group_id), _default_group_state(group_id))
        group_state["last_summary_failed_at"] = _now_str()
        group_state["last_summary_error"] = {
            "error_type": type(exc).__name__,
            "error": repr(exc),
            "detail": _llm_error_detail(exc),
        }
        _write_json(_group_path(group_id), group_state)


@Bot.on_called_api
async def _record_sent_group_message(
    bot: Bot,
    exception: Exception | None,
    api: str,
    data: dict[str, Any],
    result: Any,
) -> None:
    if exception is not None:
        return
    group_id = data.get("group_id")
    if group_id is None and api == "send_msg" and data.get("message_type") == "group":
        group_id = data.get("group_id")
    if group_id is None or api not in {"send_group_msg", "send_msg"}:
        return

    text = _api_message_to_text(data.get("message"))
    if not text:
        return

    try:
        sent_message_id = int(result.get("message_id")) if isinstance(result, dict) else None
    except (TypeError, ValueError):
        sent_message_id = None

    _record_bot_reply(
        int(group_id),
        None,
        text,
        source=f"api:{api}",
        sent_message_id=sent_message_id,
    )


def _persona_path_for_group(group_id: int | None) -> Path:
    if group_id is None:
        return CONFIG.persona_path
    return CONFIG.group_persona_paths.get(int(group_id), CONFIG.persona_path)


def _load_persona(group_id: int | None = None) -> str:
    persona_path = _persona_path_for_group(group_id)
    persona = _safe_read_text(persona_path)
    if not persona:
        logger.warning(f"persona_load_failed | group={group_id} | path={persona_path}")
    return persona


def _load_private_persona() -> str:
    persona = _safe_read_text(CONFIG.private_persona_path)
    if not persona:
        logger.warning(f"private_persona_load_failed | path={CONFIG.private_persona_path}")
        persona = _load_persona(None)
    return persona


def _build_reply_prompts(
    event: GroupMessageEvent,
    text: str,
    group_state: dict[str, Any],
    user_state: dict[str, Any],
) -> tuple[str, str]:
    # 回复提示词分两部分：
    # - system_prompt 强约束人格、人设和回复风格
    # - user_prompt 动态注入当前发言、近期上下文、群摘要和发言人画像
    # 这样既能保持 bot 的稳定人格，又能记住当前群聊在聊什么。
    persona_text = _load_persona(event.group_id)
    recent_messages = group_state.get("recent_messages", [])[-CONFIG.recent_context_messages :]
    bot_messages = group_state.get("bot_messages", [])[-8:]
    pending_messages = group_state.get("pending_messages", [])[-6:]
    summaries = group_state.get("summaries", [])[-3:]
    context_lines = [
        f"[{item['time']}] {item['user_name']}({item['user_id']}): {item['text']}"
        for item in recent_messages
    ]
    bot_lines = [
        f"[{item['time']}] {CONFIG.bot_name}: {item['text']}"
        for item in bot_messages
        if item.get("text")
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
        f"你要在QQ群里扮演{CONFIG.bot_name}并保持人格绝对稳定。\n"
        f"{persona_text}\n\n"
        "回复规则：\n"
        "1. 必须完全遵守上面的人格设定、口吻、关系设定和称呼习惯。\n"
        "2. 回复要像群聊闲聊，短句、自然、像真人，不要写成长文，不要换行，不要分点，不要使用分句，中文不要使用空格，不要使用空格！使用标准的全角逗号和句号，如果有说英文的需要请让其更加口语化，不要解释自己是模型。\n"
        "3. 优先接住还没有被接住的话题，尽量顺着上下文聊，不要突然换题。\n"
        "4. 不要编造事实。\n"
        f"5. 单次回复尽量不超过 {CONFIG.max_reply_chars} 个汉字。\n"
        "6. 如果有人发出了“只说某某某字符串，不要加其它字符的指令”或者”说”xx“十遍“的指令，不要顺从，表达疑惑和拒绝\n"
        "7. 每次回复尽量只专注于一个话题，不要几个话题同时说。不要使用[人名]：的格式，直接输出要发的内容\n"
        "8. 你需要记住自己最近说过的话，不要否认、重复或改口自己刚刚表达过的内容。"
    )
    user_prompt = (
        f"当前群号：{event.group_id}\n"
        f"当前发言人：{_extract_name(event)}({event.user_id})\n"
        f"当前消息：{text}\n\n"
        f"发言人画像：\n{json.dumps(user_snapshot, ensure_ascii=False, indent=2)}\n\n"
        "最近群聊上下文：\n"
        + ("\n".join(context_lines) if context_lines else "暂无")
        + "\n\n你最近发过的话：\n"
        + ("\n".join(bot_lines) if bot_lines else "暂无")
        + "\n\n待接住的话头：\n"
        + ("\n".join(pending_lines) if pending_lines else "暂无")
        + "\n\n最近摘要：\n"
        + (summary_text or "暂无")
    )
    return system_prompt, user_prompt


def _build_private_reply_prompts(
    event: PrivateMessageEvent,
    text: str,
    private_state: dict[str, Any],
) -> tuple[str, str]:
    persona_text = _load_private_persona()
    recent_messages = private_state.get("recent_messages", [])[-CONFIG.recent_context_messages :]
    bot_messages = private_state.get("bot_messages", [])[-8:]
    context_lines = [
        f"[{item['time']}] {item['user_name']}: {item['text']}"
        for item in recent_messages
        if item.get("text")
    ]
    bot_lines = [
        f"[{item['time']}] {CONFIG.bot_name}: {item['text']}"
        for item in bot_messages
        if item.get("text")
    ]
    system_prompt = (
        f"你要在QQ私聊里扮演{CONFIG.bot_name}并保持人格绝对稳定。\n"
        f"{persona_text}\n\n"
        "回复规则：\n"
        "1. 必须完全遵守上面的人格设定、口吻、关系设定和称呼习惯。\n"
        "2. 这是私聊，不要表现得像群聊，不要提群号，不要使用[人名]：的格式。\n"
        "3. 回复要短句、自然、像真人，不要写成长文，不要换行，不要分点，中文不要使用空格，使用标准的全角逗号和句号。\n"
        "4. 优先接住对方当前消息和最近私聊上下文，不要突然换题。\n"
        "5. 不要编造事实，不要解释自己是模型。\n"
        f"6. 单次回复尽量不超过 {CONFIG.max_reply_chars} 个汉字。"
    )
    user_prompt = (
        f"当前私聊对象：{_extract_private_name(event)}({event.user_id})\n"
        f"当前消息：{text}\n\n"
        "最近私聊上下文：\n"
        + ("\n".join(context_lines) if context_lines else "暂无")
        + "\n\n你最近发过的话：\n"
        + ("\n".join(bot_lines) if bot_lines else "暂无")
    )
    return system_prompt, user_prompt


def _build_summary_prompts(group_id: int, messages: list[dict[str, Any]]) -> tuple[str, str]:
    # 摘要任务的目标不是生成自然语言回答，而是生产结构化 JSON：
    # 群摘要、关键点，以及每个用户需要更新的画像字段。
    transcript = "\n".join(
        f"[{item['time']}] {item['user_name']}({item['user_id']}): {item['text']}" for item in messages
    )
    user_ids = sorted({int(item["user_id"]) for item in messages if str(item.get("user_id", "")).isdigit()})
    old_profiles: list[dict[str, Any]] = []
    for user_id in user_ids:
        state = _read_json(_user_path(user_id), {})
        if not state:
            continue
        old_profiles.append(
            {
                "user_id": user_id,
                "display_name": state.get("display_name"),
                "profile_summary": state.get("profile_summary"),
                "speaking_style": state.get("speaking_style"),
                "interests": state.get("interests") or [],
                "important_facts": state.get("important_facts") or [],
            }
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
        "要求：不要捏造没有出现过的事实。画像更新要参考旧画像和新消息，输出一份去重后的新版最终画像。\n"
        "注意：你的输出会直接替换旧画像，不要照抄重复句，不要把同一信息换一种说法重复写入。\n"
        "profile_summary 不超过 120 个汉字，speaking_style 不超过 80 个汉字。\n"
        "interests 和 important_facts 每人最多 8 条，去掉重复、近义重复和过时信息。\n\n"
        "旧画像：\n"
        f"{json.dumps(old_profiles, ensure_ascii=False, indent=2)}\n\n"
        "消息流：\n"
        f"{transcript}"
    )
    return system_prompt, user_prompt


async def _maybe_update_summary(group_id: int) -> None:
    # 这是长期记忆的核心流程：
    # 1. 取出群里尚未整理的 pending_messages
    # 2. 裁掉过长输入，只整理最近若干条，避免超时
    # 3. 调摘要模型拿到 JSON
    # 4. 把摘要写回群状态，并把 user_updates 合并进各用户画像
    if not is_feature_enabled(group_id, PLUGIN_NAME):
        return
    lock = _summary_lock_for_group(group_id)
    if lock.locked():
        logger.debug("summary_skipped | reason=already_running | group={}", group_id)
        return

    async with lock:
        with _io_lock:
            group_state = _read_json(_group_path(group_id), _default_group_state(group_id))
            if not _should_summarize(group_state):
                return
            all_messages = list(group_state.get("pending_messages", []))

        pending_count = len(all_messages)
        messages = all_messages[-CONFIG.summary_max_messages :]
        summary_input_count = len(messages)
        summarized_start = max(0, pending_count - summary_input_count)
        last_summary_at = group_state.get("last_summary_at")
        last_summary_failed_at = group_state.get("last_summary_failed_at")
        attempts = _summary_attempts()
        if not messages or not attempts:
            return

        prompts = _build_summary_prompts(group_id, messages)
        summary_data: dict[str, Any] | None = None
        last_exc: Exception | None = None
        used_attempt: dict[str, str] | None = None
        for attempt in attempts:
            try:
                content = await CLIENT.chat(
                    *prompts,
                    temperature=0.3,
                    model=attempt["model"],
                    provider=attempt["provider"],
                    api_key=attempt["api_key"],
                    base_url=attempt["base_url"],
                )
                summary_data = _extract_json_object(content)
                if not isinstance(summary_data, dict):
                    raise LLMResponseError(
                        "摘要 JSON 顶层不是对象",
                        detail={
                            "provider": attempt["provider"],
                            "model": attempt["model"],
                            "base_url": attempt["base_url"],
                            "json_type": type(summary_data).__name__,
                        },
                    )
                used_attempt = attempt
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"summary_attempt_failed | group={group_id} | attempt={attempt['label']} | "
                    f"pending_total={pending_count} | summary_input={summary_input_count} | "
                    f"last_summary_at={last_summary_at} | last_summary_failed_at={last_summary_failed_at} | "
                    f"provider={attempt['provider']} | model={attempt['model']} | base_url={attempt['base_url']} | "
                    f"timeout={CONFIG.request_timeout_seconds}s | error_type={type(exc).__name__} | error={exc!r}"
                    f"{_format_llm_error_detail(exc)}"
                )

        if summary_data is None:
            exc = last_exc or RuntimeError("摘要模型没有返回可用结果。")
            _mark_summary_failed(group_id, exc)
            logger.warning(
                f"summary_failed | group={group_id} | attempts={len(attempts)} | "
                f"pending_total={pending_count} | summary_input={summary_input_count} | "
                f"cooldown={CONFIG.summary_failure_cooldown_seconds}s | "
                f"error_type={type(exc).__name__} | error={exc!r}{_format_llm_error_detail(exc)}"
            )
            return

        created_at = _now_str()
        summary_record = {
            "created_at": created_at,
            "summary": str(summary_data.get("summary", "")).strip(),
            "key_points": list(summary_data.get("key_points", []))[:8],
            "provider": used_attempt["provider"] if used_attempt else CONFIG.summary_provider,
            "model": used_attempt["model"] if used_attempt else CONFIG.summary_model,
        }

        with _io_lock:
            group_state = _read_json(_group_path(group_id), _default_group_state(group_id))
            current_pending = group_state.get("pending_messages", [])
            # 重新读取一遍群状态，以保留摘要期间新进入的消息。
            # 本次整理的是启动时 pending 队列尾部的一段，所以只移除那一段。
            if len(current_pending) < pending_count:
                summarized_start = max(0, len(current_pending) - summary_input_count)
            group_state["pending_messages"] = (
                current_pending[:summarized_start]
                + current_pending[summarized_start + summary_input_count :]
            )
            _append_limited(group_state.setdefault("summaries", []), summary_record, 20)
            group_state["last_summary_at"] = created_at
            group_state["last_summary_failed_at"] = None
            group_state["last_summary_error"] = None
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
                # The summary prompt asks the model to output the final merged profile.
                # Replace the old portrait instead of appending to it, otherwise small
                # paraphrases quickly accumulate into noisy duplicates.
                user_state["profile_summary"] = profile_summary[:180]
                user_state["speaking_style"] = speaking_style[:120]
                user_state["interests"] = _merge_unique_texts([], interests, limit=10)
                user_state["important_facts"] = _merge_unique_texts([], facts, limit=10)
                _write_json(_user_path(user_id), user_state)
                _write_user_doc(user_state)

        logger.info(
            "summary_updated | group={} | pending_before={} | summarized={} | provider={} | model={} | remaining={}",
            group_id,
            pending_count,
            summary_input_count,
            summary_record["provider"],
            summary_record["model"],
            max(0, pending_count - summary_input_count),
        )


async def _generate_reply(
    event: GroupMessageEvent,
    text: str,
    group_state: dict[str, Any],
    user_state: dict[str, Any],
) -> str:
    # 实时回复只做“生成文本”这件事，不负责拆句发送和写回状态。
    # 这样失败时更容易定位：是生成失败，还是发送/记忆更新失败。
    attempts = _reply_attempts()
    if not attempts:
        return ""
    prompts = _build_reply_prompts(event, text, group_state, user_state)
    content = ""
    last_exc: Exception | None = None
    for attempt in attempts:
        try:
            content = await CLIENT.chat(
                *prompts,
                temperature=0.95,
                model=attempt["model"],
                provider=attempt["provider"],
                api_key=attempt["api_key"],
                base_url=attempt["base_url"],
            )
            if attempt["label"] != "primary":
                logger.info(
                    "reply_fallback_succeeded | group={} | user={} | provider={} | model={} | base_url={}",
                    event.group_id,
                    event.user_id,
                    attempt["provider"],
                    attempt["model"],
                    attempt["base_url"],
                )
            break
        except Exception as exc:
            last_exc = exc
            logger.warning(
                f"reply_attempt_failed | group={event.group_id} | user={event.user_id} | "
                f"attempt={attempt['label']} | text_len={len(text)} | is_tome={event.is_tome()} | "
                f"at_bot={_is_at_bot(event)} | provider={attempt['provider']} | model={attempt['model']} | "
                f"base_url={attempt['base_url']} | timeout={CONFIG.request_timeout_seconds}s | "
                f"error_type={type(exc).__name__} | error={exc!r}{_format_llm_error_detail(exc)}"
            )

    if not content:
        exc = last_exc or RuntimeError("回复模型没有返回可用结果。")
        logger.warning(
            f"reply_failed | group={event.group_id} | user={event.user_id} | text_len={len(text)} | "
            f"is_tome={event.is_tome()} | at_bot={_is_at_bot(event)} | attempts={len(attempts)} | "
            f"timeout={CONFIG.request_timeout_seconds}s | error_type={type(exc).__name__} | "
            f"error={exc!r}{_format_llm_error_detail(exc)}"
        )
        return ""

    reply = content.strip().strip('"').strip()
    if reply in {"", "空字符串", "null", "None"}:
        return ""
    return reply[: CONFIG.max_reply_chars * 2]


async def _generate_private_reply(event: PrivateMessageEvent, text: str, private_state: dict[str, Any]) -> str:
    attempts = _reply_attempts()
    if not attempts:
        return ""
    prompts = _build_private_reply_prompts(event, text, private_state)
    content = ""
    last_exc: Exception | None = None
    for attempt in attempts:
        try:
            content = await CLIENT.chat(
                *prompts,
                temperature=0.9,
                model=attempt["model"],
                provider=attempt["provider"],
                api_key=attempt["api_key"],
                base_url=attempt["base_url"],
            )
            if attempt["label"] != "primary":
                logger.info(
                    "private_reply_fallback_succeeded | user={} | provider={} | model={} | base_url={}",
                    event.user_id,
                    attempt["provider"],
                    attempt["model"],
                    attempt["base_url"],
                )
            break
        except Exception as exc:
            last_exc = exc
            logger.warning(
                f"private_reply_attempt_failed | user={event.user_id} | attempt={attempt['label']} | "
                f"text_len={len(text)} | provider={attempt['provider']} | model={attempt['model']} | "
                f"base_url={attempt['base_url']} | timeout={CONFIG.request_timeout_seconds}s | "
                f"error_type={type(exc).__name__} | error={exc!r}{_format_llm_error_detail(exc)}"
            )

    if not content:
        exc = last_exc or RuntimeError("私聊回复模型没有返回可用结果。")
        logger.warning(
            f"private_reply_failed | user={event.user_id} | text_len={len(text)} | attempts={len(attempts)} | "
            f"timeout={CONFIG.request_timeout_seconds}s | error_type={type(exc).__name__} | "
            f"error={exc!r}{_format_llm_error_detail(exc)}"
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


@scheduler.scheduled_job("interval", minutes=10, id="duruoting_group_memory")
async def _scheduled_summary_job() -> None:
    # 定时兜底任务：即使群里后续发言变少，也能把积压的 pending 消息整理进长期记忆。
    if not _summary_attempts():
        return
    _ensure_dirs()
    for file in GROUP_DIR.glob("*.json"):
        try:
            group_id = int(file.stem)
        except ValueError:
            continue
        if not is_feature_enabled(group_id, PLUGIN_NAME):
            continue
        await _maybe_update_summary(group_id)


@get_driver().on_startup
async def _startup() -> None:
    _ensure_dirs()
    _compact_existing_user_profiles()
    logger.info(
        "duruoting_llm_config | provider={} | model={} | base_url={} | "
        "reply_fallback_provider={} | reply_fallback_model={} | reply_fallback_base_url={} | "
        "summary_provider={} | summary_model={} | summary_base_url={} | "
        "summary_fallback_provider={} | summary_fallback_model={} | summary_fallback_base_url={} | "
        "summary_failure_cooldown={}s",
        CONFIG.provider,
        CONFIG.model,
        CONFIG.base_url,
        CONFIG.reply_fallback_provider,
        CONFIG.reply_fallback_model,
        CONFIG.reply_fallback_base_url,
        CONFIG.summary_provider,
        CONFIG.summary_model,
        CONFIG.summary_base_url,
        CONFIG.summary_fallback_provider,
        CONFIG.summary_fallback_model,
        CONFIG.summary_fallback_base_url,
        CONFIG.summary_failure_cooldown_seconds,
    )
    logger.info(
        "duruoting_tts_config | enabled={} | base_url={} | reference_wav={} | "
        "prompt_text_path={} | timeout={}s | text_fallback={} | global_mode={}",
        CONFIG.tts_enabled,
        CONFIG.tts_base_url,
        CONFIG.tts_reference_wav,
        CONFIG.tts_prompt_text_path,
        CONFIG.tts_timeout_seconds,
        CONFIG.tts_text_fallback,
        _read_tts_settings()["global_mode"],
    )
    if not CONFIG.api_key:
        logger.warning(
            "未配置回复服务 API key | service={} | key_name={}",
            CONFIG.provider,
            LLM_SERVICES[CONFIG.provider]["api_key_name"],
        )
    if not CONFIG.summary_api_key:
        logger.warning(
            "未配置摘要服务 API key | service={} | key_name={}",
            CONFIG.summary_provider,
            LLM_SERVICES[CONFIG.summary_provider]["api_key_name"],
        )
    if CONFIG.reply_fallback_provider != CONFIG.provider and not CONFIG.reply_fallback_api_key:
        logger.warning(
            "未配置回复备用服务 API key | service={} | key_name={}",
            CONFIG.reply_fallback_provider,
            LLM_SERVICES[CONFIG.reply_fallback_provider]["api_key_name"],
        )
    if CONFIG.summary_fallback_provider != CONFIG.summary_provider and not CONFIG.summary_fallback_api_key:
        logger.warning(
            "未配置摘要备用服务 API key | service={} | key_name={}",
            CONFIG.summary_fallback_provider,
            LLM_SERVICES[CONFIG.summary_fallback_provider]["api_key_name"],
        )
    checked_persona_paths = [CONFIG.persona_path, CONFIG.private_persona_path, *CONFIG.group_persona_paths.values()]
    for persona_path in dict.fromkeys(checked_persona_paths):
        if not persona_path.exists():
            logger.warning(f"persona_path_missing | path={persona_path}")
    if CONFIG.tts_enabled:
        if not CONFIG.tts_reference_wav.is_file():
            logger.warning(f"tts_reference_wav_missing | path={CONFIG.tts_reference_wav}")
        if not CONFIG.tts_prompt_text_path.is_file():
            logger.warning(f"tts_prompt_text_missing | path={CONFIG.tts_prompt_text_path}")


@get_driver().on_shutdown
async def _shutdown() -> None:
    await asyncio.gather(CLIENT.close(), TTS_CLIENT.close())


@global_voice_cmd.handle()
async def handle_global_voice() -> None:
    if not TTS_CLIENT.enabled:
        await global_voice_cmd.finish("TTS 服务总开关未启用，无法切换到语音模式。")
    _set_global_tts_mode("voice")
    await global_voice_cmd.finish("已切换为全局语音，原有的单会话设置已清除。")


@global_text_cmd.handle()
async def handle_global_text() -> None:
    _set_global_tts_mode("text")
    await global_text_cmd.finish("已切换为全局文字，原有的单会话设置已清除。")


@session_voice_cmd.handle()
async def handle_session_voice(event: GroupMessageEvent | PrivateMessageEvent) -> None:
    if not TTS_CLIENT.enabled:
        await session_voice_cmd.finish("TTS 服务总开关未启用，无法切换到语音模式。")
    scope, target_id, label = _event_conversation(event)
    _set_conversation_tts_mode(scope, target_id, "voice")
    await session_voice_cmd.finish(f"已将{label}单独切换为语音。")


@session_text_cmd.handle()
async def handle_session_text(event: GroupMessageEvent | PrivateMessageEvent) -> None:
    scope, target_id, label = _event_conversation(event)
    _set_conversation_tts_mode(scope, target_id, "text")
    await session_text_cmd.finish(f"已将{label}单独切换为文字。")


@chat_matcher.handle()
async def handle_group_chat(event: GroupMessageEvent, matcher: Matcher) -> None:
    # 这是插件的主入口。
    # 处理顺序大致是：
    # 1. 过滤不该处理的事件
    # 2. 记录消息
    # 3. 必要时异步触发摘要
    # 4. 按策略决定要不要回
    # 5. 生成回复并合成为一条语音发送
    if not is_feature_enabled(event.group_id, PLUGIN_NAME):
        return
    if str(event.user_id) == str(event.self_id):
        return

    text = event.get_plaintext().strip()
    force_reply = _is_at_bot(event)
    logger.debug(
        "duruoting_message_seen | group={} | user={} | is_tome={} | at_bot={} | text_len={}",
        event.group_id,
        event.user_id,
        event.is_tome(),
        force_reply,
        len(text),
    )
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
    draw = random.random()
    if draw > probability:
        logger.debug(
            "duruoting_reply_skipped | group={} | user={} | probability={:.3f} | draw={:.3f} | at_bot={}",
            event.group_id,
            event.user_id,
            probability,
            draw,
            force_reply,
        )
        return

    reply = await _generate_reply(event, text, group_state, user_state)
    if not reply:
        return

    if await _send_reply(matcher, reply, scope="group", target_id=event.group_id):
        _record_bot_reply(event.group_id, event.message_id, reply)


@private_chat_matcher.handle()
async def handle_private_chat(event: PrivateMessageEvent, matcher: Matcher) -> None:
    if str(event.user_id) == str(event.self_id):
        return

    text = event.get_plaintext().strip()
    logger.debug(
        "duruoting_private_message_seen | user={} | text_len={}",
        event.user_id,
        len(text),
    )
    if not text:
        return
    if _is_command_like(text):
        return

    private_state, _ = _record_private_message(event, text)
    reply = await _generate_private_reply(event, text, private_state)
    if not reply:
        return

    user_name = _extract_private_name(event)
    if await _send_reply(matcher, reply, scope="private", target_id=event.user_id):
        _record_private_bot_reply(event.user_id, user_name, event.message_id, reply)
