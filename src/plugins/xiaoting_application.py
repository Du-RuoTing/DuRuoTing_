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
VERSION_TEXT = "版本号：0.1.0"
MAX_RENDER_ROWS = 14

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
        _store = ApplyStore(detail_rows=_read_csv(DETAIL_CSV), group_rows=_read_csv(GROUP_CSV))
        return _store


def _normalize_subject(value: str | None) -> str:
    if value in {"历史", "文科"}:
        return "历史"
    return "物理"


def _parse_apply_query(text: str) -> tuple[str, int | None, str]:
    parts = text.strip().split()
    if not parts or parts[0] != "报考":
        return "物理", None, ""
    parts = parts[1:]
    subject = "物理"
    if parts and parts[0] in {"物理", "历史", "理科", "文科"}:
        subject = _normalize_subject(parts.pop(0))
    rank: int | None = None
    for index, part in enumerate(parts):
        digits = re.sub(r"\D", "", part)
        if digits:
            rank = int(digits)
            del parts[index]
            break
    return subject, rank, " ".join(parts).strip()


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


def _rank_band(user_rank: int, min_rank: int) -> str | None:
    if min_rank <= 0:
        return None
    ratio = min_rank / user_rank
    if 0.72 <= ratio < 0.96:
        return "冲"
    if 0.96 <= ratio <= 1.32:
        return "稳"
    if 1.32 < ratio <= 2.15:
        return "保"
    return None


def _subject_matches(row: dict[str, str], subject: str) -> bool:
    track = row.get("subject_track", "")
    if subject == "物理":
        return track in {"物理", "理科"}
    return track in {"历史", "文科"}


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


def _dedupe_candidates(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        key = (
            row.get("band", ""),
            row.get("school_name", ""),
            row.get("subject_track", ""),
            row.get("major_group_code", ""),
            row.get("major_name", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _select_candidates(store: ApplyStore, subject: str, user_rank: int, preference: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for source_type, rows in (("专业组", store.group_rows), ("专业", store.detail_rows)):
        for row in rows:
            if row.get("year") == "2025" and source_type == "专业" and row.get("major_name") == "专业组投档线":
                continue
            if not _subject_matches(row, subject):
                continue
            min_rank = _to_int(row.get("min_rank"))
            if min_rank is None:
                continue
            band = _rank_band(user_rank, min_rank)
            if not band:
                continue
            item = dict(row)
            item["band"] = band
            item["source_type"] = source_type
            candidates.append(item)

    candidates.sort(key=lambda row: (_score_candidate(row, user_rank, preference, row["source_type"]), row["band"]))
    band_limits = {"冲": 5, "稳": 6, "保": 5}
    picked: list[dict[str, str]] = []
    counts = {band: 0 for band in band_limits}
    for row in _dedupe_candidates(candidates):
        band = row["band"]
        if counts[band] >= band_limits[band]:
            continue
        picked.append(row)
        counts[band] += 1
    picked.sort(key=lambda row: ({"冲": 0, "稳": 1, "保": 2}[row["band"]], _to_int(row.get("min_rank")) or 0))
    return picked[:MAX_RENDER_ROWS]


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


def _build_data_image(subject: str, rank: int, preference: str, rows: list[dict[str, str]]) -> bytes:
    width = 1500
    title_font = _pick_font(62, True)
    meta_font = _pick_font(36)
    head_font = _pick_font(40, True)
    cell_font = _pick_font(38)
    small_font = _pick_font(28)
    row_h = 118
    height = 250 + max(1, len(rows)) * row_h + 210
    image = Image.new("RGB", (width, height), "#eef5fb")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((34, 34, width - 34, height - 34), radius=32, fill="#f8fbff", outline="#bdd3e8", width=3)
    draw.text((70, 62), "小汀报考候选数据", fill="#0f2742", font=title_font)
    pref = preference or "未填写"
    draw.text((72, 148), f"2026 参考 · {subject}组 · 位次 {rank} · 偏好：{pref}", fill="#375a7f", font=meta_font)

    headers = ["档", "年", "学校", "类型", "专业/专业组", "最低分", "位次"]
    widths = [70, 108, 300, 114, 500, 130, 150]
    x = 58
    y = 220
    draw.rounded_rectangle((48, y - 10, width - 48, y + 70), radius=18, fill="#1f5f99")
    for header, col_w in zip(headers, widths):
        draw.text((x, y + 11), header, fill="#f8fbff", font=head_font)
        x += col_w
    y += 88
    for index, row in enumerate(rows):
        bg = "#f6fbff" if index % 2 else "#edf6ff"
        draw.rounded_rectangle((48, y - 10, width - 48, y + row_h - 18), radius=18, fill=bg)
        major = row.get("major_name") or row.get("major_group_code") or row.get("major_group_name", "")
        if row.get("source_type") == "专业组":
            major = f"{row.get('major_group_code') or row.get('major_group_name', '')}组"
        values = [
            row.get("band", ""),
            row.get("year", ""),
            row.get("school_name", ""),
            row.get("source_type", ""),
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
                "档位": row.get("band", ""),
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
        "1. 先说明这是往年数据参考，不构成报考建议。\n"
        f"2. 全文必须写成“{subject}组”，不要写成其他科类。\n"
        "3. 分析时明确区分冲、稳、保，不超过 6 条要点。\n"
        "4. 如果用户明显偏专业，就优先说专业匹配；如果偏学校层次，就优先说学校层次；如果不明确，就提示需要在专业和学校层次之间取舍。\n"
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
    payload = _candidate_payload(rows[:12])
    system_prompt = (
        "你是“小汀报考”，用简洁、客观、克制的中文介绍高校或专业。"
        "总字数必须控制在 200 字以内。"
        "不要使用奇怪表情，不要输出代码块，不要写长 Markdown，不要使用粗体或标题。"
    )
    user_prompt = (
        f"用户查询：{query}\n"
        f"查询类型：{intro_type}\n"
        f"本地相关数据：{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请写一段 200 字以内的简介。要求：\n"
        "1. 如果是高校，简单说学校层次、常见优势方向和本地数据里能看到的招生信息。\n"
        "2. 如果是专业，简单说专业大致学习方向、适合倾向，以及本地数据里相关学校/位次情况。\n"
        "3. 不要承诺就业和录取结果，不要像营销文案。\n"
        "4. 不要说“用户没有指定专业”，因为用户可能是在查询高校。\n"
        "5. 最后提醒一句：具体以当年招生计划和官方信息为准。"
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
    schools = "、".join(sorted({row.get("school_name", "") for row in rows if row.get("school_name")})[:6])
    return (
        f"{query}相关记录已在本地库中找到，涉及学校包括：{schools or '暂无'}。"
        "建议结合个人兴趣、课程方向和近年位次变化一起看，具体以当年招生计划和官方信息为准。"
    )[:200]


def _clip_intro(text: str, limit: int = 200) -> str:
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
    subject, rank, preference = _parse_apply_query(event.get_plaintext())
    if rank is None:
        query = _parse_intro_query(event.get_plaintext())
        if not query:
            await apply_cmd.finish("请给出位次，例如：报考 物理 4874 新工科优先。没写科类时默认物理。")
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

    candidates = _select_candidates(store, subject, rank, preference)
    if not candidates:
        await apply_cmd.finish("没有在本地数据里筛到合适候选。可以换一个位次或补充专业/学校偏好再试。")

    image = _build_data_image(subject, rank, preference, candidates)
    fallback = (
        f"仅供参考，不作为任何报考建议。\n已按 {subject}组、位次 {rank} 筛选出 {len(candidates)} 条候选数据。"
    )
    try:
        advice = await CLIENT.chat(*_build_prompts(subject, rank, preference, candidates))
    except Exception as exc:
        logger.warning("xiaoting_apply_failed | error_type={} | error={}", type(exc).__name__, repr(exc))
        advice = fallback + "\n当前模型调用失败，先发送候选数据图。"

    await apply_cmd.finish(MessageSegment.text(advice.strip()) + MessageSegment.image(image))
