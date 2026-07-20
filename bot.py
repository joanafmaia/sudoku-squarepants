"""Discord Sudoku bot — real 9x9 puzzles with interactive panel."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont
from sudoku import Sudoku

from challenge_store import create_match_store, match_player_entries, new_match_document

DATA_FILE = Path(__file__).with_name("leaderboard.json")
VIEW_TIMEOUT = 20 * 60
DEFAULT_DIFFICULTY = "medium"

# key → weight (py-sudoku), display label, coin multiplier on win
DIFFICULTY_TIERS: dict[str, dict] = {
    "very_easy": {"label": "Very Easy", "weight": 0.25, "multiplier": 0.50},
    "easy": {"label": "Easy", "weight": 0.40, "multiplier": 0.75},
    "medium": {"label": "Medium", "weight": 0.55, "multiplier": 1.00},
    "hard": {"label": "Hard", "weight": 0.70, "multiplier": 1.50},
    "very_hard": {"label": "Very Hard", "weight": 0.80, "multiplier": 2.00},
    "expertttt": {"label": "Expertttt", "weight": 0.88, "multiplier": 3.00},
}

DIFFICULTY_CHOICES = [
    app_commands.Choice(name=meta["label"], value=key)
    for key, meta in DIFFICULTY_TIERS.items()
]

BASE_WIN_REWARD = 80
DAILY_BONUS = 75
STREAK_BONUS_PER = 10
HINT_PENALTY = 10  # coins lost per hint on win (min reward floor)
CHALLENGE_WIN_MULT = 2.0  # extra multiplier for speedrun winners
MAX_CHALLENGE_PLAYERS = 5  # challenger + up to 4 opponents
CHALLENGE_LOSER_COINS = 25
CHALLENGE_COOLDOWN_SEC = 60
INVITE_TIMEOUT_SEC = 5 * 60
DAILY_EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc).date()
DAILY_ANNOUNCE_CHANNEL_ID = int(os.getenv("DAILY_ANNOUNCE_CHANNEL_ID", "0") or 0)

# Fixed weekly difficulty for /daily (Monday=0 … Sunday=6)
DAILY_WEEKDAY_DIFFICULTY = {
    0: "very_easy",   # Monday
    1: "easy",        # Tuesday
    2: "medium",      # Wednesday
    3: "hard",        # Thursday
    4: "very_hard",   # Friday
    5: "expertttt",   # Saturday
    6: "expertttt",   # Sunday
}

# Discord embed palette — Paper & Pencil (no blue / rainbow themes)
COLOR_PAPER = discord.Color.from_str("#F4F1EA")
COLOR_PAPER_WHITE = discord.Color.from_str("#FEFEFE")
COLOR_DANGER = discord.Color.from_str("#B91C1C")  # forfeit / hard errors only

# Paper & pencil theme (light canvas)
RGB_BG = "#FFFFFF"                # solid white canvas
RGB_EMPTY = "#FFFFFF"             # clean white cells
RGB_GIVEN_CELL = "#F8F6F2"        # subtle clue wash
RGB_SELECT = "#E8ECF2"            # soft slate selection
RGB_BOX_HL = "#F4F1EA"            # quiet box highlight (paper cream)
RGB_CONFLICT = "#F8DCDC"          # only allowed accent: soft red
RGB_LINE = "#000000"              # sharp black grid (thin)
RGB_THICK = "#000000"             # sharp black 3×3 borders
RGB_TEXT = "#2C2C2C"              # dark graphite (player ink)
RGB_TEXT_GIVEN = "#000000"        # bold black clues
RGB_TEXT_CONFLICT = "#A02828"     # ink on conflict cells
RGB_PENCIL = "#96948E"            # soft graphite drafts
RGB_HEADER = "#464440"
RGB_OUTLINE = "#000000"

# Fixed Discord attachment canvas — never change size between stages
BOARD_CANVAS = 600
BOARD_HEADER_H = 44

COLS = "ABCDEFGHI"

SHOP_TITLES = {
    "rookie": {"label": "Rookie", "cost": 50},
    "solver": {"label": "Solver", "cost": 150},
    "row_master": {"label": "Row Master", "cost": 300},
    "sudoku_pro": {"label": "Sudoku Pro", "cost": 500},
    "legend": {"label": "Legend", "cost": 1000},
}

SHOP_CONSUMABLES = {
    "hint": {
        "label": "Hint",
        "cost": 40,
        "desc": "Fill one empty cell with the correct digit",
    },
}

intents = discord.Intents.default()
games: dict = {}
pending_challenges: dict[int, dict] = {}  # invite message_id → meta
challenge_cooldowns: dict[int, float] = {}  # user_id → last /challenge timestamp
match_store = create_match_store()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_data() -> dict:
    if DATA_FILE.exists():
        with DATA_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def guild_stats(data: dict, guild_id: int) -> dict:
    key = str(guild_id)
    if key not in data:
        data[key] = {}
    return data[key]


def user_stats(gstats: dict, user_id: int) -> dict:
    key = str(user_id)
    if key not in gstats:
        gstats[key] = {}
    s = gstats[key]
    s.setdefault("coins", 0)
    s.setdefault("wins", 0)
    s.setdefault("losses", 0)
    s.setdefault("games", 0)
    s.setdefault("best_time", None)
    s.setdefault("streak", 0)
    s.setdefault("best_streak", 0)
    s.setdefault("name", "Unknown")
    s.setdefault("title", None)
    s.setdefault("owned_titles", [])
    s.setdefault("hints", 0)
    s.setdefault("daily_wins", 0)
    s.setdefault("challenge_wins", 0)
    return s


def serialize_game_key(key: tuple) -> str:
    if isinstance(key, tuple) and len(key) >= 3 and key[0] == "ch":
        return f"c:{key[1]}:{key[2]}"
    return f"s:{key[0]}:{key[1]}"


def deserialize_game_key(raw: str) -> tuple | None:
    try:
        kind, rest = raw.split(":", 1)
        if kind == "c":
            match_id, uid = rest.rsplit(":", 1)
            return ("ch", match_id, int(uid))
        if kind == "s":
            guild_id, uid = rest.split(":", 1)
            return (int(guild_id), int(uid))
    except (TypeError, ValueError):
        return None
    return None


async def persist_game(key: tuple, game: dict) -> None:
    """Save live session so a bot restart can restore the board."""
    snapshot = deepcopy_game(game)
    payload = {
        "_id": serialize_game_key(key),
        "game_key": serialize_game_key(key),
        "game": snapshot,
    }
    try:
        await match_store.upsert_active_game(payload)
    except Exception as exc:  # noqa: BLE001 — persistence must not break play
        print(f"persist_game failed: {exc}")


async def drop_persisted_game(key: tuple) -> None:
    try:
        await match_store.delete_active_game(serialize_game_key(key))
    except Exception as exc:  # noqa: BLE001
        print(f"drop_persisted_game failed: {exc}")


async def remove_game(key: tuple) -> dict | None:
    game = games.pop(key, None)
    await drop_persisted_game(key)
    return game


def deepcopy_game(game: dict) -> dict:
    """JSON-safe clone of a live game dict."""
    out = {}
    for k, v in game.items():
        if k == "participants":
            out[k] = list(v) if v else []
        elif k in ("board", "solution"):
            out[k] = copy_grid(v)
        elif k == "given":
            out[k] = [row[:] for row in v]
        else:
            out[k] = v
    return out


def iter_players(gstats: dict):
    for key, value in gstats.items():
        if key.startswith("_") or not isinstance(value, dict) or not key.isdigit():
            continue
        yield key, value


def display_name(stats: dict) -> str:
    name = stats.get("name", "Unknown")
    tid = stats.get("title")
    if tid and tid in SHOP_TITLES:
        return f"{name} · {SHOP_TITLES[tid]['label']}"
    return name


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Sudoku logic
# ---------------------------------------------------------------------------

def solo_key(guild_id: int, user_id: int) -> tuple[int, int]:
    return (guild_id, user_id)


def copy_grid(grid: list) -> list:
    """Deep-copy a board of cell dicts or a plain int grid."""
    if not grid:
        return []
    if isinstance(grid[0][0], dict):
        return [
            [{"value": cell.get("value", 0), "pencil_marks": list(cell.get("pencil_marks") or [])} for cell in row]
            for row in grid
        ]
    return [row[:] for row in grid]


def make_cell(value: int = 0, pencil_marks: list[int] | None = None) -> dict:
    return {"value": int(value), "pencil_marks": list(pencil_marks or [])}


def normalize_board(board: list) -> list[list[dict]]:
    """Accept legacy int grids or cell-dict grids."""
    out: list[list[dict]] = []
    for row in board:
        new_row: list[dict] = []
        for cell in row:
            if isinstance(cell, dict):
                new_row.append(
                    make_cell(cell.get("value", 0), cell.get("pencil_marks") or [])
                )
            else:
                new_row.append(make_cell(0 if cell is None else int(cell)))
        out.append(new_row)
    return out


def cell_value(board: list[list[dict]], r: int, c: int) -> int:
    return int(board[r][c].get("value", 0))


def set_cell_value(board: list[list[dict]], r: int, c: int, value: int) -> None:
    board[r][c]["value"] = int(value)
    if value:
        board[r][c]["pencil_marks"] = []


def toggle_pencil(board: list[list[dict]], r: int, c: int, digit: int) -> list[int]:
    marks = list(board[r][c].get("pencil_marks") or [])
    if digit in marks:
        marks.remove(digit)
    else:
        marks.append(digit)
        marks.sort()
    board[r][c]["pencil_marks"] = marks
    return marks


def values_grid(board: list[list[dict]]) -> list[list[int]]:
    return [[cell_value(board, r, c) for c in range(9)] for r in range(9)]


def difficulty_weight(key: str) -> float:
    return float(DIFFICULTY_TIERS.get(key, DIFFICULTY_TIERS[DEFAULT_DIFFICULTY])["weight"])


def difficulty_label(key: str | None) -> str:
    if not key:
        return DIFFICULTY_TIERS[DEFAULT_DIFFICULTY]["label"]
    if key in DIFFICULTY_TIERS:
        return DIFFICULTY_TIERS[key]["label"]
    # Already a display label, or unknown → pass through / default
    for meta in DIFFICULTY_TIERS.values():
        if meta["label"] == key:
            return key
    return DIFFICULTY_TIERS[DEFAULT_DIFFICULTY]["label"]


def difficulty_key_from_label(label: str) -> str:
    for key, meta in DIFFICULTY_TIERS.items():
        if meta["label"] == label or key == label:
            return key
    return DEFAULT_DIFFICULTY


def difficulty_multiplier(difficulty: str | None) -> float:
    key = difficulty_key_from_label(difficulty or DEFAULT_DIFFICULTY)
    return float(DIFFICULTY_TIERS[key]["multiplier"])


def make_puzzle(difficulty: float | str = DEFAULT_DIFFICULTY, seed: int | None = None) -> tuple[list[list[dict]], list[list[bool]], list[list[int]]]:
    if isinstance(difficulty, str):
        weight = difficulty_weight(difficulty)
    else:
        weight = float(difficulty)
    if seed is not None:
        random.seed(seed)
    puzzle = Sudoku(3).difficulty(weight)
    solved = puzzle.solve()
    if solved is None:
        if seed is not None:
            random.seed()
        return make_puzzle(difficulty, seed=None)

    board = [
        [make_cell(0 if cell is None else int(cell)) for cell in row]
        for row in puzzle.board
    ]
    given = [[cell["value"] != 0 for cell in row] for row in board]
    solution = [[int(cell) for cell in row] for row in solved.board]
    if seed is not None:
        random.seed()
    return board, given, solution


def daily_difficulty_for_date(day: str) -> str:
    """Map YYYY-MM-DD weekday → fixed daily difficulty key."""
    d = datetime.fromisoformat(day).date()
    return DAILY_WEEKDAY_DIFFICULTY[d.weekday()]


def make_daily_puzzle(guild_id: int, day: str) -> tuple[list[list[dict]], list[list[bool]], list[list[int]], str]:
    """Same calendar date → same seed/grid for all players; difficulty from weekday schedule."""
    _ = guild_id  # reserved for future per-guild variants; seed is date-only for shared grids
    diff_key = daily_difficulty_for_date(day)
    seed = int(hashlib.sha256(f"sudoku9x9:daily:{day}".encode()).hexdigest()[:16], 16)
    board, given, solution = make_puzzle(difficulty=diff_key, seed=seed)
    return board, given, solution, diff_key


def get_guild_daily(data: dict, guild_id: int) -> dict:
    gstats = guild_stats(data, guild_id)
    meta = gstats.setdefault("_daily", {})
    day = utc_today()
    expected_diff = daily_difficulty_for_date(day)
    needs_regen = (
        meta.get("date") != day
        or "board" not in meta
        or meta.get("difficulty") != difficulty_label(expected_diff)
    )
    if needs_regen:
        board, given, solution, diff_key = make_daily_puzzle(guild_id, day)
        meta["date"] = day
        meta["board"] = board
        meta["given"] = given
        meta["solution"] = solution
        meta["difficulty"] = difficulty_label(diff_key)
        meta["difficulty_key"] = diff_key
        meta["results"] = {}
        save_data(data)
    else:
        meta["board"] = normalize_board(meta["board"])
        meta.setdefault("difficulty_key", daily_difficulty_for_date(day))
        meta.setdefault("difficulty", difficulty_label(meta["difficulty_key"]))
    return meta


def peers(r: int, c: int) -> list[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    for i in range(9):
        cells.add((r, i))
        cells.add((i, c))
    br, bc = 3 * (r // 3), 3 * (c // 3)
    for i in range(br, br + 3):
        for j in range(bc, bc + 3):
            cells.add((i, j))
    cells.discard((r, c))
    return list(cells)


def find_conflicts(board: list[list[dict]]) -> set[tuple[int, int]]:
    bad: set[tuple[int, int]] = set()
    for r in range(9):
        for c in range(9):
            val = cell_value(board, r, c)
            if val == 0:
                continue
            for pr, pc in peers(r, c):
                if cell_value(board, pr, pc) == val:
                    bad.add((r, c))
                    bad.add((pr, pc))
    return bad


def is_complete(board: list[list[dict]], solution: list[list[int]]) -> bool:
    return values_grid(board) == solution


def empty_cells(board: list[list[dict]]) -> list[tuple[int, int]]:
    return [(r, c) for r in range(9) for c in range(9) if cell_value(board, r, c) == 0]


def filled_count(board: list[list[dict]]) -> int:
    return sum(1 for r in range(9) for c in range(9) if cell_value(board, r, c) != 0)


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def win_reward(
    streak: int,
    *,
    daily: bool,
    hints_used: int,
    difficulty: str | None = None,
    challenge_winner: bool = False,
) -> int:
    coins = BASE_WIN_REWARD + max(0, streak - 1) * STREAK_BONUS_PER
    if daily:
        coins += DAILY_BONUS
    coins = int(round(coins * difficulty_multiplier(difficulty)))
    if challenge_winner:
        coins = int(round(coins * CHALLENGE_WIN_MULT))
    coins -= hints_used * HINT_PENALTY
    return max(20, coins)


# ---------------------------------------------------------------------------
# Board image
# ---------------------------------------------------------------------------

def cell_label(r: int, c: int) -> str:
    return f"{COLS[c]}{r + 1}"


def parse_cell(raw: str) -> tuple[int, int] | None:
    """Parse coordinates like A5, a5, 5A into (row, col) 0-based."""
    text = raw.strip().upper().replace(" ", "").replace(",", "").replace("-", "")
    if len(text) < 2 or len(text) > 3:
        return None

    # Letter then number: A5, A10 invalid
    if text[0] in COLS and text[1:].isdigit():
        c = COLS.index(text[0])
        r = int(text[1:]) - 1
        if 0 <= r <= 8:
            return r, c

    # Number then letter: 5A
    if text[-1] in COLS and text[:-1].isdigit():
        c = COLS.index(text[-1])
        r = int(text[:-1]) - 1
        if 0 <= r <= 8:
            return r, c

    return None


def board_font(size: int = 22, *, bold: bool = False) -> ImageFont.ImageFont:
    """Regular = light pen/pencil; bold = printed clues."""
    if bold:
        candidates = (
            Path("C:/Windows/Fonts/arialbd.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("C:/Windows/Fonts/arial.ttf"),
        )
    else:
        candidates = (
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("C:/Windows/Fonts/segoeui.ttf"),
        )
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def render_board(
    board: list[list[dict]],
    given: list[list[bool]],
    *,
    solution: list[list[int]] | None = None,
    selected: tuple[int, int] | None = None,
    conflicts: set[tuple[int, int]] | None = None,
    highlight_box: int | None = None,
    difficulty: str | None = None,
) -> BytesIO:
    """Paper theme: only RED marks rule conflicts. No solution-matching colors."""
    _ = solution  # intentionally unused — no green/yellow solution hints
    conflicts = conflicts or set()
    canvas = BOARD_CANVAS
    header_h = BOARD_HEADER_H
    area = canvas - header_h
    cell = area // 9
    grid = cell * 9
    origin_x = (canvas - grid) // 2
    origin_y = header_h + (area - grid) // 2

    img = Image.new("RGB", (canvas, canvas), RGB_BG)  # light theme: #FFFFFF
    draw = ImageDraw.Draw(img)
    font_player = board_font(max(22, cell * 26 // 48), bold=False)
    font_given = board_font(max(22, cell * 26 // 48), bold=True)
    header_font = board_font(18, bold=False)
    # Smaller, lighter face for draft marks
    pencil_font = board_font(max(9, cell * 11 // 48), bold=False)

    tier_name = difficulty_label(difficulty)
    hb = draw.textbbox((0, 0), tier_name, font=header_font)
    htw, hth = hb[2] - hb[0], hb[3] - hb[1]
    draw.text(
        ((canvas - htw) / 2, (header_h - hth) / 2 - 1),
        tier_name,
        fill=RGB_HEADER,
        font=header_font,
    )
    draw.line((0, header_h - 1, canvas, header_h - 1), fill=RGB_LINE, width=1)

    box_cells: set[tuple[int, int]] = set()
    if highlight_box is not None:
        br, bc = highlight_box // 3, highlight_box % 3
        for i in range(3):
            for j in range(3):
                box_cells.add((br * 3 + i, bc * 3 + j))

    for r in range(9):
        for c in range(9):
            x0 = origin_x + c * cell
            y0 = origin_y + r * cell
            x1, y1 = x0 + cell, y0 + cell
            val = cell_value(board, r, c)
            marks = list(board[r][c].get("pencil_marks") or [])

            # Background: paper white; red ONLY on conflicts; quiet selection/box tints
            if (r, c) in conflicts:
                fill = RGB_CONFLICT
            elif selected == (r, c):
                fill = RGB_SELECT
            elif given[r][c]:
                fill = RGB_GIVEN_CELL
            elif (r, c) in box_cells:
                fill = RGB_BOX_HL
            else:
                fill = RGB_EMPTY

            draw.rectangle((x0, y0, x1, y1), fill=fill)

            if val:
                text = str(val)
                if (r, c) in conflicts:
                    color = RGB_TEXT_CONFLICT
                    font = font_player
                elif given[r][c]:
                    color = RGB_TEXT_GIVEN
                    font = font_given
                else:
                    color = RGB_TEXT
                    font = font_player
                bbox = draw.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                draw.text(
                    (x0 + (cell - tw) / 2, y0 + (cell - th) / 2 - 1),
                    text,
                    fill=color,
                    font=font,
                )
            elif marks:
                # Neat mini-3×3 graphite drafts (handwriting-style notes)
                inset = max(2, cell // 16)
                inner = cell - 2 * inset
                slot_w = inner / 3
                slot_h = inner / 3
                for n in range(1, 10):
                    if n not in marks:
                        continue
                    ni = n - 1
                    cx = x0 + inset + (ni % 3) * slot_w + slot_w / 2
                    cy = y0 + inset + (ni // 3) * slot_h + slot_h / 2
                    t = str(n)
                    bbox = draw.textbbox((0, 0), t, font=pencil_font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    draw.text(
                        (cx - tw / 2, cy - th / 2 - 1),
                        t,
                        fill=RGB_PENCIL,
                        font=pencil_font,
                    )

    for i in range(10):
        width_line = 3 if i % 3 == 0 else 1
        color = RGB_THICK if i % 3 == 0 else RGB_LINE
        pos_y = origin_y + i * cell
        pos_x = origin_x + i * cell
        draw.line((origin_x, pos_y, origin_x + grid, pos_y), fill=color, width=width_line)
        draw.line((pos_x, origin_y, pos_x, origin_y + grid), fill=color, width=width_line)

    if highlight_box is not None and selected is None:
        br, bc = highlight_box // 3, highlight_box % 3
        x0 = origin_x + bc * 3 * cell
        y0 = origin_y + br * 3 * cell
        draw.rectangle(
            (x0, y0, x0 + 3 * cell, y0 + 3 * cell),
            outline=RGB_OUTLINE,
            width=3,
        )

    if selected is not None:
        r, c = selected
        x0 = origin_x + c * cell
        y0 = origin_y + r * cell
        draw.rectangle((x0, y0, x0 + cell, y0 + cell), outline=RGB_OUTLINE, width=2)

    out = BytesIO()
    img.save(out, format="PNG", compress_level=1)
    out.seek(0)
    return out


def attach_board(embed: discord.Embed, image: BytesIO) -> discord.File:
    """Attach PNG from an in-memory buffer (never writes board.png to disk)."""
    image.seek(0)
    embed.set_image(url="attachment://sudoku.png")
    # discord.File will read from the buffer; wrap a fresh BytesIO so seek is safe
    return discord.File(fp=BytesIO(image.read()), filename="sudoku.png")


# ---------------------------------------------------------------------------
# Game helpers
# ---------------------------------------------------------------------------

STAGE_BOX = "box"
STAGE_CELL = "cell"
STAGE_NUMBER = "number"

# Stage 1 — directional arrows (index 0–8 = boxes 1–9), not Sudoku digits
BOX_ARROW_LABELS = (
    "↖️", "⬆️", "↗️",
    "⬅️", "🔄", "➡️",
    "↙️", "⬇️", "↘️",
)


def box_origin(box_id: int) -> tuple[int, int]:
    br, bc = box_id // 3, box_id % 3
    return br * 3, bc * 3


def cell_in_box(box_id: int, index: int) -> tuple[int, int]:
    fr, fc = box_origin(box_id)
    return fr + index // 3, fc + index % 3


def new_game_state(
    *,
    mode: str,
    board: list[list[dict]],
    given: list[list[bool]],
    solution: list[list[int]],
    owner_id: int,
    channel_id: int,
    daily_date: str | None = None,
    difficulty: str = DEFAULT_DIFFICULTY,
    guild_id: int | None = None,
    match_id: str | None = None,
    player_slot: str | None = None,
    started_at: float | None = None,
) -> dict:
    # Persist the human-readable tier name (e.g. "Expertttt")
    tier_name = difficulty_label(difficulty)
    return {
        "mode": mode,
        "board": normalize_board(board),
        "given": [row[:] for row in given],
        "solution": copy_grid(solution),
        "difficulty": tier_name,
        "ui_stage": STAGE_BOX,
        "box_id": 0,
        "sel_r": 0,
        "sel_c": 0,
        "pencil_mode": False,
        "owner_id": owner_id,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "match_id": match_id,
        "player_slot": player_slot,
        "participants": {owner_id},
        "started_at": time.time() if started_at is None else float(started_at),
        "hints_used": 0,
        "daily_date": daily_date,
        "message_id": None,
    }


def challenge_game_key(match_id: str, user_id: int) -> tuple:
    return ("ch", match_id, user_id)


def find_challenge_game_for_user(user_id: int) -> tuple | None:
    for key, game in games.items():
        if game.get("mode") == "challenge" and game.get("owner_id") == user_id:
            return key
    return None


def paper_embed(title: str, *, description: str | None = None) -> discord.Embed:
    """Standard Paper & Pencil embed shell."""
    embed = discord.Embed(title=title, color=COLOR_PAPER)
    if description:
        embed.description = description
    return embed


def selected_cell(game: dict) -> tuple[int, int]:
    return game["sel_r"], game["sel_c"]


def build_embed(game: dict, *, status: str | None = None) -> discord.Embed:
    """Title only + board image — Paper theme."""
    _ = status
    mode = game["mode"]
    if mode == "daily":
        title = f"Daily · {game.get('daily_date', utc_today())}"
    elif mode == "challenge":
        title = "Challenge"
    else:
        title = "Sudoku"
    embed = paper_embed(f"✏️ {title}")
    if game.get("difficulty"):
        embed.add_field(name="Mode", value=mode.capitalize(), inline=True)
        embed.add_field(name="Difficulty", value=difficulty_label(game.get("difficulty")), inline=True)
        filled = filled_count(game["board"])
        embed.add_field(name="Progress", value=f"{filled}/81", inline=True)
    return embed


def board_file_for(game: dict, *, status: str | None = None) -> tuple[discord.Embed, discord.File]:
    conflicts = find_conflicts(game["board"])
    embed = build_embed(game, status=status)
    stage = game.get("ui_stage", STAGE_BOX)
    highlight_box = game.get("box_id") if stage in (STAGE_CELL, STAGE_NUMBER) else None
    selected = selected_cell(game) if stage == STAGE_NUMBER else None
    image = render_board(
        game["board"],
        game["given"],
        solution=game["solution"],
        selected=selected,
        conflicts=conflicts,
        highlight_box=highlight_box,
        difficulty=game.get("difficulty"),
    )
    return embed, attach_board(embed, image)


# ---------------------------------------------------------------------------
# Rewards / finish
# ---------------------------------------------------------------------------

def daily_puzzle_number(day: str | None = None) -> int:
    """Sequential Daily Sudoku #N from a fixed epoch (Wordle-style)."""
    raw = day or utc_today()
    d = datetime.fromisoformat(raw).date()
    return (d - DAILY_EPOCH).days + 1


def daily_share_emoji_grid(*, hints_used: int = 0) -> str:
    """3×3 Wordle-like preview for a cleared daily (greener with fewer hints)."""
    if hints_used <= 0:
        row = "🟩🟩🟩"
        return "\n".join([row, row, row])
    if hints_used == 1:
        return "🟩🟩🟩\n🟩🟨🟩\n🟩🟩🟩"
    if hints_used == 2:
        return "🟩🟨🟩\n🟨🟩🟨\n🟩🟨🟩"
    return "🟨🟨🟨\n🟨🟩🟨\n🟨🟨🟨"


def build_daily_share_text(
    *,
    day: str,
    difficulty: str | None,
    elapsed: float,
    hints_used: int = 0,
) -> str:
    number = daily_puzzle_number(day)
    tier = difficulty_label(difficulty)
    grid = daily_share_emoji_grid(hints_used=hints_used)
    return (
        f"Daily Sudoku #{number}\n"
        f"{tier} · {format_time(elapsed)}\n"
        f"{grid}"
    )


def build_daily_achievement_embed(
    user: discord.abc.User,
    *,
    day: str,
    difficulty: str | None,
    elapsed: float,
    hints_used: int,
    coins: int,
    rank: int | None,
    multiplier: float,
    share_text: str,
) -> discord.Embed:
    medal = {1: "1st", 2: "2nd", 3: "3rd"}.get(rank or 0, f"#{rank}" if rank else "—")
    embed = paper_embed(
        f"Daily #{daily_puzzle_number(day)}",
        description=f"{user.mention} cleared today's puzzle.",
    )
    embed.add_field(name="Time", value=format_time(elapsed), inline=True)
    embed.add_field(name="Difficulty", value=difficulty_label(difficulty), inline=True)
    embed.add_field(name="Rank", value=str(medal), inline=True)
    embed.add_field(name="Reward", value=f"+{coins}", inline=True)
    embed.add_field(name="Multiplier", value=f"×{multiplier:.2f}", inline=True)
    embed.add_field(name="Hints", value=str(hints_used), inline=True)
    embed.add_field(name="Share", value=f"```\n{share_text}\n```", inline=False)
    return embed


async def announce_daily_achievement(bot: "SudokuBot", embed: discord.Embed) -> None:
    if not DAILY_ANNOUNCE_CHANNEL_ID:
        return
    channel = bot.get_channel(DAILY_ANNOUNCE_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(DAILY_ANNOUNCE_CHANNEL_ID)
        except discord.HTTPException:
            return
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


def finish_win(
    data: dict,
    guild_id: int,
    user: discord.abc.User,
    game: dict,
    *,
    challenge_winner: bool = False,
    award: bool = True,
) -> discord.Embed:
    gstats = guild_stats(data, guild_id)
    stats = user_stats(gstats, user.id)
    stats["name"] = getattr(user, "display_name", user.name)
    elapsed = time.time() - game["started_at"]
    is_daily = game["mode"] == "daily"

    if not award and is_daily:
        return paper_embed(
            "Daily",
            description="Today's reward was already claimed — no duplicate coins.",
        )

    stats["wins"] += 1
    stats["games"] += 1
    stats["streak"] += 1
    stats["best_streak"] = max(stats["best_streak"], stats["streak"])
    if stats["best_time"] is None or elapsed < stats["best_time"]:
        stats["best_time"] = int(elapsed)

    coins = win_reward(
        stats["streak"],
        daily=is_daily,
        hints_used=game.get("hints_used", 0),
        difficulty=game.get("difficulty"),
        challenge_winner=challenge_winner,
    )
    stats["coins"] += coins

    if is_daily:
        stats["daily_wins"] += 1
        daily = get_guild_daily(data, guild_id)
        daily["results"][str(user.id)] = {
            "won": True,
            "time": int(elapsed),
            "hints": game.get("hints_used", 0),
            "name": stats["name"],
        }

    save_data(data)

    rank = None
    if is_daily:
        winners = [
            (uid, r)
            for uid, r in (get_guild_daily(data, guild_id).get("results") or {}).items()
            if r.get("won")
        ]
        winners.sort(key=lambda item: (item[1].get("time", 10**9), item[1].get("hints", 99)))
        for i, (uid, _) in enumerate(winners, start=1):
            if uid == str(user.id):
                rank = i
                break

    if challenge_winner:
        title = "Challenge won"
    elif is_daily:
        title = "Daily cleared"
    else:
        title = "Puzzle solved"

    embed = paper_embed(f"✏️ {title}")
    embed.add_field(name="Time", value=format_time(elapsed), inline=True)
    embed.add_field(name="Difficulty", value=difficulty_label(game.get("difficulty")), inline=True)
    embed.add_field(name="Reward", value=f"+{coins}", inline=True)
    embed.add_field(name="Streak", value=str(stats["streak"]), inline=True)
    embed.add_field(name="Balance", value=str(stats["coins"]), inline=True)
    if rank is not None:
        embed.add_field(name="Rank", value=f"#{rank}", inline=True)
    elif game.get("hints_used"):
        embed.add_field(name="Hints", value=str(game["hints_used"]), inline=True)
    else:
        embed.add_field(name="Mode", value=game["mode"].capitalize(), inline=True)
    embed._sudoku_coins = coins  # type: ignore[attr-defined]
    embed._sudoku_rank = rank  # type: ignore[attr-defined]
    return embed


async def finish_win_and_announce(
    bot: "SudokuBot",
    guild_id: int,
    user: discord.abc.User,
    game: dict,
) -> discord.Embed:
    """Award win; for daily, gate rewards/announcement on first MongoDB claim."""
    if game.get("mode") != "daily":
        return finish_win(bot.data, guild_id, user, game)

    day = game.get("daily_date") or utc_today()
    elapsed = int(time.time() - game["started_at"])
    hints = int(game.get("hints_used", 0) or 0)
    tier = difficulty_label(game.get("difficulty"))
    gstats = guild_stats(bot.data, guild_id)
    stats = user_stats(gstats, user.id)
    preview_coins = win_reward(
        stats["streak"] + 1,
        daily=True,
        hints_used=hints,
        difficulty=game.get("difficulty"),
    )

    first = await match_store.try_claim_daily_win(
        guild_id=guild_id,
        user_id=user.id,
        day=day,
        elapsed=elapsed,
        hints=hints,
        difficulty=tier,
        coins=preview_coins,
    )
    if not first:
        return finish_win(bot.data, guild_id, user, game, award=False)

    embed = finish_win(bot.data, guild_id, user, game)
    coins = getattr(embed, "_sudoku_coins", preview_coins)
    rank = getattr(embed, "_sudoku_rank", None)

    share = build_daily_share_text(
        day=day,
        difficulty=game.get("difficulty"),
        elapsed=elapsed,
        hints_used=hints,
    )
    embed.add_field(name="Share", value=f"```\n{share}\n```", inline=False)

    announce = build_daily_achievement_embed(
        user,
        day=day,
        difficulty=game.get("difficulty"),
        elapsed=elapsed,
        hints_used=hints,
        coins=int(coins),
        rank=rank,
        multiplier=difficulty_multiplier(game.get("difficulty")),
        share_text=share,
    )
    await announce_daily_achievement(bot, announce)
    return embed


def finish_forfeit(data: dict, guild_id: int, user: discord.abc.User, game: dict) -> discord.Embed:
    gstats = guild_stats(data, guild_id)
    stats = user_stats(gstats, user.id)
    stats["name"] = getattr(user, "display_name", user.name)
    stats["losses"] += 1
    stats["games"] += 1
    stats["streak"] = 0
    if game["mode"] == "daily":
        daily = get_guild_daily(data, guild_id)
        daily["results"][str(user.id)] = {
            "won": False,
            "forfeit": True,
            "name": stats["name"],
        }
    save_data(data)
    note = " Daily attempt locked for today." if game["mode"] == "daily" else ""
    return paper_embed(
        "I QUITTT",
        description=f"Streak reset.{note}",
    )


# ---------------------------------------------------------------------------
# Competitive speedrun challenges
# ---------------------------------------------------------------------------

async def sync_challenge_board(game: dict) -> None:
    if game.get("mode") != "challenge":
        return
    match_id = game.get("match_id")
    slot = game.get("player_slot")
    if not match_id or not slot:
        return
    await match_store.update_player(
        match_id,
        slot,
        {"current_board": copy_grid(game["board"])},
    )
    key = challenge_game_key(match_id, game["owner_id"])
    await persist_game(key, game)


async def open_private_match_channel(
    channel: discord.TextChannel,
    user: discord.abc.User,
    title: str,
) -> discord.abc.Messageable:
    """Private thread for one player; falls back to DM if threads are unavailable."""
    try:
        thread = await channel.create_thread(
            name=title[:100],
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=60,
        )
        await thread.add_user(user)
        return thread
    except (discord.Forbidden, discord.HTTPException):
        return await user.create_dm()


async def post_game_panel(
    destination: discord.abc.Messageable,
    key: tuple,
    game: dict,
) -> discord.Message:
    view = SudokuView(key, bot)
    embed, file = board_file_for(game)
    msg = await destination.send(embed=embed, view=view, file=file)
    view.message = msg
    game["message_id"] = msg.id
    return msg


def challenge_ready_to_settle(match: dict) -> bool:
    """True when every non-forfeit player has finished, or ≤1 player remains standing."""
    entries = match_player_entries(match)
    standing = [p for _, p in entries if not p.get("forfeit")]
    if len(standing) <= 1:
        return True
    return all(p.get("finished_time") is not None for p in standing)


def _elapsed_of(player: dict, start: float) -> float | None:
    if player.get("forfeit"):
        return None
    ft = player.get("finished_time")
    if ft is None:
        return None
    return float(ft) - start


async def settle_challenge_match(
    bot: "SudokuBot",
    match: dict,
    *,
    reason: str,
) -> None:
    """Compare finish times / forfeits and announce the winner in the origin channel."""
    if match.get("status") == "finished":
        return
    if not challenge_ready_to_settle(match):
        return

    entries = match_player_entries(match)
    guild_id = match["guild_id"]
    start = float(match["start_time"])

    standing = [p for _, p in entries if not p.get("forfeit")]
    finished = [p for p in standing if p.get("finished_time") is not None]
    winner_id: int | None = None
    detail = reason

    if not standing:
        detail = "all forfeited"
    elif len(standing) == 1:
        winner_id = standing[0]["user_id"]
        detail = "others forfeited"
    else:
        ranked = sorted(finished, key=lambda p: float(p["finished_time"]))
        best = float(ranked[0]["finished_time"])
        tied = [p for p in ranked if float(p["finished_time"]) == best]
        if len(tied) > 1:
            detail = "dead heat"
        else:
            winner_id = ranked[0]["user_id"]
            detail = "fastest finish"

    await match_store.update_match(
        match["_id"],
        {"status": "finished", "winner_id": winner_id, "settle_reason": detail},
    )

    for _slot, player in entries:
        key = challenge_game_key(match["_id"], player["user_id"])
        await remove_game(key)

    guild = bot.get_guild(guild_id)
    channel = bot.get_channel(match["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    def mention(uid: int | None) -> str:
        if uid is None:
            return "—"
        member = guild.get_member(uid) if guild else None
        return member.mention if member else f"<@{uid}>"

    if winner_id is None:
        embed = paper_embed("Challenge ended", description=f"No winner ({detail}).")
        await channel.send(embed=embed)
        return

    winner_game = {
        "mode": "challenge",
        "started_at": start,
        "difficulty": match.get("difficulty"),
        "hints_used": 0,
    }
    winner_user = guild.get_member(winner_id) if guild else None
    if winner_user is None:
        try:
            winner_user = await bot.fetch_user(winner_id)
        except discord.HTTPException:
            winner_user = None

    reward_embed = None
    if winner_user is not None:
        reward_embed = finish_win(
            bot.data,
            guild_id,
            winner_user,
            winner_game,
            challenge_winner=True,
        )
        gstats_w = guild_stats(bot.data, guild_id)
        user_stats(gstats_w, winner_id)["challenge_wins"] = (
            int(user_stats(gstats_w, winner_id).get("challenge_wins", 0)) + 1
        )
        save_data(bot.data)

    gstats = guild_stats(bot.data, guild_id)
    for _slot, player in entries:
        uid = player["user_id"]
        if uid == winner_id:
            continue
        loser_stats = user_stats(gstats, uid)
        loser_stats["losses"] += 1
        loser_stats["games"] += 1
        loser_stats["streak"] = 0
        loser_stats["coins"] += CHALLENGE_LOSER_COINS
    save_data(bot.data)

    # Ranked board: finishers by time, then forfeits / last standing
    ranked_lines: list[str] = []
    finishers = sorted(
        (p for _, p in entries if p.get("finished_time") is not None and not p.get("forfeit")),
        key=lambda p: float(p["finished_time"]),
    )
    if winner_id is not None and not any(p["user_id"] == winner_id for p in finishers):
        ranked_lines.append(f"🏆 {mention(winner_id)} — last standing ({detail})")
    for i, p in enumerate(finishers, start=1):
        et = _elapsed_of(p, start)
        medal = "🏆 " if p["user_id"] == winner_id else f"{i}. "
        time_bit = f" — **{format_time(et)}**" if et is not None else ""
        ranked_lines.append(f"{medal}{mention(p['user_id'])}{time_bit}")
    for _slot, p in entries:
        if p.get("forfeit"):
            ranked_lines.append(f"✗ {mention(p['user_id'])} — quit")
    if any(p["user_id"] != winner_id for _, p in entries):
        ranked_lines.append(f"Non-winners: +{CHALLENGE_LOSER_COINS} consolation coins each")
    ranked_lines.append(
        f"Difficulty: **{difficulty_label(match.get('difficulty'))}** · winner ×{CHALLENGE_WIN_MULT:g}"
    )

    announce = paper_embed("Challenge result", description="\n".join(ranked_lines))
    await channel.send(embed=announce)
    if reward_embed is not None:
        await channel.send(embed=reward_embed)


async def handle_challenge_completion(
    bot: "SudokuBot",
    interaction: discord.Interaction,
    game: dict,
    view: "SudokuView",
) -> None:
    """Record finish time; settle when every remaining player is done or has forfeited."""
    finished_at = time.time()
    match_id = game["match_id"]
    slot = game["player_slot"]
    elapsed = finished_at - float(game["started_at"])

    match = await match_store.update_player(
        match_id,
        slot,
        {
            "current_board": copy_grid(game["board"]),
            "finished_time": finished_at,
            "elapsed": elapsed,
        },
    )
    if not match:
        await interaction.response.edit_message(
            embed=paper_embed("Match missing"),
            view=None,
            attachments=[],
        )
        return

    image = render_board(
        game["board"],
        game["given"],
        solution=game["solution"],
        conflicts=set(),
        difficulty=game.get("difficulty"),
    )
    remaining = sum(
        1
        for _, p in match_player_entries(match)
        if not p.get("forfeit") and p.get("finished_time") is None
    )
    wait_msg = (
        "Waiting for other players…"
        if remaining
        else "Settling match…"
    )
    done = paper_embed(
        "Board complete",
        description=f"Time: **{format_time(elapsed)}**. {wait_msg}",
    )
    file = attach_board(done, image)
    await interaction.response.edit_message(embed=done, view=None, attachments=[file])
    view.stop()
    await remove_game(view.game_key)

    if challenge_ready_to_settle(match):
        await settle_challenge_match(bot, match, reason="all finished")


async def handle_challenge_forfeit(
    bot: "SudokuBot",
    interaction: discord.Interaction,
    game: dict,
    view: "SudokuView",
) -> None:
    match_id = game["match_id"]
    slot = game["player_slot"]
    match = await match_store.update_player(
        match_id,
        slot,
        {"forfeit": True, "finished_time": None},
    )

    embed = paper_embed(
        "I QUITTT",
        description="You're out. Remaining players keep racing.",
    )
    await interaction.response.edit_message(embed=embed, view=None, attachments=[])
    view.stop()
    await remove_game(view.game_key)

    if match:
        await settle_challenge_match(bot, match, reason="quit")


async def launch_challenge_match(
    *,
    interaction: discord.Interaction,
    players: list[discord.Member],
    difficulty: str,
) -> None:
    assert interaction.guild is not None
    assert isinstance(interaction.channel, discord.TextChannel)
    if len(players) < 2:
        await interaction.followup.send("Need at least 2 players to start.", ephemeral=True)
        return

    board, given, solution = make_puzzle(difficulty)
    tier = difficulty_label(difficulty)
    player_ids = [m.id for m in players]
    doc = new_match_document(
        guild_id=interaction.guild.id,
        channel_id=interaction.channel.id,
        player_ids=player_ids,
        board=board,
        given=given,
        solution=solution,
        difficulty=tier,
    )
    match_id = await match_store.insert_match(doc)
    match = await match_store.get_match(match_id)
    assert match is not None
    start_time = float(match["start_time"])
    slots = match["player_slots"]

    names = " · ".join(m.display_name for m in players)
    destinations: list[tuple[str, discord.Member, discord.abc.Messageable]] = []
    for slot, member in zip(slots, players):
        dest = await open_private_match_channel(
            interaction.channel,
            member,
            f"sudoku-{len(players)}p-{member.display_name}"[:90],
        )
        thread_id = getattr(dest, "id", None)
        await match_store.update_player(match_id, slot, {"thread_id": thread_id})
        destinations.append((slot, member, dest))

    roster = ", ".join(m.mention for m in players)
    for slot, member, dest in destinations:
        key = challenge_game_key(match_id, member.id)
        player_board = copy_grid(board)
        games[key] = new_game_state(
            mode="challenge",
            board=player_board,
            given=given,
            solution=solution,
            owner_id=member.id,
            channel_id=getattr(dest, "id", interaction.channel.id),
            guild_id=interaction.guild.id,
            match_id=match_id,
            player_slot=slot,
            difficulty=difficulty,
            started_at=start_time,
        )
        await dest.send(
            f"{member.mention} Speedrun ({len(players)} players) · **{tier}**\n"
            f"Field: {names} — go!"
        )
        await post_game_panel(dest, key, games[key])
        await persist_game(key, games[key])

    await interaction.followup.send(
        f"Challenge started ({len(players)}): {roster} · **{tier}**. "
        "Private boards are open — fastest clean solve wins.",
    )


def challenge_cooldown_remaining(user_id: int) -> int:
    last = challenge_cooldowns.get(user_id)
    if last is None:
        return 0
    left = int(CHALLENGE_COOLDOWN_SEC - (time.time() - last))
    return max(0, left)


def mark_challenge_cooldown(user_id: int) -> None:
    challenge_cooldowns[user_id] = time.time()


class ChallengeInviteView(discord.ui.View):
    def __init__(
        self,
        *,
        challenger_id: int,
        invitee_ids: list[int],
        guild_id: int,
        channel_id: int,
        difficulty: str,
    ):
        super().__init__(timeout=INVITE_TIMEOUT_SEC)
        self.challenger_id = challenger_id
        self.invitee_ids = set(invitee_ids)
        self.accepted_ids: set[int] = set()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.difficulty = difficulty
        self._launching = False
        self.message: discord.Message | None = None

    def _status_text(self, header: str) -> str:
        pending = self.invitee_ids - self.accepted_ids
        parts = [header]
        if self.accepted_ids:
            parts.append("Accepted: " + ", ".join(f"<@{uid}>" for uid in sorted(self.accepted_ids)))
        if pending:
            parts.append("Waiting: " + ", ".join(f"<@{uid}>" for uid in sorted(pending)))
        return "\n".join(parts)

    def _disable(self) -> None:
        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        uid = interaction.user.id
        if uid == self.challenger_id or uid in self.invitee_ids:
            return True
        await interaction.response.send_message(
            "Only the challenger or invited players can use this lobby.",
            ephemeral=True,
        )
        return False

    async def _try_launch(self, interaction: discord.Interaction) -> None:
        if self._launching or self.accepted_ids != self.invitee_ids or not self.invitee_ids:
            return
        self._launching = True
        self._disable()

        guild = interaction.guild
        if guild is None or not isinstance(interaction.channel, discord.TextChannel):
            return

        all_ids = [self.challenger_id, *sorted(self.accepted_ids)]
        for uid in all_ids:
            if find_challenge_game_for_user(uid):
                await interaction.followup.send(
                    "Someone already has an active challenge — cancelled.",
                    ephemeral=True,
                )
                return

        members: list[discord.Member] = []
        for uid in all_ids:
            m = guild.get_member(uid)
            if m is None:
                await interaction.followup.send("Could not resolve all players.", ephemeral=True)
                return
            members.append(m)

        if self.message:
            await self.message.edit(
                content=self._status_text("✅ Everyone accepted — starting!"),
                view=self,
            )
        await launch_challenge_match(
            interaction=interaction,
            players=members,
            difficulty=self.difficulty,
        )

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        uid = interaction.user.id
        if uid not in self.invitee_ids:
            await interaction.response.send_message(
                "Only invited players can accept.",
                ephemeral=True,
            )
            return
        if uid in self.accepted_ids:
            await interaction.response.send_message("You already accepted.", ephemeral=True)
            return
        if find_challenge_game_for_user(self.challenger_id) or find_challenge_game_for_user(uid):
            await interaction.response.send_message(
                "You or the challenger already have an active challenge.",
                ephemeral=True,
            )
            return

        self.accepted_ids.add(uid)
        await interaction.response.defer()
        if self.message:
            await self.message.edit(
                content=self._status_text(f"✅ {interaction.user.mention} accepted."),
                view=self,
            )
        await self._try_launch(interaction)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        uid = interaction.user.id
        if uid not in self.invitee_ids:
            await interaction.response.send_message(
                "Only invited players can decline.",
                ephemeral=True,
            )
            return
        self.invitee_ids.discard(uid)
        self.accepted_ids.discard(uid)

        if not self.invitee_ids:
            self._disable()
            await interaction.response.edit_message(
                content=f"❌ {interaction.user.mention} declined — challenge cancelled (no opponents left).",
                view=self,
            )
            return

        await interaction.response.edit_message(
            content=self._status_text(
                f"❌ {interaction.user.mention} declined and left the lobby."
            ),
            view=self,
        )
        if self.accepted_ids == self.invitee_ids:
            await interaction.followup.send("Lobby ready — starting without declined players.")
            await self._try_launch(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.challenger_id:
            await interaction.response.send_message(
                "Only the challenger can cancel this lobby.",
                ephemeral=True,
            )
            return
        if self._launching:
            await interaction.response.send_message("Match is already starting.", ephemeral=True)
            return
        self._disable()
        await interaction.response.edit_message(
            content=f"🚫 {interaction.user.mention} cancelled the challenge.",
            view=self,
        )

    async def on_timeout(self) -> None:
        self._disable()
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏱ Challenge invite expired.",
                view=self,
            )
        except discord.HTTPException:
            pass


class OpenChallengeLobbyView(discord.ui.View):
    """Public Join lobby — challenger starts when ready (2–5 players)."""

    def __init__(
        self,
        *,
        challenger_id: int,
        guild_id: int,
        channel_id: int,
        difficulty: str,
    ):
        super().__init__(timeout=INVITE_TIMEOUT_SEC)
        self.challenger_id = challenger_id
        self.joined_ids: list[int] = [challenger_id]
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.difficulty = difficulty
        self._launching = False
        self.message: discord.Message | None = None

    def _roster_text(self, header: str) -> str:
        roster = ", ".join(f"<@{uid}>" for uid in self.joined_ids)
        return (
            f"{header}\n"
            f"Players ({len(self.joined_ids)}/{MAX_CHALLENGE_PLAYERS}): {roster}\n"
            f"Difficulty: **{difficulty_label(self.difficulty)}**"
        )

    def _disable(self) -> None:
        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        uid = interaction.user.id
        if interaction.user.bot:
            await interaction.response.send_message("Bots can't join.", ephemeral=True)
            return
        if uid in self.joined_ids:
            await interaction.response.send_message("You're already in the lobby.", ephemeral=True)
            return
        if len(self.joined_ids) >= MAX_CHALLENGE_PLAYERS:
            await interaction.response.send_message("Lobby is full.", ephemeral=True)
            return
        if find_challenge_game_for_user(uid):
            await interaction.response.send_message("Finish your active challenge first.", ephemeral=True)
            return
        if interaction.guild and solo_key(interaction.guild.id, uid) in games:
            await interaction.response.send_message(
                "Finish your solo/daily game first (`/quit`).",
                ephemeral=True,
            )
            return
        self.joined_ids.append(uid)
        await interaction.response.edit_message(
            content=self._roster_text(f"✅ {interaction.user.mention} joined."),
            view=self,
        )

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        uid = interaction.user.id
        if uid == self.challenger_id:
            await interaction.response.send_message(
                "Challenger can't leave — use Cancel.",
                ephemeral=True,
            )
            return
        if uid not in self.joined_ids:
            await interaction.response.send_message("You're not in this lobby.", ephemeral=True)
            return
        self.joined_ids = [x for x in self.joined_ids if x != uid]
        await interaction.response.edit_message(
            content=self._roster_text(f"👋 {interaction.user.mention} left."),
            view=self,
        )

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.challenger_id:
            await interaction.response.send_message("Only the challenger can start.", ephemeral=True)
            return
        if self._launching:
            return
        if len(self.joined_ids) < 2:
            await interaction.response.send_message(
                "Need at least one other player to start.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Invalid channel.", ephemeral=True)
            return

        members: list[discord.Member] = []
        for uid in self.joined_ids:
            m = guild.get_member(uid)
            if m is None:
                await interaction.response.send_message("Could not resolve all players.", ephemeral=True)
                return
            if find_challenge_game_for_user(uid):
                await interaction.response.send_message(
                    f"{m.mention} already has an active challenge.",
                    ephemeral=True,
                )
                return
            members.append(m)

        self._launching = True
        self._disable()
        await interaction.response.defer()
        if self.message:
            await self.message.edit(
                content=self._roster_text("🏁 Starting…"),
                view=self,
            )
        await launch_challenge_match(
            interaction=interaction,
            players=members,
            difficulty=self.difficulty,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.challenger_id:
            await interaction.response.send_message(
                "Only the challenger can cancel.",
                ephemeral=True,
            )
            return
        self._disable()
        await interaction.response.edit_message(
            content=f"🚫 {interaction.user.mention} cancelled the open lobby.",
            view=self,
        )

    async def on_timeout(self) -> None:
        self._disable()
        if self.message is None:
            return
        try:
            await self.message.edit(content="⏱ Open lobby expired.", view=self)
        except discord.HTTPException:
            pass


class BoardRefreshView(discord.ui.View):
    """Shown after SudokuView times out — restores interactive controls."""

    def __init__(self, game_key: tuple, bot: "SudokuBot"):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.game_key = game_key
        self.bot = bot

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        game = games.get(self.game_key)
        if not game:
            await interaction.response.edit_message(
                content="This game has ended.",
                embed=None,
                view=None,
                attachments=[],
            )
            self.stop()
            return
        if interaction.user.id != game["owner_id"]:
            await interaction.response.send_message("Not your board.", ephemeral=True)
            return
        view = SudokuView(self.game_key, self.bot)
        embed, file = board_file_for(game)
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            attachments=[file],
            view=view,
        )
        view.message = interaction.message
        if interaction.message:
            game["message_id"] = interaction.message.id
        await persist_game(self.game_key, game)
        self.stop()


class ConfirmQuitView(discord.ui.View):
    """Challenge quit confirmation (ephemeral)."""

    def __init__(self, game_key: tuple, bot: "SudokuBot", parent: "SudokuView"):
        super().__init__(timeout=30)
        self.game_key = game_key
        self.bot = bot
        self.parent = parent

    @discord.ui.button(label="I QUITTT", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        game = games.get(self.game_key)
        if not game or game.get("mode") != "challenge":
            await interaction.response.edit_message(content="Game already ended.", view=None)
            self.stop()
            return
        if interaction.user.id != game["owner_id"]:
            await interaction.response.send_message("Not your board.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Quitting…", view=None)
        self.stop()
        match_id = game["match_id"]
        slot = game["player_slot"]
        match = await match_store.update_player(
            match_id,
            slot,
            {"forfeit": True, "finished_time": None},
        )
        self.parent.stop()
        await remove_game(self.game_key)
        channel = self.bot.get_channel(game.get("channel_id"))
        if game.get("message_id") and channel is not None:
            try:
                msg = await channel.fetch_message(game["message_id"])
                await msg.edit(
                    embed=paper_embed(
                        "I QUITTT",
                        description="You're out. Remaining players keep racing.",
                    ),
                    view=None,
                    attachments=[],
                )
            except discord.HTTPException:
                pass
        if match:
            await settle_challenge_match(self.bot, match, reason="quit")

    @discord.ui.button(label="Keep playing", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Still in the race.", view=None)
        self.stop()


# ---------------------------------------------------------------------------
# UI — 3-stage flow: Box → Cell → Number
# ---------------------------------------------------------------------------

class SudokuView(discord.ui.View):
    def __init__(self, game_key: tuple, bot: "SudokuBot"):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.game_key = game_key
        self.bot = bot
        self.message: discord.Message | None = None
        self._built_stage: str | None = None
        game = games.get(game_key)
        if game:
            self.rebuild(game)

    def _cid(self, suffix: str) -> str:
        """Stable custom_id so Discord can reuse the same component tree across edits."""
        key = self.game_key
        if isinstance(key, tuple) and len(key) >= 3 and key[0] == "ch":
            return f"sk:ch:{str(key[1])[:8]}:{key[2]}:{suffix}"[:100]
        g, k = key[0], key[1]
        return f"sk:{g}:{k}:{suffix}"[:100]

    def rebuild(self, game: dict) -> None:
        self.clear_items()
        stage = game.get("ui_stage", STAGE_BOX)
        if stage == STAGE_BOX:
            self._build_stage_box(game)
        elif stage == STAGE_CELL:
            self._build_stage_cell(game)
        else:
            self._build_stage_number(game)
        self._built_stage = stage

    def _sync_pencil_button(self, game: dict) -> None:
        pencil_on = game.get("pencil_mode", False)
        target = self._cid("num:pencil")
        for child in self.children:
            if getattr(child, "custom_id", None) == target:
                child.label = "Pencil Mode: ON" if pencil_on else "Pencil Mode"  # type: ignore[attr-defined]
                # Flat styling only — no green/yellow board mirroring
                child.style = discord.ButtonStyle.success if pencil_on else discord.ButtonStyle.secondary  # type: ignore[attr-defined]
                break

    def _add_hint_button(self, row: int, prefix: str) -> None:
        hint = discord.ui.Button(
            label="Hint",
            style=discord.ButtonStyle.secondary,
            row=row,
            custom_id=self._cid(f"{prefix}:hint"),
        )
        hint.callback = self.on_hint
        self.add_item(hint)

    def _add_quit_button(self, row: int, prefix: str) -> None:
        quit_btn = discord.ui.Button(
            label="I QUITTT",
            style=discord.ButtonStyle.danger,
            row=row,
            custom_id=self._cid(f"{prefix}:forfeit"),
        )
        quit_btn.callback = self.on_forfeit
        self.add_item(quit_btn)

    def _build_stage_box(self, game: dict) -> None:
        # 3×3 directional pad → box indices 0–8 (top-left … bottom-right)
        for i, label in enumerate(BOX_ARROW_LABELS):
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                row=i // 3,
                custom_id=self._cid(f"box:{i}"),
            )
            btn.callback = self._box_cb(i)
            self.add_item(btn)

        self._add_hint_button(3, "box")
        self._add_quit_button(3, "box")

    def _build_stage_cell(self, game: dict) -> None:
        conflicts = find_conflicts(game["board"])
        box_id = game.get("box_id", 0)
        for i in range(9):
            r, c = cell_in_box(box_id, i)
            val = cell_value(game["board"], r, c)
            given = game["given"][r][c]
            notes = list(game["board"][r][c].get("pencil_marks") or [])

            if given:
                style = discord.ButtonStyle.secondary
                label = str(val)
                disabled = True
            elif (r, c) in conflicts:
                style = discord.ButtonStyle.danger
                label = str(val) if val else "."
                disabled = False
            elif val:
                style = discord.ButtonStyle.secondary
                label = str(val)
                disabled = False
            else:
                style = discord.ButtonStyle.secondary
                label = "." if not notes else "·" + "".join(str(n) for n in notes[:4])
                disabled = False

            btn = discord.ui.Button(
                label=label[:80],
                style=style,
                row=i // 3,
                disabled=disabled,
                custom_id=self._cid(f"cell:{box_id}:{i}"),
            )
            btn.callback = self._cell_cb(i)
            self.add_item(btn)

        back = discord.ui.Button(
            label="Back to Grid",
            style=discord.ButtonStyle.secondary,
            row=3,
            custom_id=self._cid("cell:back"),
        )
        back.callback = self.on_back_to_grid
        self.add_item(back)
        self._add_hint_button(3, "cell")
        self._add_quit_button(3, "cell")

    def _build_stage_number(self, game: dict) -> None:
        for d in range(1, 10):
            btn = discord.ui.Button(
                label=str(d),
                style=discord.ButtonStyle.secondary,
                row=(d - 1) // 3,
                custom_id=self._cid(f"num:{d}"),
            )
            btn.callback = self._digit_cb(d)
            self.add_item(btn)

        back = discord.ui.Button(
            label="Back to Cells",
            style=discord.ButtonStyle.secondary,
            row=3,
            custom_id=self._cid("num:back"),
        )
        back.callback = self.on_back_to_cells
        self.add_item(back)

        pencil_on = game.get("pencil_mode", False)
        pencil = discord.ui.Button(
            label="Pencil Mode: ON" if pencil_on else "Pencil Mode",
            style=discord.ButtonStyle.success if pencil_on else discord.ButtonStyle.secondary,
            row=3,
            custom_id=self._cid("num:pencil"),
        )
        pencil.callback = self.on_toggle_pencil
        self.add_item(pencil)

        self._add_hint_button(4, "num")
        self._add_quit_button(4, "num")

    def _box_cb(self, box_id: int):
        async def _cb(interaction: discord.Interaction):
            await self.on_pick_box(interaction, box_id)
        return _cb

    def _cell_cb(self, index: int):
        async def _cb(interaction: discord.Interaction):
            await self.on_pick_cell(interaction, index)
        return _cb

    def _digit_cb(self, digit: int):
        async def _cb(interaction: discord.Interaction):
            await self.on_digit(interaction, digit)
        return _cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        game = games.get(self.game_key)
        if not game:
            await interaction.response.send_message("This game has ended.", ephemeral=True)
            self.stop()
            return False
        if game["mode"] in ("solo", "daily", "challenge") and interaction.user.id != game["owner_id"]:
            await interaction.response.send_message(
                "This board belongs to someone else. Start yours with `/play` or `/daily`.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        game = games.get(self.game_key)
        if not self.message or not game:
            return
        refresh = BoardRefreshView(self.game_key, self.bot)
        try:
            await self.message.edit(
                content="⏱ Controls timed out — press **Refresh** to keep playing.",
                view=refresh,
            )
        except discord.HTTPException:
            pass

    async def refresh(
        self,
        interaction: discord.Interaction,
        *,
        status: str | None = None,
        ended: bool = False,
        embed: discord.Embed | None = None,
    ) -> None:
        game = games.get(self.game_key)
        if ended or not game:
            final = embed or paper_embed("Game over")
            self.stop()
            await interaction.response.edit_message(embed=final, view=None, attachments=[])
            return

        stage = game.get("ui_stage", STAGE_BOX)
        # Keep Stage 3 keypad mounted with stable custom_ids — only swap image/embed
        if stage == STAGE_NUMBER and self._built_stage == STAGE_NUMBER and self.children:
            self._sync_pencil_button(game)
        else:
            self.rebuild(game)

        built, file = board_file_for(game, status=status)
        await interaction.response.edit_message(
            embed=built,
            attachments=[file],
            view=self,
        )
        await persist_game(self.game_key, game)

    async def on_pick_box(self, interaction: discord.Interaction, box_id: int) -> None:
        game = games[self.game_key]
        game["box_id"] = box_id
        game["ui_stage"] = STAGE_CELL
        await self.refresh(
            interaction,
            status=f"Box **{box_id + 1}** selected — pick a cell.",
        )

    async def on_pick_cell(self, interaction: discord.Interaction, index: int) -> None:
        game = games[self.game_key]
        r, c = cell_in_box(game["box_id"], index)
        if game["given"][r][c]:
            await self.refresh(interaction, status="That cell is a locked clue.")
            return
        game["sel_r"], game["sel_c"] = r, c
        game["ui_stage"] = STAGE_NUMBER
        await self.refresh(
            interaction,
            status=f"Cell **{cell_label(r, c)}** — enter a number.",
        )

    async def on_back_to_grid(self, interaction: discord.Interaction) -> None:
        game = games[self.game_key]
        game["ui_stage"] = STAGE_BOX
        game["pencil_mode"] = False
        await self.refresh(interaction, status="Pick a box (1–9).")

    async def on_back_to_cells(self, interaction: discord.Interaction) -> None:
        game = games[self.game_key]
        game["ui_stage"] = STAGE_CELL
        await self.refresh(
            interaction,
            status=f"Back to cells in box **{game['box_id'] + 1}**.",
        )

    async def on_toggle_pencil(self, interaction: discord.Interaction) -> None:
        game = games[self.game_key]
        game["pencil_mode"] = not game.get("pencil_mode", False)
        state = "ON" if game["pencil_mode"] else "OFF"
        await self.refresh(interaction, status=f"Pencil mode **{state}**.")

    async def on_digit(self, interaction: discord.Interaction, digit: int) -> None:
        game = games[self.game_key]

        r, c = selected_cell(game)
        label = cell_label(r, c)

        if game["given"][r][c]:
            game["ui_stage"] = STAGE_NUMBER
            await self.refresh(interaction, status=f"**{label}** is a locked clue.")
            return

        # Pencil mode: toggle draft, keep keypad open (Stage 3)
        if game.get("pencil_mode"):
            # Clear any real value when drafting (keep cell as empty + marks)
            set_cell_value(game["board"], r, c, 0)
            notes = toggle_pencil(game["board"], r, c, digit)
            game["ui_stage"] = STAGE_NUMBER
            await sync_challenge_board(game)
            await self.refresh(
                interaction,
                status=f"Pencil on **{label}**: {notes or 'cleared'}.",
            )
            return

        # Pen mode: tap same digit again to erase; otherwise place / overwrite
        current = cell_value(game["board"], r, c)
        if current == digit:
            set_cell_value(game["board"], r, c, 0)
            game["board"][r][c]["pencil_marks"] = []
            game["ui_stage"] = STAGE_NUMBER
            await sync_challenge_board(game)
            await self.refresh(interaction, status=None)
            return

        set_cell_value(game["board"], r, c, digit)
        await sync_challenge_board(game)

        if is_complete(game["board"], game["solution"]) and not find_conflicts(game["board"]):
            if game.get("mode") == "challenge":
                await handle_challenge_completion(self.bot, interaction, game, self)
                return
            if interaction.guild is None:
                return
            key = self.game_key
            embed = await finish_win_and_announce(
                self.bot,
                interaction.guild.id,
                interaction.user,
                game,
            )
            image = render_board(
                game["board"],
                game["given"],
                solution=game["solution"],
                conflicts=set(),
                difficulty=game.get("difficulty"),
            )
            file = attach_board(embed, image)
            await remove_game(key)
            self.stop()
            await interaction.response.edit_message(embed=embed, view=None, attachments=[file])
            return

        game["ui_stage"] = STAGE_NUMBER
        # Conflict feedback is visual-only (red cells on the board image)
        await self.refresh(interaction, status=None)

    async def on_hint(self, interaction: discord.Interaction) -> None:
        game = games.get(self.game_key)
        if not game:
            await interaction.response.send_message("This game has ended.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        if interaction.user.id != game["owner_id"]:
            await interaction.response.send_message("Not your board.", ephemeral=True)
            return

        gstats = guild_stats(self.bot.data, interaction.guild.id)
        stats = user_stats(gstats, interaction.user.id)
        if stats["hints"] <= 0:
            await interaction.response.send_message(
                "No hints left. Buy one in `/shop`.",
                ephemeral=True,
            )
            return
        empties = empty_cells(game["board"])
        if not empties:
            await interaction.response.send_message("Board is already full!", ephemeral=True)
            return

        random.shuffle(empties)
        r, c = empties[0]
        digit = game["solution"][r][c]
        set_cell_value(game["board"], r, c, digit)
        game["hints_used"] = game.get("hints_used", 0) + 1
        stats["hints"] -= 1
        save_data(self.bot.data)
        await sync_challenge_board(game)
        await persist_game(self.game_key, game)

        if is_complete(game["board"], game["solution"]) and not find_conflicts(game["board"]):
            if game.get("mode") == "challenge":
                await handle_challenge_completion(self.bot, interaction, game, self)
                return
            embed = await finish_win_and_announce(
                self.bot,
                interaction.guild.id,
                interaction.user,
                game,
            )
            image = render_board(
                game["board"],
                game["given"],
                solution=game["solution"],
                conflicts=set(),
                difficulty=game.get("difficulty"),
            )
            file = attach_board(embed, image)
            await remove_game(self.game_key)
            self.stop()
            await interaction.response.edit_message(embed=embed, view=None, attachments=[file])
            return

        await self.refresh(
            interaction,
            status=f"Hint: **{cell_label(r, c)}** → **{digit}** · left **{stats['hints']}**",
        )

    async def on_forfeit(self, interaction: discord.Interaction) -> None:
        game = games[self.game_key]
        if game.get("mode") == "challenge":
            if interaction.user.id != game["owner_id"]:
                await interaction.response.send_message("Not your challenge board.", ephemeral=True)
                return
            await interaction.response.send_message(
                "Really leave this speedrun?",
                view=ConfirmQuitView(self.game_key, self.bot, self),
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            return

        if game["mode"] in ("solo", "daily") and interaction.user.id != game["owner_id"]:
            await interaction.response.send_message("Only the owner can quit.", ephemeral=True)
            return

        embed = finish_forfeit(self.bot.data, interaction.guild.id, interaction.user, game)
        await remove_game(self.game_key)
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None, attachments=[])


# ---------------------------------------------------------------------------
# Shop
# ---------------------------------------------------------------------------

class ShopSelect(discord.ui.Select):
    def __init__(self, bot: "SudokuBot"):
        self.bot = bot
        options = [
            discord.SelectOption(
                label=f"{meta['label']} — {meta['cost']} coins",
                value=f"title:{tid}",
                description="Cosmetic title",
            )
            for tid, meta in SHOP_TITLES.items()
        ]
        for cid, meta in SHOP_CONSUMABLES.items():
            options.append(
                discord.SelectOption(
                    label=f"{meta['label']} — {meta['cost']} coins",
                    value=f"item:{cid}",
                    description=meta["desc"][:100],
                )
            )
        super().__init__(placeholder="Buy an item…", options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        choice = self.values[0]
        gstats = guild_stats(self.bot.data, interaction.guild.id)
        stats = user_stats(gstats, interaction.user.id)
        stats["name"] = interaction.user.display_name

        if choice.startswith("title:"):
            tid = choice.split(":", 1)[1]
            meta = SHOP_TITLES[tid]
            if tid in stats["owned_titles"]:
                stats["title"] = tid
                save_data(self.bot.data)
                await interaction.response.send_message(f"Equipped **{meta['label']}**.", ephemeral=True)
                return
            if stats["coins"] < meta["cost"]:
                await interaction.response.send_message(
                    f"Need **{meta['cost']}** coins (you have {stats['coins']}).",
                    ephemeral=True,
                )
                return
            stats["coins"] -= meta["cost"]
            stats["owned_titles"].append(tid)
            stats["title"] = tid
            save_data(self.bot.data)
            await interaction.response.send_message(
                f"Bought **{meta['label']}** (−{meta['cost']}). Balance: **{stats['coins']}**.",
                ephemeral=True,
            )
            return

        meta = SHOP_CONSUMABLES["hint"]
        if stats["coins"] < meta["cost"]:
            await interaction.response.send_message(
                f"Need **{meta['cost']}** coins (you have {stats['coins']}).",
                ephemeral=True,
            )
            return
        stats["coins"] -= meta["cost"]
        stats["hints"] += 1
        save_data(self.bot.data)
        await interaction.response.send_message(
            f"Bought a **Hint** (−{meta['cost']}). Inventory: **{stats['hints']}**. Use `/hint` in a game.",
            ephemeral=True,
        )


class ShopView(discord.ui.View):
    def __init__(self, bot: "SudokuBot"):
        super().__init__(timeout=120)
        self.add_item(ShopSelect(bot))


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

async def _health(_request) -> "web.Response":
    from aiohttp import web

    return web.Response(text="ok")


async def start_health_server(bot: "SudokuBot") -> None:
    """HTTP keep-alive for Render + UptimeRobot (PORT is set by Render)."""
    from aiohttp import web

    port = int(os.getenv("PORT", "8080") or 8080)
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    bot._health_runner = runner  # type: ignore[attr-defined]
    print(f"Health server listening on 0.0.0.0:{port} (/ and /health)")


class SudokuBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.data = load_data()
        self._health_runner = None

    async def setup_hook(self) -> None:
        await start_health_server(self)
        await match_store.connect()
        kind = type(match_store).__name__
        print(f"Challenge match store: {kind}")
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")


bot = SudokuBot()

STATUS_ROTATION = [
    discord.Game(name="/play · Sudoku 9×9"),
    discord.Game(name="/challenge · Speedrun"),
    discord.Game(name="/daily · Daily puzzle"),
    discord.Game(name="/shop · Titles & hints"),
]
_status_i = 0


@tasks.loop(seconds=40)
async def rotate_status():
    global _status_i
    await bot.change_presence(activity=STATUS_ROTATION[_status_i % len(STATUS_ROTATION)])
    _status_i += 1


@rotate_status.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


async def start_panel(interaction: discord.Interaction, key: tuple[int, int], game: dict) -> None:
    view = SudokuView(key, bot)
    embed, file = board_file_for(game)
    await interaction.response.send_message(embed=embed, view=view, file=file)
    view.message = await interaction.original_response()
    game["message_id"] = view.message.id
    await persist_game(key, game)


async def restore_persisted_sessions(bot: "SudokuBot") -> None:
    """Reload active boards after a bot restart and reattach controls."""
    try:
        docs = await match_store.list_active_games()
    except Exception as exc:  # noqa: BLE001
        print(f"restore list failed: {exc}")
        return

    restored = 0
    for doc in docs:
        key = deserialize_game_key(doc.get("game_key") or doc.get("_id", ""))
        raw = doc.get("game")
        if not key or not isinstance(raw, dict):
            continue
        game = raw
        game["board"] = normalize_board(game.get("board") or [])
        game["participants"] = set(game.get("participants") or [game.get("owner_id")])
        games[key] = game

        channel = bot.get_channel(game.get("channel_id"))
        if channel is None or not game.get("message_id"):
            continue
        try:
            msg = await channel.fetch_message(game["message_id"])
            view = SudokuView(key, bot)
            embed, file = board_file_for(game)
            await msg.edit(
                content="♻️ Session restored after restart — controls refreshed.",
                embed=embed,
                attachments=[file],
                view=view,
            )
            view.message = msg
            restored += 1
        except discord.HTTPException:
            # Keep in memory; player can still /quit
            pass

    # Abandon challenge matches with no live sessions left
    try:
        active_matches = await match_store.list_matches(status="active")
    except Exception as exc:  # noqa: BLE001
        print(f"list active matches failed: {exc}")
        active_matches = []

    for match in active_matches:
        mid = match.get("_id")
        if not mid:
            continue
        any_live = any(
            isinstance(k, tuple) and len(k) >= 3 and k[0] == "ch" and k[1] == mid
            for k in games
        )
        if any_live:
            continue
        await match_store.update_match(
            mid,
            {"status": "finished", "settle_reason": "bot restart — abandoned", "winner_id": None},
        )

    print(f"Restored {restored} active game panel(s); {len(games)} session(s) in memory.")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not rotate_status.is_running():
        rotate_status.start()
    await bot.change_presence(activity=STATUS_ROTATION[0])
    await restore_persisted_sessions(bot)


@bot.tree.command(name="help", description="How to play real Sudoku 9×9")
async def help_cmd(interaction: discord.Interaction):
    tiers = " · ".join(
        f"{meta['label']} ×{meta['multiplier']:.2f}"
        for meta in DIFFICULTY_TIERS.values()
    )
    embed = paper_embed("✏️ Sudoku")
    embed.add_field(
        name="Play",
        value="`/play` · `/daily` · `/challenge` (invite or open lobby)",
        inline=False,
    )
    embed.add_field(name="Step 1", value="Arrow pad → pick a 3×3 box", inline=True)
    embed.add_field(name="Step 2", value="Choose a cell", inline=True)
    embed.add_field(name="Step 3", value="Enter 1–9 (tap again to erase)", inline=True)
    embed.add_field(
        name="Rules",
        value="Red cells = row / column / box conflict. Pencil Mode for notes. "
        "**I QUITTT** (or `/quit`) leaves the board.",
        inline=False,
    )
    embed.add_field(
        name="Rewards",
        value=(
            f"Solve **+{BASE_WIN_REWARD}** · Daily **+{DAILY_BONUS}** · "
            f"Streak **+{STREAK_BONUS_PER}**/lvl · Hint **−{HINT_PENALTY}** · "
            f"Challenge win **×{CHALLENGE_WIN_MULT:g}** · loss **+{CHALLENGE_LOSER_COINS}**\n"
            f"{tiers}"
        ),
        inline=False,
    )
    embed.add_field(
        name="More",
        value="`/hint` `/shop` `/quit` `/leaderboard` `/stats` `/dailyboard`",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="play", description="Start a solo 9×9 Sudoku")
@app_commands.describe(difficulty="Puzzle difficulty")
@app_commands.choices(difficulty=DIFFICULTY_CHOICES)
async def play_cmd(
    interaction: discord.Interaction,
    difficulty: app_commands.Choice[str] | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    guild_id, user_id = interaction.guild.id, interaction.user.id
    if find_challenge_game_for_user(user_id):
        await interaction.response.send_message(
            "Finish your speedrun challenge first.",
            ephemeral=True,
        )
        return
    sk = solo_key(guild_id, user_id)
    if sk in games:
        await interaction.response.send_message(
            f"You already have a **{games[sk]['mode']}** game. Use **I QUITTT** or `/quit`.",
            ephemeral=True,
        )
        return

    diff_key = difficulty.value if difficulty else DEFAULT_DIFFICULTY
    board, given, solution = make_puzzle(diff_key)
    games[sk] = new_game_state(
        mode="solo",
        board=board,
        given=given,
        solution=solution,
        owner_id=user_id,
        channel_id=interaction.channel_id,
        guild_id=guild_id,
        difficulty=diff_key,
    )
    await start_panel(interaction, sk, games[sk])


@bot.tree.command(
    name="challenge",
    description="Speedrun challenge — invite players or open a Join lobby (2–5 total)",
)
@app_commands.describe(
    opponent="Optional first invitee (required unless open_lobby)",
    opponent2="Optional second opponent",
    opponent3="Optional third opponent",
    opponent4="Optional fourth opponent",
    open_lobby="Anyone can Join; you press Start (ignores opponent list)",
    difficulty="Shared puzzle difficulty",
)
@app_commands.choices(difficulty=DIFFICULTY_CHOICES)
async def challenge_cmd(
    interaction: discord.Interaction,
    opponent: discord.Member | None = None,
    opponent2: discord.Member | None = None,
    opponent3: discord.Member | None = None,
    opponent4: discord.Member | None = None,
    open_lobby: bool = False,
    difficulty: app_commands.Choice[str] | None = None,
):
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)
        return

    left = challenge_cooldown_remaining(interaction.user.id)
    if left > 0:
        await interaction.response.send_message(
            f"Challenge cooldown — try again in **{left}s**.",
            ephemeral=True,
        )
        return

    if find_challenge_game_for_user(interaction.user.id):
        await interaction.response.send_message(
            "You already have an active challenge.",
            ephemeral=True,
        )
        return
    sk_self = solo_key(interaction.guild.id, interaction.user.id)
    if sk_self in games:
        await interaction.response.send_message(
            "Finish your solo/daily game first (`/quit`).",
            ephemeral=True,
        )
        return

    diff_key = difficulty.value if difficulty else DEFAULT_DIFFICULTY
    tier = difficulty_label(diff_key)

    if open_lobby:
        mark_challenge_cooldown(interaction.user.id)
        view = OpenChallengeLobbyView(
            challenger_id=interaction.user.id,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            difficulty=diff_key,
        )
        await interaction.response.send_message(
            view._roster_text(
                f"🏁 {interaction.user.mention} opened a **{tier}** speedrun lobby. "
                f"Press **Join**, then challenger presses **Start**."
            ),
            view=view,
        )
        view.message = await interaction.original_response()
        return

    invitees: list[discord.Member] = []
    seen: set[int] = {interaction.user.id}
    for member in (opponent, opponent2, opponent3, opponent4):
        if member is None:
            continue
        if member.bot or member.id == interaction.user.id:
            await interaction.response.send_message(
                "Challenge real players only (not yourself/bots).",
                ephemeral=True,
            )
            return
        if member.id in seen:
            await interaction.response.send_message(
                "Each opponent can only be listed once.",
                ephemeral=True,
            )
            return
        seen.add(member.id)
        invitees.append(member)

    if not invitees:
        await interaction.response.send_message(
            "Pick at least one opponent, or set **open_lobby**.",
            ephemeral=True,
        )
        return
    if len(invitees) + 1 > MAX_CHALLENGE_PLAYERS:
        await interaction.response.send_message(
            f"Max {MAX_CHALLENGE_PLAYERS} players total (you + {MAX_CHALLENGE_PLAYERS - 1} opponents).",
            ephemeral=True,
        )
        return

    for uid in seen:
        if find_challenge_game_for_user(uid):
            await interaction.response.send_message(
                "Someone in this lobby already has an active challenge.",
                ephemeral=True,
            )
            return
        sk = solo_key(interaction.guild.id, uid)
        if sk in games:
            await interaction.response.send_message(
                "Everyone must finish open solo/daily games before challenging.",
                ephemeral=True,
            )
            return

    mark_challenge_cooldown(interaction.user.id)
    mentions = ", ".join(m.mention for m in invitees)
    view = ChallengeInviteView(
        challenger_id=interaction.user.id,
        invitee_ids=[m.id for m in invitees],
        guild_id=interaction.guild.id,
        channel_id=interaction.channel.id,
        difficulty=diff_key,
    )
    n = len(invitees) + 1
    await interaction.response.send_message(
        f"{mentions} — {interaction.user.mention} challenges you to a "
        f"**{tier}** speedrun (**{n} players**). Everyone must Accept for private boards — "
        "same puzzle, fastest wins. Challenger can Cancel.",
        view=view,
    )
    view.message = await interaction.original_response()


@bot.tree.command(name="daily", description="Play today's shared 9×9 daily Sudoku")
async def daily_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    guild_id, user_id = interaction.guild.id, interaction.user.id
    sk = solo_key(guild_id, user_id)
    if sk in games:
        await interaction.response.send_message(
            f"Finish your **{games[sk]['mode']}** game first (**I QUITTT** / `/quit`).",
            ephemeral=True,
        )
        return

    daily = get_guild_daily(bot.data, guild_id)
    if str(user_id) in daily["results"]:
        r = daily["results"][str(user_id)]
        state = "won" if r.get("won") else "finished"
        await interaction.response.send_message(
            f"You already **{state}** today's daily ({daily['date']}). See `/dailyboard`.",
            ephemeral=True,
        )
        return

    games[sk] = new_game_state(
        mode="daily",
        board=copy_grid(daily["board"]),
        given=[row[:] for row in daily["given"]],
        solution=copy_grid(daily["solution"]),
        owner_id=user_id,
        channel_id=interaction.channel_id,
        guild_id=guild_id,
        daily_date=daily["date"],
        difficulty=daily.get("difficulty_key") or daily_difficulty_for_date(daily["date"]),
    )
    await start_panel(interaction, sk, games[sk])


@bot.tree.command(name="dailyboard", description="Today's daily Sudoku rankings")
async def dailyboard_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    daily = get_guild_daily(bot.data, interaction.guild.id)
    results = daily.get("results") or {}
    if not results:
        await interaction.response.send_message(
            f"No results for **{daily['date']}** yet. Try `/daily`!",
            ephemeral=True,
        )
        return

    winners = [(uid, r) for uid, r in results.items() if r.get("won")]
    winners.sort(key=lambda item: (item[1].get("time", 10**9), item[1].get("hints", 99)))
    lines = []
    medals = ["1.", "2.", "3."]
    for i, (uid, r) in enumerate(winners[:10]):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        gstats = guild_stats(bot.data, interaction.guild.id)
        stats = user_stats(gstats, int(uid))
        name = display_name(stats) if stats.get("name") != "Unknown" else r.get("name", uid)
        lines.append(
            f"{prefix} **{name}** — {format_time(r.get('time', 0))} · {r.get('hints', 0)} hints"
        )

    failed = sum(1 for r in results.values() if not r.get("won"))
    embed = paper_embed(f"Daily #{daily_puzzle_number(daily['date'])}")
    embed.add_field(name="Date", value=daily["date"], inline=True)
    embed.add_field(name="Solved", value=str(len(winners)), inline=True)
    embed.add_field(name="Other", value=str(failed), inline=True)
    embed.add_field(
        name="Standings",
        value="\n".join(lines) if lines else "No solves yet.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="hint", description="Reveal one correct empty cell (uses inventory)")
async def hint_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    found = None
    sk = solo_key(interaction.guild.id, interaction.user.id)
    if sk in games and games[sk]["mode"] in ("solo", "daily"):
        found = (sk, games[sk])
    else:
        ch = find_challenge_game_for_user(interaction.user.id)
        if ch is not None:
            found = (ch, games[ch])

    if not found:
        await interaction.response.send_message("No active game. Start `/play` or `/daily`.", ephemeral=True)
        return

    key, game = found
    if interaction.user.id != game["owner_id"]:
        await interaction.response.send_message("Not your board.", ephemeral=True)
        return

    gstats = guild_stats(bot.data, interaction.guild.id)
    stats = user_stats(gstats, interaction.user.id)
    if stats["hints"] <= 0:
        await interaction.response.send_message("No hints left. Buy one in `/shop`.", ephemeral=True)
        return

    empties = empty_cells(game["board"])
    if not empties:
        await interaction.response.send_message("Board is already full!", ephemeral=True)
        return

    random.shuffle(empties)
    r, c = empties[0]
    digit = game["solution"][r][c]
    set_cell_value(game["board"], r, c, digit)
    game["hints_used"] = game.get("hints_used", 0) + 1
    stats["hints"] -= 1
    save_data(bot.data)
    await sync_challenge_board(game)
    await persist_game(key, game)

    await interaction.response.send_message(
        f"Hint: row **{r + 1}**, col **{c + 1}** → **{digit}**. "
        f"Hints left: **{stats['hints']}**.",
        ephemeral=True,
    )

    if is_complete(game["board"], game["solution"]) and not find_conflicts(game["board"]):
        if game.get("mode") == "challenge":
            finished_at = time.time()
            elapsed = finished_at - float(game["started_at"])
            match = await match_store.update_player(
                game["match_id"],
                game["player_slot"],
                {
                    "current_board": copy_grid(game["board"]),
                    "finished_time": finished_at,
                    "elapsed": elapsed,
                },
            )
            await remove_game(key)
            channel = bot.get_channel(game.get("channel_id"))
            if game.get("message_id") and channel is not None:
                try:
                    msg = await channel.fetch_message(game["message_id"])
                    image = render_board(
                        game["board"],
                        game["given"],
                        solution=game["solution"],
                        conflicts=set(),
                        difficulty=game.get("difficulty"),
                    )
                    done = paper_embed(
                        "Board complete",
                        description=f"Time: **{format_time(elapsed)}** (hint finish).",
                    )
                    file = attach_board(done, image)
                    await msg.edit(embed=done, view=None, attachments=[file])
                except discord.HTTPException:
                    pass
            if match and challenge_ready_to_settle(match):
                await settle_challenge_match(bot, match, reason="all finished")
            return

        embed = await finish_win_and_announce(
            bot,
            interaction.guild.id,
            interaction.user,
            game,
        )
        image = render_board(
            game["board"],
            game["given"],
            solution=game["solution"],
            conflicts=set(),
            difficulty=game.get("difficulty"),
        )
        file = attach_board(embed, image)
        await remove_game(key)
        channel = bot.get_channel(game.get("channel_id"))
        if game.get("message_id") and channel is not None:
            try:
                msg = await channel.fetch_message(game["message_id"])
                await msg.edit(embed=embed, view=None, attachments=[file])
            except discord.HTTPException:
                pass
        return

    channel = bot.get_channel(game.get("channel_id"))
    if game.get("message_id") and channel is not None:
        try:
            msg = await channel.fetch_message(game["message_id"])
            embed, file = board_file_for(game)
            await msg.edit(embed=embed, attachments=[file])
        except discord.HTTPException:
            pass


@bot.tree.command(name="shop", description="Buy titles and hints")
async def shop_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    gstats = guild_stats(bot.data, interaction.guild.id)
    stats = user_stats(gstats, interaction.user.id)
    stats["name"] = interaction.user.display_name
    save_data(bot.data)
    owned = ", ".join(SHOP_TITLES[t]["label"] for t in stats["owned_titles"] if t in SHOP_TITLES) or "None"
    equipped = SHOP_TITLES[stats["title"]]["label"] if stats.get("title") in SHOP_TITLES else "None"
    embed = paper_embed("✏️ Shop")
    embed.add_field(name="Balance", value=str(stats["coins"]), inline=True)
    embed.add_field(name="Title", value=equipped, inline=True)
    embed.add_field(name="Hints", value=str(stats["hints"]), inline=True)
    embed.add_field(name="Owned", value=owned, inline=False)
    await interaction.response.send_message(embed=embed, view=ShopView(bot), ephemeral=True)


@bot.tree.command(name="quit", description="I QUITTT — leave your active Sudoku game or challenge")
async def quit_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    ch_key = find_challenge_game_for_user(interaction.user.id)
    if ch_key is not None:
        game = games[ch_key]
        await interaction.response.defer(ephemeral=True)
        match_id = game["match_id"]
        slot = game["player_slot"]
        match = await match_store.update_player(
            match_id,
            slot,
            {"forfeit": True, "finished_time": None},
        )
        await remove_game(ch_key)
        if game.get("message_id"):
            channel = bot.get_channel(game.get("channel_id"))
            if channel is not None:
                try:
                    msg = await channel.fetch_message(game["message_id"])
                    await msg.edit(
                        embed=paper_embed("I QUITTT"),
                        view=None,
                        attachments=[],
                    )
                except discord.HTTPException:
                    pass
        if match:
            await settle_challenge_match(bot, match, reason="quit")
        await interaction.followup.send("I QUITTT — you're out of the challenge.", ephemeral=True)
        return

    sk = solo_key(guild_id, interaction.user.id)
    if sk in games:
        game = games[sk]
        message_id = game.get("message_id")
        channel_id = game.get("channel_id")
        embed = finish_forfeit(bot.data, guild_id, interaction.user, game)
        await remove_game(sk)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        if message_id:
            channel = bot.get_channel(channel_id)
            if channel is not None:
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.edit(embed=embed, view=None, attachments=[])
                except discord.HTTPException:
                    pass
        return

    await interaction.response.send_message("No game to quit.", ephemeral=True)


@bot.tree.command(name="leaderboard", description="Server leaderboards (coins, times, daily, challenge)")
@app_commands.describe(board="Which leaderboard to show")
@app_commands.choices(
    board=[
        app_commands.Choice(name="Coins", value="coins"),
        app_commands.Choice(name="Best time", value="time"),
        app_commands.Choice(name="Daily wins", value="daily"),
        app_commands.Choice(name="Challenge wins", value="challenge"),
    ]
)
async def leaderboard_cmd(
    interaction: discord.Interaction,
    board: app_commands.Choice[str] | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    mode = board.value if board else "coins"
    gstats = guild_stats(bot.data, interaction.guild.id)
    players = [(uid, user_stats(gstats, int(uid))) for uid, _ in iter_players(gstats)]

    if mode == "coins":
        ranked = sorted(players, key=lambda item: item[1].get("coins", 0), reverse=True)[:10]
        title = "Coins"
        fmt = lambda s: f"{s.get('coins', 0)} · {s.get('wins', 0)}W/{s.get('losses', 0)}L"
        nonempty = lambda s: s.get("coins", 0) > 0 or s.get("wins", 0) > 0
    elif mode == "time":
        ranked = sorted(
            ((uid, s) for uid, s in players if s.get("best_time") is not None),
            key=lambda item: item[1]["best_time"],
        )[:10]
        title = "Best time"
        fmt = lambda s: format_time(s["best_time"])
        nonempty = lambda s: True
    elif mode == "daily":
        ranked = sorted(players, key=lambda item: item[1].get("daily_wins", 0), reverse=True)[:10]
        title = "Daily wins"
        fmt = lambda s: f"{s.get('daily_wins', 0)} clears"
        nonempty = lambda s: s.get("daily_wins", 0) > 0
    else:
        ranked = sorted(players, key=lambda item: item[1].get("challenge_wins", 0), reverse=True)[:10]
        title = "Challenge wins"
        fmt = lambda s: f"{s.get('challenge_wins', 0)} wins"
        nonempty = lambda s: s.get("challenge_wins", 0) > 0

    ranked = [(uid, s) for uid, s in ranked if nonempty(s)]
    if not ranked:
        await interaction.response.send_message("No scores yet for this board.", ephemeral=True)
        return

    medals = ["1.", "2.", "3."]
    lines = []
    for i, (_, s) in enumerate(ranked[:10]):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} **{display_name(s)}** — {fmt(s)}")
    embed = paper_embed(f"Leaderboard · {title} · {interaction.guild.name}")
    embed.add_field(name="Top players", value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="View Sudoku stats")
@app_commands.describe(member="Optional member")
async def stats_cmd(interaction: discord.Interaction, member: discord.Member | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    target = member or interaction.user
    gstats = guild_stats(bot.data, interaction.guild.id)
    s = user_stats(gstats, target.id)
    s["name"] = target.display_name
    save_data(bot.data)
    best = format_time(s["best_time"]) if s.get("best_time") is not None else "—"
    title = SHOP_TITLES[s["title"]]["label"] if s.get("title") in SHOP_TITLES else "None"
    embed = paper_embed(f"Stats · {display_name(s)}")
    embed.add_field(name="Coins", value=str(s["coins"]), inline=True)
    embed.add_field(name="Title", value=title, inline=True)
    embed.add_field(name="Wins", value=str(s["wins"]), inline=True)
    embed.add_field(name="Losses", value=str(s["losses"]), inline=True)
    embed.add_field(name="Daily", value=str(s.get("daily_wins", 0)), inline=True)
    embed.add_field(name="Challenge", value=str(s.get("challenge_wins", 0)), inline=True)
    embed.add_field(name="Best time", value=best, inline=True)
    embed.add_field(name="Streak", value=str(s["streak"]), inline=True)
    embed.add_field(name="Best streak", value=str(s["best_streak"]), inline=True)
    embed.add_field(name="Hints", value=str(s.get("hints", 0)), inline=True)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "SEU_DISCORD_TOKEN_AQUI":
        raise SystemExit(
            "Missing DISCORD_TOKEN. Put it in .env:\n  DISCORD_TOKEN=seu_token_aqui"
        )
    bot.run(token)
