from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from nonebot import logger, on_fullmatch, on_regex
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from PIL import Image, ImageDraw, ImageFont

from .state import is_feature_enabled

try:
    import chess as pychess
except ImportError:  # pragma: no cover - depends on runtime deps
    pychess = None


PLUGIN_NAME = "棋局"
DATA_PATH = Path("data") / "chess_games.json"
FILES = "abcdefghi"

BG_COLOR = "#f8f3ed"
INK_COLOR = "#243247"
MUTED_COLOR = "#6f7786"
ACCENT_COLOR = "#355c9a"
SOFT_BLUE = "#dce8fb"
LINE_COLOR = "#6f8ebd"
LIGHT_SQUARE = "#edf4ff"
DARK_SQUARE = "#8aa7cf"
RED_SIDE = "#b22b2b"
BLACK_SIDE = "#243247"


@dataclass(slots=True)
class RenderNotice:
    title: str
    text: str

help_cmd = on_fullmatch("棋局帮助", priority=12, block=True)
start_cmd = on_regex(r"^(国际象棋|象棋)(人机|对战)(?:\s+.*)?$", priority=12, block=True)
board_cmd = on_fullmatch({"棋盘", "查看棋盘"}, priority=12, block=True)
move_cmd = on_regex(r"^走\s+(.+)$", priority=12, block=True)
hint_cmd = on_fullmatch({"棋局提示", "提示"}, priority=12, block=True)
resign_cmd = on_fullmatch({"认输", "结束棋局"}, priority=12, block=True)


def _read_games() -> dict[str, Any]:
    if not DATA_PATH.exists():
        return {}
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_games(games: dict[str, Any]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(games, ensure_ascii=False, indent=2), encoding="utf-8")


def _game_key(group_id: int) -> str:
    return str(group_id)


def _ensure_enabled(event: GroupMessageEvent) -> bool:
    return is_feature_enabled(event.group_id, PLUGIN_NAME)


def _at_users(event: GroupMessageEvent) -> list[int]:
    users: list[int] = []
    for seg in event.message:
        if seg.type != "at":
            continue
        raw = seg.data.get("qq")
        try:
            users.append(int(raw))
        except (TypeError, ValueError):
            continue
    return users


def _current_player_id(game: dict[str, Any]) -> int | None:
    side = game["turn"]
    player = game["players"].get(side)
    if player == "bot":
        return None
    try:
        return int(player)
    except (TypeError, ValueError):
        return None


def _event_display_name(event: GroupMessageEvent) -> str:
    sender = getattr(event, "sender", None)
    card = (getattr(sender, "card", "") or "").strip() if sender else ""
    nickname = (getattr(sender, "nickname", "") or "").strip() if sender else ""
    return card or nickname or f"玩家{str(event.user_id)[-4:]}"


def _fallback_player_name(user_id: int) -> str:
    return f"玩家{str(user_id)[-4:]}"


def _remember_player_name(game: dict[str, Any], user_id: int, name: str) -> None:
    names = game.setdefault("player_names", {})
    names[str(user_id)] = name


def _side_name(kind: str, side: str) -> str:
    if kind == "ichess":
        return "白方" if side == "white" else "黑方"
    return "红方" if side == "red" else "黑方"


def _mode_name(game: dict[str, Any]) -> str:
    return "人机" if "bot" in game["players"].values() else "对战"


def _player_label(game: dict[str, Any], side: str) -> str:
    player = game["players"].get(side)
    if player == "bot":
        return "机器人"
    names = game.get("player_names") or {}
    return str(names.get(str(player)) or _fallback_player_name(int(player)))


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


def _pick_symbol_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path(r"C:\Windows\Fonts\seguisym.ttf"),
        Path(r"C:\Windows\Fonts\seguihis.ttf"),
        Path(r"C:\Windows\Fonts\msyh.ttc"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _pick_xiangqi_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("fonts") / "BabelStoneXiangqiColour.ttf",
        Path("fonts") / "BabelStoneXiangqi.ttf",
        Path(r"C:\Windows\Fonts\msyhbd.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
    stroke_width: int = 0,
    stroke_fill: str | None = None,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    x = xy[0] - (bbox[2] - bbox[0]) / 2
    y = xy[1] - (bbox[3] - bbox[1]) / 2 - bbox[1] / 2
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    if not text:
        return []
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and _text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    max_width: int,
    font: ImageFont.ImageFont,
    fill: str,
    line_gap: int = 6,
    max_lines: int | None = None,
) -> int:
    lines = _wrap_text(draw, text, font, max_width)
    if max_lines is not None:
        lines = lines[:max_lines]
    line_height = font.size + line_gap if hasattr(font, "size") else 24
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height
    return y


def _append_history(
    game: dict[str, Any],
    side: str,
    actor: str,
    move: str,
    display: str | None = None,
    comment: str | None = None,
    actor_name: str | None = None,
) -> None:
    history = game.setdefault("history", [])
    history.append(
        {
            "side": side,
            "actor": actor,
            "actor_name": actor_name or ("机器人" if actor == "bot" else _player_label(game, side)),
            "move": move,
            "display": display or move,
            "comment": comment or "",
        }
    )


def _opponent_side(kind: str, side: str) -> str:
    if kind == "ichess":
        return "black" if side == "white" else "white"
    return "black" if side == "red" else "red"


def _parse_json_object(text: str) -> dict[str, Any]:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if match:
        text = match.group(1)
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)
    return json.loads(text)


async def _ask_llm_for_move(game: dict[str, Any], legal_moves: list[str], board_text: str) -> tuple[str, str]:
    # Keep this import lazy so the game plugin follows duruoting_chat's provider
    # configuration without creating a hard startup dependency cycle.
    try:
        from .duruoting_chat import CLIENT, CONFIG, _format_llm_error_detail, _reply_attempts
    except Exception as exc:  # pragma: no cover - defensive runtime fallback
        logger.warning("chess_llm_unavailable | error={!r}", exc)
        return random.choice(legal_moves), ""

    attempts = _reply_attempts()
    if not attempts:
        return random.choice(legal_moves), ""

    legal_sample = legal_moves[:120]
    system_prompt = (
        f"你是{CONFIG.bot_name}，正在QQ群里下棋。"
        "你必须只从给出的 legal_moves 中选择一个走法。"
        "输出 JSON：{\"move\":\"\",\"comment\":\"\"}。comment 不超过 30 个汉字。"
    )
    user_prompt = (
        f"棋种：{game['kind']}\n"
        f"轮到：{game['turn']}\n"
        f"棋盘：\n{board_text}\n\n"
        f"legal_moves：{json.dumps(legal_sample, ensure_ascii=False)}"
    )

    for attempt in attempts:
        try:
            content = await CLIENT.chat(
                system_prompt,
                user_prompt,
                temperature=0.4,
                model=attempt["model"],
                provider=attempt["provider"],
                api_key=attempt["api_key"],
                base_url=attempt["base_url"],
            )
            payload = _parse_json_object(content)
            move = str(payload.get("move", "")).strip()
            comment = str(payload.get("comment", "")).strip()
            if move in legal_moves:
                return move, comment[:60]
        except Exception as exc:
            logger.warning(
                f"chess_llm_move_failed | group={game.get('group_id')} | kind={game.get('kind')} | "
                f"provider={attempt['provider']} | model={attempt['model']} | "
                f"error_type={type(exc).__name__} | error={exc!r}{_format_llm_error_detail(exc)}"
            )

    return random.choice(legal_moves), ""


def _finish_text(game: dict[str, Any], winner: str | None, reason: str) -> str:
    if winner is None:
        return f"棋局结束，{reason}。"
    return f"棋局结束，{_side_name(game['kind'], winner)}胜，{reason}。"


def _save_game(game: dict[str, Any]) -> None:
    games = _read_games()
    games[_game_key(int(game["group_id"]))] = game
    _write_games(games)


def _delete_game(group_id: int) -> None:
    games = _read_games()
    games.pop(_game_key(group_id), None)
    _write_games(games)


def _load_game(group_id: int) -> dict[str, Any] | None:
    game = _read_games().get(_game_key(group_id))
    return game if isinstance(game, dict) else None


def _render_ichess_board(board: Any) -> str:
    assert pychess is not None
    symbols = {
        "P": "♙",
        "N": "♘",
        "B": "♗",
        "R": "♖",
        "Q": "♕",
        "K": "♔",
        "p": "♟",
        "n": "♞",
        "b": "♝",
        "r": "♜",
        "q": "♛",
        "k": "♚",
    }
    lines = ["  a b c d e f g h"]
    for rank in range(7, -1, -1):
        parts = [str(rank + 1)]
        for file in range(8):
            piece = board.piece_at(pychess.square(file, rank))
            parts.append(symbols.get(piece.symbol(), "·") if piece else "·")
        lines.append(" ".join(parts) + f" {rank + 1}")
    lines.append("  a b c d e f g h")
    return "\n".join(lines)


def _ichess_legal_moves(board: Any) -> list[str]:
    return [move.uci() for move in board.legal_moves]


def _parse_ichess_move(board: Any, raw: str) -> Any | None:
    raw = raw.strip()
    try:
        move = board.parse_san(raw)
        return move if move in board.legal_moves else None
    except ValueError:
        pass
    try:
        move = pychess.Move.from_uci(raw.lower())
        return move if move in board.legal_moves else None
    except ValueError:
        return None


def _ichess_status(board: Any) -> tuple[bool, str | None, str]:
    if board.is_checkmate():
        winner = "black" if board.turn == pychess.WHITE else "white"
        return True, winner, "将死"
    if board.is_stalemate():
        return True, None, "逼和"
    if board.is_insufficient_material():
        return True, None, "子力不足"
    if board.can_claim_draw():
        return True, None, "可判和"
    return False, None, ""


PIECES = {
    "rK": "帅",
    "rA": "仕",
    "rB": "相",
    "rN": "马",
    "rR": "车",
    "rC": "炮",
    "rP": "兵",
    "bK": "将",
    "bA": "士",
    "bB": "象",
    "bN": "马",
    "bR": "车",
    "bC": "炮",
    "bP": "卒",
}

XIANGQI_SYMBOLS = {
    "rK": "\U0001fa60",
    "rA": "\U0001fa61",
    "rB": "\U0001fa62",
    "rN": "\U0001fa63",
    "rR": "\U0001fa64",
    "rC": "\U0001fa65",
    "rP": "\U0001fa66",
    "bK": "\U0001fa67",
    "bA": "\U0001fa68",
    "bB": "\U0001fa69",
    "bN": "\U0001fa6a",
    "bR": "\U0001fa6b",
    "bC": "\U0001fa6c",
    "bP": "\U0001fa6d",
}


def _initial_xiangqi_board() -> list[list[str | None]]:
    board: list[list[str | None]] = [[None for _ in range(9)] for _ in range(10)]
    board[0] = ["bR", "bN", "bB", "bA", "bK", "bA", "bB", "bN", "bR"]
    board[2][1] = board[2][7] = "bC"
    for x in range(0, 9, 2):
        board[3][x] = "bP"
    board[9] = ["rR", "rN", "rB", "rA", "rK", "rA", "rB", "rN", "rR"]
    board[7][1] = board[7][7] = "rC"
    for x in range(0, 9, 2):
        board[6][x] = "rP"
    return board


def _render_xiangqi_board(board: list[list[str | None]]) -> str:
    lines = ["    a  b  c  d  e  f  g  h  i"]
    for y, row in enumerate(board):
        cells = [PIECES.get(piece, "·") if piece else "·" for piece in row]
        river = "    楚河      汉界" if y == 4 else ""
        lines.append(f"{y}  " + "  ".join(cells) + f"  {y}{river}")
    lines.append("    a  b  c  d  e  f  g  h  i")
    return "\n".join(lines)


def _coord_to_xy(coord: str) -> tuple[int, int] | None:
    coord = coord.lower().strip()
    if len(coord) != 2 or coord[0] not in FILES or not coord[1].isdigit():
        return None
    x = FILES.index(coord[0])
    y = int(coord[1])
    if not (0 <= y <= 9):
        return None
    return x, y


def _parse_xiangqi_move(raw: str) -> tuple[int, int, int, int] | None:
    raw = raw.lower().replace("-", "").replace(" ", "")
    if len(raw) != 4:
        return None
    start = _coord_to_xy(raw[:2])
    end = _coord_to_xy(raw[2:])
    if start is None or end is None:
        return None
    return start[0], start[1], end[0], end[1]


def _in_palace(side: str, x: int, y: int) -> bool:
    if x < 3 or x > 5:
        return False
    return 7 <= y <= 9 if side == "red" else 0 <= y <= 2


def _piece_side(piece: str) -> str:
    return "red" if piece.startswith("r") else "black"


def _path_clear(board: list[list[str | None]], x1: int, y1: int, x2: int, y2: int) -> bool:
    if x1 == x2:
        step = 1 if y2 > y1 else -1
        return all(board[y][x1] is None for y in range(y1 + step, y2, step))
    if y1 == y2:
        step = 1 if x2 > x1 else -1
        return all(board[y1][x] is None for x in range(x1 + step, x2, step))
    return False


def _path_count(board: list[list[str | None]], x1: int, y1: int, x2: int, y2: int) -> int:
    if x1 == x2:
        step = 1 if y2 > y1 else -1
        return sum(1 for y in range(y1 + step, y2, step) if board[y][x1] is not None)
    if y1 == y2:
        step = 1 if x2 > x1 else -1
        return sum(1 for x in range(x1 + step, x2, step) if board[y1][x] is not None)
    return 99


def _generals_face(board: list[list[str | None]]) -> bool:
    red = black = None
    for y in range(10):
        for x in range(9):
            if board[y][x] == "rK":
                red = (x, y)
            elif board[y][x] == "bK":
                black = (x, y)
    if red is None or black is None or red[0] != black[0]:
        return False
    return _path_clear(board, red[0], red[1], black[0], black[1])


def _is_basic_xiangqi_legal(board: list[list[str | None]], side: str, move: tuple[int, int, int, int]) -> bool:
    x1, y1, x2, y2 = move
    if not (0 <= x2 <= 8 and 0 <= y2 <= 9):
        return False
    piece = board[y1][x1]
    target = board[y2][x2]
    if piece is None or _piece_side(piece) != side:
        return False
    if target is not None and _piece_side(target) == side:
        return False

    kind = piece[1]
    dx, dy = x2 - x1, y2 - y1
    adx, ady = abs(dx), abs(dy)
    ok = False

    if kind == "K":
        ok = adx + ady == 1 and _in_palace(side, x2, y2)
        if target and target[1] == "K" and x1 == x2 and _path_clear(board, x1, y1, x2, y2):
            ok = True
    elif kind == "A":
        ok = adx == 1 and ady == 1 and _in_palace(side, x2, y2)
    elif kind == "B":
        eye_x, eye_y = x1 + dx // 2, y1 + dy // 2
        river_ok = y2 >= 5 if side == "red" else y2 <= 4
        ok = adx == 2 and ady == 2 and river_ok and board[eye_y][eye_x] is None
    elif kind == "N":
        if (adx, ady) in {(1, 2), (2, 1)}:
            leg_x = x1 if ady == 2 else x1 + dx // 2
            leg_y = y1 + dy // 2 if ady == 2 else y1
            ok = board[leg_y][leg_x] is None
    elif kind == "R":
        ok = (x1 == x2 or y1 == y2) and _path_clear(board, x1, y1, x2, y2)
    elif kind == "C":
        count = _path_count(board, x1, y1, x2, y2)
        ok = count == 0 if target is None else count == 1
    elif kind == "P":
        forward = -1 if side == "red" else 1
        crossed = y1 <= 4 if side == "red" else y1 >= 5
        ok = (dx == 0 and dy == forward) or (crossed and adx == 1 and dy == 0)

    if not ok:
        return False

    new_board = [row[:] for row in board]
    new_board[y2][x2] = piece
    new_board[y1][x1] = None
    return not _generals_face(new_board)


def _xiangqi_legal_moves(board: list[list[str | None]], side: str) -> list[str]:
    moves: list[str] = []
    for y1 in range(10):
        for x1 in range(9):
            piece = board[y1][x1]
            if piece is None or _piece_side(piece) != side:
                continue
            for y2 in range(10):
                for x2 in range(9):
                    move = (x1, y1, x2, y2)
                    if _is_basic_xiangqi_legal(board, side, move):
                        moves.append(f"{FILES[x1]}{y1}{FILES[x2]}{y2}")
    return moves


def _apply_xiangqi_move(board: list[list[str | None]], move: tuple[int, int, int, int]) -> str | None:
    x1, y1, x2, y2 = move
    captured = board[y2][x2]
    board[y2][x2] = board[y1][x1]
    board[y1][x1] = None
    return captured


def _game_board_text(game: dict[str, Any]) -> str:
    if game["kind"] == "ichess":
        if pychess is None:
            return "python-chess 未安装。"
        return _render_ichess_board(pychess.Board(game["fen"]))
    return _render_xiangqi_board(game["board"])


def _legal_moves(game: dict[str, Any]) -> list[str]:
    if game["kind"] == "ichess":
        if pychess is None:
            return []
        return _ichess_legal_moves(pychess.Board(game["fen"]))
    return _xiangqi_legal_moves(game["board"], game["turn"])


def _format_game(game: dict[str, Any], prefix: str = "") -> str:
    current = _side_name(game["kind"], game["turn"])
    mode = "人机" if "bot" in game["players"].values() else "对战"
    board_text = _game_board_text(game)
    return f"{prefix}棋种：{game['label']} · {mode}\n轮到：{current}\n\n{board_text}"


def _last_move(game: dict[str, Any]) -> str:
    history = game.get("history") or []
    if not history:
        return ""
    return str(history[-1].get("move", ""))


def _draw_ichess_image_board(
    draw: ImageDraw.ImageDraw,
    board: Any,
    x: int,
    y: int,
    cell: int,
    last_move: str,
) -> None:
    assert pychess is not None
    coord_font = _pick_font(24, bold=True)
    piece_font = _pick_symbol_font(54)
    piece_symbols = {
        "P": "♙",
        "N": "♘",
        "B": "♗",
        "R": "♖",
        "Q": "♕",
        "K": "♔",
        "p": "♟",
        "n": "♞",
        "b": "♝",
        "r": "♜",
        "q": "♛",
        "k": "♚",
    }
    highlight: set[int] = set()
    if len(last_move) >= 4:
        try:
            highlight = {
                pychess.parse_square(last_move[:2]),
                pychess.parse_square(last_move[2:4]),
            }
        except ValueError:
            highlight = set()

    for rank in range(7, -1, -1):
        for file in range(8):
            square = pychess.square(file, rank)
            px = x + file * cell
            py = y + (7 - rank) * cell
            fill = LIGHT_SQUARE if (file + rank) % 2 else DARK_SQUARE
            draw.rectangle((px, py, px + cell, py + cell), fill=fill)
            if square in highlight:
                draw.rectangle((px + 4, py + 4, px + cell - 4, py + cell - 4), outline="#f2b94b", width=5)
            piece = board.piece_at(square)
            if piece:
                color = "#f9fbff" if piece.color == pychess.WHITE else "#172033"
                _draw_centered(
                    draw,
                    (px + cell / 2, py + cell / 2),
                    piece_symbols[piece.symbol()],
                    piece_font,
                    color,
                    stroke_width=2 if piece.color == pychess.WHITE else 0,
                    stroke_fill="#5a7190",
                )

    draw.rectangle((x, y, x + 8 * cell, y + 8 * cell), outline=ACCENT_COLOR, width=4)
    for file, label in enumerate("abcdefgh"):
        _draw_centered(draw, (x + file * cell + cell / 2, y - 24), label, coord_font, MUTED_COLOR)
        _draw_centered(draw, (x + file * cell + cell / 2, y + 8 * cell + 28), label, coord_font, MUTED_COLOR)
    for rank in range(8):
        label = str(8 - rank)
        _draw_centered(draw, (x - 24, y + rank * cell + cell / 2), label, coord_font, MUTED_COLOR)
        _draw_centered(draw, (x + 8 * cell + 24, y + rank * cell + cell / 2), label, coord_font, MUTED_COLOR)


def _draw_xiangqi_image_board(
    draw: ImageDraw.ImageDraw,
    board: list[list[str | None]],
    x: int,
    y: int,
    cell: int,
    last_move: str,
) -> None:
    coord_font = _pick_font(22, bold=True)
    piece_font = _pick_font(32, bold=True)
    xiangqi_font = _pick_xiangqi_font(58)
    river_font = _pick_font(34, bold=True)
    line_color = "#8a6b4f"
    highlight_points: set[tuple[int, int]] = set()
    parsed = _parse_xiangqi_move(last_move)
    if parsed:
        highlight_points = {(parsed[0], parsed[1]), (parsed[2], parsed[3])}

    draw.rectangle((x - 90, y - 90, x + 8 * cell + 90, y + 9 * cell + 90), fill="#fff8ed", outline=LINE_COLOR, width=4)
    for row in range(10):
        y0 = y + row * cell
        if row == 0 or row == 9:
            draw.line((x, y0, x + 8 * cell, y0), fill=line_color, width=3)
        else:
            draw.line((x, y0, x + 8 * cell, y0), fill=line_color, width=2)
    for col in range(9):
        x0 = x + col * cell
        if col in {0, 8}:
            draw.line((x0, y, x0, y + 9 * cell), fill=line_color, width=3)
        else:
            draw.line((x0, y, x0, y + 4 * cell), fill=line_color, width=2)
            draw.line((x0, y + 5 * cell, x0, y + 9 * cell), fill=line_color, width=2)
    draw.line((x + 3 * cell, y, x + 5 * cell, y + 2 * cell), fill=line_color, width=2)
    draw.line((x + 5 * cell, y, x + 3 * cell, y + 2 * cell), fill=line_color, width=2)
    draw.line((x + 3 * cell, y + 7 * cell, x + 5 * cell, y + 9 * cell), fill=line_color, width=2)
    draw.line((x + 5 * cell, y + 7 * cell, x + 3 * cell, y + 9 * cell), fill=line_color, width=2)
    _draw_centered(draw, (x + 2.2 * cell, y + 4.5 * cell), "楚河", river_font, "#b2824c")
    _draw_centered(draw, (x + 5.8 * cell, y + 4.5 * cell), "汉界", river_font, "#b2824c")

    for col, label in enumerate(FILES):
        _draw_centered(draw, (x + col * cell, y - 52), label, coord_font, MUTED_COLOR)
        _draw_centered(draw, (x + col * cell, y + 9 * cell + 52), label, coord_font, MUTED_COLOR)
    for row in range(10):
        _draw_centered(draw, (x - 62, y + row * cell), str(row), coord_font, MUTED_COLOR)
        _draw_centered(draw, (x + 8 * cell + 62, y + row * cell), str(row), coord_font, MUTED_COLOR)

    radius = int(cell * 0.38)
    for row in range(10):
        for col in range(9):
            piece = board[row][col]
            if not piece:
                continue
            cx = x + col * cell
            cy = y + row * cell
            if (col, row) in highlight_points:
                draw.ellipse((cx - radius - 7, cy - radius - 7, cx + radius + 7, cy + radius + 7), fill="#f5cd73")
            symbol = XIANGQI_SYMBOLS.get(piece)
            if symbol:
                _draw_centered(draw, (cx, cy), symbol, xiangqi_font, RED_SIDE if piece.startswith("r") else BLACK_SIDE)
            else:
                fill = "#fffdf8"
                outline = RED_SIDE if piece.startswith("r") else BLACK_SIDE
                draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=fill, outline=outline, width=4)
                _draw_centered(draw, (cx, cy), PIECES.get(piece, "?"), piece_font, outline)


def _render_game_image(
    game: dict[str, Any],
    title: str = "棋局",
    result_text: str = "",
    notices: list[RenderNotice] | None = None,
) -> bytes:
    notices = notices or []
    width = 1220 if game["kind"] == "xiangqi" else 1180
    board_x = 82
    board_y = 206
    board_cell = 72 if game["kind"] == "ichess" else 64
    board_width = board_cell * (8 if game["kind"] == "ichess" else 8)
    board_height = board_cell * (8 if game["kind"] == "ichess" else 9)
    side_x = board_x + board_width + (150 if game["kind"] == "xiangqi" else 92)
    history = game.get("history") or []
    shown_history = history[-8:]
    board_top = board_y if game["kind"] == "ichess" else board_y + 28
    footer_gap = 142 if game["kind"] == "ichess" else 224
    height = max(board_top + board_height + footer_gap, 330 + len(shown_history) * 54 + len(notices) * 56)
    image = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(image)

    title_font = _pick_font(46, bold=True)
    meta_font = _pick_font(25)
    meta_bold = _pick_font(25, bold=True)
    body_font = _pick_font(24)
    small_font = _pick_font(21)
    history_font = _pick_font(23)
    history_bold = _pick_font(23, bold=True)

    draw.rectangle((0, 0, width, 150), fill="#eaf2ff")
    draw.rectangle((0, 136, width, 144), fill=ACCENT_COLOR)
    draw.text((58, 36), title, font=title_font, fill=INK_COLOR)
    draw.text((60, 110), f"{game['label']} · {_mode_name(game)}", font=meta_bold, fill=ACCENT_COLOR)
    watermark = "制图：github@huanyan77777  来源：杜若汀的茶馆"
    watermark_width = _text_width(draw, watermark, small_font)
    draw.text((width - watermark_width - 44, 38), watermark, font=small_font, fill=MUTED_COLOR)

    current = _side_name(game["kind"], game["turn"])
    player_line = "  ".join(
        f"{_side_name(game['kind'], side)}：{_player_label(game, side)}"
        for side in game["players"]
    )
    info_y = 186
    draw.text((side_x, info_y), "当前状态", font=meta_bold, fill=INK_COLOR)
    info_y += 38
    draw.text((side_x, info_y), f"轮到：{current}", font=meta_font, fill=ACCENT_COLOR)
    info_y += 34
    info_y = _draw_wrapped(draw, player_line, side_x, info_y, width - side_x - 56, small_font, MUTED_COLOR, 4, 3)

    if result_text:
        info_y += 16
        info_y = _draw_wrapped(draw, result_text, side_x, info_y, width - side_x - 56, meta_bold, "#9a2d2d", 6, 3)
    for notice in notices:
        info_y += 12
        draw.text((side_x, info_y), notice.title, font=meta_bold, fill=ACCENT_COLOR)
        info_y = _draw_wrapped(draw, notice.text, side_x, info_y + 34, width - side_x - 56, body_font, INK_COLOR, 5, 2)

    last_move = _last_move(game)
    if game["kind"] == "ichess":
        board = pychess.Board(game["fen"])
        _draw_ichess_image_board(draw, board, board_x, board_y, board_cell, last_move)
    else:
        _draw_xiangqi_image_board(draw, game["board"], board_x + 10, board_y + 28, board_cell, last_move)

    history_y = max(info_y + 28, 330)
    draw.text((side_x, history_y), "历史步", font=meta_bold, fill=INK_COLOR)
    history_y += 42
    if not shown_history:
        draw.text((side_x, history_y), "暂无落子", font=history_font, fill=MUTED_COLOR)
    else:
        start_no = max(1, len(history) - len(shown_history) + 1)
        for no, item in enumerate(shown_history, start=start_no):
            side = str(item.get("side", ""))
            actor = str(item.get("actor", ""))
            actor_label = str(item.get("actor_name") or ("机器人" if actor == "bot" else _player_label(game, side)))
            move_text = str(item.get("display") or item.get("move") or "")
            comment = str(item.get("comment") or "").strip()
            line = f"{no:02d}. {_side_name(game['kind'], side)} {actor_label}：{move_text}"
            draw.text((side_x, history_y), line, font=history_bold, fill=INK_COLOR)
            history_y += 30
            if comment:
                history_y = _draw_wrapped(draw, comment, side_x + 30, history_y, width - side_x - 92, small_font, MUTED_COLOR, 4, 2)
            history_y += 14

    footer = "走 e2e4 / 走 Nf3 / 走 a9a8 · 棋盘 · 认输"
    draw.text((58, height - 48), footer, font=small_font, fill=MUTED_COLOR)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _bot_move_if_needed(game: dict[str, Any]) -> str:
    if game["players"].get(game["turn"]) != "bot":
        return ""

    legal = _legal_moves(game)
    if not legal:
        winner = _opponent_side(game["kind"], game["turn"])
        _delete_game(int(game["group_id"]))
        game["_result_text"] = _finish_text(game, winner, "无合法走法")
        return game["_result_text"]

    move_text, comment = await _ask_llm_for_move(game, legal, _game_board_text(game))
    if game["kind"] == "ichess":
        board = pychess.Board(game["fen"])
        move = pychess.Move.from_uci(move_text)
        san = board.san(move)
        old_turn = game["turn"]
        board.push(move)
        game["fen"] = board.fen()
        game["turn"] = "white" if board.turn == pychess.WHITE else "black"
        _append_history(game, old_turn, "bot", move_text, f"{san} ({move_text})", comment)
        done, winner, reason = _ichess_status(board)
        if done:
            _delete_game(int(game["group_id"]))
            game["_result_text"] = _finish_text(game, winner, reason)
            return game["_result_text"]
        _save_game(game)
        return f"机器人走：{san} ({move_text})"

    move = _parse_xiangqi_move(move_text)
    if move is None:
        move_text = random.choice(legal)
        move = _parse_xiangqi_move(move_text)
    assert move is not None
    captured = _apply_xiangqi_move(game["board"], move)
    old_turn = game["turn"]
    _append_history(game, old_turn, "bot", move_text, move_text, comment)
    if captured and captured[1] == "K":
        _delete_game(int(game["group_id"]))
        game["_result_text"] = _finish_text(game, old_turn, "吃将")
        return game["_result_text"]
    game["turn"] = _opponent_side(game["kind"], old_turn)
    if not _xiangqi_legal_moves(game["board"], game["turn"]):
        _delete_game(int(game["group_id"]))
        game["_result_text"] = _finish_text(game, old_turn, "无合法走法")
        return game["_result_text"]
    _save_game(game)
    return f"机器人走：{move_text}"


@help_cmd.handle()
async def handle_game_help(event: GroupMessageEvent) -> None:
    if not _ensure_enabled(event):
        await help_cmd.finish("这个群已经关闭了棋局功能。")
    await help_cmd.finish(
        "棋局玩法：\n"
        "国际象棋人机 [执白/执黑]\n"
        "国际象棋对战 @对手\n"
        "象棋人机 [执红/执黑]\n"
        "象棋对战 @对手\n"
        "走 e2e4 / 走 Nf3 / 走 a0a1\n"
        "棋盘 / 棋局提示 / 认输 / 结束棋局\n\n"
        "国际象棋可用 UCI 或 SAN。象棋使用 a-i、0-9 坐标，棋盘上方会显示坐标。"
    )


@start_cmd.handle()
async def handle_start(event: GroupMessageEvent) -> None:
    if not _ensure_enabled(event):
        await start_cmd.finish("这个群已经关闭了棋局功能。")

    text = event.get_plaintext().strip()
    match = re.match(r"^(国际象棋|象棋)(人机|对战)(?:\s+.*)?$", text)
    if not match:
        await start_cmd.finish()

    game_name, mode = match.group(1), match.group(2)
    if _load_game(event.group_id):
        await start_cmd.finish("本群已经有一盘棋了。发送「结束棋局」可以结束当前棋局。")

    if game_name == "国际象棋" and pychess is None:
        await start_cmd.finish("国际象棋依赖 python-chess 还没安装，先用象棋吧。")

    kind = "ichess" if game_name == "国际象棋" else "xiangqi"
    first_side = "white" if kind == "ichess" else "red"
    second_side = "black"
    user_side = first_side
    if ("执黑" in text) or ("黑方" in text):
        user_side = second_side

    players: dict[str, Any]
    if mode == "人机":
        players = {
            user_side: event.user_id,
            _opponent_side(kind, user_side): "bot",
        }
    else:
        ats = [user_id for user_id in _at_users(event) if user_id != event.self_id]
        if not ats:
            await start_cmd.finish("玩家对战需要 @ 对手，例如：国际象棋对战 @对手")
        players = {
            first_side: event.user_id,
            second_side: ats[0],
        }

    game: dict[str, Any] = {
        "group_id": event.group_id,
        "kind": kind,
        "label": game_name,
        "players": players,
        "player_names": {str(event.user_id): _event_display_name(event)},
        "turn": first_side,
        "moves": [],
    }
    for user_id in players.values():
        if isinstance(user_id, int):
            game["player_names"].setdefault(str(user_id), _fallback_player_name(user_id))
    if kind == "ichess":
        game["fen"] = pychess.Board().fen()
    else:
        game["board"] = _initial_xiangqi_board()

    _save_game(game)
    await _bot_move_if_needed(game)
    await start_cmd.finish(MessageSegment.image(_render_game_image(game, "棋局开始", game.get("_result_text", ""))))


@board_cmd.handle()
async def handle_board(event: GroupMessageEvent) -> None:
    if not _ensure_enabled(event):
        await board_cmd.finish("这个群已经关闭了棋局功能。")
    game = _load_game(event.group_id)
    if not game:
        await board_cmd.finish("本群现在没有棋局。发送「棋局帮助」查看玩法。")
    await board_cmd.finish(MessageSegment.image(_render_game_image(game, "当前棋盘")))


@hint_cmd.handle()
async def handle_hint(event: GroupMessageEvent) -> None:
    if not _ensure_enabled(event):
        await hint_cmd.finish("这个群已经关闭了棋局功能。")
    game = _load_game(event.group_id)
    if not game:
        await hint_cmd.finish("本群现在没有棋局。")
    legal = _legal_moves(game)
    if not legal:
        await hint_cmd.finish("当前没有合法走法。")
    sample = "、".join(legal[:20])
    await hint_cmd.finish(f"当前合法走法示例：{sample}")


@resign_cmd.handle()
async def handle_resign(event: GroupMessageEvent) -> None:
    if not _ensure_enabled(event):
        await resign_cmd.finish("这个群已经关闭了棋局功能。")
    game = _load_game(event.group_id)
    if not game:
        await resign_cmd.finish("本群现在没有棋局。")
    if event.get_plaintext().strip() == "结束棋局":
        _delete_game(event.group_id)
        await resign_cmd.finish("棋局已结束。")
    player_id = _current_player_id(game)
    if player_id is not None and int(event.user_id) != player_id:
        await resign_cmd.finish("现在不是你走，不能替别人认输。")
    winner = _opponent_side(game["kind"], game["turn"])
    _delete_game(event.group_id)
    await resign_cmd.finish(MessageSegment.image(_render_game_image(game, "棋局结束", _finish_text(game, winner, "认输"))))


@move_cmd.handle()
async def handle_move(event: GroupMessageEvent) -> None:
    if not _ensure_enabled(event):
        await move_cmd.finish("这个群已经关闭了棋局功能。")
    game = _load_game(event.group_id)
    if not game:
        await move_cmd.finish("本群现在没有棋局。发送「棋局帮助」查看玩法。")

    player_id = _current_player_id(game)
    if player_id is None:
        await _bot_move_if_needed(game)
        await move_cmd.finish(MessageSegment.image(_render_game_image(game, "机器人回合", game.get("_result_text", ""))))
    if int(event.user_id) != player_id:
        await move_cmd.finish(f"现在轮到{_side_name(game['kind'], game['turn'])}。")
    player_name = _event_display_name(event)
    _remember_player_name(game, int(event.user_id), player_name)

    raw = event.get_plaintext().strip()[1:].strip()
    if game["kind"] == "ichess":
        if pychess is None:
            await move_cmd.finish("国际象棋依赖 python-chess 还没安装。")
        board = pychess.Board(game["fen"])
        move = _parse_ichess_move(board, raw)
        if move is None:
            await move_cmd.finish("这个走法不合法。示例：走 e2e4，或走 Nf3。")
        san = board.san(move)
        old_turn = game["turn"]
        board.push(move)
        game["fen"] = board.fen()
        game["moves"].append(raw)
        game["turn"] = "white" if board.turn == pychess.WHITE else "black"
        _append_history(game, old_turn, str(event.user_id), move.uci(), f"{san} ({move.uci()})", actor_name=player_name)
        done, winner, reason = _ichess_status(board)
        if done:
            _delete_game(event.group_id)
            await move_cmd.finish(
                MessageSegment.image(_render_game_image(game, "棋局结束", _finish_text(game, winner, reason)))
            )
        _save_game(game)
        await _bot_move_if_needed(game)
        await move_cmd.finish(MessageSegment.image(_render_game_image(game, "本回合完成", game.get("_result_text", ""))))

    parsed = _parse_xiangqi_move(raw)
    if parsed is None or not _is_basic_xiangqi_legal(game["board"], game["turn"], parsed):
        await move_cmd.finish("这个走法不合法。象棋示例：走 a9a8，坐标看「棋盘」。")

    old_turn = game["turn"]
    captured = _apply_xiangqi_move(game["board"], parsed)
    move_text = f"{FILES[parsed[0]]}{parsed[1]}{FILES[parsed[2]]}{parsed[3]}"
    game["moves"].append(move_text)
    _append_history(game, old_turn, str(event.user_id), move_text, move_text, actor_name=player_name)
    if captured and captured[1] == "K":
        _delete_game(event.group_id)
        await move_cmd.finish(
            MessageSegment.image(_render_game_image(game, "棋局结束", _finish_text(game, old_turn, "吃将")))
        )
    game["turn"] = _opponent_side(game["kind"], old_turn)
    if not _xiangqi_legal_moves(game["board"], game["turn"]):
        _delete_game(event.group_id)
        await move_cmd.finish(
            MessageSegment.image(_render_game_image(game, "棋局结束", _finish_text(game, old_turn, "无合法走法")))
        )
    _save_game(game)

    await _bot_move_if_needed(game)
    await move_cmd.finish(MessageSegment.image(_render_game_image(game, "本回合完成", game.get("_result_text", ""))))
