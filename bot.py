"""Discord Sudoku bot — real 9x9 puzzles with interactive panel."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass
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
CHALLENGE_WIN_MULT = 2.0  # extra multiplier for speedrun winners
MAX_CHALLENGE_PLAYERS = 5  # challenger + up to 4 opponents
CHALLENGE_LOSER_COINS = 25
CHALLENGE_COOLDOWN_SEC = 60
INVITE_TIMEOUT_SEC = 5 * 60
# Optional: set to your server ID for instant slash-command updates (global sync can lag).
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)

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

# Discord embed palette — Bikini Bottom (yellow / ocean / coral)
COLOR_PAPER = discord.Color.from_str("#FFE566")       # sponge yellow
COLOR_PAPER_WHITE = discord.Color.from_str("#FFF8DC")  # soft sand
COLOR_DANGER = discord.Color.from_str("#E11D48")      # jelly-red (forfeit / hard errors)
COLOR_OCEAN = discord.Color.from_str("#2DD4BF")       # lagoon teal

# Board theme — sunny Bikini Bottom grid
RGB_BG = "#7DD3FC"                # bright lagoon sky
RGB_CARD = "#FFFBEB"              # sandy paper panel
RGB_CARD_BORDER = "#F59E0B"       # pineapple gold rim
RGB_EMPTY = "#FFFEF5"             # empty cells
RGB_GIVEN_CELL = "#FEF3C7"        # soft sand wash for locked clues
RGB_SELECT = "#FDE047"            # selected cell — sponge yellow
RGB_BOX_HL = "#A5F3FC"            # selected 3×3 wash — bubble blue
RGB_CONFLICT = "#FDA4AF"          # soft coral conflict wash
RGB_LINE = "#94A3B8"              # soft sea-gray cell lines
RGB_THICK = "#0F766E"             # deep lagoon 3×3 borders
RGB_TEXT = "#1D4ED8"              # player ink — ocean blue
RGB_TEXT_GIVEN = "#134E4A"        # locked clues — deep teal
RGB_TEXT_CONFLICT = "#BE123C"
RGB_PENCIL = "#64748B"            # soft graphite notes
RGB_HEADER = "#0F766E"            # lagoon header
RGB_OUTLINE = "#F59E0B"           # gold selection ring

# Fixed Discord attachment canvas — width matches the button row in chat
# (Discord stretches both the image and action rows to the same message width).
BOARD_CANVAS = 540
BOARD_HEADER_H = 36
BOARD_CARD_PAD = 0          # full-bleed so the board aligns with the keyboard
BOARD_CARD_RADIUS = 0
BOARD_INNER_PAD = 10
BOARD_REWARD_H = 56

COLS = "ABCDEFGHI"
FONTS_DIR = Path(__file__).with_name("fonts")

# SpongeBob SquarePants economy (stored as "coins" in data)
SPONGE = "🧽"
BUBBLE = "🫧"
STAR = "⭐"
PINEAPPLE = "🍍"
JELLY = "🪼"
WAVE = "🌊"


def format_sponges(amount: int, *, signed: bool = False) -> str:
    """Display currency as sponge emojis."""
    n = int(amount)
    if signed:
        return f"+{n} {SPONGE}" if n >= 0 else f"{n} {SPONGE}"
    return f"{n} {SPONGE}"


WIN_TAUNTS = (
    f"{BUBBLE} I'm ready! I'm ready! Bikini Bottom is proud of you!",
    f"{SPONGE} Order up! Fresh sponges coming your way!",
    f"{WAVE} You did it! Even Squidward clapped (quietly).",
    f"{PINEAPPLE} Home sweet pineapple — puzzle crushed!",
    f"{JELLY} Jellyfishing? Nah — Sudoku fishing. Catch!",
)

SHOP_TITLES = {
    "rookie": {"label": "Jellyfisher", "cost": 50},
    "solver": {"label": "Fry Cook", "cost": 150},
    "row_master": {"label": "Boatmobile Ace", "cost": 300},
    "sudoku_pro": {"label": "Goofy Goober", "cost": 500},
    "legend": {"label": "Pineapple Legend", "cost": 1000},
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


async def load_persisted_game(key: tuple) -> dict | None:
    """Return an in-memory game, restoring from Mongo/memory store if needed."""
    if key in games:
        return games[key]
    gid = serialize_game_key(key)
    try:
        docs = await match_store.list_active_games()
    except Exception as exc:  # noqa: BLE001
        print(f"load_persisted_game failed: {exc}")
        return None
    for doc in docs:
        if (doc.get("_id") or doc.get("game_key")) != gid:
            continue
        raw = doc.get("game")
        if not isinstance(raw, dict):
            return None
        game = raw
        game["board"] = normalize_board(game.get("board") or [])
        game["solution"] = normalize_solution(game.get("solution"))
        game["participants"] = set(game.get("participants") or [game.get("owner_id")])
        game.pop("finishing", None)
        game.pop("_digit_lock", None)
        game.pop("rewarded", None)
        games[key] = game
        return game
    return None


def deepcopy_game(game: dict) -> dict:
    """JSON-safe clone of a live game dict."""
    out = {}
    for k, v in game.items():
        if k in ("participants",):
            out[k] = list(v) if v else []
        elif k in ("_digit_lock", "finishing", "rewarded"):
            continue  # ephemeral UI locks — don't persist
        elif k in ("board", "solution"):
            out[k] = copy_grid(v) if k == "board" else normalize_solution(v)
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
        puzzle = Sudoku(3, seed=seed).difficulty(weight)
    else:
        puzzle = Sudoku(3).difficulty(weight)
    solved = puzzle.solve()
    if solved is None:
        # Rare unsolvable draw — retry without a fixed seed
        return make_puzzle(difficulty, seed=None)

    board = [
        [make_cell(0 if cell is None else int(cell)) for cell in row]
        for row in puzzle.board
    ]
    given = [[cell["value"] != 0 for cell in row] for row in board]
    solution = [[int(cell) for cell in row] for row in solved.board]
    return board, given, solution


def daily_difficulty_for_date(day: str) -> str:
    """Map YYYY-MM-DD weekday → fixed daily difficulty key."""
    d = datetime.fromisoformat(day).date()
    return DAILY_WEEKDAY_DIFFICULTY[d.weekday()]


def make_daily_puzzle(
    guild_id: int,
    day: str,
    user_id: int,
) -> tuple[list[list[dict]], list[list[bool]], list[list[int]], str]:
    """Same day + difficulty for everyone; unique grid per player (anti-copy)."""
    diff_key = daily_difficulty_for_date(day)
    seed = int(
        hashlib.sha256(
            f"sudoku9x9:daily:{guild_id}:{day}:{user_id}".encode()
        ).hexdigest()[:16],
        16,
    )
    board, given, solution = make_puzzle(difficulty=diff_key, seed=seed)
    return board, given, solution, diff_key


def get_guild_daily(data: dict, guild_id: int) -> dict:
    """Daily meta for a guild: date, difficulty schedule, and per-user results (no shared board)."""
    gstats = guild_stats(data, guild_id)
    meta = gstats.setdefault("_daily", {})
    day = utc_today()
    expected_diff = daily_difficulty_for_date(day)
    needs_regen = (
        meta.get("date") != day
        or meta.get("difficulty") != difficulty_label(expected_diff)
    )
    if needs_regen:
        meta["date"] = day
        meta["difficulty"] = difficulty_label(expected_diff)
        meta["difficulty_key"] = expected_diff
        meta["results"] = {}
        # Drop legacy shared-board fields if present
        meta.pop("board", None)
        meta.pop("given", None)
        meta.pop("solution", None)
        save_data(data)
    else:
        meta.setdefault("difficulty_key", expected_diff)
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


def normalize_solution(solution: list | None) -> list[list[int]]:
    """Ensure solution is a 9×9 int grid (Mongo/JSON can coerce types)."""
    if not solution:
        return []
    return [[int(cell) for cell in row] for row in solution]


def is_complete(board: list[list[dict]], solution: list[list[int]]) -> bool:
    return values_grid(board) == normalize_solution(solution)


def is_solved(board: list[list[dict]], solution: list[list[int]] | None = None) -> bool:
    """True when the grid is a finished valid Sudoku (full + no conflicts).

    Prefer matching the stored solution, but a full conflict-free board still counts
    as solved — avoids false "not solved" nags when solution data drifts after restore.
    """
    if filled_count(board) < 81:
        return False
    if find_conflicts(board):
        return False
    if solution:
        sol = normalize_solution(solution)
        if sol and values_grid(board) == sol:
            return True
    return True


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
    difficulty: str | None = None,
    challenge_winner: bool = False,
) -> int:
    coins = BASE_WIN_REWARD + max(0, streak - 1) * STREAK_BONUS_PER
    if daily:
        coins += DAILY_BONUS
    coins = int(round(coins * difficulty_multiplier(difficulty)))
    if challenge_winner:
        coins = int(round(coins * CHALLENGE_WIN_MULT))
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
    """Bubbly Fredoka (SpongeBob vibe) from ./fonts, with system fallbacks.

    Note: KG Traditional Fractions is a *fraction-symbols* font (½, ⅓…) — it does not
    draw normal Sudoku digits 1–9, so we ship Fredoka (OFL) instead.
    """
    weight = 700 if bold else 500
    bundled = FONTS_DIR / "Fredoka-Variable.ttf"
    if bundled.exists():
        try:
            font = ImageFont.truetype(str(bundled), size)
            try:
                # axes: Weight 300–700, Width 75–125
                font.set_variation_by_axes([weight, 100])
            except Exception:
                pass
            return font
        except OSError:
            pass

    # Optional drop-in: any *.ttf placed in ./fonts (except OFL.txt)
    for path in sorted(FONTS_DIR.glob("*.ttf")) if FONTS_DIR.is_dir() else []:
        if path.name.startswith("Fredoka"):
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue

    if bold:
        candidates = (
            Path("C:/Windows/Fonts/seguiemj.ttf"),
            Path("C:/Windows/Fonts/segoeuib.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        )
    else:
        candidates = (
            Path("C:/Windows/Fonts/segoeui.ttf"),
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
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
    reward_sponges: int | None = None,
) -> BytesIO:
    """Bikini Bottom board — bubbly digits, lagoon colors, full-bleed panel.

    The grid fills the image edge-to-edge so it lines up with Discord's button row
    (same message width). When ``reward_sponges`` is set, a strip is drawn under the grid.
    """
    _ = solution
    conflicts = conflicts or set()
    canvas = BOARD_CANVAS
    header_h = BOARD_HEADER_H
    pad = BOARD_CARD_PAD
    radius = BOARD_CARD_RADIUS
    inner = BOARD_INNER_PAD
    reward_h = BOARD_REWARD_H if reward_sponges is not None else 0

    img = Image.new("RGB", (canvas, canvas + reward_h), RGB_CARD)
    draw = ImageDraw.Draw(img)

    # Full-width header bar (same width as the grid / Discord keyboard)
    draw.rectangle((0, 0, canvas, header_h), fill="#67E8F9")
    draw.line((0, header_h - 1, canvas, header_h - 1), fill=RGB_CARD_BORDER, width=2)

    header_label = f"~ {difficulty_label(difficulty)} ~"
    header_font = board_font(18, bold=True)
    hb = draw.textbbox((0, 0), header_label, font=header_font)
    htw, hth = hb[2] - hb[0], hb[3] - hb[1]
    draw.text(
        ((canvas - htw) / 2, (header_h - hth) / 2),
        header_label,
        fill=RGB_HEADER,
        font=header_font,
    )

    # Board card = full remaining area (no side gutters)
    card_bottom = canvas - pad
    card = (pad, header_h, canvas - pad, card_bottom)
    if radius > 0:
        draw.rounded_rectangle(card, radius=radius, fill=RGB_CARD, outline=RGB_CARD_BORDER, width=3)
    else:
        draw.rectangle(card, fill=RGB_CARD, outline=RGB_CARD_BORDER, width=3)

    grid_left = pad + inner
    grid_top = header_h + inner
    grid_right = canvas - pad - inner
    grid_bottom = card_bottom - inner
    grid_w = grid_right - grid_left
    grid_h = grid_bottom - grid_top
    cell = min(grid_w, grid_h) // 9
    grid = cell * 9
    origin_x = grid_left + (grid_w - grid) // 2
    origin_y = grid_top + (grid_h - grid) // 2

    font_player = board_font(max(24, cell * 28 // 48), bold=False)
    font_given = board_font(max(24, cell * 28 // 48), bold=True)
    pencil_font = board_font(max(14, cell * 16 // 48), bold=True)

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

            if (r, c) in conflicts:
                fill = RGB_CONFLICT
            elif selected == (r, c):
                fill = RGB_SELECT
            elif (r, c) in box_cells:
                fill = RGB_BOX_HL
            elif given[r][c]:
                fill = RGB_GIVEN_CELL
            else:
                fill = RGB_EMPTY

            draw.rectangle((x0, y0, x1, y1), fill=fill)

    # Cell lines first, then bold 3×3 charcoal borders
    for i in range(10):
        is_block = i % 3 == 0
        width_line = 3 if is_block else 1
        color = RGB_THICK if is_block else RGB_LINE
        pos_y = origin_y + i * cell
        pos_x = origin_x + i * cell
        draw.line((origin_x, pos_y, origin_x + grid, pos_y), fill=color, width=width_line)
        draw.line((pos_x, origin_y, pos_x, origin_y + grid), fill=color, width=width_line)

    draw.rectangle(card, outline=RGB_CARD_BORDER, width=3)

    # Selection rings (fills already tint cells — no wash overlay over ink)
    if highlight_box is not None and selected is None:
        br, bc = highlight_box // 3, highlight_box % 3
        bx0 = origin_x + bc * 3 * cell
        by0 = origin_y + br * 3 * cell
        bx1 = bx0 + 3 * cell
        by1 = by0 + 3 * cell
        draw.rectangle((bx0 + 1, by0 + 1, bx1 - 1, by1 - 1), outline=RGB_OUTLINE, width=4)

    if selected is not None:
        r, c = selected
        x0 = origin_x + c * cell
        y0 = origin_y + r * cell
        x1 = x0 + cell
        y1 = y0 + cell
        draw.rectangle((x0 + 1, y0 + 1, x1 - 1, y1 - 1), outline=RGB_OUTLINE, width=4)

    # Digits + pencil marks last so selection tint never washes them out
    for r in range(9):
        for c in range(9):
            x0 = origin_x + c * cell
            y0 = origin_y + r * cell
            val = cell_value(board, r, c)
            marks = list(board[r][c].get("pencil_marks") or [])

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
                inset = max(3, cell // 14)
                inner_m = cell - 2 * inset
                slot_w = inner_m / 3
                slot_h = inner_m / 3
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

    if reward_sponges is not None:
        _draw_reward_banner(draw, canvas=canvas, reward_h=reward_h, sponges=int(reward_sponges))

    out = BytesIO()
    img.save(out, format="PNG", compress_level=1)
    out.seek(0)
    return out


def _draw_reward_banner(
    draw: ImageDraw.ImageDraw,
    *,
    canvas: int,
    reward_h: int,
    sponges: int,
) -> None:
    """Full-width footer under the board: Aye aye! +N sponges."""
    y0 = canvas
    draw.rectangle((0, y0, canvas, canvas + reward_h), fill="#FFE566")
    draw.line((0, y0, canvas, y0), fill="#F59E0B", width=3)

    font = board_font(20, bold=True)
    line = f"Aye aye!  +{sponges}  sponges"
    bb = draw.textbbox((0, 0), line, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    chip = 22
    x = (canvas - tw - chip - 12) / 2
    y = y0 + (reward_h - th) / 2 - 1
    draw.text((x, y), line, fill="#0F766E", font=font)

    chip_x = x + tw + 10
    chip_y = y0 + (reward_h - chip) / 2
    draw.rounded_rectangle(
        (chip_x, chip_y, chip_x + chip, chip_y + chip),
        radius=6,
        fill="#F5D76E",
        outline="#C4A035",
        width=2,
    )
    for dx, dy in ((6, 7), (14, 10), (9, 16)):
        draw.ellipse(
            (chip_x + dx, chip_y + dy, chip_x + dx + 4, chip_y + dy + 4),
            fill="#E8C84A",
        )


def board_to_file(image: BytesIO) -> discord.File:
    """Standalone PNG attachment (full Discord image size — not embed thumbnail)."""
    image.seek(0)
    return discord.File(fp=BytesIO(image.read()), filename="sudoku.png")


def attach_board(embed: discord.Embed | None, image: BytesIO) -> discord.File:
    """Legacy helper: return file only (board is never nested in embeds)."""
    _ = embed
    return board_to_file(image)


# ---------------------------------------------------------------------------
# Game helpers
# ---------------------------------------------------------------------------

STAGE_BOX = "box"
STAGE_CELL = "cell"
STAGE_NUMBER = "number"

# Stage 1 — single-glyph arrows (fixed width, match digit/dot pads)
BOX_ARROW_LABELS = (
    "↖", "↑", "↗",
    "←", "·", "→",
    "↙", "↓", "↘",
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
        "solution": normalize_solution(solution),
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
    """Bikini Bottom embed shell — sunny yellow."""
    embed = discord.Embed(title=title, color=COLOR_PAPER)
    if description:
        embed.description = description
    return embed


@dataclass(frozen=True)
class WinOutcome:
    """Result of awarding a win — discord.Embed cannot carry custom attrs (2.7+)."""

    embed: discord.Embed
    coins: int = 0
    rank: int | None = None
    quiet: bool = False


def selected_cell(game: dict) -> tuple[int, int]:
    return game["sel_r"], game["sel_c"]


def board_caption(game: dict, *, status: str | None = None) -> str:
    """Legacy text caption — live boards stay silent (image + buttons only)."""
    _ = game, status
    return " "


def build_embed(game: dict, *, status: str | None = None) -> discord.Embed:
    """Text-only fallback — live boards use standalone attachments instead."""
    _ = status
    mode = game.get("mode", "solo")
    if mode == "daily":
        title = f"Daily · {game.get('daily_date', utc_today())}"
    elif mode == "challenge":
        title = "Challenge"
    else:
        title = "Sudoku"
    return paper_embed(title)


def board_file_for(game: dict, *, status: str | None = None) -> tuple[str, discord.File]:
    """Silent caption + large PNG attachment (no embed, no move chatter)."""
    _ = status
    conflicts = find_conflicts(game["board"])
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
    return " ", board_to_file(image)


# ---------------------------------------------------------------------------
# Rewards / finish
# ---------------------------------------------------------------------------

def daily_puzzle_number(day: str | None = None) -> int:
    """Sequential Daily Sudoku #N from a fixed epoch (Wordle-style)."""
    raw = day or utc_today()
    d = datetime.fromisoformat(raw).date()
    return (d - DAILY_EPOCH).days + 1


def daily_share_emoji_grid() -> str:
    """3×3 Wordle-like preview for a cleared daily."""
    row = "🟩🟩🟩"
    return "\n".join([row, row, row])


def build_daily_share_text(
    *,
    day: str,
    difficulty: str | None,
    elapsed: float,
) -> str:
    number = daily_puzzle_number(day)
    tier = difficulty_label(difficulty)
    grid = daily_share_emoji_grid()
    return (
        f"Daily Sudoku #{number}\n"
        f"{tier} · {format_time(elapsed)}\n"
        f"{grid}"
    )


def finish_win(
    data: dict,
    guild_id: int,
    user: discord.abc.User,
    game: dict,
    *,
    challenge_winner: bool = False,
    award: bool = True,
) -> WinOutcome:
    gstats = guild_stats(data, guild_id)
    stats = user_stats(gstats, user.id)
    stats["name"] = getattr(user, "display_name", user.name)
    elapsed = time.time() - game["started_at"]
    is_daily = game["mode"] == "daily"

    if not award and is_daily:
        # Duplicate claim (Discord retry / double-tap after win already saved).
        # Quiet marker — never post an "already claimed" nag.
        return WinOutcome(embed=paper_embed("Daily"), coins=0, quiet=True)

    stats["wins"] += 1
    stats["games"] += 1
    stats["streak"] += 1
    stats["best_streak"] = max(stats["best_streak"], stats["streak"])
    if stats["best_time"] is None or elapsed < stats["best_time"]:
        stats["best_time"] = int(elapsed)

    coins = win_reward(
        stats["streak"],
        daily=is_daily,
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
        winners.sort(key=lambda item: item[1].get("time", 10**9))
        for i, (uid, _) in enumerate(winners, start=1):
            if uid == str(user.id):
                rank = i
                break

    if challenge_winner:
        title = f"{SPONGE} Challenge won — I'm ready!"
    elif is_daily:
        title = f"{PINEAPPLE} Daily cleared — aye aye!"
    else:
        title = f"{SPONGE} Puzzle solved — yay!"

    embed = paper_embed(title)
    embed.description = random.choice(WIN_TAUNTS)
    embed.add_field(name="Time", value=format_time(elapsed), inline=True)
    embed.add_field(name="Difficulty", value=difficulty_label(game.get("difficulty")), inline=True)
    embed.add_field(name=f"Reward {SPONGE}", value=format_sponges(coins, signed=True), inline=True)
    embed.add_field(name=f"Streak {STAR}", value=str(stats["streak"]), inline=True)
    embed.add_field(name=f"Pocket {SPONGE}", value=format_sponges(stats["coins"]), inline=True)
    if rank is not None:
        embed.add_field(name="Rank", value=f"#{rank}", inline=True)
    else:
        embed.add_field(name="Mode", value=game["mode"].capitalize(), inline=True)
    return WinOutcome(embed=embed, coins=coins, rank=rank, quiet=False)


async def finish_win_and_announce(
    bot: "SudokuBot",
    guild_id: int,
    user: discord.abc.User,
    game: dict,
) -> WinOutcome:
    """Award win; for daily, gate rewards on first MongoDB claim (no channel spam)."""
    if game.get("mode") != "daily":
        return finish_win(bot.data, guild_id, user, game)

    day = game.get("daily_date") or utc_today()
    elapsed = int(time.time() - game["started_at"])
    tier = difficulty_label(game.get("difficulty"))
    gstats = guild_stats(bot.data, guild_id)
    stats = user_stats(gstats, user.id)
    preview_coins = win_reward(
        stats["streak"] + 1,
        daily=True,
        difficulty=game.get("difficulty"),
    )

    first = await match_store.try_claim_daily_win(
        guild_id=guild_id,
        user_id=user.id,
        day=day,
        elapsed=elapsed,
        hints=0,
        difficulty=tier,
        coins=preview_coins,
    )
    if not first:
        return finish_win(bot.data, guild_id, user, game, award=False)

    outcome = finish_win(bot.data, guild_id, user, game)
    share = build_daily_share_text(
        day=day,
        difficulty=game.get("difficulty"),
        elapsed=elapsed,
    )
    outcome.embed.add_field(name="Share", value=f"```\n{share}\n```", inline=False)
    return outcome


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
    note = " Daily attempt locked for today — see you at the Krusty Krab!" if game["mode"] == "daily" else ""
    return paper_embed(
        f"{WAVE} Quit",
        description=f"Streak wiped.{note}",
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
    content, file = board_file_for(game)
    msg = await destination.send(content=content, view=view, file=file)
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
        ).embed
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
        ranked_lines.append(f"Non-winners: {format_sponges(CHALLENGE_LOSER_COINS, signed=True)} consolation each")
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
    if not interaction.response.is_done():
        await interaction.response.defer()

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
        await interaction.edit_original_response(
            content=None,
            embed=paper_embed("Match missing"),
            view=None,
            attachments=[],
        )
        game.pop("finishing", None)
        game.pop("_digit_lock", None)
        view.stop()
        await remove_game(view.game_key)
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
    caption = f"**Board complete** · Time: **{format_time(elapsed)}**. {wait_msg}"
    file = board_to_file(image)
    await interaction.edit_original_response(
        content=caption,
        embed=None,
        view=None,
        attachments=[file],
    )
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
        "Quit",
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
        # Soft-disable while launching (don't stop() yet — abort must recover)
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

        async def _abort(msg: str) -> None:
            self._launching = False
            for child in self.children:
                child.disabled = False  # type: ignore[attr-defined]
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except discord.HTTPException:
                pass
            if self.message:
                try:
                    await self.message.edit(
                        content=self._status_text(msg),
                        view=self,
                    )
                except discord.HTTPException:
                    pass

        guild = interaction.guild
        if guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await _abort("Use this lobby in a server text channel.")
            return

        all_ids = [self.challenger_id, *sorted(self.accepted_ids)]
        for uid in all_ids:
            if find_challenge_game_for_user(uid):
                await _abort("Someone already has an active challenge — try again later.")
                return

        members: list[discord.Member] = []
        for uid in all_ids:
            m = guild.get_member(uid)
            if m is None:
                await _abort("Could not resolve all players.")
                return
            members.append(m)

        self._disable()
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
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Already starting…",
                        ephemeral=True,
                    )
            except discord.HTTPException:
                pass
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
        content, file = board_file_for(game)
        await interaction.response.edit_message(
            content=content,
            embed=None,
            attachments=[file],
            view=view,
        )
        view.message = interaction.message
        if interaction.message:
            game["message_id"] = interaction.message.id
        await persist_game(self.game_key, game)
        self.stop()


class ConfirmQuitView(discord.ui.View):
    """Ephemeral quit confirmation for challenge / daily / solo."""

    def __init__(
        self,
        game_key: tuple,
        bot: "SudokuBot",
        parent: "SudokuView | None" = None,
    ):
        super().__init__(timeout=30)
        self.game_key = game_key
        self.bot = bot
        self.parent = parent

    async def _edit_board_message(
        self,
        game: dict,
        *,
        embed: discord.Embed,
    ) -> None:
        channel = self.bot.get_channel(game.get("channel_id"))
        if not game.get("message_id") or channel is None:
            return
        try:
            msg = await channel.fetch_message(game["message_id"])
            await msg.edit(content=None, embed=embed, view=None, attachments=[])
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Quit", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        game = games.get(self.game_key)
        if not game:
            await interaction.response.edit_message(content="Game already ended.", view=None)
            self.stop()
            return
        if interaction.user.id != game["owner_id"]:
            await interaction.response.send_message("Not your board.", ephemeral=True)
            return

        mode = game.get("mode")
        await interaction.response.edit_message(content="Quitting…", view=None)
        self.stop()
        if self.parent is not None:
            self.parent.stop()

        if mode == "challenge":
            match_id = game["match_id"]
            slot = game["player_slot"]
            match = await match_store.update_player(
                match_id,
                slot,
                {"forfeit": True, "finished_time": None},
            )
            await remove_game(self.game_key)
            embed = paper_embed(
                "Quit",
                description="You're out. Remaining players keep racing.",
            )
            await self._edit_board_message(game, embed=embed)
            if match:
                await settle_challenge_match(self.bot, match, reason="quit")
            return

        guild = interaction.guild
        if guild is None:
            return
        embed = finish_forfeit(self.bot.data, guild.id, interaction.user, game)
        await remove_game(self.game_key)
        await self._edit_board_message(game, embed=embed)

    @discord.ui.button(label="Keep playing", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Still playing.", view=None)
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
        target = self._cid("nav:pencil")
        for child in self.children:
            if getattr(child, "custom_id", None) == target:
                child.label = "  Notes✓  " if pencil_on else "  Notes  "  # type: ignore[attr-defined]
                child.style = discord.ButtonStyle.success if pencil_on else discord.ButtonStyle.secondary  # type: ignore[attr-defined]
                break

    def _add_fixed_nav(self, game: dict, stage: str) -> None:
        """
        Row 3 — nav strip:
          Back | Notes | Quit
        """
        back = discord.ui.Button(
            label="  Back  ",
            style=discord.ButtonStyle.secondary,
            row=3,
            disabled=(stage == STAGE_BOX),
            custom_id=self._cid("nav:back"),
        )
        back.callback = self.on_nav_back
        self.add_item(back)

        pencil_on = game.get("pencil_mode", False)
        pencil = discord.ui.Button(
            label="  Notes✓  " if pencil_on else "  Notes  ",
            style=discord.ButtonStyle.success if pencil_on else discord.ButtonStyle.secondary,
            row=3,
            disabled=(stage != STAGE_NUMBER),
            custom_id=self._cid("nav:pencil"),
        )
        pencil.callback = self.on_toggle_pencil
        self.add_item(pencil)

        quit_btn = discord.ui.Button(
            label="  Quit  ",
            style=discord.ButtonStyle.danger,
            row=3,
            custom_id=self._cid("nav:quit"),
        )
        quit_btn.callback = self.on_forfeit
        self.add_item(quit_btn)

    async def on_nav_back(self, interaction: discord.Interaction) -> None:
        game = games.get(self.game_key)
        if not game:
            return
        stage = game.get("ui_stage", STAGE_BOX)
        if stage == STAGE_CELL:
            await self.on_back_to_grid(interaction)
        elif stage == STAGE_NUMBER:
            await self.on_back_to_cells(interaction)
        else:
            await interaction.response.defer()

    def _pad_label(self, text: str) -> str:
        """Pad labels so the 3-column keypad fills the message width more evenly."""
        t = (text or "·").strip()[:1] or "·"
        # Figure spaces keep Discord button columns visually wider / more aligned
        return f"\u2007\u2007{t}\u2007\u2007"

    def _build_stage_box(self, game: dict) -> None:
        for i, label in enumerate(BOX_ARROW_LABELS):
            btn = discord.ui.Button(
                label=self._pad_label(label),
                style=discord.ButtonStyle.secondary,
                row=i // 3,
                custom_id=self._cid(f"box:{i}"),
            )
            btn.callback = self._box_cb(i)
            self.add_item(btn)
        self._add_fixed_nav(game, STAGE_BOX)

    def _build_stage_cell(self, game: dict) -> None:
        conflicts = find_conflicts(game["board"])
        box_id = game.get("box_id", 0)
        for i in range(9):
            r, c = cell_in_box(box_id, i)
            val = cell_value(game["board"], r, c)
            given = game["given"][r][c]

            if given:
                style = discord.ButtonStyle.secondary
                label = self._pad_label(str(val))
                disabled = True
            elif (r, c) in conflicts:
                style = discord.ButtonStyle.danger
                label = self._pad_label(str(val) if val else "·")
                disabled = False
            elif val:
                style = discord.ButtonStyle.secondary
                label = self._pad_label(str(val))
                disabled = False
            else:
                # Always a single dot — pencil marks live on the board image only
                style = discord.ButtonStyle.secondary
                label = "·"
                disabled = False

            btn = discord.ui.Button(
                label=label,
                style=style,
                row=i // 3,
                disabled=disabled,
                custom_id=self._cid(f"cell:{box_id}:{i}"),
            )
            btn.callback = self._cell_cb(i)
            self.add_item(btn)
        self._add_fixed_nav(game, STAGE_CELL)

    def _build_stage_number(self, game: dict) -> None:
        for d in range(1, 10):
            btn = discord.ui.Button(
                label=self._pad_label(str(d)),
                style=discord.ButtonStyle.secondary,
                row=(d - 1) // 3,
                custom_id=self._cid(f"num:{d}"),
            )
            btn.callback = self._digit_cb(d)
            self.add_item(btn)
        self._add_fixed_nav(game, STAGE_NUMBER)

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
        ended: bool = False,
        embed: discord.Embed | None = None,
    ) -> None:
        # Defer immediately — Render can exceed Discord's 3s limit while rendering PNG / Mongo.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.HTTPException:
            pass

        try:
            game = games.get(self.game_key)
            if ended or not game:
                final = embed or paper_embed("Game over")
                self.stop()
                await interaction.edit_original_response(
                    content=None,
                    embed=final,
                    view=None,
                    attachments=[],
                )
                return

            self.rebuild(game)
            content, file = board_file_for(game)
            await interaction.edit_original_response(
                content=content,
                embed=None,
                attachments=[file],
                view=self,
            )
            await persist_game(self.game_key, game)
        except Exception:
            import traceback

            traceback.print_exc()
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "Something went wrong updating the board. Try again.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass
            else:
                try:
                    await interaction.followup.send(
                        "Something went wrong updating the board. Try again.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass


    async def on_pick_box(self, interaction: discord.Interaction, box_id: int) -> None:
        game = games[self.game_key]
        game["box_id"] = box_id
        game["ui_stage"] = STAGE_CELL
        await self.refresh(interaction)

    async def on_pick_cell(self, interaction: discord.Interaction, index: int) -> None:
        game = games[self.game_key]
        r, c = cell_in_box(game["box_id"], index)
        if game["given"][r][c]:
            await self.refresh(interaction)
            return
        game["sel_r"], game["sel_c"] = r, c
        game["ui_stage"] = STAGE_NUMBER
        await self.refresh(interaction)

    async def on_back_to_grid(self, interaction: discord.Interaction) -> None:
        game = games[self.game_key]
        game["ui_stage"] = STAGE_BOX
        game["pencil_mode"] = False
        await self.refresh(interaction)

    async def on_back_to_cells(self, interaction: discord.Interaction) -> None:
        game = games[self.game_key]
        game["ui_stage"] = STAGE_CELL
        await self.refresh(interaction)

    async def on_toggle_pencil(self, interaction: discord.Interaction) -> None:
        game = games[self.game_key]
        game["pencil_mode"] = not game.get("pencil_mode", False)
        await self.refresh(interaction)

    async def on_digit(self, interaction: discord.Interaction, digit: int) -> None:
        game = games.get(self.game_key)
        if not game:
            try:
                await interaction.response.send_message("This game has ended.", ephemeral=True)
            except discord.HTTPException:
                pass
            return

        if game.get("finishing"):
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"{SPONGE} Already solved — rewards are posting!",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"{SPONGE} Already solved — rewards are posting!",
                        ephemeral=True,
                    )
            except discord.HTTPException:
                pass
            return

        # Serialize digit clicks: a Discord retry / double-tap used to toggle-erase
        # the last number before the board image refreshed (looked like it "didn't stick").
        if game.get("_digit_lock"):
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except discord.HTTPException:
                pass
            return

        r, c = selected_cell(game)

        if game["given"][r][c]:
            game["ui_stage"] = STAGE_CELL
            await self.refresh(interaction)
            return

        # Pencil mode: toggle draft marks only (never erase a placed digit)
        if game.get("pencil_mode"):
            if cell_value(game["board"], r, c):
                await self.refresh(interaction)
                try:
                    await interaction.followup.send(
                        f"**{cell_label(r, c)}** has a number — tap that digit again to erase, then use Notes.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass
                return
            toggle_pencil(game["board"], r, c, digit)
            game["ui_stage"] = STAGE_NUMBER
            await self.refresh(interaction)
            await sync_challenge_board(game)
            return

        # Pen mode: lock briefly around the mutation, then release before slow PNG refresh
        # so an immediate re-tap (erase) is not swallowed.
        game["_digit_lock"] = True
        try:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except discord.HTTPException:
                pass

            current = cell_value(game["board"], r, c)
            if current == digit:
                # Re-tap same digit = erase (unless this completes the puzzle)
                if is_solved(game["board"], game.get("solution")):
                    game["finishing"] = True
                    await self._celebrate_win(interaction, game)
                    return
                set_cell_value(game["board"], r, c, 0)
                game["board"][r][c]["pencil_marks"] = []
                # Stay on the number pad so you can type a new digit right away
                game["ui_stage"] = STAGE_NUMBER
                game["_digit_lock"] = False
                await self.refresh(interaction)
                await sync_challenge_board(game)
                return

            set_cell_value(game["board"], r, c, digit)
            conflicts = find_conflicts(game["board"])
            full = filled_count(game["board"]) >= 81

            # Win: full board with no conflicts (and/or matches stored solution)
            if is_solved(game["board"], game.get("solution")):
                game["finishing"] = True  # before any await — blocks concurrent erase
                await sync_challenge_board(game)
                await self._celebrate_win(interaction, game)
                return

            await sync_challenge_board(game)

            # Conflict (red) or board full-but-wrong — keep pad open for re-tap erase
            if (r, c) in conflicts or full:
                game["ui_stage"] = STAGE_NUMBER
                game["_digit_lock"] = False
                await self.refresh(interaction)
                # Only nag when the board is full AND still has conflicts
                if full and conflicts:
                    try:
                        await interaction.followup.send(
                            f"{BUBBLE} Board is full but has conflicts — fix the red cells.",
                            ephemeral=True,
                        )
                    except discord.HTTPException:
                        pass
                return

            # Clean placement — return to cell picker for the next empty cell
            game["ui_stage"] = STAGE_CELL
            game["_digit_lock"] = False
            await self.refresh(interaction)
        finally:
            g = games.get(self.game_key)
            if g is not None and not g.get("finishing"):
                g["_digit_lock"] = False

    async def _celebrate_win(self, interaction: discord.Interaction, game: dict) -> None:
        """Award sponges and replace the live board with the completed panel + rewards."""
        game["finishing"] = True
        guild_id = None
        if interaction.guild is not None:
            guild_id = interaction.guild.id
        elif game.get("guild_id") is not None:
            guild_id = int(game["guild_id"])

        try:
            if game.get("mode") == "challenge":
                await handle_challenge_completion(self.bot, interaction, game, self)
                return

            if guild_id is None:
                await interaction.followup.send(
                    "Couldn't award rewards (missing server). Board is complete — use `/quit` if stuck.",
                    ephemeral=True,
                )
                game["finishing"] = False
                await self.refresh(interaction)
                return

            if game.get("rewarded"):
                # Award already done but UI may have failed — finish the panel once.
                try:
                    image = render_board(
                        game["board"],
                        game["given"],
                        solution=game["solution"],
                        conflicts=set(),
                        difficulty=game.get("difficulty"),
                    )
                    file = board_to_file(image)
                    key = self.game_key
                    await remove_game(key)
                    self.stop()
                    await interaction.edit_original_response(
                        content=f"{SPONGE} **Aye aye — puzzle solved!**",
                        embed=None,
                        view=None,
                        attachments=[file],
                    )
                except Exception:
                    game["finishing"] = False
                    game.pop("_digit_lock", None)
                return

            outcome = await finish_win_and_announce(
                self.bot,
                guild_id,
                interaction.user,
                game,
            )
            game["rewarded"] = True
            embed = outcome.embed
            coins = int(outcome.coins)
            quiet = bool(outcome.quiet)
            image = render_board(
                game["board"],
                game["given"],
                solution=game["solution"],
                conflicts=set(),
                difficulty=game.get("difficulty"),
                reward_sponges=None if quiet else coins,
            )
            file = board_to_file(image)
            key = self.game_key
            await remove_game(key)
            self.stop()

            if quiet:
                # Already awarded — close controls only, no extra chat message.
                try:
                    await interaction.edit_original_response(
                        content=None,
                        embed=None,
                        view=None,
                        attachments=[file],
                    )
                except discord.HTTPException:
                    pass
                return

            reward_line = f"{SPONGE} **I'm ready!** Solved · {format_sponges(coins, signed=True)}"
            try:
                await interaction.edit_original_response(
                    content=reward_line,
                    embed=embed,
                    view=None,
                    attachments=[file],
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    content=reward_line,
                    embed=embed,
                    file=file,
                )
        except Exception:
            import traceback

            traceback.print_exc()
            # Keep the correct digit on the board and recover the panel
            if self.game_key in games:
                game["finishing"] = False
                game.pop("_digit_lock", None)
                try:
                    await self.refresh(interaction)
                except Exception:
                    pass
            try:
                await interaction.followup.send(
                    f"{BUBBLE} You finished the puzzle, but rewards failed to post. "
                    f"Try `/stats` — if sponges didn't update, ping an admin.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def on_forfeit(self, interaction: discord.Interaction) -> None:
        game = games.get(self.game_key)
        if not game:
            await interaction.response.send_message("This game has ended.", ephemeral=True)
            return
        if interaction.user.id != game["owner_id"]:
            await interaction.response.send_message("Only the owner can quit.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        mode = game.get("mode")
        if mode == "challenge":
            prompt = "Really leave this speedrun?"
        elif mode == "daily":
            prompt = "Quit today's daily? This locks your attempt and resets your streak."
        else:
            prompt = "Really quit this puzzle? Streak will reset."

        await interaction.response.send_message(
            prompt,
            view=ConfirmQuitView(self.game_key, self.bot, self),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Shop
# ---------------------------------------------------------------------------

class ShopSelect(discord.ui.Select):
    def __init__(self, bot: "SudokuBot"):
        self.bot = bot
        options = [
            discord.SelectOption(
                label=f"{meta['label']} — {meta['cost']} {SPONGE}",
                value=f"title:{tid}",
                description="Cosmetic title",
            )
            for tid, meta in SHOP_TITLES.items()
        ]
        super().__init__(placeholder="Buy a title…", options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        choice = self.values[0]
        gstats = guild_stats(self.bot.data, interaction.guild.id)
        stats = user_stats(gstats, interaction.user.id)
        stats["name"] = interaction.user.display_name

        if not choice.startswith("title:"):
            await interaction.response.send_message("Unknown item.", ephemeral=True)
            return

        tid = choice.split(":", 1)[1]
        meta = SHOP_TITLES[tid]
        if tid in stats["owned_titles"]:
            stats["title"] = tid
            save_data(self.bot.data)
            await interaction.response.send_message(f"Equipped **{meta['label']}**.", ephemeral=True)
            return
        if stats["coins"] < meta["cost"]:
            await interaction.response.send_message(
                f"Need **{format_sponges(meta['cost'])}** (you have {format_sponges(stats['coins'])}).",
                ephemeral=True,
            )
            return
        stats["coins"] -= meta["cost"]
        stats["owned_titles"].append(tid)
        stats["title"] = tid
        save_data(self.bot.data)
        await interaction.response.send_message(
            f"Bought **{meta['label']}** (−{meta['cost']} {SPONGE}). "
            f"Balance: **{format_sponges(stats['coins'])}**.",
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

        # Global sync can take minutes; guild sync is instant for that server.
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(guild_synced)} slash command(s) to guild {DISCORD_GUILD_ID}.")
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global slash command(s).")


bot = SudokuBot()

STATUS_ROTATION = [
    discord.Game(name=f"{SPONGE} /play · I'm ready!"),
    discord.Game(name=f"{WAVE} /daily · Pineapple puzzle"),
    discord.Game(name=f"{JELLY} /challenge · Jellyfish race"),
    discord.Game(name=f"{SPONGE} /shop · Goofy Goober titles"),
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
    content, file = board_file_for(game)
    await interaction.response.send_message(content=content, view=view, file=file)
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
        game["solution"] = normalize_solution(game.get("solution"))
        game["participants"] = set(game.get("participants") or [game.get("owner_id")])
        game.pop("finishing", None)
        game.pop("_digit_lock", None)
        game.pop("rewarded", None)
        games[key] = game

        channel = bot.get_channel(game.get("channel_id"))
        if channel is None or not game.get("message_id"):
            continue
        try:
            msg = await channel.fetch_message(game["message_id"])
            view = SudokuView(key, bot)
            content, file = board_file_for(game)
            await msg.edit(
                content=content,
                embed=None,
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


@bot.tree.command(name="help", description="I'm ready! How to play Bikini Bottom Sudoku")
async def help_cmd(interaction: discord.Interaction):
    tiers = " · ".join(
        f"{meta['label']} ×{meta['multiplier']:.2f}"
        for meta in DIFFICULTY_TIERS.values()
    )
    embed = paper_embed(f"{SPONGE} Sudoku · Bikini Bottom")
    embed.description = (
        f"{WAVE} Ahoy! Fill 1–9 in every row, column, and box. "
        f"Earn **sponges** {SPONGE} — no duplicate numbers, only vibes."
    )
    embed.add_field(
        name=f"{BUBBLE} Play",
        value=(
            "`/play` — solo puzzle (I'm ready!)\n"
            "`/daily` — one pineapple puzzle a day\n"
            "`/challenge` — race your pals in a private board"
        ),
        inline=False,
    )
    embed.add_field(name="Step 1", value="Arrow pad → pick a 3×3 box", inline=True)
    embed.add_field(name="Step 2", value="Choose a cell", inline=True)
    embed.add_field(name="Step 3", value="Enter 1–9 (tap again to erase) · Notes = doodles", inline=True)
    embed.add_field(
        name="Rules",
        value="Red cells = row / column / box clash. **Notes** for doodle marks. "
        "**Quit** (or `/quit`) leaves the board.",
        inline=False,
    )
    embed.add_field(
        name=f"Rewards {SPONGE}",
        value=(
            f"Solve **{format_sponges(BASE_WIN_REWARD, signed=True)}** · "
            f"Daily **{format_sponges(DAILY_BONUS, signed=True)}** · "
            f"Streak **{format_sponges(STREAK_BONUS_PER, signed=True)}**/lvl · "
            f"Challenge win **×{CHALLENGE_WIN_MULT:g}** · "
            f"loss **{format_sponges(CHALLENGE_LOSER_COINS, signed=True)}**\n"
            f"{tiers}"
        ),
        inline=False,
    )
    embed.add_field(
        name="More",
        value="`/shop` `/quit` `/leaderboard` `/stats` `/dailyboard`",
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
            f"You already have a **{games[sk]['mode']}** game. Use **Quit** or `/quit`.",
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
    try:
        await start_panel(interaction, sk, games[sk])
    except Exception:
        await remove_game(sk)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Couldn't start the board. Try again.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Couldn't start the board. Try again.",
                ephemeral=True,
            )


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
                f"🏁 {interaction.user.mention} opened a **{tier}** jellyfishing race! "
                f"Press **Join**, then challenger presses **Start**. I'm ready!"
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


@bot.tree.command(name="daily", description="Play today's daily Sudoku (same level, unique board)")
async def daily_cmd(interaction: discord.Interaction):
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
            f"Finish your **{games[sk]['mode']}** game first (**Quit** / `/quit`).",
            ephemeral=True,
        )
        return

    daily = get_guild_daily(bot.data, guild_id)
    if str(user_id) in daily["results"]:
        r = daily["results"][str(user_id)]
        if r.get("in_progress"):
            restored = await load_persisted_game(sk)
            if restored and restored.get("mode") == "daily":
                await interaction.response.send_message(
                    "You already started today's daily — continue on your board message "
                    "(use **Refresh** if the buttons timed out).",
                    ephemeral=True,
                )
                return
            # Orphan lock (restart without recoverable session) — unlock and continue
            daily["results"].pop(str(user_id), None)
            save_data(bot.data)
        else:
            if r.get("won"):
                detail = "cleared"
            elif r.get("forfeit"):
                detail = "used (quit)"
            else:
                detail = "already used"
            await interaction.response.send_message(
                f"You've already **{detail}** today's daily ({daily['date']}). "
                f"Only **one** daily attempt per day — play more with `/play`, or check `/dailyboard`.",
                ephemeral=True,
            )
            return

    # Lock the attempt immediately so a restart can't grant a second daily
    daily["results"][str(user_id)] = {
        "won": False,
        "in_progress": True,
        "name": interaction.user.display_name,
    }
    save_data(bot.data)

    board, given, solution, diff_key = make_daily_puzzle(guild_id, daily["date"], user_id)
    games[sk] = new_game_state(
        mode="daily",
        board=board,
        given=given,
        solution=solution,
        owner_id=user_id,
        channel_id=interaction.channel_id,
        guild_id=guild_id,
        daily_date=daily["date"],
        difficulty=diff_key,
    )
    try:
        await start_panel(interaction, sk, games[sk])
    except Exception:
        await remove_game(sk)
        daily["results"].pop(str(user_id), None)
        save_data(bot.data)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Couldn't start the daily board. Try again.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Couldn't start the daily board. Try again.",
                ephemeral=True,
            )


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
    winners.sort(key=lambda item: item[1].get("time", 10**9))
    lines = []
    medals = ["1.", "2.", "3."]
    for i, (uid, r) in enumerate(winners[:10]):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        gstats = guild_stats(bot.data, interaction.guild.id)
        stats = user_stats(gstats, int(uid))
        name = display_name(stats) if stats.get("name") != "Unknown" else r.get("name", uid)
        lines.append(
            f"{prefix} **{name}** — {format_time(r.get('time', 0))}"
        )

    failed = sum(
        1
        for r in results.values()
        if not r.get("won") and not r.get("in_progress")
    )
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


@bot.tree.command(name="shop", description="Buy cosmetic titles")
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
    embed = paper_embed(f"{SPONGE} Krusty Shop")
    embed.description = f"{BUBBLE} Spend sponges on goofy titles. Fancy!"
    embed.add_field(name=f"Pocket {SPONGE}", value=format_sponges(stats["coins"]), inline=True)
    embed.add_field(name="Title", value=equipped, inline=True)
    embed.add_field(name="Owned", value=owned, inline=False)
    await interaction.response.send_message(embed=embed, view=ShopView(bot), ephemeral=True)


@bot.tree.command(name="quit", description="Leave your active Sudoku game or challenge")
async def quit_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    ch_key = find_challenge_game_for_user(interaction.user.id)
    if ch_key is not None:
        await interaction.response.send_message(
            "Really leave this speedrun?",
            view=ConfirmQuitView(ch_key, bot, None),
            ephemeral=True,
        )
        return

    sk = solo_key(guild_id, interaction.user.id)
    if sk in games:
        game = games[sk]
        if game.get("mode") == "daily":
            prompt = "Quit today's daily? This locks your attempt and resets your streak."
        else:
            prompt = "Really quit this puzzle? Streak will reset."
        await interaction.response.send_message(
            prompt,
            view=ConfirmQuitView(sk, bot, None),
            ephemeral=True,
        )
        return

    await interaction.response.send_message("No game to quit.", ephemeral=True)


@bot.tree.command(name="leaderboard", description="Server leaderboards (sponges, times, daily, challenge)")
@app_commands.describe(board="Which leaderboard to show")
@app_commands.choices(
    board=[
        app_commands.Choice(name="Sponges", value="coins"),
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
        title = f"{SPONGE} Sponges"
        fmt = lambda s: f"{format_sponges(s.get('coins', 0))} · {s.get('wins', 0)}W/{s.get('losses', 0)}L"
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
    embed.add_field(name="Sponges", value=format_sponges(s["coins"]), inline=True)
    embed.add_field(name="Title", value=title, inline=True)
    embed.add_field(name="Wins", value=str(s["wins"]), inline=True)
    embed.add_field(name="Losses", value=str(s["losses"]), inline=True)
    embed.add_field(name="Daily", value=str(s.get("daily_wins", 0)), inline=True)
    embed.add_field(name="Challenge", value=str(s.get("challenge_wins", 0)), inline=True)
    embed.add_field(name="Best time", value=best, inline=True)
    embed.add_field(name="Streak", value=str(s["streak"]), inline=True)
    embed.add_field(name="Best streak", value=str(s["best_streak"]), inline=True)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "SEU_DISCORD_TOKEN_AQUI":
        raise SystemExit(
            "Missing DISCORD_TOKEN. Put it in .env:\n  DISCORD_TOKEN=seu_token_aqui"
        )
    bot.run(token)
