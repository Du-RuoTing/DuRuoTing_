from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from threading import Lock
from typing import Any


DATA_DIR = Path("data")
SETTINGS_PATH = DATA_DIR / "group_settings.json"
SIGN_PATH = DATA_DIR / "sign_in.json"
DEFAULT_FEATURES = {
    "帮助": True,
    "签到": True,
    "欢迎": True,
    "roll": True,
    "闲聊": True,
    "头衔": True,
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


def get_group_features(group_id: int) -> dict[str, bool]:
    with _lock:
        data = _read_json(SETTINGS_PATH, {})
        current = data.setdefault(str(group_id), DEFAULT_FEATURES.copy())
        for key, enabled in DEFAULT_FEATURES.items():
            current.setdefault(key, enabled)
        _write_json(SETTINGS_PATH, data)
        return dict(current)


def set_group_feature(group_id: int, feature: str, enabled: bool) -> bool:
    if feature not in DEFAULT_FEATURES:
        return False
    with _lock:
        data = _read_json(SETTINGS_PATH, {})
        current = data.setdefault(str(group_id), DEFAULT_FEATURES.copy())
        current[feature] = enabled
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
