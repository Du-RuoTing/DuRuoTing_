from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from threading import Lock
from typing import Any


DATA_DIR = Path("data")
SETTINGS_PATH = DATA_DIR / "group_settings.json"
SIGN_PATH = DATA_DIR / "sign_in.json"
DEFAULT_CHAT_ENABLED_GROUPS = {1084401296}
DEFAULT_FEATURES = {
    "帮助": True,
    "签到": True,
    "欢迎": True,
    "roll": True,
    "闲聊": False,
    "头衔": True,
    "favor": True,
    "今天吃什么": True,
    "小汀报考": True,
}
LEGACY_FEATURE_NAMES = {
    "帮助": ("甯姪",),
    "签到": ("绛惧埌",),
    "欢迎": ("娆㈣繋",),
    "闲聊": ("闂茶亰",),
    "头衔": ("澶磋",),
}

_lock = Lock()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    _ensure_parent(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, value: Any) -> None:
    _ensure_parent(path)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_features(group_id: int) -> dict[str, bool]:
    features = DEFAULT_FEATURES.copy()
    features["闲聊"] = int(group_id) in DEFAULT_CHAT_ENABLED_GROUPS
    return features


def _normalize_group_features(group_id: int, current: dict[str, Any]) -> dict[str, bool]:
    defaults = _default_features(group_id)
    normalized: dict[str, bool] = {}
    for feature, default_enabled in defaults.items():
        if feature in current:
            normalized[feature] = bool(current[feature])
            continue

        legacy_names = LEGACY_FEATURE_NAMES.get(feature, ())
        legacy_value = next((current[name] for name in legacy_names if name in current), None)
        if feature == "闲聊":
            normalized[feature] = default_enabled
        elif legacy_value is not None:
            normalized[feature] = bool(legacy_value)
        else:
            normalized[feature] = default_enabled
    return normalized


def get_group_features(group_id: int) -> dict[str, bool]:
    with _lock:
        data = _read_json(SETTINGS_PATH, {})
        current = data.get(str(group_id), {})
        if not isinstance(current, dict):
            current = {}
        current = _normalize_group_features(group_id, current)
        data[str(group_id)] = current
        _write_json(SETTINGS_PATH, data)
        return dict(current)


def set_group_feature(group_id: int, feature: str, enabled: bool) -> bool:
    if feature not in DEFAULT_FEATURES:
        return False
    with _lock:
        data = _read_json(SETTINGS_PATH, {})
        current = data.get(str(group_id), {})
        if not isinstance(current, dict):
            current = {}
        current = _normalize_group_features(group_id, current)
        current[feature] = enabled
        for legacy_name in LEGACY_FEATURE_NAMES.get(feature, ()):
            current.pop(legacy_name, None)
        data[str(group_id)] = current
        _write_json(SETTINGS_PATH, data)
    return True


def is_feature_enabled(group_id: int | None, feature: str) -> bool:
    if group_id is None:
        return True
    return get_group_features(group_id).get(feature, True)


def sign_in(user_id: int) -> tuple[bool, int]:
    today = date.today()
    with _lock:
        data = _read_json(SIGN_PATH, {})
        record = data.get(str(user_id), {})
        last_date = record.get("last_date")
        streak = int(record.get("streak", 0))

        if last_date == today.isoformat():
            return False, streak

        if last_date == (today - timedelta(days=1)).isoformat():
            streak += 1
        else:
            streak = 1

        data[str(user_id)] = {"last_date": today.isoformat(), "streak": streak}
        _write_json(SIGN_PATH, data)
        return True, streak
