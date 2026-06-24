from __future__ import annotations

import csv
import os
import re
import textwrap
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

from nonebot import on_fullmatch, on_regex
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from PIL import Image, ImageDraw, ImageFont


DATA_DIR = Path(
    os.getenv(
        "GAOKAO_ADMISSION_DATA_DIR",
        "tmp/gaokao_admission_integrated_20250624",
    )
)
DETAIL_CSV = DATA_DIR / "henan_major_admission_scores_2022_2025.csv"
GROUP_CSV = DATA_DIR / "henan_major_group_admission_scores_2025.csv"
SOURCE_TEXT = "数据来源：@Jorge de Burgos 如有错误请反馈 @桓衍"
VERSION_TEXT = "版本号：1.0.1"
AUTHOR_TEXT = "制图：github@huanyan77777"
MAX_RESULT_ROWS = 48
SUBJECTS = {"物理", "历史", "文科", "理科", "艺术", "体育", "艺术类", "体育类"}

gaokao_help = on_fullmatch({"gaokaohelp", "高考帮助"}, priority=8, block=True)
gaokao_query = on_regex(r"^(?:gaokao|高考|录取|录取查询)\s+(.+)$", priority=8, block=True)


@dataclass(slots=True)
class AdmissionStore:
    detail_rows: list[dict[str, str]]
    group_rows: list[dict[str, str]]
    schools: set[str]


_store: AdmissionStore | None = None
_store_lock = Lock()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return [dict(row) for row in csv.DictReader(file)]


def _load_store() -> AdmissionStore:
    global _store
    with _store_lock:
        if _store is not None:
            return _store
        if not DETAIL_CSV.exists() or not GROUP_CSV.exists():
            raise FileNotFoundError(f"高考录取数据不存在：{DATA_DIR}")
        detail_rows = _read_csv(DETAIL_CSV)
        group_rows = _read_csv(GROUP_CSV)
        schools = {row["school_name"] for row in [*detail_rows, *group_rows] if row.get("school_name")}
        _store = AdmissionStore(detail_rows=detail_rows, group_rows=group_rows, schools=schools)
        return _store


def _pick_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    source_han_name = "SourceHanSerifSC-Bold.otf" if bold else "SourceHanSerifSC-Regular.otf"
    candidates = [
        Path("fonts") / source_han_name,
        Path("fonts") / ("NotoSerifSC-Bold.otf" if bold else "NotoSerifSC-Regular.otf"),
        Path(r"C:\Windows\Fonts\NotoSerifSC-VF.ttf"),
        Path(r"C:\Windows\Fonts\STSONG.TTF"),
        Path(r"C:\Windows\Fonts\simsunb.ttf" if bold else r"C:\Windows\Fonts\simsun.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path("fonts") / ("MiSans-Bold.ttf" if bold else "MiSans-Regular.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _normalize_subject(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return {"文科": "历史", "理科": "物理", "艺术": "艺术类", "体育": "体育类"}.get(value, value)


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value.strip())


def _parse_query(raw: str, store: AdmissionStore) -> tuple[str | None, str | None, str]:
    subject: str | None = None
    parts = raw.strip().split()
    if parts and parts[0] in SUBJECTS:
        subject = _normalize_subject(parts.pop(0))

    if not parts:
        return subject, None, ""

    school = parts[0]
    if school not in store.schools:
        return subject, None, f"未找到学校“{school}”。请使用完整学校名，并用空格分隔，例如：gaokao 东南大学 计算机"
    return subject, school, _compact_text("".join(parts[1:]))


def _is_group_query(query: str) -> bool:
    return bool(re.fullmatch(r"(?:专业组)?\(?\d{2,3}\)?", query))


def _year_sort_value(row: dict[str, str]) -> int:
    try:
        return int(row.get("year", "0") or 0)
    except ValueError:
        return 0


def _major_group_sort_value(row: dict[str, str]) -> tuple[int, int | str]:
    code = row.get("major_group_code") or row.get("major_group_name", "")
    digits = re.sub(r"\D", "", code)
    if digits:
        return (0, int(digits))
    return (1, code)


def _subject_sort_value(row: dict[str, str]) -> tuple[int, str]:
    subject = row.get("subject_track", "")
    priority = {"物理": 0, "理科": 1, "历史": 2, "文科": 3}
    return (priority.get(subject, 9), subject)


def _admission_sort_key(row: dict[str, str]) -> tuple[tuple[int, str], tuple[int, int | str], int, str, str]:
    return (
        _subject_sort_value(row),
        _major_group_sort_value(row),
        -_year_sort_value(row),
        row.get("major_name", ""),
        row.get("major_code", ""),
    )


def _match_detail_rows(store: AdmissionStore, school: str, query: str, subject: str | None) -> list[dict[str, str]]:
    query = query.strip()
    rows = [row for row in store.detail_rows if row.get("school_name") == school]
    if subject:
        rows = [row for row in rows if row.get("subject_track") == subject]
    if query:
        if _is_group_query(query):
            code = re.sub(r"\D", "", query)
            rows = [row for row in rows if row.get("major_group_code") == code]
        else:
            rows = [
                row
                for row in rows
                if query in _compact_text(row.get("major_name", ""))
                or query in _compact_text(row.get("major_group_code", ""))
                or query in _compact_text(row.get("major_group_name", ""))
                or query in _compact_text(row.get("major_note", ""))
                or query in _compact_text(row.get("subject_requirement", ""))
            ]
    return sorted(rows, key=_admission_sort_key)


def _match_group_rows(store: AdmissionStore, school: str, query: str, subject: str | None) -> list[dict[str, str]]:
    rows = [row for row in store.group_rows if row.get("school_name") == school]
    if subject:
        rows = [row for row in rows if row.get("subject_track") == subject]
    if query:
        code = re.sub(r"\D", "", query) if _is_group_query(query) else query
        rows = [
            row
            for row in rows
            if code in _compact_text(row.get("major_group_code", ""))
            or code in _compact_text(row.get("major_group_name", ""))
            or code in _compact_text(row.get("subject_requirement", ""))
            or code in _compact_text(row.get("notes", ""))
        ]
    return sorted(rows, key=_admission_sort_key)


def _latest_years(rows: list[dict[str, str]], limit: int = 4) -> list[dict[str, str]]:
    years = sorted({row.get("year", "") for row in rows if row.get("year")}, reverse=True)[:limit]
    return [row for row in rows if row.get("year") in years]


def _select_result_rows(
    detail_rows: list[dict[str, str]], group_rows: list[dict[str, str]], query: str
) -> tuple[list[dict[str, str]], str]:
    if _is_group_query(query) and group_rows:
        return group_rows, "专业组"
    if detail_rows:
        return _latest_years(detail_rows), "专业"
    if group_rows:
        return group_rows, "专业组"
    return [], "专业"


def _line_wrap(text: str, width: int) -> list[str]:
    if not text:
        return [""]
    return textwrap.wrap(text, width=width, break_long_words=True, replace_whitespace=False) or [text]


def _wrap_cell(value: str, width: int, max_lines: int = 2) -> list[str]:
    value = str(value)
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return [value]
    return _line_wrap(value, max(4, width // 28))[:max_lines]


def _draw_table_row(
    draw: ImageDraw.ImageDraw,
    y: int,
    values: list[str],
    widths: list[int],
    font: ImageFont.ImageFont,
    fill: str,
    bg: str,
    card_width: int | None = None,
) -> int:
    x = 42
    row_h = 81
    right = card_width - 28 if card_width is not None else 42 + sum(widths)
    draw.rounded_rectangle((28, y - 8, right, y + row_h - 10), radius=12, fill=bg)
    for value, width in zip(values, widths):
        lines = _wrap_cell(value, width)
        draw.text((x, y), lines[0], fill=fill, font=font)
        if len(lines) > 1:
            draw.text((x, y + 38), lines[1], fill=fill, font=font)
        x += width
    return y + row_h


def _draw_record_card(
    draw: ImageDraw.ImageDraw,
    y: int,
    values: list[str],
    widths: list[int],
    remark: str,
    cell_font: ImageFont.ImageFont,
    remark_font: ImageFont.ImageFont,
    bg: str,
    card_width: int,
) -> int:
    remark_lines = _line_wrap(remark, 32)[:5] if remark else []
    row_h = 82 + max(1, len(remark_lines)) * 39
    draw.rounded_rectangle((28, y - 8, card_width - 28, y + row_h - 10), radius=14, fill=bg)

    x = 42
    for value, width in zip(values, widths):
        lines = _wrap_cell(value, width)
        draw.text((x, y), lines[0], fill="#071a33", font=cell_font)
        if len(lines) > 1:
            draw.text((x, y + 38), lines[1], fill="#071a33", font=cell_font)
        x += width

    remark_y = y + 67
    draw.text((42, remark_y), "备注：", fill="#0a4f8a", font=remark_font)
    if remark_lines:
        for index, line in enumerate(remark_lines):
            draw.text((122, remark_y + index * 39), line, fill="#0b2745", font=remark_font)
    else:
        draw.text((122, remark_y), "无", fill="#4d6680", font=remark_font)
    return y + row_h


def _build_result_image(
    school: str,
    subject: str | None,
    query: str,
    rows: list[dict[str, str]],
    result_type: str,
    total_count: int,
) -> bytes:
    title_font = _pick_font(40, bold=True)
    meta_font = _pick_font(21)
    header_font = _pick_font(20, bold=True)
    cell_font = _pick_font(19)
    small_font = _pick_font(17)

    width = 712
    row_h = 54
    estimated_row_heights: list[int] = []
    image = Image.new("RGB", (width, height), "#f4efe7")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=24, fill="#fffaf3", outline="#e3d3bd", width=2)

    subject_text = f" · {subject}" if subject else ""
    query_text = query or "全部"
    draw.text((48, 48), f"{school}{subject_text}录取查询", fill="#2f281f", font=title_font)
    draw.text((50, 102), f"查询：{query_text} · 类型：{result_type} · 展示 {len(rows)}/{total_count} 条", fill="#766858", font=meta_font)
    draw.text((50, 134), "说明：2023/2024 改革前文理科仅作趋势参考；2025 专业组由专业分聚合。", fill="#9a6a43", font=small_font)

    if result_type == "专业组":
        headers = ["年份", "科类", "批次", "专业组", "选科要求", "最低分", "最低位次", "备注"]
        widths = [70, 70, 120, 90, 220, 80, 110, 570]
        body = [
            [
                row.get("year", ""),
                row.get("subject_track", ""),
                row.get("batch", ""),
                row.get("major_group_code") or row.get("major_group_name", ""),
                row.get("subject_requirement", ""),
                row.get("min_score", ""),
                row.get("min_rank", ""),
                row.get("notes", ""),
            ]
            for row in rows
        ]
    else:
        headers = ["年份", "科类", "批次", "专业", "专业组", "最低分", "最低位次", "选科/备注"]
        widths = [70, 70, 120, 230, 80, 80, 110, 570]
        body = [
            [
                row.get("year", ""),
                row.get("subject_track", ""),
                row.get("batch", ""),
                row.get("major_name", ""),
                row.get("major_group_code") or row.get("major_group_name", ""),
                row.get("min_score", ""),
                row.get("min_rank", ""),
                "；".join(item for item in (row.get("subject_requirement"), row.get("major_note")) if item),
            ]
            for row in rows
        ]

    y = 184
    y = _draw_table_row(draw, y, headers, widths, header_font, "#4a3829", "#efe2d0")
    if body:
        for index, values in enumerate(body):
            bg = "#fff6ec" if index % 2 else "#fbf0e4"
            y = _draw_table_row(draw, y, values, widths, cell_font, "#4c4036", bg)
    else:
        draw.text((52, y + 10), "没有匹配到数据。请检查学校名、科类、专业名或专业组代码。", fill="#7d5b45", font=meta_font)
        y += row_h

    draw.text((48, height - 66), f"{SOURCE_TEXT}    {VERSION_TEXT}", fill="#6d5b4b", font=small_font)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_result_image(
    school: str,
    subject: str | None,
    query: str,
    rows: list[dict[str, str]],
    result_type: str,
    total_count: int,
) -> bytes:
    title_font = _pick_font(57, bold=True)
    meta_font = _pick_font(32)
    header_font = _pick_font(29, bold=True)
    cell_font = _pick_font(29)
    remark_font = _pick_font(27)
    notice_font = _pick_font(30, bold=True)
    small_font = _pick_font(26)

    width = 1080
    if result_type == "专业组":
        headers = ["年份", "科类", "批次", "专业组", "选科要求", "最低分", "最低位次"]
        widths = [86, 86, 170, 105, 300, 105, 148]
        entries = [
            (
                [
                    row.get("year", ""),
                    row.get("subject_track", ""),
                    row.get("batch", ""),
                    row.get("major_group_code") or row.get("major_group_name", ""),
                    row.get("subject_requirement", ""),
                    row.get("min_score", ""),
                    row.get("min_rank", ""),
                ],
                row.get("notes", ""),
            )
            for row in rows
        ]
    else:
        headers = ["年份", "科类", "批次", "专业", "组", "最低分", "最低位次"]
        widths = [86, 86, 170, 315, 78, 105, 160]
        entries = [
            (
                [
                    row.get("year", ""),
                    row.get("subject_track", ""),
                    row.get("batch", ""),
                    row.get("major_name", ""),
                    row.get("major_group_code") or row.get("major_group_name", ""),
                    row.get("min_score", ""),
                    row.get("min_rank", ""),
                ],
                "；".join(item for item in (row.get("subject_requirement"), row.get("major_note")) if item),
            )
            for row in rows
        ]

    card_heights = [82 + max(1, len(_line_wrap(remark, 32)[:5])) * 39 for _, remark in entries]
    content_bottom = 300 + 81 + (sum(card_heights) if card_heights else 108)
    height = content_bottom + 160
    image = Image.new("RGB", (width, height), "#eef5fb")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=28, fill="#f8fbff", outline="#bdd3e8", width=2)

    subject_text = f" · {subject}" if subject else ""
    query_text = query or "全部"
    draw.text((48, 48), f"{school}{subject_text}录取查询", fill="#0f2742", font=title_font)
    limit_text = f" · 已截断前 {len(rows)} 条" if total_count > len(rows) else ""
    draw.text(
        (50, 128),
        f"查询：{query_text} · 类型：{result_type} · 展示 {len(rows)}/{total_count} 条{limit_text}",
        fill="#375a7f",
        font=meta_font,
    )
    draw.text((50, 178), "仅供参考，不作为任何报考建议", fill="#0f2742", font=notice_font)
    draw.text((50, 222), "由于 2025 年为新高考，其投档线要相对前几年略高，请注意甄别", fill="#0f2742", font=notice_font)

    y = 300
    y = _draw_table_row(draw, y, headers, widths, header_font, "#f8fbff", "#1f5f99", card_width=width)
    if entries:
        for index, (values, remark) in enumerate(entries):
            bg = "#f6fbff" if index % 2 else "#edf6ff"
            y = _draw_record_card(draw, y, values, widths, remark, cell_font, remark_font, bg, width)
    else:
        draw.text((52, y + 10), "没有匹配到数据。请检查学校名、科类、专业名、备注或专业组代码。", fill="#375a7f", font=meta_font)

    draw.text((48, content_bottom + 44), SOURCE_TEXT, fill="#375a7f", font=small_font)
    draw.text((48, content_bottom + 88), f"{AUTHOR_TEXT}    {VERSION_TEXT}", fill="#375a7f", font=small_font)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_help_text() -> str:
    return (
        "高考录取查询用法：\n"
        "1. gaokao 学校 专业名\n"
        "2. gaokao 科类 学校 专业名\n"
        "3. gaokao 学校 专业组代码\n"
        "4. 也可以用：高考 / 录取 / 录取查询\n\n"
        "示例：\n"
        "gaokao 武汉大学 法学\n"
        "gaokao 物理 郑州大学 101\n"
        "录取 历史 北京大学 法学\n"
        "高考 河南大学 计算机\n\n"
        "科类可填：物理、历史、文科、理科。学校必须使用完整名称，并用空格分隔。结果会以图片发送。"
    )


@gaokao_help.handle()
async def handle_gaokao_help() -> None:
    await gaokao_help.finish(_build_help_text())


@gaokao_query.handle()
async def handle_gaokao_query(event: GroupMessageEvent) -> None:
    raw = re.sub(r"^(?:gaokao|高考|录取|录取查询)\s+", "", event.get_plaintext().strip(), count=1)
    try:
        store = _load_store()
    except FileNotFoundError as exc:
        await gaokao_query.finish(str(exc))

    subject, school, query = _parse_query(raw, store)
    if not school:
        tip = query or "请按“gaokao 学校 专业名”查询，发送 gaokaohelp 查看示例。"
        await gaokao_query.finish(tip)

    detail_rows = _match_detail_rows(store, school, query, subject)
    group_rows = _match_group_rows(store, school, query, subject)
    rows, result_type = _select_result_rows(detail_rows, group_rows, query)
    total_count = len(group_rows) if result_type == "专业组" else len(detail_rows)
    image = _build_result_image(
        school,
        subject,
        query,
        rows[:MAX_RESULT_ROWS],
        result_type,
        total_count,
    )
    await gaokao_query.finish(MessageSegment.image(image))
