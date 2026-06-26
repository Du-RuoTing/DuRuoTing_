import csv
import json
import os
import re
import textwrap
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from nonebot import get_driver, logger, on_regex
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from PIL import Image, ImageDraw, ImageFont

from .state import is_feature_enabled


PLUGIN_NAME = "小汀报考"
DATA_DIR = Path(os.getenv("GAOKAO_ADMISSION_DATA_DIR", "tmp/gaokao_admission_integrated_20250624"))
DETAIL_CSV = DATA_DIR / "henan_major_admission_scores_2022_2025.csv"
GROUP_CSV = DATA_DIR / "henan_major_group_admission_scores_2025.csv"
SOURCE_TEXT = "数据来源：@Jorge de Burgos 如有错误请反馈 @桓衍"
AUTHOR_TEXT = "制图：github@huanyan77777"
VERSION_TEXT = "版本号：0.2.0"
DEFAULT_RENDER_ROWS = 24
MAX_RENDER_ROWS = 48
SPECIAL_PLAN_KEYWORDS = ("国家专项", "专项计划", "专项")
PREFERENCE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "人工智能": ("人工智能", "智能科学", "智能制造", "机器人工程", "自动化", "计算机", "软件", "数据科学"),
    "ai": ("人工智能", "智能科学", "机器人工程", "自动化", "计算机", "软件", "数据科学"),
    "计算机": ("计算机", "软件", "网络空间安全", "信息安全", "数据科学", "人工智能", "物联网", "数字媒体技术"),
    "软件": ("软件", "计算机", "数据科学", "网络工程", "信息安全"),
    "信息": ("电子信息", "通信", "信息工程", "计算机", "人工智能", "网络空间安全"),
    "电子": ("电子信息", "电子科学", "微电子", "集成电路", "通信", "光电信息"),
    "通信": ("通信", "电子信息", "信息工程", "网络空间安全"),
    "电气": ("电气工程", "智能电网", "自动化", "能源动力"),
    "自动化": ("自动化", "机器人工程", "智能制造", "电气工程", "控制"),
    "机械": ("机械", "车辆工程", "智能制造", "机器人工程", "自动化"),
    "能源": ("能源", "储能", "新能源", "动力工程", "电气工程"),
    "数学": ("数学", "信息与计算科学", "统计", "数据科学"),
    "医学": ("临床医学", "口腔医学", "基础医学", "药学", "医学"),
    "法学": ("法学", "知识产权", "政治学"),
    "经管": ("经济", "金融", "会计", "工商管理", "管理科学"),
    "财经": ("经济", "金融", "会计", "财政", "税收", "审计"),
    "师范": ("师范", "教育学", "汉语言文学", "数学与应用数学", "英语"),
    "新工科": ("工科", "计算机", "软件", "人工智能", "电子", "通信", "自动化", "机器人", "集成电路", "电气", "智能制造"),
    "工科": ("工科", "计算机", "软件", "人工智能", "电子", "通信", "自动化", "机械", "电气", "能源"),
}
REGION_ALIASES: dict[str, set[str]] = {
    "一线": {"北京", "上海", "广东"},
    "一线城市": {"北京", "上海", "广东"},
    "北上广深": {"北京", "上海", "广东"},
    "河南": {"河南"},
    "北京": {"北京"},
    "上海": {"上海"},
    "广东": {"广东"},
    "江苏": {"江苏"},
    "浙江": {"浙江"},
    "湖北": {"湖北"},
    "湖南": {"湖南"},
    "山东": {"山东"},
    "陕西": {"陕西"},
    "四川": {"四川"},
    "重庆": {"重庆"},
    "天津": {"天津"},
}

LLM_SERVICES: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key_name": "DEEPSEEK_API_KEY",
    },
    "packy": {
        "base_url": "https://www.packyapi.com/v1",
        "api_key_name": "PACKY_API_KEY",
    },
}


@dataclass(slots=True)
class ApplyConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    request_timeout_seconds: int


@dataclass(slots=True)
class ApplyStore:
    detail_rows: list[dict[str, str]]
    group_rows: list[dict[str, str]]
    detail_index: dict[tuple[str, str, str, str], list[dict[str, str]]]


_store: ApplyStore | None = None
_store_lock = Lock()
apply_cmd = on_regex(r"^报考(?:\s+.+)?$", priority=8, block=True)


def _get_config_value(name: str, default: str = "") -> str:
    try:
        config = get_driver().config
        value = getattr(config, name.lower(), None)
    except ValueError:
        value = None
    if value is None:
        value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip()


def _read_config_int(name: str, default: int) -> int:
    value = _get_config_value(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("{} 不是合法整数，回退为 {}", name, default)
        return default


def _load_config() -> ApplyConfig:
    provider = _get_config_value("XIAOTING_APPLY_SERVICE", "deepseek")
    provider = provider.lower().strip()
    if provider not in LLM_SERVICES:
        logger.warning("XIAOTING_APPLY_SERVICE={} 不存在，已回退到 deepseek。", provider)
        provider = "deepseek"
    service = LLM_SERVICES[provider]
    return ApplyConfig(
        provider=provider,
        api_key=_get_config_value("XIAOTING_APPLY_API_KEY", _get_config_value(service["api_key_name"])),
        base_url=_get_config_value("XIAOTING_APPLY_BASE_URL", service["base_url"]),
        model=_get_config_value("XIAOTING_APPLY_MODEL", _get_config_value("DU_RUO_TING_REPLY_MODEL", "deepseek-v4-flash")),
        request_timeout_seconds=max(15, _read_config_int("XIAOTING_APPLY_TIMEOUT_SECONDS", 90)),
    )


CONFIG = _load_config()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return [dict(row) for row in csv.DictReader(file)]


def _load_store() -> ApplyStore:
    global _store
    with _store_lock:
        if _store is not None:
            return _store
        if not DETAIL_CSV.exists() or not GROUP_CSV.exists():
            raise FileNotFoundError(f"报考数据不存在：{DATA_DIR}")
        detail_rows = _read_csv(DETAIL_CSV)
        group_rows = _read_csv(GROUP_CSV)
        detail_index: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
        for row in detail_rows:
            if row.get("year") != "2025" or _is_special_plan(row) or row.get("major_name") == "专业组投档线":
                continue
            key = (
                row.get("school_code", ""),
                row.get("school_name", ""),
                row.get("subject_track", ""),
                row.get("major_group_code", ""),
            )
            detail_index.setdefault(key, []).append(row)
        _store = ApplyStore(detail_rows=detail_rows, group_rows=group_rows, detail_index=detail_index)
        return _store


def _normalize_subject(value: str | None) -> str:
    if value in {"历史", "文科"}:
        return "历史"
    return "物理"


def _parse_apply_query(text: str) -> tuple[str, int | None, str, int, str, set[str] | None]:
    render_limit = DEFAULT_RENDER_ROWS
    num_match = re.search(r"(?:^|\s)\.num=(\d+)(?=\s|$)", text)
    if num_match:
        render_limit = max(1, min(MAX_RENDER_ROWS, int(num_match.group(1))))
        text = (text[: num_match.start()] + " " + text[num_match.end() :]).strip()
    parts = text.strip().split()
    if not parts or parts[0] != "报考":
        return "物理", None, "", render_limit, "", None
    parts = parts[1:]
    subject = "物理"
    if parts and parts[0] in {"物理", "历史", "理科", "文科"}:
        subject = _normalize_subject(parts.pop(0))
    region_label = ""
    location_filter: set[str] | None = None
    for index, part in list(enumerate(parts)):
        value = part.removeprefix("地区=").removeprefix("地区:")
        if value in REGION_ALIASES:
            region_label = value
            location_filter = REGION_ALIASES[value]
            del parts[index]
            break
    rank: int | None = None
    for index, part in enumerate(parts):
        digits = re.sub(r"\D", "", part)
        if digits:
            rank = int(digits)
            del parts[index]
            break
    return subject, rank, " ".join(parts).strip(), render_limit, region_label, location_filter


def _parse_intro_query(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _subject_matches(row: dict[str, str], subject: str) -> bool:
    track = row.get("subject_track", "")
    if subject == "物理":
        return track in {"物理", "理科"}
    return track in {"历史", "文科"}


def _is_special_plan(row: dict[str, str]) -> bool:
    text = " ".join(
        str(row.get(key, ""))
        for key in (
            "batch",
            "major_name",
            "major_group_name",
            "major_note",
            "major_list",
            "notes",
            "source",
            "source_file",
        )
    )
    return any(keyword in text for keyword in SPECIAL_PLAN_KEYWORDS)


def _preference_score(row: dict[str, str], preference: str) -> int:
    if not preference:
        return 0
    text = "".join(
        [
            row.get("school_name", ""),
            row.get("major_name", ""),
            row.get("major_group_code", ""),
            row.get("major_list", ""),
            row.get("major_note", ""),
            row.get("notes", ""),
        ]
    )
    score = 0
    preference_terms = _preference_terms(preference)
    for token in preference_terms:
        if len(token) >= 2 and token in text:
            score += 8 if token in preference else 3
    return score


def _preference_terms(preference: str) -> list[str]:
    terms = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", preference.lower())
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        for key, values in PREFERENCE_KEYWORDS.items():
            if key.lower() in term or term in key.lower():
                expanded.extend(values)
    result: list[str] = []
    seen: set[str] = set()
    for term in expanded:
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        result.append(term)
    return result


def _score_candidate(row: dict[str, str], user_rank: int, preference: str, source_type: str) -> tuple[int, int]:
    min_rank = _to_int(row.get("min_rank")) or 10**9
    distance = abs(min_rank - user_rank)
    score = -distance
    text = "".join(
        [
            row.get("school_name", ""),
            row.get("major_name", ""),
            row.get("major_list", ""),
            row.get("major_note", ""),
            row.get("notes", ""),
        ]
    )
    preference_terms = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", preference)
    if "新工科" in preference or "工科" in preference:
        preference_terms.extend(["工科", "计算机", "软件", "人工智能", "电子", "通信", "自动化", "机器人", "集成电路"])
    if "专业" in preference:
        preference_terms.extend(["计算机", "软件", "人工智能", "电子", "通信", "自动化"])
    for token in preference_terms:
        if len(token) >= 2 and token in text:
            score += 80000
    if row.get("is_985") == "是":
        score += 8000
    elif row.get("is_211") == "是":
        score += 4000
    if source_type == "专业组":
        score += 1200
    if row.get("year") == "2025":
        score += 2500
    return (-score, distance)


def _dedupe_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = (
            row.get("year", ""),
            row.get("school_name", ""),
            row.get("subject_track", ""),
            row.get("major_group_code", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _group_detail_rows(store: ApplyStore, group_row: dict[str, str], user_rank: int) -> list[dict[str, str]]:
    key = (
        group_row.get("school_code", ""),
        group_row.get("school_name", ""),
        group_row.get("subject_track", ""),
        group_row.get("major_group_code", ""),
    )
    rows = store.detail_index.get(key, [])
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for row in sorted(rows, key=lambda item: (_to_int(item.get("min_rank")) or 10**9, item.get("major_name", ""))):
        major = row.get("major_name", "").strip()
        if not major or major in seen:
            continue
        seen.add(major)
        item = dict(row)
        min_rank = _to_int(item.get("min_rank"))
        item["steady_mark"] = "\u221a" if min_rank is not None and min_rank >= user_rank else ""
        result.append(item)
    if result:
        return result

    fallback = []
    for major in re.split(r"[；;]", group_row.get("major_list", "")):
        major = major.strip()
        if major:
            fallback.append({"major_name": major, "min_rank": "", "min_score": "", "steady_mark": ""})
    return fallback


def _location_matches(row: dict[str, str], location_filter: set[str] | None) -> bool:
    if not location_filter:
        return True
    return row.get("school_location", "") in location_filter


def _select_candidate_pool(
    store: ApplyStore,
    subject: str,
    user_rank: int,
    preference: str,
    pool_limit: int,
    location_filter: set[str] | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in store.group_rows:
        if (
            row.get("year") != "2025"
            or not _subject_matches(row, subject)
            or not _location_matches(row, location_filter)
            or _is_special_plan(row)
        ):
            continue
        min_rank = _to_int(row.get("min_rank"))
        if min_rank is None:
            continue
        item: dict[str, Any] = dict(row)
        item["source_type"] = "专业组"
        item["children"] = _group_detail_rows(store, row, user_rank)
        item["distance"] = abs(min_rank - user_rank)
        item["preference_score"] = _preference_score(row, preference)
        candidates.append(item)

    candidates = _dedupe_candidates(candidates)
    candidates.sort(
        key=lambda row: (
            row.get("distance", 10**9),
            -row.get("preference_score", 0),
            _to_int(row.get("min_rank")) or 10**9,
            row.get("school_name", ""),
            row.get("major_group_code", ""),
        )
    )
    return candidates[:pool_limit]


def _local_diverse_candidates(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    school_counts: dict[str, int] = {}
    for max_per_school in (2, 4, 999):
        for row in rows:
            if row in result:
                continue
            school = row.get("school_name", "")
            if school_counts.get(school, 0) >= max_per_school:
                continue
            result.append(row)
            school_counts[school] = school_counts.get(school, 0) + 1
            if len(result) >= limit:
                return result
    return result


def _select_candidates(
    store: ApplyStore,
    subject: str,
    user_rank: int,
    preference: str,
    limit: int,
    location_filter: set[str] | None = None,
) -> list[dict[str, Any]]:
    pool = _select_candidate_pool(store, subject, user_rank, preference, max(limit * 4, 80), location_filter)
    return _local_diverse_candidates(pool, limit)


def _pick_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path("fonts") / ("SourceHanSerifSC-Bold.otf" if bold else "SourceHanSerifSC-Regular.otf"),
        Path(r"C:\Windows\Fonts\STSONG.TTF"),
        Path(r"C:\Windows\Fonts\simsunb.ttf" if bold else r"C:\Windows\Fonts\simsun.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _wrap(text: str, chars: int, limit: int = 2) -> list[str]:
    value = str(text)
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return [value]
    return textwrap.wrap(value, width=chars, break_long_words=True, replace_whitespace=False)[:limit] or [""]


def _child_lines(row: dict[str, Any]) -> list[str]:
    children = row.get("children") or []
    lines: list[str] = []
    for child in children:
        name = child.get("major_name", "")
        min_rank = child.get("min_rank", "")
        mark = child.get("steady_mark", "")
        if min_rank:
            lines.append(f"{name} {min_rank}{mark}")
        else:
            lines.append(name)
    return lines


def _child_display_columns(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    columns: tuple[list[str], list[str]] = ([], [])
    for item in _child_lines(row):
        wrapped = _wrap(item, 15, 4)
        target = columns[0] if len(columns[0]) <= len(columns[1]) else columns[1]
        target.extend(wrapped)
    return columns

def _build_data_image(subject: str, rank: int, preference: str, rows: list[dict[str, Any]], region_label: str = "") -> bytes:
    width = 1500
    title_font = _pick_font(62, True)
    meta_font = _pick_font(36)
    head_font = _pick_font(40, True)
    cell_font = _pick_font(38)
    child_font = _pick_font(28)
    small_font = _pick_font(28)
    base_row_h = 108
    row_heights = []
    for row in rows:
        child_columns = _child_display_columns(row)
        child_count = max(len(child_columns[0]), len(child_columns[1]))
        row_heights.append(base_row_h + max(0, child_count) * 34)
    height = 250 + sum(row_heights or [base_row_h]) + 210
    image = Image.new("RGB", (width, height), "#eef5fb")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((34, 34, width - 34, height - 34), radius=32, fill="#f8fbff", outline="#bdd3e8", width=3)
    draw.text((70, 62), "小汀报考候选数据", fill="#0f2742", font=title_font)
    pref = preference or "未填写"
    region = region_label or "不限"
    draw.text((72, 148), f"2026 参考 · {subject}组 · 位次 {rank} · 地区：{region} · 偏好：{pref}", fill="#375a7f", font=meta_font)

    headers = ["年", "学校", "批次", "专业组/组内专业", "最低分", "位次"]
    widths = [92, 450, 140, 480, 130, 150]
    x = 58
    y = 220
    draw.rounded_rectangle((48, y - 10, width - 48, y + 70), radius=18, fill="#1f5f99")
    for header, col_w in zip(headers, widths):
        draw.text((x, y + 11), header, fill="#f8fbff", font=head_font)
        x += col_w
    y += 88
    for index, row in enumerate(rows):
        row_h = row_heights[index]
        bg = "#f6fbff" if index % 2 else "#edf6ff"
        draw.rounded_rectangle((48, y - 10, width - 48, y + row_h - 18), radius=18, fill=bg)
        major = f"{row.get('major_group_code') or row.get('major_group_name', '')}组"
        values = [
            row.get("year", ""),
            row.get("school_name", ""),
            row.get("batch", ""),
            major,
            row.get("min_score", ""),
            row.get("min_rank", ""),
        ]
        x = 58
        for value, col_w in zip(values, widths):
            lines = _wrap(value, max(3, col_w // 38), 2)
            draw.text((x, y), lines[0], fill="#071a33", font=cell_font)
            if len(lines) > 1:
                draw.text((x, y + 50), lines[1], fill="#071a33", font=cell_font)
            x += col_w
        child_x = 112
        child_y = y + 54
        left_lines, right_lines = _child_display_columns(row)
        for line_index, line in enumerate(left_lines):
            draw.text((child_x, child_y + line_index * 34), line, fill="#375a7f", font=child_font)
        for line_index, line in enumerate(right_lines):
            draw.text((child_x + 600, child_y + line_index * 34), line, fill="#375a7f", font=child_font)
        y += row_h

    draw.text((72, height - 136), SOURCE_TEXT, fill="#375a7f", font=small_font)
    draw.text((72, height - 90), f"{AUTHOR_TEXT}    {VERSION_TEXT}", fill="#375a7f", font=small_font)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _candidate_payload(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    payload = []
    for row in rows:
        payload.append(
            {
                "年份": row.get("year", ""),
                "学校": row.get("school_name", ""),
                "科类": row.get("subject_track", ""),
                "类型": row.get("source_type", ""),
                "专业或专业组": row.get("major_name") or row.get("major_group_code") or row.get("major_group_name", ""),
                "最低分": row.get("min_score", ""),
                "最低位次": row.get("min_rank", ""),
                "备注": (row.get("major_list") or row.get("major_note") or row.get("notes") or "")[:120],
                "985": row.get("is_985", ""),
                "211": row.get("is_211", ""),
            }
        )
    return payload


def _rerank_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = []
    for index, row in enumerate(rows):
        payload.append(
            {
                "id": index,
                "学校": row.get("school_name", ""),
                "地区": row.get("school_location", ""),
                "专业组": row.get("major_group_code", ""),
                "最低位次": row.get("min_rank", ""),
                "距离": row.get("distance", ""),
                "专业列表": "；".join(_child_lines(row))[:260],
                "985": row.get("is_985", ""),
                "211": row.get("is_211", ""),
            }
        )
    return payload


def _extract_json_array(text: str) -> list[Any]:
    text = text.strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return []
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


async def _ai_diverse_candidates(
    rows: list[dict[str, Any]],
    subject: str,
    rank: int,
    preference: str,
    limit: int,
    region_label: str,
) -> list[dict[str, Any]]:
    if not CLIENT.enabled or not preference or len(rows) <= limit:
        return _local_diverse_candidates(rows, limit)
    system_prompt = (
        "你只负责给候选专业组排序，必须只输出 JSON 数组，不要解释。"
        "目标是在位次接近的前提下，让学科方向尽量多样，避免同一学校和同一专业方向过度重复。"
    )
    user_prompt = (
        f"考生：河南 2026，{subject}组，位次 {rank}。\n"
        f"偏好：{preference or '未说明'}。\n"
        f"地区筛选：{region_label or '不限'}。\n"
        f"需要返回 {limit} 个 id。\n"
        "规则：\n"
        "1. 优先选择最低位次与考生位次接近的候选。\n"
        "2. 在满足偏好的同时，保留相邻学科方向，如计算机、电子信息、自动化、数学、物理、能源、电气、机械等，不要只堆一个方向。\n"
        "3. 同一学校一般不超过 2 个，除非候选不足。\n"
        "4. 只能从给定候选 id 中选择，输出形如 [3, 8, 1] 的 JSON 数组。\n"
        f"候选：{json.dumps(_rerank_payload(rows), ensure_ascii=False)}"
    )
    try:
        reply = await CLIENT.chat(system_prompt, user_prompt)
    except Exception as exc:
        logger.warning("xiaoting_rerank_failed | error_type={} | error={}", type(exc).__name__, repr(exc))
        return _local_diverse_candidates(rows, limit)

    picked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for value in _extract_json_array(reply):
        if not isinstance(value, int) or value in seen or value < 0 or value >= len(rows):
            continue
        picked.append(rows[value])
        seen.add(value)
        if len(picked) >= limit:
            return picked
    for row in _local_diverse_candidates(rows, limit):
        if row not in picked:
            picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def _build_prompts(subject: str, rank: int, preference: str, candidates: list[dict[str, str]]) -> tuple[str, str]:
    system_prompt = (
        "你是“小汀报考”，一个谨慎、客观、简洁的河南高考志愿参考助手。"
        "你必须尊重提问者偏好，不替用户做决定，不夸大录取概率，不编造没有给出的数据。"
        "你的建议只能基于给定候选数据。"
        f"用户给定科类是{subject}组，必须严格按{subject}组表述，不能改写成其他科类。"
        "全文尽量控制在 450 字以内。不要使用表格、代码块、粗体、标题或表情。"
    )
    user_prompt = (
        f"考生信息：2026 年河南高考，{subject}组，位次 {rank}。\n"
        f"提问者偏好：{preference or '未说明，默认兼顾学校层次和专业方向'}。\n\n"
        "候选数据如下：\n"
        f"{json.dumps(_candidate_payload(candidates), ensure_ascii=False, indent=2)}\n\n"
        "请输出简短中文建议，要求：\n"
        "1. 先说明这是 2025 年数据查询参考，不构成报考建议。\n"
        f"2. 全文必须写成“{subject}组”，不要写成其他科类。\n"
        "3. 聚焦位次接近的可选项，不要使用冲、稳、保分类。\n"
        "4. 如果用户明显偏专业，就优先说专业组与专业匹配；如果偏学校层次，就优先说学校层次；如果不明确，就提示需要在专业和学校层次之间取舍。\n"
        "5. 语气客观、简单、尊重用户想法，不要制造焦虑。\n"
        "6. 不要输出表格，数据表会另行渲染成图片；不要使用 Markdown 粗体、代码块或表情。\n"
        "7. 全文尽量控制在 450 字以内，直接给结论。"
    )
    return system_prompt, user_prompt


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value.strip())


def _intro_context(store: ApplyStore, query: str) -> tuple[str, list[dict[str, str]]]:
    query_compact = _compact_text(query.removesuffix("专业"))
    schools = {row.get("school_name", "") for row in [*store.detail_rows, *store.group_rows]}
    school_match = next((school for school in schools if _compact_text(school) == query_compact), "")
    if school_match:
        rows = [
            row
            for row in store.group_rows
            if row.get("school_name") == school_match and row.get("year") == "2025"
        ][:12]
        if not rows:
            rows = [row for row in store.detail_rows if row.get("school_name") == school_match][-12:]
        return "高校", rows

    rows = [
        row
        for row in store.detail_rows
        if query_compact
        and (
            query_compact in _compact_text(row.get("major_name", ""))
            or query_compact in _compact_text(row.get("major_note", ""))
            or query_compact in _compact_text(row.get("major_list", ""))
        )
    ]
    rows.sort(key=lambda row: (row.get("year", ""), _to_int(row.get("min_rank")) or 10**9), reverse=True)
    return "专业", rows[:16]


def _build_intro_prompts(query: str, intro_type: str, rows: list[dict[str, str]]) -> tuple[str, str]:
    system_prompt = (
        "你是“小汀报考”，用简洁、客观、克制的中文介绍高校或专业。"
        "总字数必须控制在 150 字以内，尽量 100 字以内。"
        "不要使用奇怪表情，不要输出代码块，不要写长 Markdown，不要使用粗体或标题。"
    )
    if intro_type == "专业":
        user_prompt = (
            f"用户查询：{query}\n"
            "查询类型：专业\n\n"
            "请只写一段 150 字以内的专业简介，尽量 100 字以内。要求：\n"
            "1. 只介绍这个专业大致学什么、适合什么兴趣/能力倾向、常见发展方向。\n"
            "2. 不要加入学校、位次、分数、录取数据或本地数据库信息。\n"
            "3. 不要承诺就业和录取结果，不要像营销文案。\n"
            "4. 不要使用 Markdown 粗体、标题、表格、代码块或表情。"
        )
        return system_prompt, user_prompt

    payload = _candidate_payload(rows[:12])
    user_prompt = (
        f"用户查询：{query}\n"
        f"查询类型：{intro_type}\n"
        f"本地相关数据：{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请写一段 150 字以内的简介，尽量 100 字以内。要求：\n"
        "1. 简单说学校层次、常见优势方向和本地数据里能看到的招生信息。\n"
        "2. 不要承诺就业和录取结果，不要像营销文案。\n"
        "3. 不要说“用户没有指定专业”。\n"
        "4. 最后提醒一句：具体以当年招生计划和官方信息为准。"
    )
    return system_prompt, user_prompt


def _fallback_intro(query: str, intro_type: str, rows: list[dict[str, str]]) -> str:
    if intro_type == "高校":
        subjects = "、".join(sorted({row.get("subject_track", "") for row in rows if row.get("subject_track")})[:3])
        groups = "、".join(sorted({row.get("major_group_code", "") for row in rows if row.get("major_group_code")})[:5])
        return (
            f"{query}相关数据已在本地库中找到。2025 年可参考科类：{subjects or '暂无'}；"
            f"可见专业组：{groups or '暂无'}。这只是本地历史数据概览，具体以当年招生计划和官方信息为准。"
        )[:200]
    return (
        f"{query}主要介绍该专业的大致学习方向、适合倾向和常见发展路径。"
        "建议结合个人兴趣、课程设置和培养方案一起判断，不要只看名称选择。"
    )[:200]


def _clip_intro(text: str, limit: int = 150) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    if len(value) <= limit:
        return value
    clipped = value[: limit - 1].rstrip("，；、 ")
    return f"{clipped}。"


class ApplyClient:
    def __init__(self, config: ApplyConfig):
        self._config = config
        self._client = httpx.AsyncClient(timeout=config.request_timeout_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self._config.api_key)

    @staticmethod
    def _chat_url(base_url: str) -> str:
        base_url = base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        if not self.enabled:
            raise RuntimeError("小汀报考 API key 未配置。")
        response = await self._client.post(
            self._chat_url(self._config.base_url),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._config.model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"].strip()

    async def close(self) -> None:
        await self._client.aclose()


CLIENT = ApplyClient(CONFIG)


try:
    get_driver().on_shutdown(CLIENT.close)
except ValueError:
    pass


@apply_cmd.handle()
async def handle_apply(event: GroupMessageEvent) -> None:
    if not is_feature_enabled(event.group_id, PLUGIN_NAME):
        return
    subject, rank, preference, render_limit, region_label, location_filter = _parse_apply_query(event.get_plaintext())
    if rank is None:
        query = _parse_intro_query(event.get_plaintext())
        if not query:
            await apply_cmd.finish("请给出位次，例如：报考 物理 4874 河南 新工科优先 .num=24。没写科类时默认物理。")
        try:
            store = _load_store()
        except FileNotFoundError as exc:
            await apply_cmd.finish(str(exc))
        intro_type, rows = _intro_context(store, query)
        if not rows:
            await apply_cmd.finish(f"没有在本地数据里找到“{query}”的相关记录。")
        try:
            intro = await CLIENT.chat(*_build_intro_prompts(query, intro_type, rows))
        except Exception as exc:
            logger.warning("xiaoting_intro_failed | error_type={} | error={}", type(exc).__name__, repr(exc))
            intro = _fallback_intro(query, intro_type, rows)
        await apply_cmd.finish(_clip_intro(intro))

    try:
        store = _load_store()
    except FileNotFoundError as exc:
        await apply_cmd.finish(str(exc))

    candidate_pool = _select_candidate_pool(store, subject, rank, preference, max(render_limit * 4, 80), location_filter)
    candidates = await _ai_diverse_candidates(candidate_pool, subject, rank, preference, render_limit, region_label)
    if not candidates:
        await apply_cmd.finish("没有在本地数据里筛到合适候选。可以换一个位次或补充专业/学校偏好再试。")

    image = _build_data_image(subject, rank, preference, candidates, region_label)
    await apply_cmd.finish(MessageSegment.text("以下是为你准备的 2025 年参考数据") + MessageSegment.image(image))
