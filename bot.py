"""Discord Sudoku bot — real 9x9 puzzles with interactive panel."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import asyncio
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

from challenge_store import create_match_store, match_player_entries, new_match_document

DATA_FILE = Path(__file__).with_name("leaderboard.json")
VIEW_TIMEOUT = 20 * 60
DEFAULT_DIFFICULTY = "medium"

# key → target clue count (unique solution), display label, coin multiplier on win
# Clue targets follow human-style bands — NEVER "fewer clues = more solutions".
DIFFICULTY_TIERS: dict[str, dict] = {
    "very_easy": {"label": "Very Easy", "clues": 50, "multiplier": 0.80},
    "easy": {"label": "Easy", "clues": 44, "multiplier": 1.00},
    "medium": {"label": "Medium", "clues": 38, "multiplier": 1.25},
    "hard": {"label": "Hard", "clues": 32, "multiplier": 1.60},
    "very_hard": {"label": "Very Hard", "clues": 30, "multiplier": 2.20},
    "expertttt": {"label": "Expertttt", "clues": 28, "multiplier": 3.00},
}

DIFFICULTY_CHOICES = [
    app_commands.Choice(name=meta["label"], value=key)
    for key, meta in DIFFICULTY_TIERS.items()
]

BASE_WIN_REWARD = 75
DAILY_BONUS = 60
STREAK_BONUS_PER = 10
GARY_WISDOM_HINT_BONUS = 3
GARY_WISDOM_GAMES_PER_PURCHASE = 2
KRABBY_SNACK_MULT = 1.25
GOLDEN_SPATULA_MULT = 1.50
REWARD_BOOST_GAMES_PER_PURCHASE = 3
HINT_SPONGE_COST = 15

# Ordered list of difficulty keys (matches DIFF_KEYS in sudoku-core.js)
DIFF_KEYS_LIST: list[str] = list(DIFFICULTY_TIERS.keys())


def difficulty_index(key: str) -> int:
    """Return the 0-based index of a difficulty key for the Activity client."""
    # Accept keys ("very_easy") or labels ("Very Easy"); unknown → medium.
    canonical = difficulty_key_from_label(key) if key else DEFAULT_DIFFICULTY
    try:
        return DIFF_KEYS_LIST.index(canonical)
    except ValueError:
        return DIFF_KEYS_LIST.index(DEFAULT_DIFFICULTY)


def resolve_session_difficulty(session: dict | None) -> tuple[str, int]:
    """Return (canonical_key, diff_index), keeping both fields in sync.

    Prefer diff_index when present (/play slash-command source of truth).
    Otherwise derive from difficulty label/key. Falls back to medium.
    """
    medium_idx = DIFF_KEYS_LIST.index(DEFAULT_DIFFICULTY)
    if not session:
        return DEFAULT_DIFFICULTY, medium_idx
    if session.get("diff_index") is not None:
        try:
            idx = int(session["diff_index"])
            if 0 <= idx < len(DIFF_KEYS_LIST):
                return DIFF_KEYS_LIST[idx], idx
        except (TypeError, ValueError):
            pass
    raw = session.get("difficulty")
    if raw:
        key = difficulty_key_from_label(str(raw))
        try:
            return key, DIFF_KEYS_LIST.index(key)
        except ValueError:
            return DEFAULT_DIFFICULTY, medium_idx
    return DEFAULT_DIFFICULTY, medium_idx


def session_difficulty_key(session: dict | None) -> str | None:
    """Canonical difficulty key from an activity session doc, or None if unset."""
    if not session:
        return None
    if session.get("diff_index") is None and not session.get("difficulty"):
        return None
    key, _idx = resolve_session_difficulty(session)
    return key

CHALLENGE_WIN_MULT = 2.0  # extra multiplier for speedrun winners
MAX_CHALLENGE_PLAYERS = 5  # challenger + up to 4 opponents
CHALLENGE_LOSER_COINS = 15
# Last standing when opponents forfeit — sponges only, no best_time / full win payout.
CHALLENGE_FORFEIT_WIN_COINS = 40
CHALLENGE_COOLDOWN_SEC = 60
INVITE_TIMEOUT_SEC = 5 * 60
DAILY_EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc).date()
# Optional: set to your server ID for instant slash-command updates (global sync can lag).
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DISCORD_GUILD_ID = _env_int("DISCORD_GUILD_ID", 0)
ACTIVITY_WATCH_CHANNEL_ID = _env_int("ACTIVITY_WATCH_CHANNEL_ID", 1527293243434209300)
DAILY_ANNOUNCE_CHANNEL_ID = _env_int("DAILY_ANNOUNCE_CHANNEL_ID", 1527293243434209300)

# Hard-coded bot admins for /z-admin resetdaily, resetchallenge, claimdaily (not Discord role-based).
BOT_ADMIN_IDS = frozenset(
    {
        507706734035599360,
        1500912704280596571,
    }
)


def is_bot_admin(user_id: int | None) -> bool:
    try:
        return int(user_id or 0) in BOT_ADMIN_IDS
    except (TypeError, ValueError):
        return False


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

# Board theme — sunny Bikini Bottom grid (default palette)
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
RGB_HEADER_BAR = "#67E8F9"        # header strip fill
RGB_OUTLINE = "#F59E0B"           # gold selection ring

DEFAULT_BOARD_PALETTE = {
    "header_bar": RGB_HEADER_BAR,
    "header_text": RGB_HEADER,
    "card": RGB_CARD,
    "card_border": RGB_CARD_BORDER,
    "empty": RGB_EMPTY,
    "given_cell": RGB_GIVEN_CELL,
    "select": RGB_SELECT,
    "box_hl": RGB_BOX_HL,
    "conflict": RGB_CONFLICT,
    "line": RGB_LINE,
    "thick": RGB_THICK,
    "text": RGB_TEXT,
    "text_given": RGB_TEXT_GIVEN,
    "text_conflict": RGB_TEXT_CONFLICT,
    "pencil": RGB_PENCIL,
    "outline": RGB_OUTLINE,
}

# Fixed Discord attachment canvas — larger = bigger chat preview (full-bleed with keypad)
# Taller header for mobile-readable titles; canvas grown so the 9×9 stays roomy.
BOARD_CANVAS = 860
BOARD_HEADER_H = 72
BOARD_CARD_PAD = 0          # full-bleed so the board aligns with the keyboard
BOARD_CARD_RADIUS = 0
BOARD_INNER_PAD = 14        # margin around grid — room for random emoji pins
PIN_EMOJI_SIZE = 26
EMOJI_PIN_DIR = Path(__file__).with_name("assets") / "emoji_pins"

COLS = "ABCDEFGHI"
FONTS_DIR = Path(__file__).with_name("fonts")

# SpongeBob SquarePants economy (stored as "coins" in data)
# XP = permanent career score (leaderboard); sponges = spendable shop currency
SPONGE = "🧽"
XP = "⭐"
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


def format_xp(amount: int, *, signed: bool = False) -> str:
    """Display career XP (never spent)."""
    n = int(amount)
    if signed:
        return f"+{n} XP" if n >= 0 else f"{n} XP"
    return f"{n} XP"

WIN_TAUNTS = (
    f"{BUBBLE} I'm ready! I'm ready! Bikini Bottom is proud of you!",
    f"{SPONGE} Order up! Fresh sponges coming your way!",
    f"{WAVE} You did it! Even Squidward clapped (quietly).",
    f"{PINEAPPLE} Home sweet pineapple — puzzle crushed!",
    f"{JELLY} Jellyfishing? Nah — Sudoku fishing. Catch!",
)

# Titles = header flair only. One free starter; rest are a longer sponge grind.
SHOP_TITLES = {
    "rookie": {"label": "🪼 Jellyfisher", "cost": 0, "pin": "Jellyfisher", "emoji": "🪼"},
    "patrick": {"label": "⭐ Starfish Genius", "cost": 60, "pin": "Starfish", "emoji": "⭐"},
    "solver": {"label": "🍔 Fry Cook", "cost": 120, "pin": "Fry Cook", "emoji": "🍔"},
    "larry": {"label": "💪 Larry Lobster", "cost": 200, "pin": "Larry", "emoji": "💪"},
    "barnacle": {"label": "🦸 Barnacle Boy", "cost": 300, "pin": "Barnacle", "emoji": "🦸"},
    "row_master": {"label": "🚗 Boatmobile Ace", "cost": 420, "pin": "Boatmobile", "emoji": "🚗"},
    "puff": {"label": "⛵ Boating School Grad", "cost": 550, "pin": "Boating Grad", "emoji": "⛵"},
    "dutchman": {"label": "👻 Flying Dutchman", "cost": 700, "pin": "Dutchman", "emoji": "👻"},
    "sudoku_pro": {"label": "🍦 Goofy Goober", "cost": 900, "pin": "Goober", "emoji": "🍦"},
    "plankton": {"label": "🦠 Plankton Plotter", "cost": 1150, "pin": "Plankton", "emoji": "🦠"},
    "mermaid": {"label": "🧜 Mermaid Man", "cost": 1450, "pin": "Mermaid Man", "emoji": "🧜"},
    "legend": {"label": "🍍 Pineapple Legend", "cost": 1800, "pin": "Legend", "emoji": "🍍"},
    "neptune": {"label": "👑 King Neptune", "cost": 2200, "pin": "Neptune", "emoji": "👑"},
    # Crew tributes — Bikini Bottom shout-outs
    "darkstriker": {"label": "🦹 Dark Striker", "cost": 500, "pin": "Striker", "emoji": "🦹"},
    "behindyou": {"label": "👀 Behind You", "cost": 750, "pin": "Shadow", "emoji": "👀"},
    "glock_sheets": {"label": "📊 Glock Sheets", "cost": 900, "pin": "Sheets", "emoji": "📊"},
    "bookie": {"label": "📚 Book Queen", "cost": 1050, "pin": "Bookie", "emoji": "📚"},
    "stacked": {"label": "😎 Stacked Smooth", "cost": 1200, "pin": "Stacked", "emoji": "😎"},
    "drea_mom": {"label": "🫶 Mama Drea", "cost": 1400, "pin": "Mama", "emoji": "🫶"},
    "hulk_r5": {"label": "🧌 Hulk Command", "cost": 1650, "pin": "Hulk", "emoji": "🧌"},
    "apex_whale": {"label": "🐋 Apex Whale", "cost": 2500, "pin": "Apex", "emoji": "🐋"},
    "fuzzy": {"label": "🔪 Fuzzy Wuzzy", "cost": 1750, "pin": "Fuzzy", "emoji": "🔪"},
    "xiao": {"label": "🐰 Cute Xiao", "cost": 1050, "pin": "Xiao", "emoji": "🐰"},
}

# Pins = border stickers only. One free; paid pins scale up so cosmetics stay a chase.
SHOP_PINS = {
    "xp_boost": {"label": "🔮 Puff's Crystal Ball (2x XP - 3 Games)", "pin": "Crystal Ball", "emoji": "🔮", "cost": 120},
    "streak_shield": {"label": "🛡️ Krabby Shield (missed daily days)", "pin": "Shield", "emoji": "🛡️", "cost": 150},
    "gary_wisdom": {"label": "🐌 Gary's Wisdom (+3 free hints/game ×2)", "pin": "Gary", "emoji": "🐌", "cost": 60},
    "krabby_snack": {"label": "🍟 Krabby Snack (+25% Sponges — 3 wins)", "pin": "Snack", "emoji": "🍟", "cost": 80},
    "golden_spatula": {"label": "🥇 Golden Spatula (+50% XP — 3 wins)", "pin": "Spatula", "emoji": "🥇", "cost": 80},
    "wave": {"label": "🌊 Wave Pin", "pin": "Wave", "emoji": "🌊", "cost": 0, "theme": "ocean"},
    # Former title emojis → buyable border pins
    "pin_jelly": {"label": "🪼 Jelly Pin", "pin": "Jelly", "emoji": "🪼", "cost": 40, "theme": "ocean"},
    "pin_star": {"label": "⭐ Star Pin", "pin": "Star", "emoji": "⭐", "cost": 60, "theme": "ocean"},
    "pin_burger": {"label": "🍔 Burger Pin", "pin": "Burger", "emoji": "🍔", "cost": 90, "theme": "ocean"},
    "pin_flex": {"label": "💪 Flex Pin", "pin": "Flex", "emoji": "💪", "cost": 120, "theme": "ocean"},
    "pin_hero": {"label": "🦸 Hero Pin", "pin": "Hero", "emoji": "🦸", "cost": 160, "theme": "ocean"},
    "pin_boat": {"label": "🚗 Boat Pin", "pin": "Boat", "emoji": "🚗", "cost": 210, "theme": "ocean"},
    "pin_sail": {"label": "⛵ Sail Pin", "pin": "Sail", "emoji": "⛵", "cost": 270, "theme": "ocean"},
    "pin_ghost": {"label": "👻 Ghost Pin", "pin": "Ghost", "emoji": "👻", "cost": 340, "theme": "ocean"},
    "pin_goober": {"label": "🍦 Goober Pin", "pin": "Goober", "emoji": "🍦", "cost": 420, "theme": "ocean"},
    "pin_bug": {"label": "🦠 Bug Pin", "pin": "Bug", "emoji": "🦠", "cost": 500, "theme": "ocean"},
    "pin_mermaid": {"label": "🧜 Mermaid Pin", "pin": "Mermaid", "emoji": "🧜", "cost": 600, "theme": "ocean"},
    "pin_pineapple": {"label": "🍍 Pineapple Pin", "pin": "Pineapple", "emoji": "🍍", "cost": 720, "theme": "ocean"},
    "pin_crown": {"label": "👑 Crown Pin", "pin": "Crown", "emoji": "👑", "cost": 850, "theme": "ocean"},
    # Extra unique stickers
    "coral": {"label": "🪸 Coral Pin", "pin": "Coral", "emoji": "🪸", "cost": 50, "theme": "ocean"},
    "crab": {"label": "🦀 Crab Pin", "pin": "Crab", "emoji": "🦀", "cost": 80, "theme": "ocean"},
    "bubble": {"label": "🫧 Bubble Pin", "pin": "Bubble", "emoji": "🫧", "cost": 110, "theme": "ocean"},
    "shell": {"label": "🐚 Shell Pin", "pin": "Shell", "emoji": "🐚", "cost": 150, "theme": "ocean"},
    "squid": {"label": "🦑 Squid Pin", "pin": "Squid", "emoji": "🦑", "cost": 200, "theme": "ocean"},
    "sandy": {"label": "🐿️ Dome Pin", "pin": "Dome", "emoji": "🐿️", "cost": 260, "theme": "ocean"},
    "pearl": {"label": "💎 Pearl Pin", "pin": "Pearl", "emoji": "💎", "cost": 330, "theme": "ocean"},
    "anchor": {"label": "⚓ Anchor Pin", "pin": "Anchor", "emoji": "⚓", "cost": 410, "theme": "ocean"},
    "shark": {"label": "🦈 Shark Pin", "pin": "Shark", "emoji": "🦈", "cost": 500, "theme": "ocean"},
    "bucket": {"label": "🪣 Bucket Pin", "pin": "Bucket", "emoji": "🪣", "cost": 600, "theme": "ocean"},
    "sponge": {"label": "🧽 Sponge Pin", "pin": "Sponge", "emoji": "🧽", "cost": 720, "theme": "ocean"},
    "whirl": {"label": "🌀 Whirlpool Pin", "pin": "Whirlpool", "emoji": "🌀", "cost": 850, "theme": "ocean"},
    # Later chase stickers
    "fish": {"label": "🐠 Clownfish Pin", "pin": "Clownfish", "emoji": "🐠", "cost": 950, "theme": "ocean"},
    "octopus": {"label": "🐙 Octopus Pin", "pin": "Octopus", "emoji": "🐙", "cost": 1100, "theme": "ocean"},
    "hook": {"label": "🎣 Hook Pin", "pin": "Hook", "emoji": "🎣", "cost": 1250, "theme": "ocean"},
    "kelp": {"label": "🌿 Kelp Pin", "pin": "Kelp", "emoji": "🌿", "cost": 1400, "theme": "ocean"},
    "patty": {"label": "🥪 Patty Pin", "pin": "Patty", "emoji": "🥪", "cost": 1550, "theme": "ocean"},
    "clarinet": {"label": "🎺 Clarinet Pin", "pin": "Clarinet", "emoji": "🎺", "cost": 1700, "theme": "ocean"},
    "money": {"label": "💰 Money Pin", "pin": "Money", "emoji": "💰", "cost": 1850, "theme": "ocean"},
    "formula": {"label": "🧪 Formula Pin", "pin": "Formula", "emoji": "🧪", "cost": 2000, "theme": "ocean"},
    "glove": {"label": "🎢 Glove World Pin", "pin": "Glove World", "emoji": "🎢", "cost": 2200, "theme": "ocean"},
    "moon": {"label": "🌙 Rock Bottom Pin", "pin": "Rock Bottom", "emoji": "🌙", "cost": 2500, "theme": "ocean"},
    # Crew tribute pins
    "pin_goof": {"label": "🦹 Thief Pin", "pin": "Thief", "emoji": "🦹", "cost": 250, "theme": "crew"},
    "pin_shadow": {"label": "👀 BehindYou Pin", "pin": "Behind You", "emoji": "👀", "cost": 380, "theme": "crew"},
    "pin_sheets": {"label": "📊 Sheets Pin", "pin": "Sheets", "emoji": "📊", "cost": 460, "theme": "crew"},
    "pin_book": {"label": "📚 Book Pin", "pin": "Book", "emoji": "📚", "cost": 540, "theme": "crew"},
    "pin_smooth": {"label": "😎 Stacked Pin", "pin": "Stacked", "emoji": "😎", "cost": 620, "theme": "crew"},
    "pin_drea": {"label": "🫶 Drea Pin", "pin": "Drea", "emoji": "🫶", "cost": 700, "theme": "crew"},
    "pin_hulk": {"label": "🧌 Hulk Pin", "pin": "Hulk", "emoji": "🧌", "cost": 820, "theme": "crew"},
    "pin_apex": {"label": "🐋 Apex Pin", "pin": "Apex", "emoji": "🐋", "cost": 1000, "theme": "crew"},
    "pin_fuzzy": {"label": "🔪 Fuzzy Pin", "pin": "Fuzzy", "emoji": "🔪", "cost": 880, "theme": "crew"},
    "pin_xiao": {"label": "🐰 Cute Xiao Pin", "pin": "Xiao", "emoji": "🐰", "cost": 540, "theme": "crew"},
}

SHOP_BOOST_KEYS = frozenset({
    "xp_boost",
    "streak_shield",
    "gary_wisdom",
    "krabby_snack",
    "golden_spatula",
})
SHOP_BUNDLE_DISCOUNT = 0.5  # 50% off one pin + one title per UTC day
SHOP_PAGE_SIZE = 11

ACHIEVEMENTS = {
    # Speed
    "speed_demon": {"label": "⚡ Speed Demon", "desc": "Solve a puzzle in under 3 mins"},
    "jelly_flash": {"label": "🪼 Jelly Flash", "desc": "Solve a puzzle in under 90 seconds"},
    # Streaks
    "streak_master": {"label": "🔥 Streak Master", "desc": "Reach a 7-day daily streak"},
    "kelp_calendar": {"label": "📅 Kelp Calendar", "desc": "Reach a 30-day daily streak"},
    # Economy
    "sponge_boss": {"label": "🧽 Sponge Boss", "desc": "Earn 1,000 Sponges lifetime"},
    "krusty_whale": {"label": "🐋 Krusty Whale", "desc": "Earn 5,000 Sponges lifetime"},
    # Wins
    "first_order": {"label": "🍔 First Order Up", "desc": "Win your first puzzle"},
    "puzzle_master": {"label": "🧩 Puzzle Master", "desc": "Complete 25 total wins"},
    "century_fry": {"label": "🍍 Century Fry Cook", "desc": "Complete 100 total wins"},
    # Daily
    "daily_diver": {"label": "🌊 Daily Diver", "desc": "Clear 10 Daily Sudokus"},
    "pineapple_regular": {"label": "🍍 Pineapple Regular", "desc": "Clear 50 Daily Sudokus"},
    # Challenge
    "chum_challenger": {"label": "⚔️ Chum Challenger", "desc": "Win 5 speedrun challenges"},
    "arena_ace": {"label": "🏆 Arena Ace", "desc": "Win 25 speedrun challenges"},
    # Cosmetics / career
    "pin_hoarder": {"label": "🎨 Pin Hoarder", "desc": "Own 8 border pins"},
    "pin_collector": {"label": "🪸 Pin Collector", "desc": "Own 16 border pins"},
    "pin_museum": {"label": "🏛️ Pin Museum", "desc": "Own 32 border pins"},
    "title_tour": {"label": "👑 Title Tour", "desc": "Own 5 shop titles"},
    "title_wardrobe": {"label": "👗 Title Wardrobe", "desc": "Own 10 shop titles"},
    "xp_voyager": {"label": "⭐ XP Voyager", "desc": "Reach 5,000 career XP"},
    "xp_reef": {"label": "🪸 XP Reef Walker", "desc": "Reach 15,000 career XP"},
    "xp_king": {"label": "👑 XP King Tide", "desc": "Reach 40,000 career XP"},
    "xp_neptune": {"label": "🔱 XP Neptune", "desc": "Reach 100,000 career XP"},
}

# ISO-week quests (UTC). Progress resets each Monday 00:00 UTC.
WEEKLY_QUESTS: tuple[dict, ...] = (
    {
        "id": "daily_triple",
        "label": "Clear 3 Daily Sudokus",
        "emoji": "🍍",
        "counter": "weekly_dailies",
        "need": 3,
        "reward": 75,
    },
    {
        "id": "board_five",
        "label": "Finish 5 boards (any mode)",
        "emoji": "🧩",
        "counter": "weekly_boards",
        "need": 5,
        "reward": 60,
    },
    {
        "id": "race_win",
        "label": "Win 1 speedrun challenge",
        "emoji": "⚔️",
        "counter": "weekly_challenges",
        "need": 1,
        "reward": 100,
    },
)

# Career XP thresholds (XP mirrors sponge grants on win).
# Curve stays friendly early, then accelerates so endgame is a long grind.
# Rough wins at ~100 XP/avg: L2≈3 · L5≈28 · L8≈140 · L10≈400 · L12≈1000 · L15≈3200
LEVEL_RANKS = [
    (0, 1, "🍔 Fry Cook"),
    (250, 2, "🍔 Senior Fry Cook"),
    (700, 3, "🪼 Jellycatcher"),
    (1500, 4, "🪼 Jellyfisher Master"),
    (2800, 5, "🚗 Boatmobile Student"),
    (5000, 6, "🚗 Boatmobile Ace"),
    (8500, 7, "🐚 Shell City Explorer"),
    (14000, 8, "🍦 Goofy Goober Master"),
    (23000, 9, "🧜 Hero of Bikini Bottom"),
    (40000, 10, "👑 King of Bikini Bottom"),
    (65000, 11, "🪸 Reef Warden"),
    (100000, 12, "🪣 Chum Bucket Rival"),
    (150000, 13, "👻 Dutchman's Crew"),
    (220000, 14, "🍍 Pineapple Immortal"),
    (320000, 15, "👑 Neptune's Heir"),
]


def evaluate_user_level(xp: int) -> tuple[int, str]:
    """Return (level_num, rank_label) based on total XP — always live from LEVEL_RANKS."""
    try:
        xp_n = int(xp or 0)
    except (TypeError, ValueError):
        xp_n = 0
    current_lvl = 1
    current_rank = "🍔 Fry Cook"
    for threshold, lvl, title in LEVEL_RANKS:
        if xp_n >= threshold:
            current_lvl = lvl
            current_rank = title
        else:
            break
    return current_lvl, current_rank


def next_level_threshold(xp: int) -> int | None:
    """XP needed to reach the next rank, or None at max level."""
    try:
        xp_n = int(xp or 0)
    except (TypeError, ValueError):
        xp_n = 0
    lvl, _ = evaluate_user_level(xp_n)
    for threshold, rank_lvl, _title in LEVEL_RANKS:
        if rank_lvl == lvl + 1:
            return int(threshold)
    return None


def format_rank_compact(xp: int) -> str:
    """Short rank line for mobile-friendly embeds (no long progress sentence)."""
    try:
        xp_n = int(xp or 0)
    except (TypeError, ValueError):
        xp_n = 0
    lvl, title = evaluate_user_level(xp_n)
    nxt = next_level_threshold(xp_n)
    if nxt is None:
        return f"L{lvl} {title}"
    return f"L{lvl} {title} · {xp_n}/{nxt}"


def build_stats_embed(stats: dict, *, avatar_url: str | None = None) -> discord.Embed:
    """Compact player card — description-only so Discord mobile stays short."""
    _ = avatar_url  # intentionally unused (thumbnail eats phone width)
    best = (
        format_time(stats["best_time"])
        if stats.get("best_time") is not None
        else "—"
    )
    longest = (
        format_time(stats["longest_time"])
        if stats.get("longest_time") is not None
        else "—"
    )
    title = (
        SHOP_TITLES[stats["title"]]["label"]
        if stats.get("title") in SHOP_TITLES
        else "Civilian"
    )
    streak = int(stats.get("streak", 0) or 0)
    best_streak = int(stats.get("best_streak", 0) or 0)
    wins = int(stats.get("wins", 0) or 0)
    losses = int(stats.get("losses", 0) or 0)
    games_n = int(stats.get("games", 0) or 0) or (wins + losses)
    win_rate = f"{(100 * wins / games_n):.0f}%" if games_n else "—"
    shields = int(stats.get("streak_shields") or 0)
    badge_ids = [b for b in (stats.get("badges") or []) if b in ACHIEVEMENTS]
    have = len(badge_ids)
    total = len(ACHIEVEMENTS)
    badge_emojis: list[str] = []
    for bid in badge_ids[:5]:
        label = ACHIEVEMENTS[bid]["label"]
        emoji = label.split(" ", 1)[0] if label else ""
        if emoji:
            badge_emojis.append(emoji)
    badge_preview = " ".join(badge_emojis) if badge_emojis else "—"

    ensure_weekly_progress(stats)
    weekly_claimed = set(stats.get("weekly_claimed") or [])
    weekly_done = sum(1 for q in WEEKLY_QUESTS if q["id"] in weekly_claimed)
    weekly_total = len(WEEKLY_QUESTS)

    embed = paper_embed(f"{SPONGE} {display_name(stats)}")
    embed.description = (
        f"**{format_rank_compact(stats.get('xp', 0))}**\n"
        f"{title} · {STAR} **{streak}** (best {best_streak})\n"
        f"{format_sponges(stats.get('coins', 0))} · "
        f"spent {int(stats.get('sponges_spent', 0) or 0)} · "
        f"{format_xp(stats.get('xp', 0))}\n"
        f"**{wins}**W–**{losses}**L ({win_rate}) · best **{best}** · longest **{longest}**\n"
        f"{PINEAPPLE} **{int(stats.get('daily_wins', 0) or 0)}** · "
        f"{JELLY} **{int(stats.get('challenge_wins', 0) or 0)}** · "
        f"**{games_n}** boards · 🛡️ **{shields}**\n"
        f"📅 Weekly **{weekly_done}/{weekly_total}** · `/weekly`\n"
        f"🏆 **{have}/{total}** {badge_preview} · `/achievements`"
    )
    return embed


def format_rank_line(xp: int) -> str:
    """Single-line rank + progress toward the next level."""
    try:
        xp_n = int(xp or 0)
    except (TypeError, ValueError):
        xp_n = 0
    lvl, title = evaluate_user_level(xp_n)
    nxt = next_level_threshold(xp_n)
    if nxt is None:
        return f"Lvl {lvl} · {title} (max)"
    return f"Lvl {lvl} · {title} · {xp_n}/{nxt} XP → L{lvl + 1}"


def evaluate_user_achievements(stats: dict) -> list[str]:
    unlocked = set(stats.get("badges") or [])

    try:
        best_time = float(stats.get("best_time") if stats.get("best_time") is not None else 0)
    except (TypeError, ValueError):
        best_time = 0.0
    if best_time > 0 and best_time <= 180:
        unlocked.add("speed_demon")
    if best_time > 0 and best_time <= 90:
        unlocked.add("jelly_flash")

    try:
        streak = max(int(stats.get("streak") or 0), int(stats.get("best_streak") or 0))
    except (TypeError, ValueError):
        streak = 0
    if streak >= 7:
        unlocked.add("streak_master")
    if streak >= 30:
        unlocked.add("kelp_calendar")

    try:
        coins = int(stats.get("coins") or 0) + int(stats.get("sponges_spent") or 0)
    except (TypeError, ValueError):
        coins = 0
    if coins >= 1000:
        unlocked.add("sponge_boss")
    if coins >= 5000:
        unlocked.add("krusty_whale")

    try:
        wins = max(int(stats.get("wins") or 0), int(stats.get("activity_wins") or 0))
    except (TypeError, ValueError):
        wins = 0
    if wins >= 1:
        unlocked.add("first_order")
    if wins >= 25:
        unlocked.add("puzzle_master")
    if wins >= 100:
        unlocked.add("century_fry")

    try:
        daily_wins = int(stats.get("daily_wins") or 0)
    except (TypeError, ValueError):
        daily_wins = 0
    if daily_wins >= 10:
        unlocked.add("daily_diver")
    if daily_wins >= 50:
        unlocked.add("pineapple_regular")

    try:
        chall = int(stats.get("challenge_wins") or 0)
    except (TypeError, ValueError):
        chall = 0
    if chall >= 5:
        unlocked.add("chum_challenger")
    if chall >= 25:
        unlocked.add("arena_ace")

    pin_count = len(owned_pin_ids(stats))
    if pin_count >= 8:
        unlocked.add("pin_hoarder")
    if pin_count >= 16:
        unlocked.add("pin_collector")
    if pin_count >= 32:
        unlocked.add("pin_museum")

    try:
        title_count = len(list(stats.get("owned_titles") or []))
    except (TypeError, ValueError):
        title_count = 0
    if title_count >= 5:
        unlocked.add("title_tour")
    if title_count >= 10:
        unlocked.add("title_wardrobe")

    try:
        xp_n = int(stats.get("xp") or 0)
    except (TypeError, ValueError):
        xp_n = 0
    if xp_n >= 5000:
        unlocked.add("xp_voyager")
    if xp_n >= 15000:
        unlocked.add("xp_reef")
    if xp_n >= 40000:
        unlocked.add("xp_king")
    if xp_n >= 100000:
        unlocked.add("xp_neptune")

    # Stable catalog order for known badges; keep any legacy ids at the end
    ordered = [b for b in ACHIEVEMENTS if b in unlocked]
    for b in unlocked:
        if b not in ACHIEVEMENTS:
            ordered.append(b)
    stats["badges"] = ordered
    return list(ordered)


def achievement_catalog_embed(stats: dict, *, viewer_name: str | None = None) -> discord.Embed:
    """Full badge book: unlocked vs locked with how-to-earn hints."""
    unlocked = set(evaluate_user_achievements(stats))
    total = len(ACHIEVEMENTS)
    have = sum(1 for b in ACHIEVEMENTS if b in unlocked)
    who = viewer_name or display_name(stats)
    embed = paper_embed(f"🏆 Achievements · {who}")
    embed.description = (
        f"{WAVE} **{have}/{total}** unlocked — keep frying!\n"
        f"Badges update automatically when you hit the goal."
    )

    done_lines: list[str] = []
    todo_lines: list[str] = []
    for key, meta in ACHIEVEMENTS.items():
        line = f"**{meta['label']}** — {meta['desc']}"
        if key in unlocked:
            done_lines.append(f"✅ {line}")
        else:
            todo_lines.append(f"🔒 {line}")

    def _chunks(lines: list[str], limit: int = 1000) -> list[str]:
        if not lines:
            return ["_None yet._"]
        chunks: list[str] = []
        buf = ""
        for line in lines:
            add = (line + "\n") if buf else line
            if buf and len(buf) + 1 + len(line) > limit:
                chunks.append(buf)
                buf = line
            else:
                buf = f"{buf}\n{line}" if buf else line
        if buf:
            chunks.append(buf)
        return chunks

    for i, chunk in enumerate(_chunks(done_lines)):
        name = "✅ Unlocked" if i == 0 else f"✅ Unlocked ({i + 1})"
        embed.add_field(name=name, value=chunk, inline=False)
    for i, chunk in enumerate(_chunks(todo_lines)):
        name = "🔒 Still locked" if i == 0 else f"🔒 Still locked ({i + 1})"
        embed.add_field(name=name, value=chunk, inline=False)
    return embed

# Legacy shop pin ids → current ids (owned_themes / owned_pins from older builds)
SHOP_PIN_ALIASES = {
    "jellyfish": "coral",
    "krusty": "crab",
    "goober": "bubble",
    "rock_bottom": "anchor",
    "chum": "bucket",
}

intents = discord.Intents.default()
games: dict = {}
pending_challenges: dict[int, dict] = {}  # invite message_id → meta
challenge_cooldowns: dict[int, float] = {}  # user_id → last /challenge timestamp
_challenge_live_tasks: dict[str, asyncio.Task] = {}
_activity_notify_inflight: set[str] = set()
_daily_finish_locks: dict[str, asyncio.Lock] = {}
_challenge_match_locks: dict[str, asyncio.Lock] = {}
_leaderboard_mirror_generation = 0
_leaderboard_mirror_lock = asyncio.Lock()
WATCH_ACTIVE_SEC = 45
CHALLENGE_LIVE_DEBOUNCE_SEC = 4.0
ACTIVITY_WATCH_END_GRACE_SEC = 20
ACTIVITY_BLOCKING_MAX_AGE_SEC = 7200  # 2h — abandoned /play shouldn't block challenge all day
WATCH_LIST_MAX_AGE_SEC = 7200
WATCH_IDLE_HIDE_SEC = 900  # hide idle boards from /watch after 15 min without moves
WATCH_RESTORE_MAX_AGE_SEC = 86400
CHALLENGE_BOARD_CELLS = 81
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
    global _leaderboard_mirror_generation
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        # Don't lose the in-memory award if the disk write fails (e.g. ephemeral FS hiccup)
        print(f"save_data failed: {exc}")
    # Mirror to Mongo so Render restarts keep sponges / stats
    try:
        loop = asyncio.get_running_loop()
        _leaderboard_mirror_generation += 1
        gen = _leaderboard_mirror_generation
        snapshot = deepcopy(data)
        loop.create_task(_mirror_leaderboard_mongo(snapshot, gen))
    except Exception as exc:
        print(f"save_data mirror failed: {exc}")


async def _mirror_leaderboard_mongo(data: dict, generation: int) -> None:
    """Write only the latest snapshot — older in-flight mirrors are skipped."""
    async with _leaderboard_mirror_lock:
        if generation != _leaderboard_mirror_generation:
            return
        try:
            await match_store.save_leaderboard(data, revision=generation)
        except Exception as exc:  # noqa: BLE001
            print(f"mongo leaderboard save failed: {exc}")


async def restore_leaderboard_from_mongo(bot: "SudokuBot") -> None:
    """Load durable stats from Mongo; recover wiped players from daily_completions."""
    try:
        remote = await match_store.load_leaderboard()
    except Exception as exc:  # noqa: BLE001
        print(f"load_leaderboard failed: {exc}")
        remote = None

    if remote:
        bot.data = remote
        try:
            with DATA_FILE.open("w", encoding="utf-8") as f:
                json.dump(bot.data, f, indent=2)
        except OSError as exc:
            print(f"write restored leaderboard failed: {exc}")
        print("Restored leaderboard from Mongo.")
    elif bot.data:
        try:
            await match_store.save_leaderboard(bot.data)
            print("Seeded Mongo leaderboard from local file.")
        except Exception as exc:  # noqa: BLE001
            print(f"seed leaderboard failed: {exc}")

    # Career XP backfill (wins/daily/challenge → xp); shop spend never reduces XP
    try:
        touched = migrate_leaderboard_xp(bot.data)
        if touched:
            try:
                with DATA_FILE.open("w", encoding="utf-8") as f:
                    json.dump(bot.data, f, indent=2)
            except OSError as exc:
                print(f"write xp-migrated leaderboard failed: {exc}")
            await match_store.save_leaderboard(bot.data)
            print(f"Migrated career XP / shop spend / price-cut refunds for {touched} player(s) → Mongo.")
    except Exception as exc:  # noqa: BLE001
        print(f"xp migration failed: {exc}")

    # If a redeploy wiped stats but daily claims remain, rebuild the bare minimum
    try:
        claims = await match_store.list_daily_completions()
    except Exception as exc:  # noqa: BLE001
        print(f"list_daily_completions failed: {exc}")
        return

    changed = False
    for doc in claims:
        try:
            guild_id = int(doc["guild_id"])
            user_id = int(doc["user_id"])
        except (KeyError, TypeError, ValueError):
            continue
        coins = int(doc.get("coins") or 0)
        if coins <= 0:
            continue
        name = doc.get("name") or "Unknown"
        day = doc.get("date")
        elapsed = doc.get("elapsed")
        gstats = guild_stats(bot.data, guild_id)
        stats = user_stats(gstats, user_id)
        daily_meta = gstats.setdefault("_daily", {})
        results = daily_meta.setdefault("results", {})
        uid = str(user_id)
        prior = results.get(uid) or {}

        wiped = int(stats.get("coins") or 0) == 0 and int(stats.get("daily_wins") or 0) == 0
        if not wiped:
            # Still mark today's claim so /daily doesn't double-pay after a partial wipe
            if day and prior.get("won") is not True:
                if daily_meta.get("date") in (None, day):
                    daily_meta["date"] = day
                    results[uid] = {
                        "won": True,
                        "time": elapsed,
                        "name": name,
                        "coins": coins,
                    }
                    changed = True
            continue

        stats["coins"] = coins
        stats["wins"] = max(int(stats.get("wins") or 0), 1)
        stats["games"] = max(int(stats.get("games") or 0), 1)
        stats["daily_wins"] = max(int(stats.get("daily_wins") or 0), 1)
        stats["streak"] = max(int(stats.get("streak") or 0), 1)
        stats["best_streak"] = max(int(stats.get("best_streak") or 0), stats["streak"])
        if elapsed is not None:
            record_solve_times(stats, elapsed)
        stats["name"] = name
        if day:
            daily_meta["date"] = day
        results[uid] = {
            "won": True,
            "time": elapsed,
            "name": name,
            "coins": coins,
        }
        changed = True
        print(f"Recovered {name} ({user_id}): {coins} sponges from daily claim {day}")

    if changed:
        save_data(bot.data)

    # One-time: reset inflated win-count streaks to a fair calendar baseline of 1.
    try:
        reset_n = migrate_fair_daily_streaks(bot.data)
        if reset_n:
            save_data(bot.data)
            try:
                await match_store.save_leaderboard(bot.data)
            except Exception as save_exc:  # noqa: BLE001
                print(f"fair streak reset mongo save failed: {save_exc}")
            print(f"Fair daily streak reset → 1 for {reset_n} player(s).")
    except Exception as exc:  # noqa: BLE001
        print(f"fair streak reset failed: {exc}")

    # One-time: restore known players wiped to 1 by the fair reset.
    try:
        restored = migrate_restore_known_streaks(bot.data)
        if restored:
            save_data(bot.data)
            try:
                await match_store.save_leaderboard(bot.data)
            except Exception as save_exc:  # noqa: BLE001
                print(f"streak restore mongo save failed: {save_exc}")
            print(f"Restored daily streaks: {', '.join(restored)}")
    except Exception as exc:  # noqa: BLE001
        print(f"known streak restore failed: {exc}")

    # One-time: clear Xiao's bogus ~12s best_time from the timer bug.
    try:
        cleared = migrate_clear_xiao_bogus_best_time(bot.data)
        if cleared:
            save_data(bot.data)
            try:
                await match_store.save_leaderboard(bot.data)
            except Exception as save_exc:  # noqa: BLE001
                print(f"xiao best_time clear mongo save failed: {save_exc}")
            print(f"Cleared bogus best_time: {', '.join(cleared)}")
    except Exception as exc:  # noqa: BLE001
        print(f"xiao best_time clear failed: {exc}")


STREAK_FAIR_RESET_FLAG = "streak_calendar_fair_reset_v1"
STREAK_RESTORE_V1_FLAG = "streak_restore_bookqueen_fuzzy_xiao_v1"
# Display-name substrings (case-insensitive) → streak to restore after fair reset.
STREAK_RESTORE_V1_TARGETS: dict[str, int] = {
    "bookqueen": 3,
    "fuzzy": 3,
    "xiao": 3,
}

XIAO_BEST_TIME_BUG_RESET_FLAG = "xiao_best_time_bug_reset_v1"
# Known Discord user id for [THC]Xiao (from Activity win posts).
XIAO_BEST_TIME_BUG_USER_IDS = frozenset({922903420053098497})
# Career bests at/under this many seconds are treated as the timer bug for Xiao.
XIAO_BEST_TIME_BUG_MAX_SEC = 15


def migrate_clear_xiao_bogus_best_time(data: dict) -> list[str]:
    """One-shot: wipe Xiao's impossible ~12s career best from the elapsed bug."""
    if data.get(XIAO_BEST_TIME_BUG_RESET_FLAG):
        return []
    cleared: list[str] = []
    xiao_seen = False
    for guild_key, gstats in list(data.items()):
        if not isinstance(gstats, dict) or not str(guild_key).isdigit():
            continue
        for uid, stats in iter_players(gstats):
            if not isinstance(stats, dict):
                continue
            try:
                uid_i = int(uid)
            except (TypeError, ValueError):
                continue
            name = str(stats.get("name") or "").casefold()
            if uid_i not in XIAO_BEST_TIME_BUG_USER_IDS and "xiao" not in name:
                continue
            xiao_seen = True
            best = stats.get("best_time")
            if best is None:
                continue
            try:
                t = float(best)
            except (TypeError, ValueError):
                continue
            if t > XIAO_BEST_TIME_BUG_MAX_SEC:
                continue
            before = format_time(t)
            # Clear bogus best; only wipe longest if it is also the bug (or seeded from it).
            stats["best_time"] = None
            longest = stats.get("longest_time")
            try:
                lt = None if longest is None else float(longest)
            except (TypeError, ValueError):
                lt = None
            if lt is None or lt <= XIAO_BEST_TIME_BUG_MAX_SEC or lt == t:
                stats["longest_time"] = None
            badges = [
                b
                for b in (stats.get("badges") or [])
                if b not in ("speed_demon", "jelly_flash")
            ]
            stats["badges"] = badges
            cleared.append(f"{stats.get('name') or uid}@{guild_key} ({before})")
    # Only stamp the flag once we've actually seen Xiao (or cleared someone),
    # so a partial/empty leaderboard load doesn't skip the fix forever.
    if xiao_seen or cleared:
        data[XIAO_BEST_TIME_BUG_RESET_FLAG] = True
    return cleared


def migrate_fair_daily_streaks(data: dict) -> int:
    """Set every player's daily streak to 1 once (legacy win-count streaks were inflated)."""
    if data.get(STREAK_FAIR_RESET_FLAG):
        return 0
    today = utc_today()
    touched = 0
    for guild_key, gstats in list(data.items()):
        if not isinstance(gstats, dict) or not str(guild_key).isdigit():
            continue
        daily_meta = gstats.get("_daily") if isinstance(gstats.get("_daily"), dict) else {}
        results = daily_meta.get("results") if isinstance(daily_meta.get("results"), dict) else {}
        daily_date = str(daily_meta.get("date") or "")
        for uid, stats in iter_players(gstats):
            if not isinstance(stats, dict):
                continue
            stats["streak"] = 1
            stats["best_streak"] = 1
            # If they already cleared today's daily, stamp today so tomorrow can become 2.
            prior = results.get(str(uid)) or {}
            if daily_date == today and prior.get("won"):
                stats["last_streak_day"] = today
            else:
                stats["last_streak_day"] = None
            touched += 1
    data[STREAK_FAIR_RESET_FLAG] = True
    return touched


def apply_manual_streak(
    stats: dict,
    *,
    streak: int,
    day: str | None = None,
    won_today: bool = False,
) -> None:
    """Set calendar streak and stamp last_streak_day so the chain stays continuous."""
    value = max(0, int(streak))
    today = day or utc_today()
    stats["streak"] = value
    stats["best_streak"] = max(int(stats.get("best_streak") or 0), value)
    if value <= 0:
        stats["last_streak_day"] = None
        return
    if won_today:
        stats["last_streak_day"] = today
    else:
        # Treat the streak as ending yesterday so today's win continues it.
        yesterday = (
            datetime.fromisoformat(str(today)).date() - timedelta(days=1)
        ).isoformat()
        stats["last_streak_day"] = yesterday


def migrate_restore_known_streaks(data: dict) -> list[str]:
    """One-shot: restore Bookqueen / Fuzzy / Xiao streaks wiped by the fair reset."""
    if data.get(STREAK_RESTORE_V1_FLAG):
        return []
    today = utc_today()
    restored: list[str] = []
    for guild_key, gstats in list(data.items()):
        if not isinstance(gstats, dict) or not str(guild_key).isdigit():
            continue
        daily_meta = gstats.get("_daily") if isinstance(gstats.get("_daily"), dict) else {}
        results = daily_meta.get("results") if isinstance(daily_meta.get("results"), dict) else {}
        daily_date = str(daily_meta.get("date") or "")
        for uid, stats in iter_players(gstats):
            if not isinstance(stats, dict):
                continue
            name = str(stats.get("name") or "").casefold()
            target = None
            for needle, value in STREAK_RESTORE_V1_TARGETS.items():
                if needle in name:
                    target = value
                    break
            if target is None:
                continue
            prior = results.get(str(uid)) or {}
            won_today = bool(daily_date == today and prior.get("won"))
            apply_manual_streak(
                stats, streak=target, day=today, won_today=won_today
            )
            restored.append(f"{stats.get('name') or uid}→{target}")
    data[STREAK_RESTORE_V1_FLAG] = True
    return restored


def catalog_spend_total(stats: dict) -> int:
    """Sum of shop prices for currently owned titles + pins (legacy purchase estimate)."""
    total = 0
    for tid in stats.get("owned_titles") or []:
        meta = SHOP_TITLES.get(tid)
        if meta:
            total += int(meta.get("cost") or 0)
    for tid in owned_pin_ids(stats):
        meta = SHOP_PINS.get(tid)
        if meta:
            total += int(meta.get("cost") or 0)
    return total


def refund_shop_price_cuts(stats: dict) -> bool:
    """One-time pocket credit when owned cosmetics got cheaper (or became free).

    Only refunds the gap between recorded sponges_spent and today's catalog value,
    so players who only received free auto-grants (spent 0) get nothing.
    Returns True if stats were touched (flag and/or coins).
    """
    if stats.get("_price_cut_refund_v1") == 1:
        return False
    stats.setdefault("sponges_spent", 0)
    approx_now = catalog_spend_total(stats)
    spent = int(stats.get("sponges_spent") or 0)
    credit = max(0, spent - approx_now)
    if credit:
        stats["coins"] = int(stats.get("coins") or 0) + credit
        stats["sponges_spent"] = approx_now
    stats["_price_cut_refund_v1"] = 1
    return True


def seed_sponges_spent(stats: dict) -> bool:
    """Backfill sponges_spent from owned cosmetics (pre-counter purchases)."""
    stats.setdefault("sponges_spent", 0)
    if stats.get("_spent_migrated") == 1:
        return False
    approx = catalog_spend_total(stats)
    stats["sponges_spent"] = max(int(stats.get("sponges_spent") or 0), approx)
    stats["_spent_migrated"] = 1
    return True


def seed_career_xp(stats: dict) -> bool:
    """Backfill career XP from recorded wins (shop spend never reduces XP).

    Returns True if stats were changed.
    """
    stats.setdefault("xp", 0)
    # Bump version when the backfill formula changes so veterans get a refresh
    if stats.get("_xp_migrated") == 2:
        return False
    wins = int(stats.get("wins") or 0)
    daily = int(stats.get("daily_wins") or 0)
    chall = int(stats.get("challenge_wins") or 0)
    approx = (
        wins * BASE_WIN_REWARD
        + daily * DAILY_BONUS
        + chall * int(round(BASE_WIN_REWARD * (CHALLENGE_WIN_MULT - 1)))
    )
    stats["xp"] = max(int(stats.get("xp") or 0), approx)
    stats["_xp_migrated"] = 2
    return True


def migrate_leaderboard_xp(data: dict) -> int:
    """Seed XP for every player blob in the leaderboard payload. Returns players touched."""
    touched = 0
    for guild_key, gstats in list(data.items()):
        if not isinstance(gstats, dict) or guild_key.startswith("_"):
            continue
        for user_key, stats in list(gstats.items()):
            if not isinstance(stats, dict) or user_key.startswith("_") or not str(user_key).isdigit():
                continue
            changed = seed_career_xp(stats)
            # Ensure pin ownership keys exist before spend backfill
            stats.setdefault("owned_themes", [])
            stats.setdefault("owned_pins", stats.get("owned_themes") or [])
            if stats.get("owned_themes"):
                merged = list(
                    dict.fromkeys([*(stats.get("owned_pins") or []), *stats["owned_themes"]])
                )
                stats["owned_pins"] = merged
                stats["owned_themes"] = merged
            if seed_sponges_spent(stats):
                changed = True
            if refund_shop_price_cuts(stats):
                changed = True
            if changed:
                touched += 1
    return touched


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
    s.setdefault("sponges_spent", 0)
    seed_career_xp(s)
    s.setdefault("wins", 0)
    s.setdefault("losses", 0)
    s.setdefault("games", 0)
    s.setdefault("best_time", None)
    s.setdefault("longest_time", None)
    # Seed longest from known best when we only ever tracked fastest before.
    if s.get("longest_time") is None and s.get("best_time") is not None:
        try:
            s["longest_time"] = int(float(s["best_time"]))
        except (TypeError, ValueError):
            pass
    s.setdefault("streak", 0)
    s.setdefault("best_streak", 0)
    s.setdefault("last_streak_day", None)
    s.setdefault("name", "Unknown")
    s.setdefault("title", None)
    s.setdefault("owned_titles", [])
    # Pins used to be sold as "themes"; keep owned_themes as the storage key
    s.setdefault("owned_themes", [])
    s.setdefault("owned_pins", s.get("owned_themes") or [])
    # Merge legacy owned_themes into owned_pins once
    if s.get("owned_themes"):
        merged = list(dict.fromkeys([*(s.get("owned_pins") or []), *s["owned_themes"]]))
        s["owned_pins"] = merged
        s["owned_themes"] = merged
    # Normalize legacy pin ids (jellyfish→coral, etc.)
    normalized = owned_pin_ids(s)
    if normalized != list(s.get("owned_pins") or []):
        s["owned_pins"] = normalized
        s["owned_themes"] = normalized
    # Auto-claim free shop items (cost 0)
    for tid, meta in SHOP_TITLES.items():
        if int(meta.get("cost") or 0) <= 0 and tid not in s["owned_titles"]:
            s["owned_titles"].append(tid)
            if not s.get("title"):
                s["title"] = tid
    owned = owned_pin_ids(s)
    free_pins_added = False
    for pid, meta in SHOP_PINS.items():
        if int(meta.get("cost") or 0) <= 0 and pid not in owned:
            owned.append(pid)
            free_pins_added = True
    if free_pins_added:
        s["owned_pins"] = owned
        s["owned_themes"] = owned
    seed_sponges_spent(s)
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
        "owner_id": snapshot.get("owner_id"),
        "owner_name": snapshot.get("owner_name") or "Unknown",
        "mode": snapshot.get("mode"),
        "guild_id": snapshot.get("guild_id"),
        "game": snapshot,
    }
    try:
        await match_store.upsert_active_game(payload)
    except Exception as exc:  # noqa: BLE001 — persistence must not break play
        print(f"persist_game failed: {exc}")
    if snapshot.get("mode") == "daily":
        try:
            await sync_daily_watch_session(key, snapshot)
        except Exception as exc:  # noqa: BLE001
            print(f"sync_daily_watch_session failed: {exc}")


async def drop_persisted_game(key: tuple, game: dict | None = None) -> None:
    if game is None:
        game = games.get(key)
    if game:
        try:
            gid = int(game.get("guild_id") or key[0])
            uid = int(game.get("owner_id") or key[1])
            mode = game.get("mode")
            if mode == "daily":
                sid = daily_watch_session_id(gid, uid)
                try:
                    await end_activity_watch(bot, sid, force=True)
                except Exception as watch_exc:  # noqa: BLE001
                    print(f"drop_persisted_game end_watch failed: {watch_exc}")
                await clear_activity_session(bot, sid)
            elif mode in ("solo", "play"):
                sid = f"activity:{gid}:{uid}"
                try:
                    await end_activity_watch(bot, sid, force=True)
                except Exception as watch_exc:  # noqa: BLE001
                    print(f"drop_persisted_game end_watch failed: {watch_exc}")
                await clear_activity_session(bot, sid)
        except Exception as exc:  # noqa: BLE001
            print(f"drop_persisted_game clear activity failed: {exc}")
    try:
        await match_store.delete_active_game(serialize_game_key(key))
    except Exception as exc:  # noqa: BLE001
        print(f"drop_persisted_game failed: {exc}")


async def remove_game(key: tuple) -> dict | None:
    game = games.pop(key, None)
    # drop_persisted_game owns end_activity_watch + clear_activity_session.
    await drop_persisted_game(key, game)
    return game


async def close_solved_session(
    bot: "SudokuBot",
    key: tuple,
    game: dict,
    user: discord.abc.User,
    guild_id: int | None,
) -> int:
    """If the board is already solved, award (once) and clear the session. Returns sponges awarded."""
    coins = 0
    if game.get("mode") == "challenge":
        await remove_game(key)
        return 0
    if is_solved(game.get("board") or [], game.get("solution")):
        if not game.get("rewarded") and guild_id is not None:
            try:
                mode_n = normalize_game_mode(game.get("mode"))
                if mode_n == "solo" or game.get("mode") == "play":
                    given = game.get("given")
                    given_bool = None
                    if isinstance(given, list) and len(given) == 9:
                        given_bool = [
                            [bool(given[r][c]) for c in range(9)] for r in range(9)
                        ]
                    puzzle_key = play_puzzle_fingerprint(
                        given_bool,
                        board=game.get("board"),
                        solution=game.get("solution"),
                    )
                    if puzzle_key:
                        outcome = await award_play_win(
                            bot, guild_id, user, game, puzzle_key=puzzle_key
                        )
                        if outcome is not None:
                            coins = int(outcome.coins)
                else:
                    outcome = await finish_win_and_announce(bot, guild_id, user, game)
                    coins = int(outcome.coins)
                game["rewarded"] = True
                try:
                    await persist_game(key, game)
                except Exception as persist_exc:  # noqa: BLE001
                    print(f"close_solved_session persist rewarded failed: {persist_exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"close_solved_session award failed: {exc}")
    await remove_game(key)
    return coins


async def _panel_play_claim_ok(
    guild_id: int, user: discord.abc.User, game: dict
) -> bool:
    """False if this solo/play puzzle was already paid (Activity fingerprint)."""
    mode = normalize_game_mode(game.get("mode"))
    if mode not in ("solo", "play"):
        return True
    given = game.get("given")
    if not isinstance(given, list):
        return True
    given_bool = [[bool(given[r][c]) for c in range(9)] for r in range(9)]
    puzzle_key = play_puzzle_fingerprint(
        given_bool,
        board=game.get("board"),
        solution=game.get("solution"),
    )
    if not puzzle_key:
        return False
    try:
        if await match_store.has_play_win(guild_id, user.id, puzzle_key):
            return False
        claimed = await match_store.try_claim_play_win(
            guild_id=guild_id,
            user_id=user.id,
            puzzle_key=puzzle_key,
        )
        return claimed is not False
    except Exception as exc:  # noqa: BLE001
        print(f"panel play claim failed: {exc}")
        return False


async def award_play_win(
    bot: "SudokuBot",
    guild_id: int,
    user: discord.abc.User,
    game: dict,
    *,
    puzzle_key: str | None,
) -> WinOutcome | None:
    """Claim then pay; release claim if payout fails. None = already paid.

    Requires a fingerprint — refuses award when puzzle_key is missing (fail-closed).
    """
    if not puzzle_key:
        raise ValueError("puzzle_key required for play win award")
    claimed = False
    try:
        if await match_store.has_play_win(guild_id, user.id, puzzle_key):
            return None
        ok = await match_store.try_claim_play_win(
            guild_id=guild_id,
            user_id=user.id,
            puzzle_key=puzzle_key,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"award_play_win claim failed: {exc}")
        raise
    if not ok:
        return None
    claimed = True
    try:
        return finish_win(bot.data, guild_id, user, game)
    except Exception:
        if claimed and puzzle_key:
            try:
                await match_store.release_play_win(guild_id, user.id, puzzle_key)
            except Exception as rel_exc:  # noqa: BLE001
                print(f"award_play_win release failed: {rel_exc}")
        raise


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
        games[key] = game
        return game
    return None


def deepcopy_game(game: dict) -> dict:
    """JSON-safe clone of a live game dict."""
    out = {}
    for k, v in game.items():
        if k in ("participants",):
            out[k] = list(v) if v else []
        elif k in ("_digit_lock", "finishing"):
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


def equipped_title_id(stats: dict) -> str | None:
    tid = stats.get("title")
    if tid and tid in SHOP_TITLES:
        return tid
    return None


def resolve_pin_id(tid: str) -> str | None:
    """Map legacy pin/theme ids onto the current SHOP_PINS catalog."""
    if tid in SHOP_PINS:
        return tid
    mapped = SHOP_PIN_ALIASES.get(tid)
    if mapped and mapped in SHOP_PINS:
        return mapped
    return None


def owned_pin_ids(stats: dict) -> list[str]:
    """Pin catalog IDs the player owns (legacy key: owned_themes)."""
    raw = list(stats.get("owned_pins") or stats.get("owned_themes") or [])
    out: list[str] = []
    seen: set[str] = set()
    for tid in raw:
        resolved = resolve_pin_id(str(tid))
        if resolved and resolved not in seen:
            out.append(resolved)
            seen.add(resolved)
    return out


def owned_pin_emojis(stats: dict) -> list[str]:
    """Border emojis from the Pins catalog only (titles are header flair, not pins)."""
    pins: list[str] = []
    seen: set[str] = set()
    for tid in owned_pin_ids(stats):
        meta = SHOP_PINS.get(tid)
        emoji = (meta or {}).get("emoji")
        if emoji and emoji not in seen:
            pins.append(emoji)
            seen.add(emoji)
    return pins


def sync_title_to_active_games(user_id: int, guild_id: int, title_id: str | None) -> None:
    for game in games.values():
        if game.get("owner_id") == user_id and game.get("guild_id") == guild_id:
            game["owner_title"] = title_id


def sync_pins_to_active_games(user_id: int, guild_id: int, pin_emojis: list[str]) -> None:
    for game in games.values():
        if game.get("owner_id") == user_id and game.get("guild_id") == guild_id:
            game["pin_emojis"] = list(pin_emojis)


def _daily_finish_lock(guild_id: int, user_id: int, day: str) -> asyncio.Lock:
    key = f"{guild_id}:{user_id}:{day}"
    lock = _daily_finish_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _daily_finish_locks[key] = lock
    return lock


def _challenge_match_lock(match_id: str) -> asyncio.Lock:
    """Serialize finish recording + settlement for one challenge match."""
    lock = _challenge_match_locks.get(match_id)
    if lock is None:
        lock = asyncio.Lock()
        _challenge_match_locks[match_id] = lock
    return lock


async def sync_cosmetics_to_activity_sessions(
    user_id: int,
    guild_id: int,
    *,
    title_id: str | None,
    pin_emojis: list[str],
) -> None:
    """Mirror equipped shop cosmetics into the player's open Activity session."""
    session, session_id = await lookup_user_activity_session(guild_id, user_id)
    if not session or not session_id:
        return
    await match_store.merge_activity_session(
        session_id,
        {
            "equipped_title_id": title_id,
            "pin_emojis": list(pin_emojis),
            "cosmetics_updated_at": time.time(),
        },
    )


def schedule_activity_cosmetics_sync(
    user_id: int,
    guild_id: int,
    *,
    title_id: str | None,
    pin_emojis: list[str],
) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            sync_cosmetics_to_activity_sessions(
                user_id,
                guild_id,
                title_id=title_id,
                pin_emojis=pin_emojis,
            )
        )
    except RuntimeError:
        pass


def push_cosmetics_sync(user_id: int, guild_id: int, stats: dict) -> None:
    """Update in-memory games + persisted Activity session after shop changes."""
    title_id = equipped_title_id(stats)
    pins = owned_pin_emojis(stats)
    sync_title_to_active_games(user_id, guild_id, title_id)
    sync_pins_to_active_games(user_id, guild_id, pins)
    schedule_activity_cosmetics_sync(
        user_id,
        guild_id,
        title_id=title_id,
        pin_emojis=pins,
    )


def cosmetic_pin_text(meta: dict | None, *, fallback: str = "") -> str:
    """Short ASCII-friendly badge text (captions / /z-admin testboard)."""
    if not meta:
        return fallback
    pin = (meta.get("pin") or "").strip()
    if pin:
        return pin[:18]
    label = str(meta.get("label") or fallback)
    cleaned = label.lstrip()
    while cleaned and ord(cleaned[0]) > 127:
        cleaned = cleaned[1:].lstrip()
    return (cleaned or fallback)[:18]


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def utc_iso_week(day: str | None = None) -> str:
    """UTC ISO week key, e.g. 2026-W31."""
    raw = day or utc_today()
    d = datetime.fromisoformat(str(raw)).date()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def ensure_weekly_progress(stats: dict, *, day: str | None = None) -> str:
    """Reset weekly counters when the ISO week rolls over. Returns current week key."""
    week = utc_iso_week(day)
    if stats.get("weekly_week") != week:
        stats["weekly_week"] = week
        stats["weekly_dailies"] = 0
        stats["weekly_boards"] = 0
        stats["weekly_challenges"] = 0
        stats["weekly_claimed"] = []
    stats.setdefault("weekly_dailies", 0)
    stats.setdefault("weekly_boards", 0)
    stats.setdefault("weekly_challenges", 0)
    stats.setdefault("weekly_claimed", [])
    return week


def note_weekly_win(
    stats: dict,
    *,
    is_daily: bool = False,
    challenge_winner: bool = False,
    day: str | None = None,
) -> list[str]:
    """Bump weekly counters and auto-claim any newly completed quests. Returns payout notes."""
    ensure_weekly_progress(stats, day=day)
    stats["weekly_boards"] = int(stats.get("weekly_boards") or 0) + 1
    if is_daily:
        stats["weekly_dailies"] = int(stats.get("weekly_dailies") or 0) + 1
    if challenge_winner:
        stats["weekly_challenges"] = int(stats.get("weekly_challenges") or 0) + 1
    return claim_ready_weekly_quests(stats)


def claim_ready_weekly_quests(stats: dict) -> list[str]:
    """Pay out completed unclaimed weekly quests. Returns human-readable notes."""
    ensure_weekly_progress(stats)
    claimed = set(stats.get("weekly_claimed") or [])
    notes: list[str] = []
    for quest in WEEKLY_QUESTS:
        qid = str(quest["id"])
        if qid in claimed:
            continue
        progress = int(stats.get(str(quest["counter"])) or 0)
        need = int(quest["need"])
        if progress < need:
            continue
        reward = int(quest["reward"])
        stats["coins"] = int(stats.get("coins") or 0) + reward
        claimed.add(qid)
        notes.append(
            f"{quest['emoji']} {quest['label']} · {format_sponges(reward, signed=True)}"
        )
    stats["weekly_claimed"] = [q["id"] for q in WEEKLY_QUESTS if q["id"] in claimed]
    return notes


def weekly_progress_lines(stats: dict) -> list[str]:
    """Status lines for /weekly and /stats."""
    week = ensure_weekly_progress(stats)
    claimed = set(stats.get("weekly_claimed") or [])
    lines: list[str] = []
    for quest in WEEKLY_QUESTS:
        progress = int(stats.get(str(quest["counter"])) or 0)
        need = int(quest["need"])
        done = progress >= need
        qid = str(quest["id"])
        if qid in claimed:
            mark = "✅"
        elif done:
            mark = "🎁"
        else:
            mark = "⬜"
        shown = min(progress, need)
        lines.append(
            f"{mark} {quest['emoji']} **{quest['label']}** · "
            f"{shown}/{need} · {format_sponges(int(quest['reward']))}"
        )
    lines.append(f"_Week `{week}` (resets Monday UTC)_")
    return lines


def build_weekly_embed(stats: dict, *, viewer_name: str | None = None) -> discord.Embed:
    who = viewer_name or display_name(stats)
    # Auto-claim anything already complete when opening /weekly.
    notes = claim_ready_weekly_quests(stats)
    embed = paper_embed(f"📅 Weekly Goals · {who}")
    embed.description = "\n".join(weekly_progress_lines(stats))
    if notes:
        embed.add_field(
            name="Claimed just now",
            value="\n".join(notes),
            inline=False,
        )
    return embed


def apply_daily_calendar_streak(stats: dict, day: str) -> int:
    """Advance streak for consecutive UTC days with a completed /daily.

    - At most +1 per calendar day
    - Missed days break the streak, unless streak shields cover each missed day
    - Returns the streak value used for rewards after this win
    """
    today = datetime.fromisoformat(str(day)).date()
    last_raw = stats.get("last_streak_day")
    current = max(0, int(stats.get("streak") or 0))

    if str(last_raw or "") == str(day):
        # Already counted today's daily — do not inflate.
        return max(current, 1)

    last = None
    if last_raw:
        try:
            last = datetime.fromisoformat(str(last_raw)).date()
        except ValueError:
            last = None

    if last is None:
        # First calendar win (or legacy win-count streak) — start a day streak.
        new_streak = 1
    else:
        gap = (today - last).days
        if gap <= 0:
            return max(current, 1)
        if gap == 1:
            new_streak = current + 1 if current > 0 else 1
        else:
            missed = gap - 1
            shields = max(0, int(stats.get("streak_shields") or 0))
            if shields >= missed:
                stats["streak_shields"] = shields - missed
                new_streak = current + 1 if current > 0 else 1
            else:
                new_streak = 1

    stats["streak"] = new_streak
    stats["last_streak_day"] = str(day)
    stats["best_streak"] = max(int(stats.get("best_streak") or 0), new_streak)
    return new_streak


def preview_daily_calendar_streak(stats: dict, day: str) -> int:
    """Compute what the streak would become without mutating player stats."""
    probe = {
        "streak": stats.get("streak"),
        "best_streak": stats.get("best_streak"),
        "last_streak_day": stats.get("last_streak_day"),
        "streak_shields": stats.get("streak_shields"),
    }
    return apply_daily_calendar_streak(probe, day)


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


def clear_pencil_digit_peers(board: list[list[dict]], r: int, c: int, digit: int) -> None:
    """Remove ``digit`` from notes in the same row, column, and 3×3 box."""
    digit = int(digit)
    if digit < 1 or digit > 9:
        return
    br, bc = (r // 3) * 3, (c // 3) * 3
    peers: set[tuple[int, int]] = set()
    for i in range(9):
        peers.add((r, i))
        peers.add((i, c))
    for i in range(3):
        for j in range(3):
            peers.add((br + i, bc + j))
    peers.discard((r, c))
    for pr, pc in peers:
        marks = list(board[pr][pc].get("pencil_marks") or [])
        if digit in marks:
            marks.remove(digit)
            board[pr][pc]["pencil_marks"] = marks


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


def difficulty_clues(key: str) -> int:
    return int(DIFFICULTY_TIERS.get(key, DIFFICULTY_TIERS[DEFAULT_DIFFICULTY])["clues"])


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


def _sudoku_cell_ok(grid: list[list[int]], r: int, c: int, v: int) -> bool:
    if any(grid[r][j] == v for j in range(9)):
        return False
    if any(grid[i][c] == v for i in range(9)):
        return False
    br, bc = (r // 3) * 3, (c // 3) * 3
    for i in range(br, br + 3):
        for j in range(bc, bc + 3):
            if grid[i][j] == v:
                return False
    return True


def _sudoku_candidates(grid: list[list[int]], r: int, c: int) -> list[int]:
    used = [False] * 10
    for j in range(9):
        used[grid[r][j]] = True
    for i in range(9):
        used[grid[i][c]] = True
    br, bc = (r // 3) * 3, (c // 3) * 3
    for i in range(br, br + 3):
        for j in range(bc, bc + 3):
            used[grid[i][j]] = True
    return [v for v in range(1, 10) if not used[v]]


def _sudoku_pick_empty(grid: list[list[int]]) -> tuple[int, int, list[int]] | None:
    """MRV: empty cell with the fewest candidates (speeds uniqueness checks a lot)."""
    best: tuple[int, int, list[int]] | None = None
    best_n = 10
    for r in range(9):
        for c in range(9):
            if grid[r][c] != 0:
                continue
            cands = _sudoku_candidates(grid, r, c)
            n = len(cands)
            if n == 0:
                return r, c, []
            if n < best_n:
                best = (r, c, cands)
                best_n = n
                if n == 1:
                    return best
    return best


def _sudoku_fill(grid: list[list[int]], rng: random.Random) -> bool:
    """Fill an empty/partial grid with a valid complete Sudoku (randomized)."""
    pick = _sudoku_pick_empty(grid)
    if pick is None:
        return True
    r, c, cands = pick
    if not cands:
        return False
    rng.shuffle(cands)
    for v in cands:
        grid[r][c] = v
        if _sudoku_fill(grid, rng):
            return True
        grid[r][c] = 0
    return False


def _sudoku_count_solutions(grid: list[list[int]], limit: int = 2) -> int:
    """Count solutions up to `limit` (2 is enough to prove non-uniqueness)."""
    count = 0

    def bt() -> None:
        nonlocal count
        if count >= limit:
            return
        pick = _sudoku_pick_empty(grid)
        if pick is None:
            count += 1
            return
        r, c, cands = pick
        if not cands:
            return
        for v in cands:
            grid[r][c] = v
            bt()
            grid[r][c] = 0
            if count >= limit:
                return

    bt()
    return count


def generate_unique_sudoku(
    *,
    target_clues: int,
    seed: int | None = None,
) -> tuple[list[list[int]], list[list[int]]]:
    """Build a uniquely solvable puzzle near `target_clues` givens.

    Returns (puzzle_grid with 0=empty, solution_grid).
    Retries a few dig orders so harder tiers actually reach the clue target.
    """
    target = max(17, min(50, int(target_clues)))
    base_seed = seed if seed is not None else random.randrange(1 << 30)

    best_puzzle: list[list[int]] | None = None
    best_solution: list[list[int]] | None = None
    best_clues = 81

    for attempt in range(5):
        rng = random.Random(base_seed + attempt * 1_000_003)
        solution = [[0] * 9 for _ in range(9)]
        if not _sudoku_fill(solution, rng):
            continue

        puzzle = [row[:] for row in solution]
        order = [(r, c) for r in range(9) for c in range(9)]
        rng.shuffle(order)

        for r, c in order:
            clues_now = sum(1 for row in puzzle for v in row if v)
            if clues_now <= target:
                break
            backup = puzzle[r][c]
            puzzle[r][c] = 0
            if _sudoku_count_solutions(puzzle, limit=2) != 1:
                puzzle[r][c] = backup

        clues = sum(1 for row in puzzle for v in row if v)
        if clues < best_clues:
            best_clues = clues
            best_puzzle = puzzle
            best_solution = solution
        if clues <= target:
            break

    if best_puzzle is None or best_solution is None:
        # Last resort — should be unreachable
        return generate_unique_sudoku(target_clues=target, seed=None)

    return best_puzzle, best_solution


# Header flair when a title is equipped — one vibe per difficulty tier
TITLE_HEADER_LINES = {
    "Very Easy": "Ahoy, {title} — jellyfishing warm-up!",
    "Easy": "I'm ready, {title}!",
    "Medium": "Order up, {title}!",
    "Hard": "Aye aye, {title} — hold the tartar sauce!",
    "Very Hard": "Jumping jellyfish, {title}!",
    "Expertttt": "Barnacles! Go get 'em, {title}!",
}


def titled_header_badge(title_pin: str, emoji: str = "") -> str:
    """Title name with optional leading emoji for header flair."""
    pin = (title_pin or "").strip()
    em = (emoji or "").strip()
    if em and pin:
        return f"{em} {pin}"
    return pin or em


def titled_header_line(tier: str, title_pin: str, emoji: str = "") -> str:
    """Difficulty + SpongeBob flair with the equipped title (emoji + name)."""
    template = TITLE_HEADER_LINES.get(tier) or "I'm ready, {title}!"
    badge = titled_header_badge(title_pin, emoji)
    return f"~ {tier} ~  {template.format(title=badge)}"


def make_puzzle(
    difficulty: float | str = DEFAULT_DIFFICULTY, seed: int | None = None
) -> tuple[list[list[dict]], list[list[bool]], list[list[int]]]:
    """Unique-solution Sudoku for the given difficulty tier."""
    if isinstance(difficulty, str):
        key = difficulty_key_from_label(difficulty)
        clues = difficulty_clues(key)
    else:
        # Legacy float weight → map into clue band (kept for old callers)
        w = float(difficulty)
        clues = int(round(50 - w * 32))
        clues = max(17, min(50, clues))

    puzzle, solution = generate_unique_sudoku(target_clues=clues, seed=seed)

    board = [[make_cell(int(v)) for v in row] for row in puzzle]
    given = [[v != 0 for v in row] for row in puzzle]
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


def daily_session_has_player_progress(session: dict | None) -> bool:
    """True if the player filled any non-clue cell on this daily board."""
    if not session:
        return False
    board = session.get("board")
    given = session.get("given")
    if not isinstance(board, list) or not isinstance(given, list) or len(board) != 9:
        return False
    try:
        for r in range(9):
            for c in range(9):
                is_given = bool(given[r][c]) if r < len(given) and c < len(given[r]) else False
                if is_given:
                    continue
                cell = board[r][c]
                val = int(cell.get("value", 0)) if isinstance(cell, dict) else int(cell or 0)
                if val:
                    return True
                if isinstance(cell, dict) and (cell.get("pencil_marks") or []):
                    return True
    except (TypeError, ValueError, IndexError):
        return False
    return False


def ensure_daily_session_schedule(session: dict) -> tuple[dict, bool]:
    """Align a daily Activity session to today's weekday difficulty.

    Weekday schedule is authoritative — a Medium board on Friday is regenerated
    to Very Hard (progress on the wrong-tier board is discarded).
    """
    if not session or session.get("session_kind") != "daily":
        return session, False
    day = str(session.get("daily_date") or utc_today())
    expected = daily_difficulty_for_date(day)
    stored, stored_idx = resolve_session_difficulty(session)
    if stored == expected:
        changed = False
        if session.get("difficulty") != expected or session.get("diff_index") != stored_idx:
            session["difficulty"] = expected
            try:
                session["diff_index"] = DIFF_KEYS_LIST.index(expected)
            except ValueError:
                session["diff_index"] = difficulty_index(expected)
            session["daily_date"] = day
            changed = True
        return session, changed

    try:
        guild_id = int(session.get("guild_id") or 0)
        user_id = int(session.get("user_id") or 0)
    except (TypeError, ValueError):
        guild_id, user_id = 0, 0
    board, given, solution, diff_key = make_daily_puzzle(guild_id, day, user_id)
    try:
        diff_index = DIFF_KEYS_LIST.index(diff_key)
    except ValueError:
        diff_index = difficulty_index(diff_key)
    session = {
        **session,
        "daily_date": day,
        "difficulty": diff_key,
        "diff_index": diff_index,
        "board": board,
        "given": given,
        "solution": solution,
        "filled": game_filled_count({"board": board}),
        "elapsed": 0,
        "hints_used": 0,
        "hints_gary_used": 0,
    }
    return session, True


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
    out: list[list[int]] = []
    for row in solution:
        if not isinstance(row, (list, tuple)) or len(row) != 9:
            return []
        out_row: list[int] = []
        for cell in row:
            if isinstance(cell, dict):
                out_row.append(int(cell.get("value") or 0))
            else:
                try:
                    out_row.append(int(cell))
                except (TypeError, ValueError):
                    return []
        out.append(out_row)
    if len(out) != 9:
        return []
    # A real solution is a full grid — reject sparse/puzzle grids stored by mistake.
    if any(v < 1 or v > 9 for row in out for v in row):
        return []
    return out


def is_complete(board: list[list[dict]], solution: list[list[int]]) -> bool:
    return values_grid(board) == normalize_solution(solution)


def is_solved(board: list[list[dict]], solution: list[list[int]] | None = None) -> bool:
    """True when the board matches the unique stored solution (full + no conflicts)."""
    if filled_count(board) < 81:
        return False
    if find_conflicts(board):
        return False
    if not solution:
        # No solution on record — accept any conflict-free complete grid
        return True
    sol = normalize_solution(solution)
    if not sol:
        # Solution was provided but corrupt/sparse — do not accept blindly.
        return False
    return values_grid(board) == sol


def filled_count(board: list[list[dict]]) -> int:
    return sum(1 for r in range(9) for c in range(9) if cell_value(board, r, c) != 0)


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def record_solve_times(stats: dict, elapsed: float | int | None) -> None:
    """Update career fastest (`best_time`) and longest (`longest_time`) solve times."""
    if elapsed is None:
        return
    try:
        t = int(float(elapsed))
    except (TypeError, ValueError):
        return
    if t < 0:
        return
    best = stats.get("best_time")
    if best is None or t < float(best):
        stats["best_time"] = t
    longest = stats.get("longest_time")
    if longest is None or t > float(longest):
        stats["longest_time"] = t


def clear_solve_times(stats: dict) -> None:
    """Wipe career best/longest solve times (and speed badges tied to them)."""
    stats["best_time"] = None
    stats["longest_time"] = None
    badges = [b for b in (stats.get("badges") or []) if b not in ("speed_demon", "jelly_flash")]
    stats["badges"] = badges


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


def _twemoji_code(emoji: str) -> str:
    """Twemoji filename codepoints (skip variation selector)."""
    parts = [f"{ord(ch):x}" for ch in emoji if ord(ch) != 0xFE0F]
    return "-".join(parts)


_EMOJI_PIN_MEMO: dict[tuple[str, int], Image.Image] = {}


def load_emoji_pin(emoji: str, size: int = PIN_EMOJI_SIZE) -> Image.Image | None:
    """Load an emoji PNG for border/header pins (disk + memory cache; misses not cached)."""
    if not emoji:
        return None
    key = (emoji, int(size))
    hit = _EMOJI_PIN_MEMO.get(key)
    if hit is not None:
        return hit
    code = _twemoji_code(emoji)
    if not code:
        return None
    try:
        EMOJI_PIN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    path = EMOJI_PIN_DIR / f"{code}.png"
    if not path.exists():
        # Twemoji 14 misses newer glyphs (e.g. 🪼 U+1FABC); fall back to newer packs.
        urls = (
            f"https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{code}.png",
            f"https://cdn.jsdelivr.net/npm/emoji-datasource-twitter@15.1.2/img/twitter/64/{code}.png",
            f"https://cdn.jsdelivr.net/gh/googlefonts/noto-emoji@main/png/72/emoji_u{code}.png",
            f"https://cdn.jsdelivr.net/npm/emoji-datasource-google@15.1.2/img/google/64/{code}.png",
        )
        fetched = False
        for url in urls:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if resp.status == 200:
                        path.write_bytes(resp.read())
                        fetched = True
                        break
            except Exception:
                continue
        if not fetched:
            return None
    try:
        im = Image.open(path).convert("RGBA")
        out = im.resize((size, size), Image.Resampling.LANCZOS)
        _EMOJI_PIN_MEMO[key] = out
        return out
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _border_pin_slots(
    *,
    canvas: int,
    header_h: int,
    origin_x: int,
    origin_y: int,
    grid: int,
    pin_size: int,
) -> list[tuple[int, int]]:
    """Candidate top-left positions in the cream margin around the grid (no top — conflicts with header)."""
    slots: list[tuple[int, int]] = []
    gap = pin_size + 6
    # Bottom margin
    bottom_y = origin_y + grid + max(2, (canvas - (origin_y + grid) - pin_size) // 2)
    if bottom_y + pin_size <= canvas - 2:
        for x in range(origin_x, origin_x + grid - pin_size + 1, gap):
            slots.append((x, bottom_y))
    # Left margin
    left_x = max(2, (origin_x - pin_size) // 2)
    for y in range(origin_y, origin_y + grid - pin_size + 1, gap):
        slots.append((left_x, y))
    # Right margin
    right_x = origin_x + grid + max(2, (canvas - (origin_x + grid) - pin_size) // 2)
    if right_x + pin_size <= canvas - 2:
        for y in range(origin_y, origin_y + grid - pin_size + 1, gap):
            slots.append((right_x, y))
    return slots


def paste_owned_emoji_pins(
    img: Image.Image,
    *,
    pin_emojis: list[str] | None,
    pin_seed: int | None,
    canvas: int,
    header_h: int,
    origin_x: int,
    origin_y: int,
    grid: int,
) -> Image.Image:
    """Scatter purchased cosmetic emojis randomly (stable seed) on the frame margins."""
    emojis = [e for e in (pin_emojis or []) if e]
    if not emojis:
        return img
    pin_size = PIN_EMOJI_SIZE
    slots = _border_pin_slots(
        canvas=canvas,
        header_h=header_h,
        origin_x=origin_x,
        origin_y=origin_y,
        grid=grid,
        pin_size=pin_size,
    )
    if not slots:
        return img

    rng = random.Random(int(pin_seed or 1))
    rng.shuffle(slots)
    # One pin per owned emoji — no duplicates (buying more cosmetics = more unique pins)
    unique: list[str] = []
    seen: set[str] = set()
    for e in emojis:
        if e not in seen:
            unique.append(e)
            seen.add(e)
    chosen_slots = slots[: min(len(slots), len(unique))]
    base = img.convert("RGBA")
    for i, (x, y) in enumerate(chosen_slots):
        emoji = unique[i]
        pin = load_emoji_pin(emoji, pin_size)
        if pin is None:
            continue
        # Soft circular backing so pins read as "stuck" on the border
        badge = Image.new("RGBA", (pin_size + 6, pin_size + 6), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(badge)
        bdraw.ellipse(
            (0, 0, pin_size + 5, pin_size + 5),
            fill=(255, 255, 255, 210),
            outline=(245, 158, 11, 255),
            width=2,
        )
        badge.alpha_composite(pin, dest=(3, 3))
        base.alpha_composite(badge, dest=(max(0, x - 3), max(0, y - 3)))
    return base.convert("RGB")


def render_board(
    board: list[list[dict]],
    given: list[list[bool]],
    *,
    solution: list[list[int]] | None = None,
    selected: tuple[int, int] | None = None,
    conflicts: set[tuple[int, int]] | None = None,
    highlight_box: int | None = None,
    difficulty: str | None = None,
    theme_id: str | None = None,
    title_id: str | None = None,
    pin_emojis: list[str] | None = None,
    pin_seed: int | None = None,
) -> BytesIO:
    """Bikini Bottom board — large grid with random owned-emoji pins on the margins."""
    _ = solution
    _ = theme_id  # color packs removed; always Lagoon Classic palette
    pal = DEFAULT_BOARD_PALETTE
    conflicts = conflicts or set()
    canvas = BOARD_CANVAS
    header_h = BOARD_HEADER_H
    pad = BOARD_CARD_PAD
    radius = BOARD_CARD_RADIUS
    inner = BOARD_INNER_PAD

    img = Image.new("RGB", (canvas, canvas), pal["card"])
    draw = ImageDraw.Draw(img)

    # Full-width header bar (same width as the grid / Discord keyboard)
    draw.rectangle((0, 0, canvas, header_h), fill=pal["header_bar"])
    draw.line((0, header_h - 1, canvas, header_h - 1), fill=pal["card_border"], width=2)

    tier = difficulty_label(difficulty)
    title_meta = SHOP_TITLES.get(title_id or "")
    title_pin = cosmetic_pin_text(title_meta) if title_meta else ""
    title_emoji = str((title_meta or {}).get("emoji") or "").strip() if title_meta else ""
    header_fill = pal["header_text"]

    def _fit_font(text: str, *, max_size: int, min_size: int = 18) -> ImageFont.ImageFont:
        """Largest bold font that fits the header width (mobile-readable)."""
        for size in range(max_size, min_size - 1, -2):
            font = board_font(size, bold=True)
            bb = draw.textbbox((0, 0), text, font=font)
            if bb[2] - bb[0] <= canvas - 20:
                return font
        return board_font(min_size, bold=True)

    if not title_pin:
        header_label = f"~ {tier} ~"
        header_font = _fit_font(header_label, max_size=36, min_size=22)
        hb = draw.textbbox((0, 0), header_label, font=header_font)
        htw, hth = hb[2] - hb[0], hb[3] - hb[1]
        draw.text(
            ((canvas - htw) / 2, (header_h - hth) / 2),
            header_label,
            fill=header_fill,
            font=header_font,
        )
    else:
        # Two lines fill the blue bar: difficulty on top, flair + title below
        template = TITLE_HEADER_LINES.get(tier) or "I'm ready, {title}!"
        pre, _, post = template.partition("{title}")
        line1 = f"~ {tier} ~"
        pin_draw = title_pin
        header_emoji_size = 32

        def _flair_width(font, pin: str) -> tuple[int, int, int]:
            lb = draw.textbbox((0, 0), pre, font=font)
            rb = draw.textbbox((0, 0), pin + post, font=font)
            lw = lb[2] - lb[0]
            rw = rb[2] - rb[0]
            th = max(lb[3] - lb[1], rb[3] - rb[1], header_emoji_size)
            em_w = (header_emoji_size + 4) if title_emoji else 0
            return lw + em_w + rw, lw, th

        line1_font = _fit_font(line1, max_size=32, min_size=20)
        flair_font = board_font(28, bold=True)
        total_w, left_w, text_h = _flair_width(flair_font, pin_draw)
        for size, em in ((26, 30), (24, 28), (22, 26), (20, 24)):
            if total_w <= canvas - 16:
                break
            flair_font = board_font(size, bold=True)
            header_emoji_size = em
            total_w, left_w, text_h = _flair_width(flair_font, pin_draw)
        while total_w > canvas - 16 and len(pin_draw) > 4:
            pin_draw = pin_draw[:-1]
            trial = pin_draw + "…"
            total_w, left_w, text_h = _flair_width(flair_font, trial)
            if total_w <= canvas - 16 or len(pin_draw) <= 4:
                pin_draw = trial
                break

        l1b = draw.textbbox((0, 0), line1, font=line1_font)
        l1w, l1h = l1b[2] - l1b[0], l1b[3] - l1b[1]
        gap = 2
        block_h = l1h + gap + text_h
        y0 = max(2, (header_h - block_h) / 2)

        draw.text(
            ((canvas - l1w) / 2, y0),
            line1,
            fill=header_fill,
            font=line1_font,
        )
        y_flair = y0 + l1h + gap
        x = (canvas - total_w) / 2
        draw.text((x, y_flair), pre, fill=header_fill, font=flair_font)
        x += left_w
        if title_emoji:
            em_img = load_emoji_pin(title_emoji, header_emoji_size)
            if em_img is not None:
                ey = int(y_flair + (text_h - header_emoji_size) / 2)
                img.paste(em_img, (int(x), ey), em_img)
                x += header_emoji_size + 4
            else:
                eb = draw.textbbox((0, 0), title_emoji + " ", font=flair_font)
                draw.text(
                    (x, y_flair),
                    title_emoji + " ",
                    fill=header_fill,
                    font=flair_font,
                )
                x += eb[2] - eb[0]
        draw.text((x, y_flair), pin_draw + post, fill=header_fill, font=flair_font)

    # Board card = full remaining area (classic large grid)
    card_bottom = canvas - pad
    card = (pad, header_h, canvas - pad, card_bottom)
    if radius > 0:
        draw.rounded_rectangle(
            card, radius=radius, fill=pal["card"], outline=pal["card_border"], width=3
        )
    else:
        draw.rectangle(card, fill=pal["card"], outline=pal["card_border"], width=3)

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
                fill = pal["conflict"]
            elif selected == (r, c):
                fill = pal["select"]
            elif (r, c) in box_cells:
                fill = pal["box_hl"]
            elif given[r][c]:
                fill = pal["given_cell"]
            else:
                fill = pal["empty"]

            draw.rectangle((x0, y0, x1, y1), fill=fill)

    # Cell lines first, then bold 3×3 charcoal borders
    for i in range(10):
        is_block = i % 3 == 0
        width_line = 3 if is_block else 1
        color = pal["thick"] if is_block else pal["line"]
        pos_y = origin_y + i * cell
        pos_x = origin_x + i * cell
        draw.line((origin_x, pos_y, origin_x + grid, pos_y), fill=color, width=width_line)
        draw.line((pos_x, origin_y, pos_x, origin_y + grid), fill=color, width=width_line)

    draw.rectangle(card, outline=pal["card_border"], width=3)

    # Selection rings (fills already tint cells — no wash overlay over ink)
    if highlight_box is not None and selected is None:
        br, bc = highlight_box // 3, highlight_box % 3
        bx0 = origin_x + bc * 3 * cell
        by0 = origin_y + br * 3 * cell
        bx1 = bx0 + 3 * cell
        by1 = by0 + 3 * cell
        draw.rectangle((bx0 + 1, by0 + 1, bx1 - 1, by1 - 1), outline=pal["outline"], width=4)

    if selected is not None:
        r, c = selected
        x0 = origin_x + c * cell
        y0 = origin_y + r * cell
        x1 = x0 + cell
        y1 = y0 + cell
        draw.rectangle((x0 + 1, y0 + 1, x1 - 1, y1 - 1), outline=pal["outline"], width=4)

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
                    color = pal["text_conflict"]
                    font = font_player
                elif given[r][c]:
                    color = pal["text_given"]
                    font = font_given
                else:
                    color = pal["text"]
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
                        fill=pal["pencil"],
                        font=pencil_font,
                    )

    img = paste_owned_emoji_pins(
        img,
        pin_emojis=pin_emojis,
        pin_seed=pin_seed,
        canvas=canvas,
        header_h=header_h,
        origin_x=origin_x,
        origin_y=origin_y,
        grid=grid,
    )

    out = BytesIO()
    img.save(out, format="PNG", compress_level=1)
    out.seek(0)
    return out


WIN_BANNER_LINES = (
    "I'm ready! You earned",
    "Order up! You earned",
    "Aye aye! You earned",
    "Bikini Bottom pays",
    "Goofy Goober bonus",
)


def try_consume_gary_wisdom(stats: dict) -> int:
    """Consume one Gary's Wisdom charge; return bonus hints for this game (0 if none)."""
    charges = int(stats.get("gary_wisdom_charges") or 0)
    if charges <= 0:
        return 0
    stats["gary_wisdom_charges"] = charges - 1
    return GARY_WISDOM_HINT_BONUS


def attach_gary_wisdom_to_session(
    stats: dict,
    doc: dict,
    *,
    existing: dict | None,
    same_puzzle: bool,
) -> None:
    """Attach Gary's Wisdom hint bonus when a new Activity game session starts."""
    if existing and int(existing.get("gary_wisdom_bonus") or 0) > 0:
        if same_puzzle or (existing.get("session_kind") == "daily"):
            doc["gary_wisdom_bonus"] = int(existing["gary_wisdom_bonus"])
            return
    bonus = try_consume_gary_wisdom(stats)
    if bonus > 0:
        doc["gary_wisdom_bonus"] = bonus


def attach_gary_wisdom_to_game(stats: dict, game: dict) -> None:
    """Attach Gary's Wisdom bonus to an in-memory game (challenge / panel)."""
    if int(game.get("gary_wisdom_bonus") or 0) > 0:
        return
    bonus = try_consume_gary_wisdom(stats)
    if bonus > 0:
        game["gary_wisdom_bonus"] = bonus


def hint_gary_free_remaining(container: dict) -> int:
    total = int(container.get("gary_wisdom_bonus") or 0)
    used = int(container.get("hints_gary_used") or 0)
    return max(0, total - used)


def apply_hint_charge(stats: dict, container: dict) -> dict:
    """Spend Gary free hints first, otherwise pocket sponges. Mutates stats + container."""
    gary_free = hint_gary_free_remaining(container)
    if gary_free > 0:
        container["hints_gary_used"] = int(container.get("hints_gary_used") or 0) + 1
        return {
            "ok": True,
            "cost": 0,
            "gary_free_left": gary_free - 1,
            "paid_with": "gary",
            "pocket": int(stats.get("coins") or 0),
        }
    pocket = int(stats.get("coins") or 0)
    if pocket < HINT_SPONGE_COST:
        return {
            "ok": False,
            "error": "insufficient_sponges",
            "cost": HINT_SPONGE_COST,
            "gary_free_left": 0,
            "pocket": pocket,
        }
    stats["coins"] = pocket - HINT_SPONGE_COST
    return {
        "ok": True,
        "cost": HINT_SPONGE_COST,
        "gary_free_left": 0,
        "paid_with": "sponges",
        "pocket": int(stats.get("coins") or 0),
    }


def format_xp_boost_win_line(*, used: bool, remaining: int | None = None) -> str:
    """Optional win footer when Puff's Crystal Ball doubled the payout."""
    if not used:
        return ""
    rem = f" · **{remaining}** left" if remaining is not None else ""
    return f"\n🔮 **Crystal Ball 2×** active{rem}"


def format_win_boost_lines(
    *,
    xp_boost_used: bool = False,
    xp_boost_remaining: int | None = None,
    krabby_snack_used: bool = False,
    krabby_snack_remaining: int | None = None,
    golden_spatula_used: bool = False,
    golden_spatula_remaining: int | None = None,
) -> str:
    """Readable multi-line footer for active win power-ups."""
    lines: list[str] = []
    if xp_boost_used:
        rem = f" · **{xp_boost_remaining}** left" if xp_boost_remaining is not None else ""
        lines.append(f"🔮 **Crystal Ball 2×** active{rem}")
    if krabby_snack_used:
        rem = f" · **{krabby_snack_remaining}** left" if krabby_snack_remaining is not None else ""
        lines.append(f"🍟 **Krabby Snack +25%** sponges{rem}")
    if golden_spatula_used:
        rem = f" · **{golden_spatula_remaining}** left" if golden_spatula_remaining is not None else ""
        lines.append(f"🥇 **Golden Spatula +50%** XP{rem}")
    if not lines:
        return ""
    return "\n" + "\n".join(lines)


def win_reward_caption(
    coins: int,
    xp: int | None = None,
    *,
    xp_boost_used: bool = False,
    xp_boost_remaining: int | None = None,
    krabby_snack_used: bool = False,
    krabby_snack_remaining: int | None = None,
    golden_spatula_used: bool = False,
    golden_spatula_remaining: int | None = None,
) -> str:
    """Readable win line under the board image (XP + sponges)."""
    line = random.choice(WIN_BANNER_LINES)
    gained_xp = int(coins if xp is None else xp)
    boost_line = format_win_boost_lines(
        xp_boost_used=xp_boost_used,
        xp_boost_remaining=xp_boost_remaining,
        krabby_snack_used=krabby_snack_used,
        krabby_snack_remaining=krabby_snack_remaining,
        golden_spatula_used=golden_spatula_used,
        golden_spatula_remaining=golden_spatula_remaining,
    )
    return (
        f"{BUBBLE} **{line} {format_xp(gained_xp, signed=True)} · "
        f"{format_sponges(max(int(coins), 0), signed=True)}!**{boost_line}"
    )


def build_activity_win_embed(
    *,
    user_id: int,
    difficulty: str,
    elapsed: int,
    coins: int,
    xp: int,
    streak: int,
    is_daily: bool = False,
    user_stats_dict: dict | None = None,
    xp_boost_used: bool = False,
    xp_boost_remaining: int | None = None,
    krabby_snack_used: bool = False,
    krabby_snack_remaining: int | None = None,
    golden_spatula_used: bool = False,
    golden_spatula_remaining: int | None = None,
) -> discord.Embed:
    """Channel announcement when someone clears a Sudoku puzzle."""
    mention = f"<@{user_id}>"
    tier = difficulty_label(difficulty)
    badge = PINEAPPLE if is_daily else SPONGE
    label = "Daily Sudoku" if is_daily else "Sudoku"
    stats_ref = user_stats_dict or {}
    total_xp = int(stats_ref.get("xp") or 0)
    badges = evaluate_user_achievements(stats_ref) if stats_ref else []
    badge_str = " ".join(ACHIEVEMENTS[b]["label"].split()[0] for b in badges if b in ACHIEVEMENTS)
    badge_line = f"\n🎖️ **Badges:** {badge_str}" if badge_str else ""
    boost_line = format_win_boost_lines(
        xp_boost_used=xp_boost_used,
        xp_boost_remaining=xp_boost_remaining,
        krabby_snack_used=krabby_snack_used,
        krabby_snack_remaining=krabby_snack_remaining,
        golden_spatula_used=golden_spatula_used,
        golden_spatula_remaining=golden_spatula_remaining,
    )

    embed = paper_embed(f"{badge} {mention} completed the {label}!")
    embed.description = (
        f"🏆 **Rank:** {format_rank_line(total_xp)}\n"
        f"🎯 **{tier}** · ⏱️ **{format_time(elapsed)}** · {STAR} **Streak: {streak}**\n"
        f"🎁 **{format_xp(xp, signed=True)}** · **{format_sponges(coins, signed=True)}**"
        f"{boost_line}{badge_line}"
    )
    return embed


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
    owner_name: str | None = None,
    owner_title: str | None = None,
    pin_emojis: list[str] | None = None,
    pin_seed: int | None = None,
) -> dict:
    # Persist the canonical difficulty key (e.g. "expertttt"); labels are for display only.
    diff_key = difficulty_key_from_label(difficulty or DEFAULT_DIFFICULTY)
    return {
        "mode": mode,
        "board": normalize_board(board),
        "given": [row[:] for row in given],
        "solution": normalize_solution(solution),
        "difficulty": diff_key,
        "ui_stage": STAGE_BOX,
        "box_id": 0,
        "sel_r": 0,
        "sel_c": 0,
        "pencil_mode": False,
        "owner_id": owner_id,
        "owner_name": owner_name or "Unknown",
        "owner_title": owner_title,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "match_id": match_id,
        "player_slot": player_slot,
        "participants": {owner_id},
        "started_at": time.time() if started_at is None else float(started_at),
        "hints_used": 0,
        "daily_date": daily_date,
        "message_id": None,
        "pin_emojis": list(pin_emojis or []),
        "pin_seed": int(pin_seed if pin_seed is not None else random.randrange(1 << 30)),
    }


def challenge_game_key(match_id: str, user_id: int) -> tuple:
    return ("ch", str(match_id), int(user_id))


def find_challenge_game_for_user(user_id: int) -> tuple | None:
    uid = int(user_id)
    for key, game in games.items():
        if game.get("mode") != "challenge":
            continue
        try:
            owner = int(game.get("owner_id") or 0)
        except (TypeError, ValueError):
            continue
        if owner == uid:
            return key
    return None


async def purge_challenge_games_for_match(match_id: str, match: dict | None = None) -> int:
    """Drop leftover in-memory/persisted boards for a match. Returns how many removed."""
    mid = str(match_id)
    removed = 0
    # Pop every in-memory key for this match first (covers legacy string user_id keys).
    for key, game in list(games.items()):
        if game.get("mode") != "challenge":
            continue
        game_mid = str(game.get("match_id") or "")
        key_mid = str(key[1]) if isinstance(key, tuple) and len(key) >= 2 else ""
        if game_mid != mid and key_mid != mid:
            continue
        await remove_game(key)
        removed += 1
    # Also clear normalized persisted ids for roster players (even if not in memory).
    uids: set[int] = set()
    if match:
        for _slot, player in match_player_entries(match):
            try:
                uid = int(player.get("user_id") or 0)
            except (TypeError, ValueError):
                uid = 0
            if uid:
                uids.add(uid)
    for uid in uids:
        await remove_game(challenge_game_key(mid, uid))
    return removed


async def reconcile_challenge_game_for_user(user_id: int) -> tuple | None:
    """Return a still-live challenge key, dropping boards for finished/orphan races.

    Ghost boards after settle (or a finished player waiting on peers) used to keep
    blocking `/play` forever even though the race was done.
    """
    uid = int(user_id)
    ch_key = find_challenge_game_for_user(uid)
    if not ch_key:
        return None
    game = games.get(ch_key)
    if not game:
        return None
    match_id = game.get("match_id")
    if not match_id and isinstance(ch_key, tuple) and len(ch_key) >= 2:
        match_id = ch_key[1]
    if not match_id:
        await remove_game(ch_key)
        return None
    try:
        match = await match_store.get_match(str(match_id))
    except Exception as exc:  # noqa: BLE001
        print(f"reconcile_challenge_game_for_user get_match failed: {exc}")
        # Fail closed — keep blocking if we cannot verify.
        return ch_key
    if not match or match.get("status") == "finished" or match.get("rewards_applied"):
        await purge_challenge_games_for_match(str(match_id), match)
        return None
    for _slot, player in match_player_entries(match):
        if int(player.get("user_id") or 0) != uid:
            continue
        if player.get("forfeit") or player.get("finished_time") is not None:
            # Player is done; board should not block /play (Mongo wait message may still apply).
            await remove_game(ch_key)
            return None
        return ch_key
    # Owner no longer on the match roster — orphan.
    await remove_game(ch_key)
    return None


async def ensure_challenge_game_for_user(bot: "SudokuBot", user_id: int) -> tuple | None:
    """Return in-memory challenge key, rehydrating from an active Mongo match if needed."""
    ch_key = await reconcile_challenge_game_for_user(user_id)
    if ch_key:
        return ch_key
    try:
        active = await match_store.list_matches(status="active")
    except Exception as exc:  # noqa: BLE001
        print(f"ensure_challenge_game_for_user list failed: {exc}")
        return None
    uid = int(user_id)
    for match in active:
        for _slot, player in match_player_entries(match):
            if int(player.get("user_id") or 0) != uid:
                continue
            if player.get("forfeit") or player.get("finished_time") is not None:
                continue
            try:
                await restore_challenge_games_from_match(bot, match)
            except Exception as exc:  # noqa: BLE001
                print(f"ensure_challenge_game_for_user restore failed: {exc}")
                return None
            return await reconcile_challenge_game_for_user(uid)
    return None


async def challenge_blocks_user(user_id: int) -> str | None:
    """Reason the user cannot start play/daily/another challenge (unsettled race)."""
    if await reconcile_challenge_game_for_user(user_id):
        return "Finish your speedrun challenge first (`/quit`)."
    try:
        active = await match_store.list_matches(status="active")
    except Exception as exc:  # noqa: BLE001
        print(f"challenge_blocks_user list failed (fail-closed): {exc}")
        return (
            "Couldn't verify challenge status right now — try again in a moment."
        )
    uid = int(user_id)
    for match in active:
        for _slot, player in match_player_entries(match):
            if int(player.get("user_id") or 0) != uid:
                continue
            if player.get("forfeit"):
                continue
            if player.get("finished_time") is not None:
                return (
                    "You're waiting for other players to finish your challenge "
                    "before you can start something new."
                )
            return "Finish your speedrun challenge first (`/quit`)."
    return None


def paper_embed(title: str, *, description: str | None = None) -> discord.Embed:
    """Bikini Bottom embed shell — sunny yellow + themed footer."""
    embed = discord.Embed(title=title, color=COLOR_PAPER)
    if description:
        embed.description = description
    embed.set_footer(text=f"{SPONGE} Bikini Bottom Sudoku  ·  I'm ready!")
    return embed


def streak_flavor(streak: int) -> str:
    if streak <= 0:
        return "cold streak — time to jellyfish again"
    if streak == 1:
        return "just getting started"
    if streak < 5:
        return "warming up the grill"
    if streak < 10:
        return "Krusty Krab regular"
    return "legendary fry cook energy"


@dataclass(frozen=True)
class WinOutcome:
    """Result of awarding a win — discord.Embed cannot carry custom attrs (2.7+)."""

    embed: discord.Embed
    coins: int = 0
    xp: int = 0
    rank: int | None = None
    quiet: bool = False
    # When quiet: already_won | forfeited | claim_unavailable
    quiet_reason: str | None = None
    xp_boost_used: bool = False
    xp_boost_remaining: int = 0
    krabby_snack_used: bool = False
    krabby_snack_remaining: int = 0
    golden_spatula_used: bool = False
    golden_spatula_remaining: int = 0


def win_boost_caption_kwargs(outcome: WinOutcome) -> dict:
    """Keyword args for win_reward_caption / client payloads from a WinOutcome."""
    return {
        "xp_boost_used": bool(outcome.xp_boost_used),
        "xp_boost_remaining": int(outcome.xp_boost_remaining) if outcome.xp_boost_used else None,
        "krabby_snack_used": bool(outcome.krabby_snack_used),
        "krabby_snack_remaining": (
            int(outcome.krabby_snack_remaining) if outcome.krabby_snack_used else None
        ),
        "golden_spatula_used": bool(outcome.golden_spatula_used),
        "golden_spatula_remaining": (
            int(outcome.golden_spatula_remaining) if outcome.golden_spatula_used else None
        ),
    }


def selected_cell(game: dict) -> tuple[int, int]:
    return game["sel_r"], game["sel_c"]


def board_caption(game: dict, *, status: str | None = None) -> str:
    """Legacy text caption — live boards stay silent (image + buttons only)."""
    _ = game, status
    return " "


def normalize_game_mode(mode: str | None) -> str:
    """Collapse Activity /play alias into legacy solo for forfeit paths."""
    if mode == "play":
        return "solo"
    return mode or "solo"


def build_embed(game: dict, *, status: str | None = None) -> discord.Embed:
    """Text-only fallback — live boards use standalone attachments instead."""
    _ = status
    mode = normalize_game_mode(game.get("mode"))
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
        title_id=game.get("owner_title"),
        pin_emojis=game.get("pin_emojis"),
        pin_seed=game.get("pin_seed"),
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
    elapsed = float(game_elapsed_sec(game))
    is_daily = game["mode"] == "daily"

    if not award and is_daily:
        # Duplicate claim (Discord retry / double-tap after win already saved).
        # Quiet marker — never post an "already claimed" nag.
        return WinOutcome(embed=paper_embed("Daily"), coins=0, xp=0, quiet=True)

    stats["wins"] += 1
    stats["games"] += 1
    if is_daily:
        day = game.get("daily_date") or utc_today()
        apply_daily_calendar_streak(stats, day)
    # /play and challenge use the current daily streak for bonus, but do not advance it.
    record_solve_times(stats, elapsed)

    coins = win_reward(
        int(stats.get("streak") or 0),
        daily=is_daily,
        difficulty=game.get("difficulty"),
        challenge_winner=challenge_winner,
    )
    xp = coins  # career XP mirrors sponge grant before split boosts

    snack_charges = int(stats.get("krabby_snack_charges") or 0)
    krabby_snack_used = snack_charges > 0
    if krabby_snack_used:
        coins = int(round(coins * KRABBY_SNACK_MULT))
        stats["krabby_snack_charges"] = snack_charges - 1
    krabby_snack_remaining = int(stats.get("krabby_snack_charges") or 0)

    spatula_charges = int(stats.get("golden_spatula_charges") or 0)
    golden_spatula_used = spatula_charges > 0
    if golden_spatula_used:
        xp = int(round(xp * GOLDEN_SPATULA_MULT))
        stats["golden_spatula_charges"] = spatula_charges - 1
    golden_spatula_remaining = int(stats.get("golden_spatula_charges") or 0)

    boost_charges = int(stats.get("xp_boost_charges") or 0)
    xp_boost_used = boost_charges > 0
    if xp_boost_used:
        coins *= 2
        xp *= 2
        stats["xp_boost_charges"] = boost_charges - 1
    xp_boost_remaining = int(stats.get("xp_boost_charges") or 0)

    stats["coins"] += coins
    stats["xp"] = int(stats.get("xp") or 0) + xp

    if is_daily:
        stats["daily_wins"] += 1
        daily = get_guild_daily(data, guild_id)
        daily["results"][str(user.id)] = {
            "won": True,
            "time": int(elapsed),
            "name": stats["name"],
            "coins": coins,
            "xp": xp,
        }
    elif normalize_game_mode(game.get("mode")) == "solo":
        stats["last_activity_win_at"] = time.time()

    weekly_notes = note_weekly_win(
        stats,
        is_daily=is_daily,
        challenge_winner=challenge_winner,
        day=(game.get("daily_date") if is_daily else None) or utc_today(),
    )

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

    tier = difficulty_label(game.get("difficulty"))
    badge = PINEAPPLE if is_daily else SPONGE
    label = "Daily Sudoku" if is_daily else "Sudoku"
    user_badges = evaluate_user_achievements(stats)
    badge_str = " ".join(ACHIEVEMENTS[b]["label"].split()[0] for b in user_badges if b in ACHIEVEMENTS)
    badge_line = f"\n🎖️ **Badges:** {badge_str}" if badge_str else ""
    boost_line = format_win_boost_lines(
        xp_boost_used=xp_boost_used,
        xp_boost_remaining=xp_boost_remaining if xp_boost_used else None,
        krabby_snack_used=krabby_snack_used,
        krabby_snack_remaining=krabby_snack_remaining if krabby_snack_used else None,
        golden_spatula_used=golden_spatula_used,
        golden_spatula_remaining=golden_spatula_remaining if golden_spatula_used else None,
    )

    weekly_line = ""
    if weekly_notes:
        weekly_line = "\n📅 **Weekly:** " + " · ".join(weekly_notes)

    embed = paper_embed(f"{badge} {user.mention} completed the {label}!")
    embed.description = (
        f"🏆 **Rank:** {format_rank_line(int(stats.get('xp') or 0))}\n"
        f"🎯 **{tier}** · ⏱️ **{format_time(elapsed)}** · {STAR} **Streak: {stats['streak']}**\n"
        f"🎁 **{format_xp(xp, signed=True)}** · **{format_sponges(coins, signed=True)}**"
        f"{boost_line}{badge_line}{weekly_line}"
    )
    return WinOutcome(
        embed=embed,
        coins=coins,
        xp=xp,
        rank=rank,
        quiet=False,
        xp_boost_used=xp_boost_used,
        xp_boost_remaining=xp_boost_remaining,
        krabby_snack_used=krabby_snack_used,
        krabby_snack_remaining=krabby_snack_remaining,
        golden_spatula_used=golden_spatula_used,
        golden_spatula_remaining=golden_spatula_remaining,
    )


async def finish_win_and_announce(
    bot: "SudokuBot",
    guild_id: int,
    user: discord.abc.User,
    game: dict,
) -> WinOutcome:
    """Award win; for daily, serialize finish + claim to prevent double payout."""
    if game.get("mode") != "daily":
        return finish_win(bot.data, guild_id, user, game)

    day = game.get("daily_date") or utc_today()
    elapsed = game_elapsed_sec(game)
    tier = difficulty_label(game.get("difficulty"))

    lock = _daily_finish_lock(guild_id, user.id, day)
    async with lock:
        daily_meta = get_guild_daily(bot.data, guild_id)
        prior = daily_meta.get("results", {}).get(str(user.id)) or {}

        if prior.get("won"):
            prior_coins = int(prior.get("coins") or 0)
            prior_xp = int(prior.get("xp") or prior_coins)
            quiet = finish_win(bot.data, guild_id, user, game, award=False)
            return WinOutcome(
                embed=quiet.embed,
                coins=prior_coins,
                xp=prior_xp,
                rank=quiet.rank,
                quiet=True,
                quiet_reason="already_won",
            )

        if prior.get("forfeit"):
            quiet = finish_win(bot.data, guild_id, user, game, award=False)
            return WinOutcome(
                embed=quiet.embed,
                coins=0,
                xp=0,
                rank=quiet.rank,
                quiet=True,
                quiet_reason="forfeited",
            )

        gstats = guild_stats(bot.data, guild_id)
        stats = user_stats(gstats, user.id)
        preview_streak = preview_daily_calendar_streak(stats, day)
        preview_coins = win_reward(
            preview_streak,
            daily=True,
            difficulty=game.get("difficulty"),
        )

        # Fail-closed like award_play_win: never pay if the durable claim store is down.
        claimed = False
        try:
            claimed_ok = await match_store.try_claim_daily_win(
                guild_id=guild_id,
                user_id=user.id,
                day=day,
                elapsed=elapsed,
                hints=int(game.get("hints_used") or game.get("hints") or 0),
                difficulty=tier,
                coins=preview_coins,
                player_name=getattr(user, "display_name", None) or getattr(user, "name", None),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"try_claim_daily_win failed (fail-closed): {exc}")
            raise

        if claimed_ok is False:
            try:
                already = await match_store.has_daily_claim(guild_id, user.id, day)
            except Exception:
                already = True
            if already:
                quiet = finish_win(bot.data, guild_id, user, game, award=False)
                return WinOutcome(
                    embed=quiet.embed,
                    coins=int(prior.get("coins") or preview_coins),
                    xp=int(prior.get("xp") or prior.get("coins") or preview_coins),
                    rank=quiet.rank,
                    quiet=True,
                    quiet_reason="already_won",
                )
            # Duplicate key may be a durable forfeit — never award over it.
            try:
                forfeited = await match_store.has_daily_forfeit(guild_id, user.id, day)
            except Exception as ff_exc:  # noqa: BLE001
                print(f"has_daily_forfeit after claim miss: {ff_exc}")
                forfeited = True
            if forfeited:
                quiet = finish_win(bot.data, guild_id, user, game, award=False)
                return WinOutcome(
                    embed=quiet.embed,
                    coins=0,
                    xp=0,
                    rank=quiet.rank,
                    quiet=True,
                    quiet_reason="forfeited",
                )
            # Claim miss without claim/forfeit doc — refuse local award (fail-closed).
            print(
                f"try_claim_daily_win returned False without claim/forfeit "
                f"guild={guild_id} user={user.id} day={day}; refusing award"
            )
            quiet = finish_win(bot.data, guild_id, user, game, award=False)
            return WinOutcome(
                embed=quiet.embed,
                coins=0,
                xp=0,
                rank=quiet.rank,
                quiet=True,
                quiet_reason="claim_unavailable",
            )

        claimed = True
        try:
            return finish_win(bot.data, guild_id, user, game)
        except Exception:
            if claimed:
                try:
                    await match_store.release_daily_win(guild_id, user.id, day)
                except Exception as rel_exc:  # noqa: BLE001
                    print(f"release_daily_win failed: {rel_exc}")
            raise


async def record_daily_forfeit_mongo(guild_id: int, user_id: int, day: str) -> None:
    try:
        await match_store.try_record_daily_forfeit(
            guild_id=guild_id, user_id=user_id, day=day
        )
    except Exception as exc:  # noqa: BLE001
        print(f"record_daily_forfeit_mongo failed: {exc}")


async def finish_forfeit(
    data: dict, guild_id: int, user: discord.abc.User, game: dict
) -> discord.Embed:
    """Record a quit/forfeit. Daily path uses the same lock as wins to avoid races."""
    gstats = guild_stats(data, guild_id)
    stats = user_stats(gstats, user.id)
    stats["name"] = getattr(user, "display_name", user.name)
    mode = normalize_game_mode(game.get("mode"))

    if mode == "daily":
        day = game.get("daily_date") or utc_today()
        lock = _daily_finish_lock(guild_id, user.id, day)
        async with lock:
            daily = get_guild_daily(data, guild_id)
            prior = daily.get("results", {}).get(str(user.id)) or {}
            if prior.get("won"):
                return paper_embed(
                    f"{WAVE} Quit",
                    description="Today's daily is already cleared — streak unchanged.",
                )
            if prior.get("forfeit"):
                return paper_embed(
                    f"{WAVE} Quit",
                    description="Today's daily was already forfeited.",
                )
            try:
                if await match_store.has_daily_claim(guild_id, user.id, day):
                    daily["results"][str(user.id)] = {
                        "won": True,
                        "name": stats["name"],
                    }
                    save_data(data)
                    return paper_embed(
                        f"{WAVE} Quit",
                        description="Today's daily is already cleared — streak unchanged.",
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"finish_forfeit has_daily_claim failed: {exc}")
                return paper_embed(
                    f"{WAVE} Quit",
                    description="Couldn't verify today's daily status — try again in a moment.",
                )

            try:
                claimed = await match_store.try_record_daily_forfeit(
                    guild_id=guild_id, user_id=user.id, day=day
                )
            except Exception as exc:  # noqa: BLE001
                print(f"finish_forfeit try_record_daily_forfeit failed: {exc}")
                return paper_embed(
                    f"{WAVE} Quit",
                    description="Couldn't lock today's forfeit — try again in a moment.",
                )
            if not claimed:
                try:
                    if await match_store.has_daily_claim(guild_id, user.id, day):
                        daily["results"][str(user.id)] = {
                            "won": True,
                            "name": stats["name"],
                        }
                        save_data(data)
                        return paper_embed(
                            f"{WAVE} Quit",
                            description="Today's daily is already cleared — streak unchanged.",
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"finish_forfeit reconcile claim failed: {exc}")
                daily["results"][str(user.id)] = {
                    "won": False,
                    "forfeit": True,
                    "name": stats["name"],
                }
                save_data(data)
                return paper_embed(
                    f"{WAVE} Quit",
                    description="Today's daily was already forfeited.",
                )

            stats["losses"] += 1
            stats["games"] += 1
            stats["streak"] = 0
            stats["last_streak_day"] = None
            daily["results"][str(user.id)] = {
                "won": False,
                "forfeit": True,
                "name": stats["name"],
            }
            save_data(data)
        return paper_embed(
            f"{WAVE} Quit",
            description="Streak wiped. Daily attempt locked for today — see you at the Krusty Krab!",
        )

    stats["losses"] += 1
    stats["games"] += 1
    save_data(data)
    return paper_embed(
        f"{WAVE} Quit",
        description="Quit — daily streak unchanged.",
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
        {
            "current_board": copy_grid(game["board"]),
            "last_move_at": time.time(),
            "hints_used": int(game.get("hints_used") or 0),
            "hints_gary_used": int(game.get("hints_gary_used") or 0),
            "gary_wisdom_bonus": int(game.get("gary_wisdom_bonus") or 0),
        },
    )
    key = challenge_game_key(match_id, game["owner_id"])
    await persist_game(key, game)
    schedule_challenge_live_update(match_id)


def challenge_home_channel(
    channel: discord.abc.Messageable | None,
) -> discord.TextChannel | None:
    """Text channel where challenge threads should be created (parent if inside a thread)."""
    if isinstance(channel, discord.TextChannel):
        return channel
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        if isinstance(parent, discord.TextChannel):
            return parent
    return None


def as_challenge_text_channel(
    channel: discord.abc.Messageable | None,
) -> discord.TextChannel | None:
    """Prefer a TextChannel for challenge announce/cleanup (unwrap threads)."""
    return challenge_home_channel(channel)


async def resolve_channel(
    bot: "SudokuBot",
    channel_id: int | None,
) -> discord.abc.Messageable | None:
    """Cache lookup, then API fetch — private threads often miss cache after restart."""
    if not channel_id:
        return None
    try:
        channel = bot.get_channel(int(channel_id))
    except (TypeError, ValueError):
        return None
    if channel is not None:
        return channel
    # fetch_channel needs a live HTTP session — skip during reconnect/shutdown.
    if getattr(bot, "is_closed", lambda: False)():
        return None
    http = getattr(bot, "http", None)
    global_over = getattr(http, "_global_over", None) if http is not None else None
    if global_over is None or not hasattr(global_over, "is_set"):
        return None
    try:
        return await bot.fetch_channel(int(channel_id))
    except (discord.HTTPException, discord.NotFound, discord.Forbidden, AttributeError, TypeError):
        return None


async def open_private_match_channel(
    channel: discord.TextChannel,
    user: discord.abc.User,
    title: str,
) -> discord.abc.Messageable:
    """Private board destination: private thread → public thread → DM."""
    name = title[:100]
    try:
        thread = await channel.create_thread(
            name=name,
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=60,
        )
        try:
            await thread.add_user(user)
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"add_user to private thread failed for {getattr(user, 'id', user)}: {exc}")
        return thread
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"private thread failed for {getattr(user, 'id', user)}: {exc}")

    try:
        thread = await channel.create_thread(
            name=name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60,
        )
        return thread
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"public thread failed for {getattr(user, 'id', user)}: {exc}")

    try:
        return await user.create_dm()
    except (discord.Forbidden, discord.HTTPException) as exc:
        raise RuntimeError(
            f"Can't open a private board for <@{getattr(user, 'id', 0)}> — "
            "need **Create Private/Public Threads** here, or open DMs from server members."
        ) from exc


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


async def abort_challenge_launch(match_id: str, player_ids: list[int]) -> None:
    """Clear partial sessions if challenge start fails mid-way."""
    for uid in player_ids:
        await remove_game(challenge_game_key(match_id, uid))
    match: dict | None = None
    try:
        match = await match_store.get_match(str(match_id))
    except Exception as exc:  # noqa: BLE001
        print(f"abort_challenge_launch get_match failed: {exc}")
    try:
        await match_store.update_match(
            match_id,
            {
                "status": "finished",
                "settle_reason": "launch failed",
                "winner_id": None,
                "winner_name": None,
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"abort_challenge_launch update failed: {exc}")
    if match:
        channel = as_challenge_text_channel(
            await resolve_channel(bot, match.get("channel_id"))
        )
        if channel is not None:
            await cleanup_challenge_channel_messages(
                bot,
                channel,
                launch_message_id=match.get("launch_message_id"),
                live_message_id=match.get("live_message_id"),
            )
    # Cancel any pending live refresh for this match.
    existing = _challenge_live_tasks.pop(str(match_id), None)
    if existing and not existing.done():
        existing.cancel()


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
    settle_stale_after_sec: float = 300.0,
) -> None:
    """Compare finish times / forfeits and announce the winner in the origin channel."""
    match_id = match.get("_id")
    if not match_id:
        return

    # Claim settlement + award under lock; Discord I/O happens after release.
    announce_payload: dict | None = None
    async with _challenge_match_lock(str(match_id)):
        try:
            fresh = await match_store.get_match(str(match_id))
        except Exception as exc:  # noqa: BLE001
            print(f"settle_challenge_match get_match failed: {exc}")
            fresh = None
        if not fresh:
            return
        if fresh.get("rewards_applied") or fresh.get("status") == "finished":
            # Still scrub ghost boards so /play is not blocked after a settled race.
            await purge_challenge_games_for_match(str(match_id), fresh)
            return
        if not challenge_ready_to_settle(fresh):
            return

        try:
            claimed = await match_store.try_begin_match_settlement(
                str(match_id),
                stale_after_sec=settle_stale_after_sec,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"settle_challenge_match try_begin failed: {exc}")
            claimed = None
        if not claimed:
            try:
                again = await match_store.get_match(str(match_id))
            except Exception as exc:  # noqa: BLE001
                print(f"settle_challenge_match reget after claim miss failed: {exc}")
                again = None
            if again and (
                again.get("rewards_applied") or again.get("status") == "finished"
            ):
                await purge_challenge_games_for_match(str(match_id), again)
            return
        match = claimed

        entries = match_player_entries(match)
        guild_id = match["guild_id"]
        start = float(match["start_time"])

        standing = [p for _, p in entries if not p.get("forfeit")]
        finished = [p for p in standing if p.get("finished_time") is not None]
        winner_id: int | None = None
        detail = reason
        tied_user_ids: set[int] = set()

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
                tied_user_ids = {int(p["user_id"]) for p in tied}
            else:
                winner_id = ranked[0]["user_id"]
                detail = "fastest finish"

        winner_name = None
        if winner_id is not None:
            for _, p in entries:
                if p.get("user_id") == winner_id:
                    winner_name = p.get("name")
                    break
        elif detail == "dead heat" and tied_user_ids:
            names = [
                str(p.get("name") or p.get("user_id"))
                for _, p in entries
                if int(p.get("user_id") or 0) in tied_user_ids
            ]
            winner_name = " & ".join(names) if names else "dead heat"
        # Award after durable settle claim; rewards_applied seals the ledger.
        await purge_challenge_games_for_match(str(match["_id"]), match)

        guild = bot.get_guild(guild_id)
        reward_notes: list[str] = []

        async def _award_solved_winner(wid: int) -> None:
            winner_elapsed = 0.0
            winner_solved = False
            for _, p in entries:
                if int(p.get("user_id") or 0) != int(wid):
                    continue
                if p.get("elapsed") is not None:
                    winner_elapsed = float(p["elapsed"])
                    winner_solved = True
                elif p.get("finished_time") is not None:
                    winner_elapsed = max(0.0, float(p["finished_time"]) - start)
                    winner_solved = True
                break
            if not winner_solved:
                return
            winner_user = guild.get_member(wid) if guild else None
            if winner_user is None:
                try:
                    winner_user = await bot.fetch_user(wid)
                except discord.HTTPException:
                    winner_user = None
            if winner_user is None:
                pname = None
                for _, p in entries:
                    if int(p.get("user_id") or 0) == int(wid):
                        pname = p.get("name") or p.get("username")
                        break
                print(
                    f"settle_challenge_match: fetch_user failed for winner {wid}; "
                    "awarding via stub user"
                )
                winner_user = _stub_discord_user(wid, pname)
            wname = getattr(winner_user, "display_name", None) or winner_user.name
            gstats_w = guild_stats(bot.data, guild_id)
            wstats = user_stats(gstats_w, wid)
            wstats["name"] = wname or wstats.get("name") or "Unknown"
            winner_game = {
                "mode": "challenge",
                "started_at": time.time() - winner_elapsed,
                "elapsed": int(winner_elapsed),
                "difficulty": match.get("difficulty"),
                "hints_used": 0,
            }
            outcome = finish_win(
                bot.data,
                guild_id,
                winner_user,
                winner_game,
                challenge_winner=True,
            )
            reward_notes.append(
                f"{format_sponges(int(outcome.coins), signed=True)} · "
                f"+{int(outcome.xp)} XP → <@{wid}>"
            )
            wstats["challenge_wins"] = int(wstats.get("challenge_wins", 0) or 0) + 1

        if detail == "dead heat" and tied_user_ids:
            for wid in sorted(tied_user_ids):
                await _award_solved_winner(wid)
            save_data(bot.data)
        elif winner_id is not None:
            winner_elapsed = 0.0
            winner_solved = False
            for _, p in entries:
                if p.get("user_id") == winner_id:
                    if p.get("elapsed") is not None:
                        winner_elapsed = float(p["elapsed"])
                        winner_solved = True
                    elif p.get("finished_time") is not None:
                        winner_elapsed = max(0.0, float(p["finished_time"]) - start)
                        winner_solved = True
                    break

            winner_user = guild.get_member(winner_id) if guild else None
            if winner_user is None:
                try:
                    winner_user = await bot.fetch_user(winner_id)
                except discord.HTTPException:
                    winner_user = None
            if winner_user is None:
                pname = None
                for _, p in entries:
                    if int(p.get("user_id") or 0) == int(winner_id):
                        pname = p.get("name") or p.get("username")
                        break
                print(
                    f"settle_challenge_match: fetch_user failed for winner {winner_id}; "
                    "awarding via stub user"
                )
                winner_user = _stub_discord_user(winner_id, pname)

            wname = getattr(winner_user, "display_name", None) or winner_user.name
            if wname and wname != winner_name:
                winner_name = wname
                await match_store.update_match(match["_id"], {"winner_name": wname})
            gstats_w = guild_stats(bot.data, guild_id)
            wstats = user_stats(gstats_w, winner_id)
            wstats["name"] = wname or wstats.get("name") or "Unknown"
            if winner_solved:
                # Real solve — full challenge win payout + best_time.
                winner_game = {
                    "mode": "challenge",
                    "started_at": time.time() - winner_elapsed,
                    "elapsed": int(winner_elapsed),
                    "difficulty": match.get("difficulty"),
                    "hints_used": 0,
                }
                outcome = finish_win(
                    bot.data,
                    guild_id,
                    winner_user,
                    winner_game,
                    challenge_winner=True,
                )
                reward_notes.append(
                    f"{format_sponges(int(outcome.coins), signed=True)} · "
                    f"+{int(outcome.xp)} XP → <@{winner_id}>"
                )
            else:
                # Last standing after opponents forfeit — no board solve.
                # Sponges + challenge_wins only; never best_time / full XP win.
                coins = CHALLENGE_FORFEIT_WIN_COINS
                wstats["coins"] = int(wstats.get("coins") or 0) + coins
                reward_notes.append(
                    f"Last standing — {format_sponges(coins, signed=True)} "
                    f"(no solve time) → <@{winner_id}>"
                )
            wstats["challenge_wins"] = int(wstats.get("challenge_wins", 0) or 0) + 1
            save_data(bot.data)

        gstats = guild_stats(bot.data, guild_id)
        for _slot, player in entries:
            uid = int(player["user_id"])
            if winner_id is not None and uid == int(winner_id):
                continue
            if uid in tied_user_ids:
                continue
            if player.get("forfeit"):
                continue
            loser_stats = user_stats(gstats, uid)
            loser_stats["losses"] += 1
            loser_stats["games"] += 1
            # Challenge loss does not wipe the calendar daily streak.
            loser_stats["coins"] += CHALLENGE_LOSER_COINS
        save_data(bot.data)

        await match_store.update_match(
            match["_id"],
            {
                "status": "finished",
                "winner_id": winner_id,
                "winner_name": winner_name,
                "settle_reason": detail,
                "rewards_applied": True,
            },
        )

        announce_payload = {
            "match": match,
            "entries": entries,
            "guild_id": guild_id,
            "start": start,
            "winner_id": winner_id,
            "tied_user_ids": tied_user_ids,
            "detail": detail,
            "reward_notes": reward_notes,
            "channel_id": match.get("channel_id"),
            "launch_message_id": match.get("launch_message_id"),
            "live_message_id": match.get("live_message_id"),
        }

    # Don't refresh the live panel after settle — it will be deleted below.
    if announce_payload is None:
        return

    # Cancel pending live edits so they cannot race with cleanup deletes.
    pending = _challenge_live_tasks.pop(str(match_id), None)
    if pending and not pending.done():
        pending.cancel()

    channel = as_challenge_text_channel(
        await resolve_channel(bot, announce_payload["channel_id"])
    )
    launch_id = announce_payload.get("launch_message_id")
    live_id = announce_payload.get("live_message_id")
    if channel is None:
        print(
            f"settle_challenge_match: origin channel missing for match {match_id} "
            f"(result/cleanup skipped; launch={launch_id} live={live_id})"
        )
        return

    guild = bot.get_guild(announce_payload["guild_id"])
    winner_id = announce_payload["winner_id"]
    tied_user_ids = set(announce_payload.get("tied_user_ids") or set())
    detail = announce_payload["detail"]
    entries = announce_payload["entries"]
    start = announce_payload["start"]
    match = announce_payload["match"]
    reward_notes = list(announce_payload.get("reward_notes") or [])

    def mention(uid: int | None) -> str:
        if uid is None:
            return "—"
        member = guild.get_member(uid) if guild else None
        return member.mention if member else f"<@{uid}>"

    try:
        if winner_id is None and detail != "dead heat":
            embed = paper_embed("Challenge ended", description=f"No winner ({detail}).")
            await channel.send(embed=embed)
            return

        # Ranked board: finishers by time, then forfeits / last standing
        ranked_lines: list[str] = []
        finishers = sorted(
            (
                p
                for _, p in entries
                if p.get("finished_time") is not None and not p.get("forfeit")
            ),
            key=lambda p: float(p["finished_time"]),
        )
        if detail == "dead heat" and tied_user_ids:
            tied_mentions = ", ".join(mention(uid) for uid in sorted(tied_user_ids))
            ranked_lines.append(f"🏆 Dead heat — shared win: {tied_mentions}")
        elif winner_id is not None and not any(
            p["user_id"] == winner_id for p in finishers
        ):
            ranked_lines.append(f"🏆 {mention(winner_id)} — last standing ({detail})")
        for i, p in enumerate(finishers, start=1):
            et = _elapsed_of(p, start)
            uid = int(p["user_id"])
            is_champ = (
                (winner_id is not None and uid == int(winner_id))
                or uid in tied_user_ids
            )
            medal = "🏆 " if is_champ else f"{i}. "
            time_bit = f" — **{format_time(et)}**" if et is not None else ""
            ranked_lines.append(f"{medal}{mention(uid)}{time_bit}")
        for _slot, p in entries:
            if p.get("forfeit"):
                ranked_lines.append(f"✗ {mention(p['user_id'])} — quit")
        consolation_finishers = [
            p
            for _, p in entries
            if not p.get("forfeit")
            and p.get("finished_time") is not None
            and int(p["user_id"]) != (int(winner_id) if winner_id is not None else -1)
            and int(p["user_id"]) not in tied_user_ids
        ]
        if consolation_finishers:
            ranked_lines.append(
                f"Other finishers: {format_sponges(CHALLENGE_LOSER_COINS, signed=True)} consolation each"
            )
        if detail in ("fastest finish", "dead heat"):
            ranked_lines.append(
                f"Difficulty: **{difficulty_label(match.get('difficulty'))}** · winner ×{CHALLENGE_WIN_MULT:g}"
            )
        else:
            ranked_lines.append(
                f"Difficulty: **{difficulty_label(match.get('difficulty'))}**"
            )
        for note in reward_notes:
            ranked_lines.append(f"🎁 {note}")
        ranked_lines.append(
            f"{STAR} Daily streak is **never** reset by challenge results."
        )

        announce = paper_embed("Challenge result", description="\n".join(ranked_lines))
        await channel.send(embed=announce)
    except Exception as exc:  # noqa: BLE001
        print(f"settle_challenge_match announce failed for {match_id}: {exc}")
    finally:
        await cleanup_challenge_channel_messages(
            bot,
            channel,
            launch_message_id=launch_id,
            live_message_id=live_id,
        )


async def cleanup_challenge_channel_messages(
    bot_ref: "SudokuBot",
    channel: discord.abc.Messageable,
    *,
    launch_message_id: int | str | None,
    live_message_id: int | str | None,
) -> None:
    """Delete the Play launch + live progress messages after the match ends."""
    for label, raw_id in (
        ("launch", launch_message_id),
        ("live", live_message_id),
    ):
        if not raw_id:
            continue
        try:
            msg_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        except (discord.HTTPException, AttributeError) as exc:
            print(f"cleanup challenge {label} message {raw_id} failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup challenge {label} message {raw_id} error: {exc}")


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
    match: dict | None = None
    player: dict | None = None
    already = False

    async with _challenge_match_lock(str(match_id)):
        try:
            current = await match_store.get_match(str(match_id))
        except Exception as exc:  # noqa: BLE001
            print(f"handle_challenge_completion get_match failed: {exc}")
            current = None
        if not current:
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

        if current.get("status") == "finished":
            await interaction.edit_original_response(
                content=None,
                embed=paper_embed("Challenge already settled"),
                view=None,
                attachments=[],
            )
            game.pop("finishing", None)
            game.pop("_digit_lock", None)
            view.stop()
            await remove_game(view.game_key)
            return

        player = current.get(slot) if isinstance(current.get(slot), dict) else None
        if player and player.get("forfeit"):
            match = current
            already = True
        elif player and player.get("finished_time") is not None:
            # Idempotent: already recorded — don't overwrite finish time.
            match = current
            already = True
        else:
            already = False
            match = await match_store.try_claim_player_finish(
                match_id,
                slot,
                {
                    "current_board": copy_grid(game["board"]),
                    "finished_time": finished_at,
                    "elapsed": elapsed,
                },
            )
            if match is None:
                # Lost the race (or match finished) — re-read for settle / already state.
                try:
                    match = await match_store.get_match(str(match_id))
                except Exception as exc:  # noqa: BLE001
                    print(f"handle_challenge_completion reget failed: {exc}")
                    match = None
                already = True

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
        title_id=game.get("owner_title"),
        pin_emojis=game.get("pin_emojis"),
        pin_seed=game.get("pin_seed"),
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
    shown_elapsed = float(player.get("elapsed") or elapsed) if already and player else elapsed
    who = (player or {}).get("name") or interaction.user.display_name
    caption = (
        f"✅ **{who}** finished · {interaction.user.mention} · "
        f"**{format_time(shown_elapsed)}**\n{wait_msg}"
    )
    file = board_to_file(image)
    await interaction.edit_original_response(
        content=caption,
        embed=None,
        view=None,
        attachments=[file],
    )
    view.stop()
    await remove_game(view.game_key)

    schedule_challenge_live_update(match_id, immediate=True)

    if challenge_ready_to_settle(match):
        await settle_challenge_match(bot, match, reason="all finished")


async def handle_challenge_completion_activity(
    bot: "SudokuBot",
    user_id: int,
    game: dict,
    elapsed: int,
) -> dict:
    """Record finish time from Activity; settle challenge when everyone is done.

    Returns a status dict for the HTTP layer (ok / already / errors).
    """
    finished_at = time.time()
    match_id = game["match_id"]
    slot = game["player_slot"]
    already = False

    async with _challenge_match_lock(str(match_id)):
        try:
            current = await match_store.get_match(str(match_id))
        except Exception as exc:  # noqa: BLE001
            print(f"handle_challenge_completion_activity get_match failed: {exc}")
            current = None
        if not current:
            key = challenge_game_key(match_id, user_id)
            await remove_game(key)
            return {"ok": False, "error": "match_missing"}
        if current.get("status") == "finished":
            key = challenge_game_key(match_id, user_id)
            await remove_game(key)
            return {"ok": False, "error": "already_settled"}

        player = current.get(slot) if isinstance(current.get(slot), dict) else None
        if not player:
            key = challenge_game_key(match_id, user_id)
            await remove_game(key)
            return {"ok": False, "error": "match_missing"}
        if player.get("forfeit"):
            key = challenge_game_key(match_id, user_id)
            await remove_game(key)
            return {"ok": False, "error": "forfeited"}
        if player.get("finished_time") is not None:
            match = current
            already = True
        else:
            match = await match_store.try_claim_player_finish(
                match_id,
                slot,
                {
                    "current_board": copy_grid(game["board"]),
                    "finished_time": finished_at,
                    "elapsed": elapsed,
                },
            )
            if match is None:
                try:
                    match = await match_store.get_match(str(match_id))
                except Exception as exc:  # noqa: BLE001
                    print(f"handle_challenge_completion_activity reget failed: {exc}")
                    match = None
                already = True

    key = challenge_game_key(match_id, user_id)
    await remove_game(key)

    if not match:
        return {"ok": False, "error": "match_missing"}

    if already:
        # Don't re-post completion spam; still try settle in case peers finished.
        if challenge_ready_to_settle(match):
            await settle_challenge_match(bot, match, reason="all finished")
        return {"ok": True, "challenge": True, "already": True}

    # Post this player's finished board once (named), then refresh the live board.
    channel_id = match.get("channel_id") or game.get("channel_id")
    if channel_id:
        try:
            channel = await resolve_channel(bot, int(channel_id))
            if channel is not None:
                player = None
                for _, p in match_player_entries(match):
                    if int(p.get("user_id") or 0) == int(user_id):
                        player = p
                        break
                player_name = (
                    (player or {}).get("name")
                    or game.get("owner_name")
                    or f"Player {user_id}"
                )
                image = render_board(
                    game["board"],
                    game["given"],
                    solution=game["solution"],
                    conflicts=set(),
                    difficulty=game.get("difficulty"),
                    title_id=game.get("owner_title"),
                    pin_emojis=game.get("pin_emojis"),
                    pin_seed=game.get("pin_seed") or user_id,
                )
                file = board_to_file(image)
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
                caption = (
                    f"✅ **{player_name}** finished · "
                    f"<@{user_id}> · **{format_time(elapsed)}**\n"
                    f"{wait_msg}"
                )
                await channel.send(content=caption, file=file)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to post challenge finish board for {user_id}: {exc}")

    schedule_challenge_live_update(match_id, immediate=True)

    if challenge_ready_to_settle(match):
        await settle_challenge_match(bot, match, reason="all finished")

    return {"ok": True, "challenge": True, "elapsed": elapsed}


async def forfeit_challenge_player(
    bot: "SudokuBot",
    match_id: str,
    slot: str,
    *,
    game_key: tuple | None = None,
    reason: str = "quit",
) -> dict | None:
    """Mark a challenge player as forfeited without clobbering an existing finish."""
    match: dict | None = None
    async with _challenge_match_lock(str(match_id)):
        try:
            current = await match_store.get_match(str(match_id))
        except Exception as exc:  # noqa: BLE001
            print(f"forfeit_challenge_player get_match failed: {exc}")
            current = None
        if not current or current.get("status") == "finished":
            return None
        player = current.get(slot) if isinstance(current.get(slot), dict) else None
        if not player or player.get("forfeit") or player.get("finished_time") is not None:
            return current
        match = await match_store.try_claim_player_forfeit(str(match_id), slot)
        if match is None:
            try:
                match = await match_store.get_match(str(match_id))
            except Exception as exc:  # noqa: BLE001
                print(f"forfeit_challenge_player reget failed: {exc}")
                match = current

    if game_key is not None:
        await remove_game(game_key)
    if match:
        schedule_challenge_live_update(str(match_id), immediate=True)
        if challenge_ready_to_settle(match):
            await settle_challenge_match(bot, match, reason=reason)
    return match


async def forfeit_challenge_activity(bot: "SudokuBot", user_id: int) -> bool:
    """Forfeit when a challenge player closes the Activity (no /quit)."""
    ch_key = find_challenge_game_for_user(user_id)
    if ch_key is not None:
        game = games.get(ch_key)
        if game and game.get("mode") == "challenge":
            match_id = game.get("match_id")
            slot = game.get("player_slot")
            if not match_id or not slot:
                await remove_game(ch_key)
                return False
            await forfeit_challenge_player(
                bot,
                str(match_id),
                slot,
                game_key=ch_key,
                reason="quit",
            )
            return True

    try:
        active = await match_store.list_matches(status="active")
    except Exception as exc:  # noqa: BLE001
        print(f"forfeit_challenge_activity list_matches failed: {exc}")
        return False
    for match in active:
        mid = match.get("_id")
        if not mid:
            continue
        for slot, player in match_player_entries(match):
            if int(player.get("user_id") or 0) != int(user_id):
                continue
            if player.get("forfeit") or player.get("finished_time") is not None:
                continue
            await forfeit_challenge_player(
                bot,
                str(mid),
                slot,
                game_key=None,
                reason="quit",
            )
            return True
    return False


async def launch_challenge_match(
    *,
    interaction: discord.Interaction,
    players: list[discord.Member],
    difficulty: str,
) -> bool:
    """Start a challenge. Caller must already have deferred the interaction.

    Returns True on success. On failure, sends an ephemeral followup when possible.
    """
    match_id: str | None = None
    player_ids: list[int] = []
    try:
        if interaction.guild is None:
            await interaction.followup.send(
                "Use this in a server text channel.",
                ephemeral=True,
            )
            return False
        home = challenge_home_channel(interaction.channel)
        if home is None:
            await interaction.followup.send(
                "Use this in a server text channel (or its thread).",
                ephemeral=True,
            )
            return False
        if len(players) < 2:
            await interaction.followup.send("Need at least 2 players to start.", ephemeral=True)
            return False

        board, given, solution = make_puzzle(difficulty)
        tier = difficulty_label(difficulty)
        player_ids = [m.id for m in players]
        player_names = [m.display_name for m in players]
        doc = new_match_document(
            guild_id=interaction.guild.id,
            channel_id=home.id,
            player_ids=player_ids,
            board=board,
            given=given,
            solution=solution,
            difficulty=difficulty_key_from_label(difficulty),
            player_names=player_names,
        )
        match_id = await match_store.insert_match(doc)
        match = await match_store.get_match(match_id)
        if match is None:
            await interaction.followup.send(
                "Could not create challenge match in database.",
                ephemeral=True,
            )
            return False
        start_time = float(match["start_time"])
        slots = match["player_slots"]

        names = " · ".join(m.display_name for m in players)
        roster = ", ".join(m.mention for m in players)

        # One shared Play button in the text channel (Discord blocks Activities in threads).
        # No per-player private threads / Play spam.
        try:
            launch_msg = await home.send(
                f"🏁 Speedrun · **{tier}** ({len(players)} players)\n"
                f"{roster}\n"
                f"Field: {names}\n"
                f"Same puzzle — fastest wins. Tap **Play in Activity** below!",
                view=ChallengeLaunchActivityView(),
            )
        except discord.HTTPException as exc:
            raise RuntimeError(
                f"Couldn't post Play button in {home.mention} ({exc})."
            ) from exc

        launch_message_id = launch_msg.id
        try:
            await match_store.update_match(
                match_id, {"launch_message_id": launch_message_id}
            )
        except Exception as exc:  # noqa: BLE001
            print(f"challenge launch_message_id save failed: {exc}")

        for slot, member in zip(slots, players):
            key = challenge_game_key(match_id, member.id)
            player_board = copy_grid(board)
            pstats = user_stats(guild_stats(bot.data, interaction.guild.id), member.id)
            games[key] = new_game_state(
                mode="challenge",
                board=player_board,
                given=given,
                solution=solution,
                owner_id=member.id,
                owner_name=member.display_name,
                owner_title=equipped_title_id(pstats),
                channel_id=home.id,
                guild_id=interaction.guild.id,
                match_id=match_id,
                player_slot=slot,
                difficulty=difficulty,
                started_at=start_time,
                pin_emojis=owned_pin_emojis(pstats),
            )
            attach_gary_wisdom_to_game(pstats, games[key])
            games[key]["message_id"] = launch_message_id
            await match_store.update_player(
                match_id, slot, {"name": member.display_name}
            )
            await persist_game(key, games[key])

        save_data(bot.data)
        await interaction.followup.send(
            f"Challenge started · **{tier}** — one Play button in {home.mention}.",
            ephemeral=True,
        )
        fresh = await match_store.get_match(match_id)
        if fresh is not None:
            await post_challenge_live_panel(bot, home, fresh, interaction.guild)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"launch_challenge_match failed: {exc}")
        if match_id is not None:
            await abort_challenge_launch(match_id, player_ids)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Couldn't start the challenge ({exc}). Try again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"Couldn't start the challenge ({exc}). Try again.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass
        return False


def challenge_cooldown_remaining(user_id: int) -> int:
    last = challenge_cooldowns.get(user_id)
    if last is None:
        return 0
    left = int(CHALLENGE_COOLDOWN_SEC - (time.time() - last))
    return max(0, left)


def mark_challenge_cooldown(user_id: int) -> None:
    challenge_cooldowns[user_id] = time.time()


def challenge_board_filled(board_raw: list) -> int:
    board = normalize_board(board_raw or [])
    return sum(1 for r in range(9) for c in range(9) if cell_value(board, r, c) > 0)


def challenge_active_player(match: dict) -> tuple[str, dict] | None:
    """Unfinished player who moved most recently (within WATCH_ACTIVE_SEC)."""
    now = time.time()
    best_slot: str | None = None
    best_player: dict | None = None
    best_ts = 0.0
    for slot, player in match_player_entries(match):
        if player.get("forfeit") or player.get("finished_time"):
            continue
        ts = float(player.get("last_move_at") or 0)
        if ts > best_ts:
            best_ts = ts
            best_slot = slot
            best_player = player
    if best_player is None or now - best_ts > WATCH_ACTIVE_SEC:
        return None
    return best_slot, best_player


def challenge_player_mention(guild: discord.Guild | None, player: dict) -> str:
    uid = player.get("user_id")
    if uid is None:
        return str(player.get("name") or "Unknown")
    if guild is not None:
        member = guild.get_member(int(uid))
        if member is not None:
            return member.mention
    return f"<@{uid}>"


def challenge_standings_lines(
    match: dict,
    guild: discord.Guild | None,
    *,
    player_sessions: dict[str, dict] | None = None,
) -> list[str]:
    start = float(match.get("start_time") or time.time())
    active = challenge_active_player(match)
    active_uid = active[1].get("user_id") if active else None
    lines: list[str] = []
    for _slot, player in match_player_entries(match):
        mention = challenge_player_mention(guild, player)
        if player.get("forfeit"):
            lines.append(f"✗ {mention} — quit")
            continue
        if player.get("finished_time") is not None:
            # Always derive display time from server wall clock (matches settlement ranking).
            elapsed = max(0.0, float(player["finished_time"]) - start)
            lines.append(f"✅ {mention} — **{format_time(elapsed)}**")
            continue
        filled = challenge_board_filled(player.get("current_board") or [])
        elapsed = time.time() - start
        marker = "🎮 " if player.get("user_id") == active_uid else "▶ "
        watcher_suffix = ""
        if player_sessions and player.get("user_id") is not None:
            watcher_suffix = format_activity_watchers_suffix(
                player_sessions.get(str(player["user_id"])),
                guild,
            )
        lines.append(
            f"{marker}{mention} — {filled}/{CHALLENGE_BOARD_CELLS} · "
            f"{format_time(elapsed)}{watcher_suffix}"
        )
    return lines


def build_challenge_live_embed(
    match: dict,
    guild: discord.Guild | None,
    *,
    player_sessions: dict[str, dict] | None = None,
) -> discord.Embed:
    tier = difficulty_label(match.get("difficulty"))
    if match.get("status") == "finished":
        title = "Challenge ended"
        footer = "Race over."
    else:
        title = f"Live challenge — {tier}"
        footer = "Fastest clean solve wins · /watch to spectate"
    embed = paper_embed(title)
    embed.description = (
        "\n".join(challenge_standings_lines(match, guild, player_sessions=player_sessions))
        or "No players."
    )
    active = challenge_active_player(match)
    if active and match.get("status") != "finished":
        _slot, player = active
        embed.add_field(
            name="Now playing",
            value=f"{challenge_player_mention(guild, player)} is playing the **challenge race**.",
            inline=False,
        )
    embed.set_footer(text=footer)
    return embed


def build_challenge_watch_view(match: dict, bot_ref: "SudokuBot") -> "ChallengeWatchView":
    view = ChallengeWatchView(match["_id"], bot_ref)
    if match.get("status") != "finished":
        view.rebuild_player_buttons(match)
    else:
        for child in view.children:
            child.disabled = True  # type: ignore[attr-defined]
    return view


async def update_challenge_live_message(bot_ref: "SudokuBot", match_id: str) -> None:
    try:
        match = await match_store.get_match(match_id)
        if not match or not match.get("live_message_id"):
            return
        channel = as_challenge_text_channel(
            await resolve_channel(bot_ref, match.get("channel_id"))
        )
        if channel is None:
            return
        # Finished matches delete launch/live in settle — never edit a stale panel.
        if match.get("status") == "finished":
            await cleanup_challenge_channel_messages(
                bot_ref,
                channel,
                launch_message_id=None,
                live_message_id=match.get("live_message_id"),
            )
            return
        guild = bot_ref.get_guild(int(match.get("guild_id") or 0))
        try:
            msg = await channel.fetch_message(int(match["live_message_id"]))
        except (discord.HTTPException, discord.NotFound, AttributeError):
            return
        player_sessions = await activity_sessions_for_challenge(match)
        embed = build_challenge_live_embed(match, guild, player_sessions=player_sessions)
        view = build_challenge_watch_view(match, bot_ref)
        try:
            await msg.edit(embed=embed, view=view)
            view.message = msg
            bot_ref.add_view(view)
        except (discord.HTTPException, AttributeError) as exc:
            print(f"update_challenge_live_message failed for {match_id}: {exc}")
    except Exception as exc:  # noqa: BLE001
        # Background task — never leave "Task exception was never retrieved".
        print(f"update_challenge_live_message error for {match_id}: {exc}")


def schedule_challenge_live_update(match_id: str, *, immediate: bool = False) -> None:
    existing = _challenge_live_tasks.get(match_id)
    if existing and not existing.done():
        existing.cancel()
    if immediate:
        asyncio.create_task(
            update_challenge_live_message(bot, match_id),
            name=f"challenge-live-{match_id}",
        )
        return

    async def _debounced() -> None:
        try:
            await asyncio.sleep(CHALLENGE_LIVE_DEBOUNCE_SEC)
            await update_challenge_live_message(bot, match_id)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"challenge live debounce error for {match_id}: {exc}")
        finally:
            if _challenge_live_tasks.get(match_id) is asyncio.current_task():
                _challenge_live_tasks.pop(match_id, None)

    _challenge_live_tasks[match_id] = asyncio.create_task(
        _debounced(), name=f"challenge-live-debounce-{match_id}"
    )


async def post_challenge_live_panel(
    bot_ref: "SudokuBot",
    home: discord.TextChannel,
    match: dict,
    guild: discord.Guild,
) -> None:
    match_id = match["_id"]
    player_sessions = await activity_sessions_for_challenge(match)
    embed = build_challenge_live_embed(match, guild, player_sessions=player_sessions)
    view = build_challenge_watch_view(match, bot_ref)
    try:
        msg = await home.send(embed=embed, view=view, silent=True)
        view.message = msg
        bot_ref.add_view(view)
        await match_store.update_match(match_id, {"live_message_id": msg.id})
    except discord.HTTPException as exc:
        print(f"post_challenge_live_panel failed for {match_id}: {exc}")


async def restore_challenge_watch_views(bot_ref: "SudokuBot") -> None:
    try:
        active = await match_store.list_matches(status="active")
    except Exception as exc:  # noqa: BLE001
        print(f"restore_challenge_watch_views list failed: {exc}")
        return
    restored = 0
    for match in active:
        if not match.get("live_message_id"):
            continue
        view = build_challenge_watch_view(match, bot_ref)
        bot_ref.add_view(view)
        restored += 1
    if restored:
        print(f"Restored {restored} challenge watch panel(s).")


class ChallengeWatchView(discord.ui.View):
    def __init__(self, match_id: str, bot_ref: "SudokuBot"):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.bot = bot_ref
        self.message: discord.Message | None = None

    def rebuild_player_buttons(self, match: dict) -> None:
        self.clear_items()
        refresh = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.primary,
            custom_id=f"watch:{self.match_id}:refresh",
            row=0,
        )
        refresh.callback = self._on_refresh
        self.add_item(refresh)

        row = 1
        for slot, player in match_player_entries(match):
            if player.get("forfeit"):
                continue
            name = str(player.get("name") or "Player")[:20]
            btn = discord.ui.Button(
                label=f"View {name}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"watch:{self.match_id}:board:{slot}",
                row=row,
            )
            btn.callback = self._make_board_cb(slot)
            self.add_item(btn)
            row = min(row + 1, 4)

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await update_challenge_live_message(self.bot, self.match_id)
        await interaction.followup.send("Live board updated.", ephemeral=True)

    def _make_board_cb(self, slot: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await self._show_player_board(interaction, slot)

        return _cb

    async def _show_player_board(self, interaction: discord.Interaction, slot: str) -> None:
        match = await match_store.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        player = match.get(slot)
        if not isinstance(player, dict):
            await interaction.response.send_message("Player not found.", ephemeral=True)
            return
        if player.get("forfeit"):
            await interaction.response.send_message("That player quit.", ephemeral=True)
            return
        board = normalize_board(
            player.get("current_board") or match.get("board_template") or []
        )
        given = match.get("given") or []
        name = str(player.get("name") or "Player")
        status = "finished" if player.get("finished_time") else "racing"
        image = render_board(board, given, difficulty=match.get("difficulty"))
        file = board_to_file(image)
        await interaction.response.send_message(
            f"**{name}** — {status} · spectator view (read-only)",
            file=file,
            ephemeral=True,
        )


def activity_session_mention(guild: discord.Guild | None, session: dict) -> str:
    uid = session.get("user_id")
    if uid is None:
        return str(session.get("name") or "Player")
    if guild is not None:
        member = guild.get_member(int(uid))
        if member is not None:
            return member.mention
    return f"<@{uid}>"


from activity_watchers import prune_watchers


def prune_activity_watchers(watchers: dict | None) -> dict:
    return prune_watchers(watchers)


def format_activity_watchers_suffix(
    session: dict | None,
    guild: discord.Guild | None,
) -> str:
    watchers = prune_activity_watchers((session or {}).get("watchers"))
    if not watchers:
        return ""
    labels: list[str] = []
    for viewer_id, meta in sorted(
        watchers.items(),
        key=lambda item: str(item[1].get("name") or ""),
    ):
        try:
            uid = int(viewer_id)
        except (TypeError, ValueError):
            labels.append(str(meta.get("name") or "Player"))
            continue
        if guild is not None and guild.get_member(uid) is not None:
            labels.append(f"<@{uid}>")
        else:
            labels.append(str(meta.get("name") or "Player"))
    if len(labels) == 1:
        tail = labels[0]
    elif len(labels) == 2:
        tail = f"{labels[0]} & {labels[1]}"
    else:
        tail = f"{labels[0]}, {labels[1]} +{len(labels) - 2}"
    return f" · 👀 {tail}"


async def activity_sessions_for_challenge(match: dict) -> dict[str, dict]:
    guild_id = int(match.get("guild_id") or 0)
    out: dict[str, dict] = {}
    for _slot, player in match_player_entries(match):
        uid = player.get("user_id")
        if uid is None:
            continue
        session, _sid = await lookup_user_activity_session(guild_id, int(uid))
        if session:
            out[str(uid)] = session
    return out


def format_challenge_watchers_suffix(
    match: dict,
    player_sessions: dict[str, dict] | None,
    guild: discord.Guild | None,
) -> str:
    if not player_sessions:
        return ""
    unique: dict[str, str] = {}
    for session in player_sessions.values():
        for viewer_id, meta in prune_activity_watchers(session.get("watchers")).items():
            if viewer_id in unique:
                continue
            try:
                uid = int(viewer_id)
            except (TypeError, ValueError):
                unique[viewer_id] = str(meta.get("name") or "Player")
                continue
            if guild is not None and guild.get_member(uid) is not None:
                unique[viewer_id] = f"<@{uid}>"
            else:
                unique[viewer_id] = str(meta.get("name") or "Player")
    if not unique:
        return ""
    labels = [unique[k] for k in sorted(unique.keys(), key=lambda k: unique[k])]
    if len(labels) == 1:
        return f" · 👀 {labels[0]}"
    if len(labels) == 2:
        return f" · 👀 {labels[0]} & {labels[1]}"
    return f" · 👀 {labels[0]}, {labels[1]} +{len(labels) - 2}"


def activity_most_recent_user_id(sessions: list[dict]) -> str | None:
    now = time.time()
    best_uid: str | None = None
    best_ts = 0.0
    for session in sessions:
        ts = float(session.get("last_move_at") or session.get("updated_at") or 0)
        if ts > best_ts:
            best_ts = ts
            best_uid = str(session.get("user_id"))
    if best_uid is None or now - best_ts > WATCH_ACTIVE_SEC:
        return None
    return best_uid


def build_activity_live_embed(
    sessions: list[dict],
    guild: discord.Guild | None,
) -> discord.Embed:
    active_uid = activity_most_recent_user_id(sessions)
    lines: list[str] = []
    for session in sessions:
        mention = activity_session_mention(guild, session)
        filled = int(session.get("filled") or 0)
        elapsed = activity_session_elapsed(session)
        tier = difficulty_label(resolve_session_difficulty(session)[0])
        marker = "🎮 " if str(session.get("user_id")) == active_uid else "▶ "
        lines.append(
            f"{marker}{mention} — **{tier}** · {filled}/{CHALLENGE_BOARD_CELLS} · "
            f"{format_time(elapsed)}{format_activity_watchers_suffix(session, guild)}"
        )
    embed = paper_embed("Watch games")
    kinds = {(s.get("session_kind") or "play") for s in sessions}
    if kinds == {"daily"}:
        embed.title = f"{PINEAPPLE} Daily in progress"
    elif kinds <= {"play"}:
        embed.title = f"{SPONGE} /play in progress"
    embed.description = "\n".join(lines) or "Nobody is playing right now."
    if active_uid:
        for session in sessions:
            if str(session.get("user_id")) == active_uid:
                kind = session.get("session_kind") or "play"
                if kind == "daily":
                    playing = f"{activity_session_mention(guild, session)} is playing today's **Daily Sudoku**."
                else:
                    playing = f"{activity_session_mention(guild, session)} is playing **Bikini Bottom Sudoku**."
                embed.add_field(
                    name="Now playing",
                    value=playing,
                    inline=False,
                )
                break
    embed.set_footer(text="Activity spectator view · /watch")
    return embed


def build_activity_watch_view(
    guild_id: int,
    channel_id: int | None,
    bot_ref: "SudokuBot",
    sessions: list[dict],
) -> "ActivityWatchMenuView":
    view = ActivityWatchMenuView(guild_id, channel_id, bot_ref)
    view.rebuild_buttons(sessions)
    return view


def daily_watch_session_id(guild_id: int, user_id: int) -> str:
    return f"activity:{guild_id}:{user_id}"


async def get_blocking_activity_session(
    guild_id: int,
    user_id: int,
    *,
    kinds: set[str] | None = None,
) -> dict | None:
    """Return an open Activity session that should block starting another mode."""
    session, _sid = await lookup_user_activity_session(guild_id, user_id)
    if not session:
        return None
    kind = session.get("session_kind") or "play"
    if kinds is not None and kind not in kinds:
        return None
    if session.get("won_at"):
        return None
    # Preference-only docs (diff_index without board) shouldn't block.
    if not session.get("board") and not session.get("solution"):
        return None
    if kind == "daily":
        day = session.get("daily_date") or ""
        if day and day != utc_today():
            return None
    else:
        ts = float(session.get("last_move_at") or session.get("updated_at") or 0)
        if ts > 0 and (time.time() - ts) > ACTIVITY_BLOCKING_MAX_AGE_SEC:
            return None
    return session


async def daily_attempt_blocks_modes(guild_id: int, user_id: int) -> str | None:
    """Block /play and /challenge while today's daily attempt is still open."""
    daily = get_guild_daily(bot.data, guild_id)
    uid = str(user_id)
    r = (daily.get("results") or {}).get(uid) or {}
    if r.get("in_progress") and not r.get("won"):
        return (
            "Finish or `/quit` today's **daily** first — your attempt is still in progress."
        )
    blocking = await get_blocking_activity_session(
        guild_id, user_id, kinds={"daily"}
    )
    if blocking:
        return (
            "Finish or `/quit` today's **daily** Activity first — "
            "your attempt is still open."
        )
    return None


async def activity_blocks_challenge(guild_id: int, user_id: int) -> str | None:
    """User-facing reason if an Activity play/daily session blocks a challenge join."""
    blocking = await get_blocking_activity_session(
        guild_id, user_id, kinds={"play", "daily"}
    )
    if not blocking:
        return None
    kind = blocking.get("session_kind") or "play"
    return f"<@{user_id}> — finish your active **{kind}** Activity first (`/quit`)."


def parse_watch_session_ids(session_id: str) -> tuple[int, int]:
    parts = str(session_id).split(":")
    guild_id = int(parts[1]) if len(parts) >= 3 else 0
    user_id = int(parts[2]) if len(parts) >= 3 else 0
    return guild_id, user_id


def game_filled_count(game: dict) -> int:
    board = game.get("board") or []
    total = 0
    for r in range(9):
        for c in range(9):
            cell = board[r][c]
            value = cell.get("value") if isinstance(cell, dict) else int(cell or 0)
            if value:
                total += 1
    return total


def game_elapsed_sec(game: dict) -> int:
    """Prefer explicit active elapsed when present; else wall-clock from started_at."""
    if game.get("elapsed") is not None:
        try:
            return max(0, int(float(game["elapsed"])))
        except (TypeError, ValueError):
            pass
    return max(0, int(time.time() - float(game.get("started_at") or time.time())))


def activity_session_elapsed(session: dict) -> int:
    """Active play time for spectators — frozen elapsed + live open segment."""
    base = max(0, int(session.get("elapsed") or 0))
    running = session.get("timer_running_since")
    if running:
        try:
            base += max(0, int(time.time() - float(running)))
        except (TypeError, ValueError):
            pass
    started = float(session.get("started_at") or 0)
    if started > 0:
        wall = max(0, int(time.time() - started))
        # Don't crush when started_at was reset after active time accrued.
        stored = max(0, int(session.get("elapsed") or 0))
        if wall >= stored:
            base = min(base, wall)
    return max(0, base)


def _activity_session_priority(doc: dict | None, *, today: str) -> int:
    """Higher wins when several Activity docs exist for the same user."""
    if not doc:
        return -1
    kind = str(doc.get("session_kind") or "play")
    has_board = bool(doc.get("board") or doc.get("solution"))
    if kind == "daily":
        day = str(doc.get("daily_date") or "")
        if day and day != today:
            return 20 if has_board else 5
        return 100 if has_board else 60
    if kind == "challenge":
        return 90 if has_board else 40
    return 50 if has_board else 10


def _pick_best_activity_session(
    candidates: list[tuple[dict | None, str]],
    *,
    today: str | None = None,
) -> tuple[dict | None, str | None]:
    day = today or utc_today()
    best_doc: dict | None = None
    best_sid: str | None = None
    best_score = -1
    for doc, sid in candidates:
        if not doc:
            continue
        score = _activity_session_priority(doc, today=day)
        if score > best_score:
            best_score = score
            best_doc = doc
            best_sid = sid
    return best_doc, best_sid


async def lookup_user_activity_session(
    guild_id: int,
    user_id: int,
) -> tuple[dict | None, str | None]:
    """Primary activity session for a user, including orphan fallback.

    Today's daily always beats a leftover /play board (common Activity guild=0 race).
    """
    session_id = daily_watch_session_id(guild_id, user_id)
    session = await match_store.get_activity_session(session_id)

    orphan_id = daily_watch_session_id(0, user_id)
    orphan = await match_store.get_activity_session(orphan_id)
    if orphan:
        orphan_gid = str(orphan.get("guild_id") or "0")
        if orphan_gid not in ("", "0", str(guild_id)):
            orphan = None

    candidates: list[tuple[dict | None, str]] = [
        (session, session_id),
        (orphan, orphan_id),
    ]
    alt = await match_store.find_activity_session_by_user_id(user_id, guild_id=guild_id)
    if alt:
        candidates.append((alt, str(alt.get("_id") or session_id)))
    # When guild lookup is ambiguous, also consider any other open doc for the user.
    any_doc = await match_store.find_activity_session_by_user_id(user_id)
    if any_doc:
        candidates.append((any_doc, str(any_doc.get("_id") or "")))

    best, best_sid = _pick_best_activity_session(candidates)
    if best and best_sid:
        return best, best_sid
    return None, None


def activity_session_watch_visible(session: dict) -> bool:
    """False when a board has been idle too long for /watch listings."""
    last = float(session.get("last_move_at") or session.get("updated_at") or 0)
    if last <= 0:
        return True
    return (time.time() - last) <= WATCH_IDLE_HIDE_SEC


def play_puzzle_fingerprint(
    given: list[list[bool]] | None,
    *,
    board: list | None = None,
    solution: list[list[int]] | None = None,
) -> str | None:
    """Stable id for a /play puzzle from clue positions + clue digits."""
    import hashlib

    if not given or len(given) != 9:
        return None
    sol = normalize_solution(solution) if solution is not None else None
    parts: list[str] = []
    for r in range(9):
        row = given[r]
        if not isinstance(row, list) or len(row) != 9:
            return None
        cells: list[str] = []
        for c in range(9):
            if not bool(row[c]):
                cells.append("0")
                continue
            digit = 0
            if board is not None:
                try:
                    digit = int(cell_value(board, r, c) or 0)
                except Exception:
                    digit = 0
            if digit <= 0 and sol is not None:
                digit = int(sol[r][c] or 0)
            if digit <= 0:
                return None
            cells.append(str(digit))
        parts.append("".join(cells))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


async def sync_daily_watch_session(key: tuple, game: dict) -> None:
    """Mirror in-progress /daily boards into the shared watch session store."""
    if game.get("mode") != "daily":
        return
    guild_id = int(game.get("guild_id") or key[0])
    user_id = int(game.get("owner_id") or key[1])
    board = game.get("board")
    given = game.get("given")
    solution = game.get("solution")
    if not board or not given or not solution:
        return
    session_id = daily_watch_session_id(guild_id, user_id)
    new_filled = game_filled_count(game)
    existing = await match_store.get_activity_session(session_id)
    last_move_at = time.time()
    if existing:
        prev_filled = int(existing.get("filled") or 0)
        if new_filled <= prev_filled and existing.get("last_move_at"):
            last_move_at = float(existing["last_move_at"])
    day = str(game.get("daily_date") or utc_today())
    diff_key = daily_difficulty_for_date(day)
    try:
        diff_index = DIFF_KEYS_LIST.index(diff_key)
    except ValueError:
        diff_index = difficulty_index(diff_key)
    doc = {
        "_id": session_id,
        "session_kind": "daily",
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "difficulty": diff_key,
        "diff_index": diff_index,
        "elapsed": game_elapsed_sec(game),
        "board": board,
        "given": given,
        "solution": solution,
        "filled": new_filled,
        "name": game.get("owner_name") or "Player",
        "channel_id": str(game.get("channel_id")) if game.get("channel_id") else None,
        "daily_date": game.get("daily_date"),
        "started_at": float(game.get("started_at") or time.time()),
        "last_move_at": last_move_at,
    }
    if existing:
        for watch_key in (
            "watch_notified",
            "watch_message_id",
            "watch_channel_id",
            "watch_posted_at",
            "watch_once_notified",
        ):
            if existing.get(watch_key) is not None:
                doc[watch_key] = existing[watch_key]
    await match_store.upsert_activity_session(doc)


async def activity_watch_is_live(
    bot_ref: "SudokuBot",
    session: dict | None,
) -> bool:
    """True only when the channel announcement still exists."""
    if not session or not session.get("watch_notified"):
        return False
    raw_msg = session.get("watch_message_id")
    if not raw_msg:
        return False
    channel_id = session.get("watch_channel_id") or ACTIVITY_WATCH_CHANNEL_ID
    channel = await resolve_channel(bot_ref, int(channel_id))
    if channel is None:
        return False
    try:
        await channel.fetch_message(int(raw_msg))
        return True
    except discord.HTTPException:
        return False


def format_activity_watch_announcement(
    mention: str, session: dict | None
) -> str:
    kind = (session or {}).get("session_kind") or "play"
    if kind == "daily":
        day = (session or {}).get("daily_date") or utc_today()
        return f"{mention} is playing today's **Daily Sudoku** (`{day}`)!"
    return f"{mention} is playing **Bikini Bottom Sudoku**!"


async def notify_activity_play_started(
    bot_ref: "SudokuBot",
    session_id: str,
    *,
    fallback_user: discord.abc.User | None = None,
    force: bool = False,
    watch_channel_id: int | None = None,
    announcement: str | None = None,
) -> None:
    """Post a one-time watch invite once a spectatable board is saved."""
    channel_id = int(watch_channel_id or ACTIVITY_WATCH_CHANNEL_ID or 0)
    if not channel_id:
        print(f"activity watch notify skipped for {session_id}: no watch channel configured")
        return
    if session_id in _activity_notify_inflight:
        print(f"activity watch notify skipped for {session_id}: already in flight")
        return

    _activity_notify_inflight.add(session_id)
    try:
        session = await match_store.get_activity_session(session_id)
        if not activity_session_spectatable(session):
            print(
                f"activity watch notify skipped for {session_id}: "
                "no spectatable board yet"
            )
            return
        # Already announced this open-session — never post a second "is playing".
        if not force and session and session.get("watch_once_notified"):
            if await activity_watch_is_live(bot_ref, session):
                print(f"activity watch notify skipped for {session_id}: already live")
                return
            # Flag set but message id missing / deleted — still skip to avoid orphans.
            print(
                f"activity watch notify skipped for {session_id}: "
                "already notified this session"
            )
            return
        posted_at = float((session or {}).get("watch_posted_at") or 0)
        if not force and session and session.get("watch_notified") and (time.time() - posted_at < 120):
            print(f"activity watch notify skipped for {session_id}: announcement posted recently")
            return
        if not force and await activity_watch_is_live(bot_ref, session):
            print(f"activity watch notify skipped for {session_id}: announcement already live")
            return

        # Claim the once-flag before Discord I/O so concurrent saves cannot double-post.
        await match_store.merge_activity_session(
            session_id,
            {
                "watch_once_notified": True,
                "watch_notified": True,
                "watch_posted_at": time.time(),
            },
        )

        # Drop any stale announcement id before posting a replacement.
        if session and session.get("watch_message_id"):
            try:
                await delete_activity_watch_message(
                    bot_ref, session, session_id=session_id
                )
            except Exception as exc:  # noqa: BLE001
                print(f"activity watch pre-delete failed for {session_id}: {exc}")

        channel = await resolve_channel(bot_ref, channel_id)
        if channel is None:
            print(f"activity watch channel {channel_id} not found for {session_id}")
            await match_store.merge_activity_session(
                session_id,
                {
                    "watch_once_notified": False,
                    "watch_notified": False,
                    "watch_message_id": None,
                },
            )
            return

        parts = str(session_id).split(":")
        guild_id = int(parts[1]) if len(parts) >= 3 else 0
        user_id = int(parts[2]) if len(parts) >= 3 else 0
        guild = bot_ref.get_guild(guild_id)
        if fallback_user is not None:
            mention = fallback_user.mention
            player_name = getattr(fallback_user, "display_name", fallback_user.name)
        elif session:
            mention = activity_session_mention(guild, session)
            player_name = str(session.get("name") or "Player")
        elif user_id:
            mention = f"<@{user_id}>"
            player_name = "Player"
        else:
            await match_store.merge_activity_session(
                session_id,
                {
                    "watch_once_notified": False,
                    "watch_notified": False,
                },
            )
            return

        if announcement is None:
            announcement = format_activity_watch_announcement(mention, session)

        watch_view = ActivityPlayWatchView(session_id, bot_ref)
        msg = await channel.send(
            content=announcement,
            view=watch_view,
        )
        watch_view.message = msg
        bot_ref.add_view(watch_view)
        try:
            await match_store.merge_activity_session(
                session_id,
                {
                    "guild_id": str(guild_id),
                    "user_id": str(user_id),
                    "name": player_name,
                    "watch_notified": True,
                    "watch_message_id": msg.id,
                    "watch_channel_id": str(channel_id),
                    "watch_posted_at": time.time(),
                    "watch_once_notified": True,
                },
            )
        except Exception as store_exc:  # noqa: BLE001
            # Message is live but untracked — delete it so we don't orphan.
            print(
                f"activity watch store message_id failed for {session_id}: {store_exc}"
            )
            try:
                await msg.delete()
            except Exception as del_exc:  # noqa: BLE001
                print(f"activity watch rollback delete failed for {session_id}: {del_exc}")
            await match_store.merge_activity_session(
                session_id,
                {
                    "watch_once_notified": False,
                    "watch_notified": False,
                    "watch_message_id": None,
                },
            )
            return
        print(f"activity watch posted for {session_id} in {channel_id}")
    except discord.HTTPException as exc:
        print(f"notify_activity_play_started failed for {session_id}: {exc}")
        try:
            await match_store.merge_activity_session(
                session_id,
                {
                    "watch_once_notified": False,
                    "watch_notified": False,
                    "watch_message_id": None,
                },
            )
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"notify_activity_play_started error for {session_id}: {exc}")
        try:
            await match_store.merge_activity_session(
                session_id,
                {
                    "watch_once_notified": False,
                    "watch_notified": False,
                    "watch_message_id": None,
                },
            )
        except Exception:
            pass
    finally:
        _activity_notify_inflight.discard(session_id)


async def delete_activity_watch_message(
    bot_ref: "SudokuBot",
    session: dict,
    *,
    session_id: str | None = None,
) -> bool:
    """Remove the channel watch announcement when the /play session ends."""
    raw_msg = session.get("watch_message_id")
    if not raw_msg:
        return True
    sid = session_id or session.get("_id")
    try:
        msg_id = int(raw_msg)
    except (TypeError, ValueError):
        print(f"delete_activity_watch_message: invalid message id {raw_msg!r} for {sid}")
        return False

    channel_ids: list[int] = []
    for raw in (
        session.get("watch_channel_id"),
        ACTIVITY_WATCH_CHANNEL_ID,
        DAILY_ANNOUNCE_CHANNEL_ID,
        session.get("channel_id"),
    ):
        try:
            cid = int(raw or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid and cid not in channel_ids:
            channel_ids.append(cid)
    if not channel_ids:
        print(f"delete_activity_watch_message: no channel for session {sid}")
        return False

    last_exc: discord.HTTPException | None = None
    for channel_id in channel_ids:
        channel = await resolve_channel(bot_ref, channel_id)
        if channel is None:
            continue
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.delete()
            print(f"deleted activity watch message {msg_id} in channel {channel_id}")
            return True
        except discord.HTTPException as exc:
            last_exc = exc
            if getattr(exc, "code", None) == 10008 or exc.status == 404:
                print(f"activity watch message {msg_id} already deleted")
                if sid:
                    await match_store.merge_activity_session(
                        str(sid),
                        {
                            "watch_message_id": None,
                            "watch_notified": False,
                            "watch_once_notified": False,
                        },
                    )
                return True
            print(
                f"delete_activity_watch_message failed channel={channel_id} "
                f"msg={msg_id}: {exc}"
            )
    if last_exc is not None:
        print(f"delete_activity_watch_message: all channels failed for msg {msg_id}")
    return False


async def _resolve_watch_session_for_end(session_id: str) -> tuple[dict | None, str]:
    """Find the activity session document that owns the live watch announcement."""
    session = await match_store.get_activity_session(session_id)
    if session and session.get("watch_message_id"):
        return session, str(session.get("_id") or session_id)

    parts = str(session_id).split(":")
    uid = parts[2] if len(parts) >= 3 else ""
    gid = parts[1] if len(parts) >= 3 else ""
    if uid:
        watch = await match_store.find_activity_watch_session(
            uid, guild_id=gid if gid not in ("", "0") else None
        )
        if watch:
            return watch, str(watch.get("_id") or session_id)
        if gid not in ("", "0"):
            watch = await match_store.find_activity_watch_session(uid)
            if watch:
                return watch, str(watch.get("_id") or session_id)

        if gid not in ("", "0"):
            alt = await match_store.find_activity_session_by_user_id(uid, guild_id=gid)
        else:
            alt = await match_store.find_activity_session_by_user_id(uid)
        if alt and alt.get("watch_message_id"):
            return alt, str(alt.get("_id") or session_id)

        if gid != "0":
            orphan = await match_store.get_activity_session(f"activity:0:{uid}")
            if orphan and orphan.get("watch_message_id"):
                orphan_gid = str(orphan.get("guild_id") or "0")
                if orphan_gid in ("", "0", gid):
                    return orphan, str(orphan.get("_id") or f"activity:0:{uid}")

    if session:
        return session, str(session.get("_id") or session_id)
    return None, session_id


async def end_activity_watch(
    bot_ref: "SudokuBot",
    session_id: str,
    *,
    force: bool = False,
) -> bool:
    """Remove the watch announcement but keep the in-progress board for resume.

    Returns True when there is no live announcement left (safe to drop the
    session doc). Returns False when the Discord message is still up.
    """
    session, session_id = await _resolve_watch_session_for_end(session_id)
    if not session or not session.get("watch_message_id"):
        print(f"activity watch end skipped (no message) for {session_id}")
        return True
    if not force:
        posted_at = float(session.get("watch_posted_at") or 0)
        if posted_at and (time.time() - posted_at) < ACTIVITY_WATCH_END_GRACE_SEC:
            print(f"activity watch end ignored (grace) for {session_id}")
            return False
    deleted = await delete_activity_watch_message(bot_ref, session, session_id=session_id)
    if deleted:
        await match_store.merge_activity_session(
            session_id,
            {
                "watch_notified": False,
                "watch_message_id": None,
                # Allow a fresh "is playing" post when they reopen after leaving.
                "watch_once_notified": False,
            },
        )
        print(f"activity watch ended for {session_id}")
        return True
    print(f"activity watch end failed (message still live) for {session_id}")
    return False


async def clear_activity_session(bot_ref: "SudokuBot", session_id: str) -> bool:
    """Drop the persisted session after removing any live watch announcement.

    Refuses to delete the session doc while a watch message is still live so
    we never orphan an \"is playing\" announcement.
    """
    try:
        session = await match_store.get_activity_session(session_id)
        if session and session.get("watch_message_id"):
            ended = await end_activity_watch(bot_ref, session_id, force=True)
            if not ended:
                refreshed = await match_store.get_activity_session(session_id)
                if refreshed and refreshed.get("watch_message_id"):
                    print(
                        f"clear_activity_session refused for {session_id}: "
                        "watch message still live"
                    )
                    return False
    except Exception as exc:  # noqa: BLE001
        print(f"clear_activity_session end_watch failed: {exc}")
        try:
            session = await match_store.get_activity_session(session_id)
            if session and session.get("watch_message_id"):
                print(
                    f"clear_activity_session refused for {session_id}: "
                    f"end_watch error with live message ({exc})"
                )
                return False
        except Exception:
            return False
    await match_store.delete_activity_session(session_id)
    return True


async def restore_activity_play_watch_views(bot_ref: "SudokuBot") -> None:
    restored = 0
    for guild_key in bot_ref.data:
        try:
            gid = int(guild_key)
        except (TypeError, ValueError):
            continue
        try:
            sessions = await match_store.list_activity_sessions(
                gid,
                max_age_sec=WATCH_RESTORE_MAX_AGE_SEC,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"restore_activity_play_watch_views list failed for {gid}: {exc}")
            continue
        for session in sessions:
            if not session.get("watch_notified"):
                continue
            sid = str(session.get("_id") or "")
            if not sid:
                continue
            bot_ref.add_view(ActivityPlayWatchView(sid, bot_ref))
            restored += 1
    if restored:
        print(f"Restored {restored} activity play watch button(s).")


class ActivityPlayWatchView(discord.ui.View):
    """Persistent Watch button on the channel watch announcement."""

    def __init__(self, session_id: str, bot_ref: "SudokuBot"):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.bot = bot_ref
        self.message: discord.Message | None = None

        watch_btn = discord.ui.Button(
            label="Watch",
            style=discord.ButtonStyle.primary,
            custom_id=f"watchplay:{session_id}:watch",
        )
        watch_btn.callback = self._on_watch
        self.add_item(watch_btn)

    async def _on_watch(self, interaction: discord.Interaction) -> None:
        await open_activity_spectator_in_activity(interaction, self.session_id, self.bot)


def activity_session_spectatable(session: dict | None) -> bool:
    """True when a session has enough board data for Activity spectators."""
    if not session:
        return False
    board = session.get("board")
    given = session.get("given")
    return bool(board) and isinstance(given, list) and len(given) == 9


async def get_watch_session_for_spectator(session_id: str) -> dict | None:
    """Load the watch session, falling back to any board saved for the same player."""
    session = await match_store.get_activity_session(session_id)
    if session and activity_session_spectatable(session):
        return session
    parts = str(session_id).split(":")
    if len(parts) >= 3:
        uid = parts[2]
        gid = parts[1]
        alt = await match_store.find_activity_session_by_user_id(
            uid, guild_id=gid if gid not in ("", "0") else None
        )
        if alt and activity_session_spectatable(alt):
            return alt
        if gid not in ("", "0"):
            # Orphan boards keyed activity:0:{uid} with guild_id "0".
            alt = await match_store.find_activity_session_by_user_id(uid)
            if alt and activity_session_spectatable(alt):
                return alt
    return session


def _stub_discord_user(user_id: int, name: str | None = None) -> Any:
    """Minimal user stand-in when guild cache / fetch_user fails."""

    class StubUser:
        def __init__(self) -> None:
            self.id = int(user_id)
            self.name = name or "Player"
            self.display_name = self.name
            self.mention = f"<@{self.id}>"

    return StubUser()


async def open_activity_spectator_in_activity(
    interaction: discord.Interaction,
    session_id: str,
    bot_ref: "SudokuBot",
) -> None:
    """Open the Embedded App in read-only spectator mode for another player's session."""
    guild_id, target_user_id = parse_watch_session_ids(session_id)
    if not target_user_id:
        await interaction.response.send_message("Invalid session.", ephemeral=True)
        return
    if interaction.user.id == target_user_id:
        await interaction.response.send_message(
            "That's your own game — use `/play` to continue playing.",
            ephemeral=True,
        )
        return
    session = await get_watch_session_for_spectator(session_id)
    if not session:
        await interaction.response.send_message("This game has ended.", ephemeral=True)
        return
    # Resolve real guild — orphan ids use activity:0:{uid}.
    resolved_guild = guild_id
    try:
        sg = int(session.get("guild_id") or 0)
        if sg:
            resolved_guild = sg
    except (TypeError, ValueError):
        pass
    if not resolved_guild and interaction.guild is not None:
        resolved_guild = interaction.guild.id
    if not resolved_guild:
        await interaction.response.send_message(
            "Couldn't resolve which server this game belongs to.",
            ephemeral=True,
        )
        return
    await match_store.set_spectate_intent(
        interaction.user.id,
        guild_id=resolved_guild,
        target_user_id=target_user_id,
    )
    try:
        await interaction.response.launch_activity()
    except Exception as exc:  # noqa: BLE001
        print(f"launch_activity spectate failed: {type(exc).__name__}: {exc}")
        msg = "Couldn't open the Activity right now — try again in a moment."
        code = getattr(exc, "code", None)
        if code == 50234:
            msg = (
                "Activities aren't enabled for this app yet. "
                "Ask the server owner to enable **Enable Activities** in the Developer Portal."
            )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class ActivityWatchMenuView(discord.ui.View):
    """Ephemeral /watch menu — Activity spectator for /play/daily and challenge races."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int | None,
        bot_ref: "SudokuBot",
    ):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.bot = bot_ref

    def rebuild_buttons(self, sessions: list[dict]) -> None:
        for session in sessions[:5]:
            name = str(session.get("name") or "Player")[:20]
            sid = str(session.get("_id") or "")
            if not sid:
                continue
            btn = discord.ui.Button(
                label=f"Watch — {name}",
                style=discord.ButtonStyle.primary,
            )
            btn.callback = self._make_watch_cb(sid)
            self.add_item(btn)

    def rebuild_challenge_buttons(self, matches: list[dict]) -> None:
        for match in matches[:3]:
            match_id = str(match.get("_id") or "")
            if not match_id:
                continue
            tier = difficulty_label(match.get("difficulty"))[:14]
            n = len(match_player_entries(match))
            btn = discord.ui.Button(
                label=f"🏁 {tier} ({n})",
                style=discord.ButtonStyle.success,
            )
            btn.callback = self._make_challenge_cb(match_id)
            self.add_item(btn)

    def _make_watch_cb(self, session_id: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await open_activity_spectator_in_activity(interaction, session_id, self.bot)

        return _cb

    def _make_challenge_cb(self, match_id: str):
        async def _cb(interaction: discord.Interaction) -> None:
            match = await match_store.get_match(match_id)
            if not match or match.get("status") == "finished":
                await interaction.response.send_message(
                    "That challenge has already ended.",
                    ephemeral=True,
                )
                return
            player_sessions = await activity_sessions_for_challenge(match)
            embed = build_challenge_live_embed(
                match,
                interaction.guild,
                player_sessions=player_sessions,
            )
            view = build_challenge_watch_view(match, self.bot)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            try:
                view.message = await interaction.original_response()
                self.bot.add_view(view)
            except discord.HTTPException:
                pass

        return _cb


async def resolve_member_or_user(
    bot_ref: "SudokuBot",
    guild: discord.Guild | None,
    user_id: int,
    fallback: discord.abc.User | None = None,
) -> discord.abc.User | discord.Member | None:
    if fallback is not None and getattr(fallback, "id", None) == user_id:
        return fallback
    if guild is not None:
        m = guild.get_member(user_id)
        if m is not None:
            return m
        try:
            return await guild.fetch_member(user_id)
        except Exception:
            pass
    u = bot_ref.get_user(user_id)
    if u is not None:
        return u
    try:
        return await bot_ref.fetch_user(user_id)
    except Exception:
        return None


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
        if guild is None or challenge_home_channel(interaction.channel) is None:
            await _abort("Use this lobby in a server text channel.")
            return

        all_ids = [self.challenger_id, *sorted(self.accepted_ids)]
        for uid in all_ids:
            block = await challenge_blocks_user(uid)
            if block:
                await _abort(block if "<@" in block else f"<@{uid}> — {block}")
                return
            if solo_key(guild.id, uid) in games:
                await _abort(
                    "Everyone must finish open solo/daily games before challenging."
                )
                return
            block_msg = await activity_blocks_challenge(guild.id, uid)
            if block_msg:
                await _abort(block_msg)
                return
            daily_block = await daily_attempt_blocks_modes(guild.id, uid)
            if daily_block:
                await _abort(daily_block)
                return

        members: list[discord.abc.User | discord.Member] = []
        for uid in all_ids:
            m = await resolve_member_or_user(bot, guild, uid, fallback=interaction.user)
            if m is None:
                try:
                    m = await guild.fetch_member(uid)
                except discord.HTTPException:
                    m = None
            if m is None:
                await _abort("Could not resolve all players.")
                return
            members.append(m)

        # Soft-disable until launch succeeds (don't stop() yet — abort must recover)
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            await self.message.edit(
                content=self._status_text("✅ Everyone accepted — starting!"),
                view=self,
            )
        ok = await launch_challenge_match(
            interaction=interaction,
            players=members,
            difficulty=self.difficulty,
        )
        if not ok:
            await _abort("Challenge failed to start — lobby reopened.")
            return
        self._disable()

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
        if await reconcile_challenge_game_for_user(self.challenger_id) or (
            await reconcile_challenge_game_for_user(uid)
        ):
            await interaction.response.send_message(
                "You or the challenger already have an active challenge.",
                ephemeral=True,
            )
            return
        block = await challenge_blocks_user(uid)
        if block:
            await interaction.response.send_message(block, ephemeral=True)
            return
        block_ch = await challenge_blocks_user(self.challenger_id)
        if block_ch:
            await interaction.response.send_message(
                "The challenger is still in an active race.",
                ephemeral=True,
            )
            return
        if interaction.guild is not None:
            block_msg = await activity_blocks_challenge(interaction.guild.id, uid)
            if block_msg:
                await interaction.response.send_message(block_msg, ephemeral=True)
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
        if await reconcile_challenge_game_for_user(uid):
            await interaction.response.send_message("Finish your active challenge first.", ephemeral=True)
            return
        block = await challenge_blocks_user(uid)
        if block:
            await interaction.response.send_message(block, ephemeral=True)
            return
        if interaction.guild and solo_key(interaction.guild.id, uid) in games:
            await interaction.response.send_message(
                "Finish your solo/daily game first (`/quit`).",
                ephemeral=True,
            )
            return
        if interaction.guild is not None:
            block_msg = await activity_blocks_challenge(interaction.guild.id, uid)
            if block_msg:
                await interaction.response.send_message(block_msg, ephemeral=True)
                return
            daily_block = await daily_attempt_blocks_modes(interaction.guild.id, uid)
            if daily_block:
                await interaction.response.send_message(daily_block, ephemeral=True)
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
        if guild is None or challenge_home_channel(interaction.channel) is None:
            await interaction.response.send_message("Invalid channel.", ephemeral=True)
            return

        self._launching = True
        # Soft-disable until launch succeeds
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.defer()

        async def _start_abort(msg: str) -> None:
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
                        content=self._roster_text(f"⚠️ {msg}"),
                        view=self,
                    )
                except discord.HTTPException:
                    pass

        members: list[discord.Member] = []
        for uid in self.joined_ids:
            m = guild.get_member(uid)
            if m is None:
                try:
                    m = await guild.fetch_member(uid)
                except discord.HTTPException:
                    m = None
            if m is None:
                await _start_abort("Could not resolve all players.")
                return
            if await reconcile_challenge_game_for_user(uid):
                await _start_abort(f"<@{uid}> already has an active challenge.")
                return
            block = await challenge_blocks_user(uid)
            if block:
                await _start_abort(f"<@{uid}> — {block}")
                return
            if solo_key(guild.id, uid) in games:
                await _start_abort(
                    f"<@{uid}> must finish their solo/daily game first (`/quit`)."
                )
                return
            block_msg = await activity_blocks_challenge(guild.id, uid)
            if block_msg:
                await _start_abort(block_msg)
                return
            daily_block = await daily_attempt_blocks_modes(guild.id, uid)
            if daily_block:
                await _start_abort(daily_block)
                return
            members.append(m)

        if self.message:
            await self.message.edit(
                content=self._roster_text("🏁 Starting…"),
                view=self,
            )
        ok = await launch_challenge_match(
            interaction=interaction,
            players=members,
            difficulty=self.difficulty,
        )
        if not ok:
            self._launching = False
            for child in self.children:
                child.disabled = False  # type: ignore[attr-defined]
            if self.message:
                try:
                    await self.message.edit(
                        content=self._roster_text("⚠️ Start failed — lobby reopened."),
                        view=self,
                    )
                except discord.HTTPException:
                    pass
            return
        self._disable()

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
        # No timeout: Refresh must stay clickable until the player resumes.
        super().__init__(timeout=None)
        self.game_key = game_key
        self.bot = bot
        self.message: discord.Message | None = None

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Ack immediately — PNG render on Render often exceeds Discord's 3s window.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.HTTPException:
            return

        game = games.get(self.game_key)
        if not game:
            try:
                await interaction.edit_original_response(
                    content="This game has ended.",
                    embed=None,
                    view=None,
                    attachments=[],
                )
            except discord.errors.NotFound:
                pass
            self.stop()
            return
        if interaction.user.id != game["owner_id"]:
            try:
                await interaction.followup.send("Not your board.", ephemeral=True)
            except (discord.errors.NotFound, discord.HTTPException):
                pass
            return

        view = SudokuView(self.game_key, self.bot)
        try:
            content, file = board_file_for(game)
            await interaction.edit_original_response(
                content=content,
                embed=None,
                attachments=[file],
                view=view,
            )
        except discord.errors.NotFound:
            # 10015 Unknown Webhook — interaction token already expired; ignore quietly.
            return
        except Exception:
            import traceback

            traceback.print_exc()
            try:
                await interaction.followup.send(
                    "Couldn't refresh the board — try `/play` or tap Refresh again.",
                    ephemeral=True,
                )
            except (discord.errors.NotFound, discord.HTTPException):
                pass
            return

        try:
            view.message = await interaction.original_response()
        except discord.errors.NotFound:
            return
        if view.message:
            game["message_id"] = view.message.id
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
        channel = await resolve_channel(self.bot, game.get("channel_id"))
        if not game.get("message_id") or channel is None:
            return
        try:
            msg = await channel.fetch_message(game["message_id"])
            await msg.edit(content=None, embed=embed, view=None, attachments=[])
        except (discord.HTTPException, AttributeError):
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

        # Race: last move already celebrating — don't forfeit a solved board.
        if game.get("finishing"):
            await interaction.response.edit_message(
                content="Board already solved — rewards are posting.",
                view=None,
            )
            self.stop()
            return

        mode = game.get("mode")
        board = game.get("board") or []
        solution = game.get("solution")
        solved = bool(solution and is_solved(board, solution))

        await interaction.response.edit_message(content="Quitting…", view=None)
        self.stop()
        if self.parent is not None:
            self.parent.stop()

        if mode == "challenge":
            match_id = game["match_id"]
            slot = game["player_slot"]
            await forfeit_challenge_player(
                self.bot,
                match_id,
                slot,
                game_key=self.game_key,
                reason="quit",
            )
            embed = paper_embed(
                "Quit",
                description="You're out. Remaining players keep racing.",
            )
            await self._edit_board_message(game, embed=embed)
            # forfeit_challenge_player already remove_game'd when game_key set.
            if self.game_key in games:
                await remove_game(self.game_key)
            return

        guild = interaction.guild
        guild_id = guild.id if guild is not None else game.get("guild_id")
        if guild_id is None:
            await remove_game(self.game_key)
            return
        guild_id = int(guild_id)

        if solved:
            # Mirror Activity quit views: award a completed board instead of forfeiting.
            game["finishing"] = True
            embed = paper_embed(
                "Already recorded",
                description="Rewards were already recorded for this puzzle.",
            )
            try:
                if not game.get("rewarded"):
                    mode_n = normalize_game_mode(game.get("mode"))
                    if mode_n == "solo" or game.get("mode") == "play":
                        given = game.get("given")
                        given_bool = None
                        if isinstance(given, list) and len(given) == 9:
                            given_bool = [
                                [bool(given[r][c]) for c in range(9)] for r in range(9)
                            ]
                        puzzle_key = play_puzzle_fingerprint(
                            given_bool,
                            board=game.get("board"),
                            solution=game.get("solution"),
                        )
                        if not puzzle_key:
                            embed = paper_embed(
                                "Solved",
                                description=(
                                    "Board solved but puzzle could not be fingerprinted — "
                                    "check `/stats`."
                                ),
                            )
                        else:
                            outcome = await award_play_win(
                                self.bot,
                                guild_id,
                                interaction.user,
                                game,
                                puzzle_key=puzzle_key,
                            )
                            if outcome is None:
                                embed = paper_embed(
                                    "Already recorded",
                                    description="Rewards were already recorded for this puzzle.",
                                )
                            else:
                                embed = paper_embed(
                                    "Solved — recovered",
                                    description=(
                                        f"+{outcome.coins} sponges · streak preserved!"
                                    ),
                                )
                    else:
                        outcome = await finish_win_and_announce(
                            self.bot, guild_id, interaction.user, game
                        )
                        if outcome.quiet:
                            if outcome.quiet_reason == "forfeited":
                                desc = "You already forfeited today's daily."
                            elif outcome.quiet_reason == "claim_unavailable":
                                desc = (
                                    "Couldn't verify today's daily claim — "
                                    "try again in a moment."
                                )
                            else:
                                desc = "Daily already recorded for today."
                            embed = paper_embed(
                                "Already recorded",
                                description=desc,
                            )
                        else:
                            embed = paper_embed(
                                "Solved — recovered",
                                description=(
                                    f"+{outcome.coins} sponges · streak preserved!"
                                ),
                            )
                    game["rewarded"] = True
            except Exception as exc:  # noqa: BLE001
                print(f"ConfirmQuitView solved award failed: {exc}")
                embed = paper_embed(
                    "Solved",
                    description=(
                        "Board was solved but rewards could not be verified — "
                        "check `/stats`."
                    ),
                )
            await remove_game(self.game_key)
            await self._edit_board_message(game, embed=embed)
            try:
                await interaction.followup.send(
                    embed.description or "Done.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return

        embed = await finish_forfeit(self.bot.data, guild_id, interaction.user, game)
        await remove_game(self.game_key)
        await self._edit_board_message(game, embed=embed)

    @discord.ui.button(label="Keep playing", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Still playing.", view=None)
        self.stop()


class ConfirmQuitChallengeMongoView(discord.ui.View):
    """Quit a challenge that exists in Mongo but not yet in memory."""

    def __init__(self, match_id: str, slot: str, bot_ref: "SudokuBot"):
        super().__init__(timeout=30)
        self.match_id = match_id
        self.slot = slot
        self.bot = bot_ref

    @discord.ui.button(label="Quit", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Quitting…", view=None)
        self.stop()
        await forfeit_challenge_player(
            self.bot,
            self.match_id,
            self.slot,
            game_key=None,
            reason="quit",
        )
        try:
            await interaction.followup.send("You're out of the speedrun.", ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Keep playing", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Still racing.", view=None)
        self.stop()


class ConfirmQuitActivityDailyView(discord.ui.View):
    """Confirm daily quit for Activity session (locks attempt, resets streak)."""

    def __init__(self, session_id: str, bot_ref: "SudokuBot"):
        super().__init__(timeout=30)
        self.session_id = session_id
        self.bot = bot_ref

    @discord.ui.button(label="Quit", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        session = await match_store.get_activity_session(self.session_id)
        if not session:
            await interaction.response.edit_message(content="Game already ended.", view=None)
            self.stop()
            return

        guild_id = int(session.get("guild_id") or 0)
        uid = int(session.get("user_id") or 0)
        if interaction.user.id != uid:
            await interaction.response.send_message("Not your board.", ephemeral=True)
            return

        await interaction.response.edit_message(content="Quitting...", view=None)
        self.stop()

        board = normalize_board(session.get("board") or [])
        solution = normalize_solution(session.get("solution"))
        given_raw = session.get("given")

        if board and solution and is_solved(board, solution):
            day = session.get("daily_date") or utc_today()
            diff_key = daily_difficulty_for_date(day)
            game_state = {
                "mode": "daily",
                "daily_date": day,
                "started_at": float(session.get("started_at") or time.time()),
                "difficulty": diff_key,
                "board": board,
                "given": given_raw,
                "solution": solution,
                "hints_used": int(session.get("hints_used") or 0),
            }
            outcome = await finish_win_and_announce(
                self.bot, guild_id, interaction.user, game_state
            )
            if outcome.quiet:
                if outcome.quiet_reason == "forfeited":
                    msg = "You already forfeited today's daily."
                elif outcome.quiet_reason == "claim_unavailable":
                    msg = "Couldn't verify today's daily claim — try again in a moment."
                else:
                    msg = "Daily already recorded for today."
            else:
                msg = (
                    f"Solved daily recovered — +{outcome.coins} sponges, "
                    f"streak preserved!"
                )
        else:
            day = session.get("daily_date") or utc_today()
            game = {
                "mode": "daily",
                "daily_date": session.get("daily_date"),
                "started_at": session.get("started_at") or time.time(),
                "difficulty": daily_difficulty_for_date(day),
            }
            await finish_forfeit(self.bot.data, guild_id, interaction.user, game)
            msg = "Forfeited today's daily. Streak wiped."

        try:
            await end_activity_watch(self.bot, self.session_id, force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"daily quit end_watch failed: {exc}")
        await clear_activity_session(self.bot, self.session_id)
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Keep playing", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Still playing.", view=None)
        self.stop()


class ConfirmQuitActivityPlayView(discord.ui.View):
    """Confirm quit for a regular /play Activity session."""

    def __init__(self, session_id: str, bot_ref: "SudokuBot"):
        super().__init__(timeout=30)
        self.session_id = session_id
        self.bot = bot_ref

    @discord.ui.button(label="Quit", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        session = await match_store.get_activity_session(self.session_id)
        if not session:
            await interaction.response.edit_message(content="Game already ended.", view=None)
            self.stop()
            return
        uid = int(session.get("user_id") or 0)
        if interaction.user.id != uid:
            await interaction.response.send_message("Not your board.", ephemeral=True)
            return

        await interaction.response.edit_message(content="Quitting...", view=None)
        self.stop()

        guild_id = int(session.get("guild_id") or interaction.guild_id or 0)
        board = normalize_board(session.get("board") or [])
        solution = normalize_solution(session.get("solution"))
        given_raw = session.get("given")
        given_bool: list[list[bool]] | None = None
        if isinstance(given_raw, list) and len(given_raw) == 9:
            given_bool = [[bool(given_raw[r][c]) for c in range(9)] for r in range(9)]

        if board and solution and is_solved(board, solution):
            started_at = float(session.get("started_at") or time.time())
            puzzle_key = play_puzzle_fingerprint(
                given_bool,
                board=board,
                solution=solution,
            )
            store_ok = True
            already_paid = False
            if puzzle_key:
                try:
                    already_paid = await match_store.has_play_win(
                        guild_id, uid, puzzle_key
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"play quit has_play_win failed: {exc}")
                    store_ok = False
            if not store_ok:
                msg = (
                    "Solved puzzle closed (could not verify rewards — "
                    "check `/stats` or retry later)."
                )
            elif not puzzle_key:
                msg = (
                    "Solved puzzle closed (could not fingerprint puzzle — "
                    "rewards not applied; check `/stats`)."
                )
            elif already_paid:
                msg = "Solved puzzle closed (rewards were already recorded)."
            else:
                diff_key, _ = resolve_session_difficulty(session)
                game_state = {
                    "mode": "play",
                    "started_at": started_at,
                    "difficulty": diff_key,
                }
                try:
                    outcome = await award_play_win(
                        self.bot,
                        guild_id,
                        interaction.user,
                        game_state,
                        puzzle_key=puzzle_key,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"play quit award_play_win failed: {exc}")
                    msg = (
                        "Solved puzzle closed (could not verify rewards — "
                        "check `/stats` or retry later)."
                    )
                else:
                    if outcome is None:
                        msg = "Solved puzzle closed (rewards were already recorded)."
                    else:
                        msg = "Solved puzzle recovered — rewards applied!"
        else:
            diff_key, _ = resolve_session_difficulty(session)
            game = {
                "mode": "play",
                "started_at": session.get("started_at") or time.time(),
                "difficulty": diff_key,
            }
            await finish_forfeit(self.bot.data, guild_id, interaction.user, game)
            msg = "Quit. Your daily streak is unchanged."

        try:
            await end_activity_watch(self.bot, self.session_id, force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"play quit end_watch failed: {exc}")
        try:
            from activity_http import _clear_user_activity_sessions

            await _clear_user_activity_sessions(self.bot, guild_id, uid)
        except Exception as exc:  # noqa: BLE001
            print(f"play quit clear sessions failed: {exc}")
            await clear_activity_session(self.bot, self.session_id)
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Keep playing", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Still playing.", view=None)
        self.stop()


class ChallengeLaunchActivityView(discord.ui.View):
    """Persistent shared Play button for challenge races (one message for all players)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Play in Activity 🎮",
        style=discord.ButtonStyle.primary,
        custom_id="challenge_launch_activity",
    )
    async def play_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ch_key = await ensure_challenge_game_for_user(bot, interaction.user.id)
        if not ch_key:
            await interaction.response.send_message(
                "You do not have an active challenge in this server. Start one with `/challenge`.",
                ephemeral=True,
            )
            return
        # Discord rejects Activities launched from private (and often public) threads.
        if isinstance(interaction.channel, discord.Thread):
            parent = interaction.channel.parent
            home: discord.TextChannel | None = (
                parent if isinstance(parent, discord.TextChannel) else None
            )
            if home is None:
                game = games.get(ch_key) or {}
                resolved = await resolve_channel(bot, game.get("channel_id"))
                home = resolved if isinstance(resolved, discord.TextChannel) else None
            if home is None:
                await interaction.response.send_message(
                    "Discord can't start Activities in this thread. "
                    "Go to the sudoku text channel and tap the shared Play button, "
                    "or `/quit` and start a new `/challenge`.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Discord blocks Activities in threads. "
                f"Open {home.mention} and tap the shared **Play in Activity** button.",
                ephemeral=True,
            )
            return
        await _launch_activity_window(interaction)


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
            await interaction.response.edit_message(
                content="This game has ended.",
                embed=None,
                view=None,
                attachments=[],
            )
            self.stop()
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
        refresh.message = self.message
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
        except discord.errors.NotFound:
            # 10015 Unknown Webhook — token expired; ignore quietly.
            return
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
            clear_pencil_digit_peers(game["board"], r, c, digit)
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
        """Award sponges and update the same board message — no new channel posts."""
        game["finishing"] = True
        key = self.game_key
        guild_id = None
        if interaction.guild is not None:
            guild_id = interaction.guild.id
        elif game.get("guild_id") is not None:
            guild_id = int(game["guild_id"])

        coins = 0
        try:
            if game.get("mode") == "challenge":
                await handle_challenge_completion(self.bot, interaction, game, self)
                return

            if guild_id is None:
                # Still close the session so /play is not blocked
                await remove_game(key)
                self.stop()
                try:
                    await interaction.edit_original_response(view=None)
                except discord.HTTPException:
                    pass
                try:
                    await interaction.followup.send(
                        "Board complete, but couldn't award (missing server).",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass
                return

            # 1) Award XP + sponges first
            boost_kwargs: dict = {}
            if not game.get("rewarded"):
                mode_n = normalize_game_mode(game.get("mode"))
                if mode_n == "solo" or game.get("mode") == "play":
                    given = game.get("given")
                    given_bool = None
                    if isinstance(given, list) and len(given) == 9:
                        given_bool = [
                            [bool(given[r][c]) for c in range(9)] for r in range(9)
                        ]
                    puzzle_key = play_puzzle_fingerprint(
                        given_bool,
                        board=game.get("board"),
                        solution=game.get("solution"),
                    )
                    if puzzle_key:
                        outcome = await award_play_win(
                            self.bot,
                            guild_id,
                            interaction.user,
                            game,
                            puzzle_key=puzzle_key,
                        )
                        if outcome is not None:
                            coins = int(outcome.coins)
                            xp = int(outcome.xp)
                            boost_kwargs = win_boost_caption_kwargs(outcome)
                        else:
                            coins = 0
                            xp = 0
                    else:
                        coins = 0
                        xp = 0
                else:
                    outcome = await finish_win_and_announce(
                        self.bot,
                        guild_id,
                        interaction.user,
                        game,
                    )
                    coins = int(outcome.coins)
                    xp = int(outcome.xp)
                    boost_kwargs = win_boost_caption_kwargs(outcome)
                game["rewarded"] = True
                try:
                    await persist_game(key, game)
                except Exception as persist_exc:  # noqa: BLE001
                    print(f"celebrate_win persist rewarded failed: {persist_exc}")
            else:
                coins = 0
                xp = 0

            # 2) Same message: solved board image + reward as readable text underneath
            file = board_to_file(
                render_board(
                    game["board"],
                    game["given"],
                    solution=game.get("solution"),
                    conflicts=set(),
                    difficulty=game.get("difficulty"),
                    title_id=game.get("owner_title"),
                    pin_emojis=game.get("pin_emojis"),
                    pin_seed=game.get("pin_seed"),
                )
            )
            caption = (
                win_reward_caption(coins, xp, **boost_kwargs)
                if coins > 0 or xp > 0
                else f"{BUBBLE} **Board complete!**"
            )
            try:
                await interaction.edit_original_response(
                    content=caption,
                    embed=None,
                    view=None,
                    attachments=[file],
                )
                if game.get("mode") == "daily" and guild_id is not None:
                    daily_meta = get_guild_daily(self.bot.data, guild_id)
                    entry = daily_meta.setdefault("results", {}).setdefault(
                        str(interaction.user.id), {}
                    )
                    entry["won"] = True
                    entry["announced_debug"] = True
                    save_data(self.bot.data)
            except discord.HTTPException as ui_exc:
                print(f"win board edit failed: {ui_exc}")
                # Last resort: strip controls only — never leave session open
                try:
                    await interaction.edit_original_response(view=None, embed=None)
                except discord.HTTPException:
                    pass
        except Exception:
            import traceback

            traceback.print_exc()
            try:
                await interaction.followup.send(
                    f"{BUBBLE} Puzzle solved — check `/stats` for sponges.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        finally:
            # ALWAYS close the session so /play /daily work without Quit
            self.stop()
            if key in games:
                await remove_game(key)
            else:
                await drop_persisted_game(key)

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

        if game.get("finishing"):
            await interaction.response.send_message(
                "Board already solved — rewards are posting.",
                ephemeral=True,
            )
            return

        # Solved but celebrate not started — award via board edit (not ephemeral defer).
        if game.get("mode") != "challenge" and is_solved(
            game.get("board") or [], game.get("solution")
        ):
            game["finishing"] = True
            await interaction.response.send_message(
                "Board already solved — applying rewards…",
                ephemeral=True,
            )
            guild_id = interaction.guild.id
            coins = 0
            xp = 0
            boost_kwargs: dict = {}
            try:
                if not game.get("rewarded"):
                    mode_n = normalize_game_mode(game.get("mode"))
                    if mode_n == "solo" or game.get("mode") == "play":
                        given = game.get("given")
                        given_bool = None
                        if isinstance(given, list) and len(given) == 9:
                            given_bool = [
                                [bool(given[r][c]) for c in range(9)] for r in range(9)
                            ]
                        puzzle_key = play_puzzle_fingerprint(
                            given_bool,
                            board=game.get("board"),
                            solution=game.get("solution"),
                        )
                        if puzzle_key:
                            outcome = await award_play_win(
                                self.bot,
                                guild_id,
                                interaction.user,
                                game,
                                puzzle_key=puzzle_key,
                            )
                            if outcome is not None:
                                coins = int(outcome.coins)
                                xp = int(outcome.xp)
                                boost_kwargs = win_boost_caption_kwargs(outcome)
                    else:
                        outcome = await finish_win_and_announce(
                            self.bot, guild_id, interaction.user, game
                        )
                        coins = int(outcome.coins)
                        xp = int(outcome.xp)
                        boost_kwargs = win_boost_caption_kwargs(outcome)
                    game["rewarded"] = True
            except Exception as exc:  # noqa: BLE001
                print(f"on_forfeit solved award failed: {exc}")
            caption = (
                win_reward_caption(coins, xp, **boost_kwargs)
                if coins > 0 or xp > 0
                else f"{BUBBLE} **Board complete!**"
            )
            try:
                file = board_to_file(
                    render_board(
                        game["board"],
                        game["given"],
                        solution=game.get("solution"),
                        conflicts=set(),
                        difficulty=game.get("difficulty"),
                        title_id=game.get("owner_title"),
                        pin_emojis=game.get("pin_emojis"),
                        pin_seed=game.get("pin_seed"),
                    )
                )
                channel = await resolve_channel(self.bot, game.get("channel_id"))
                if game.get("message_id") and channel is not None:
                    msg = await channel.fetch_message(game["message_id"])
                    await msg.edit(
                        content=caption,
                        embed=None,
                        view=None,
                        attachments=[file],
                    )
            except Exception as ui_exc:  # noqa: BLE001
                print(f"on_forfeit solved board edit failed: {ui_exc}")
            self.stop()
            await remove_game(self.game_key)
            return

        mode = game.get("mode")
        if mode == "challenge":
            prompt = "Really leave this speedrun?"
        elif mode == "daily":
            prompt = "Quit today's daily? This locks your attempt and resets your streak."
        else:
            prompt = "Really quit this puzzle? Your daily streak stays safe."

        await interaction.response.send_message(
            prompt,
            view=ConfirmQuitView(self.game_key, self.bot, self),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Shop
# ---------------------------------------------------------------------------

def daily_bundle_pin_id(day: str | None = None) -> str | None:
    """Stable paid pin id on sale for this UTC day (50% off)."""
    day = day or utc_today()
    paid = [
        tid
        for tid, meta in SHOP_PINS.items()
        if tid not in SHOP_BOOST_KEYS and int(meta.get("cost") or 0) > 0
    ]
    if not paid:
        return None
    digest = hashlib.md5(f"thcoku-bundle:{day}".encode()).hexdigest()
    return paid[int(digest, 16) % len(paid)]


def daily_bundle_pin(day: str | None = None) -> dict | None:
    """Catalog-shaped daily pin deal entry, or None."""
    day = day or utc_today()
    tid = daily_bundle_pin_id(day)
    if not tid:
        return None
    meta = SHOP_PINS[tid]
    full = int(meta["cost"])
    sale = max(1, int(round(full * SHOP_BUNDLE_DISCOUNT)))
    return {
        "kind": "pin",
        "id": tid,
        "label": meta["label"],
        "emoji": meta.get("emoji", WAVE),
        "cost": sale,
        "full_cost": full,
        "on_sale": True,
        "theme": meta.get("theme") or "ocean",
        "pin": meta.get("pin") or cosmetic_pin_text(meta),
        "bundle_day": day,
    }


def daily_bundle_title_id(day: str | None = None) -> str | None:
    """Stable paid title id on sale for this UTC day (50% off)."""
    day = day or utc_today()
    paid = [
        tid
        for tid, meta in SHOP_TITLES.items()
        if int(meta.get("cost") or 0) > 0
    ]
    if not paid:
        return None
    digest = hashlib.md5(f"thcoku-title-bundle:{day}".encode()).hexdigest()
    return paid[int(digest, 16) % len(paid)]


def daily_bundle_title(day: str | None = None) -> dict | None:
    """Catalog-shaped daily title deal entry, or None."""
    day = day or utc_today()
    tid = daily_bundle_title_id(day)
    if not tid:
        return None
    meta = SHOP_TITLES[tid]
    full = int(meta["cost"])
    sale = max(1, int(round(full * SHOP_BUNDLE_DISCOUNT)))
    return {
        "kind": "title",
        "id": tid,
        "label": meta["label"],
        "emoji": meta.get("emoji", SPONGE),
        "cost": sale,
        "full_cost": full,
        "on_sale": True,
        "theme": None,
        "pin": meta.get("pin") or cosmetic_pin_text(meta),
        "bundle_day": day,
    }


def shop_catalog(kind: str) -> list[dict]:
    """Browseable catalog entries for the Krusty Shop."""
    if kind == "boosts":
        return [
            {
                "kind": "boost",
                "id": tid,
                "label": meta["label"],
                "emoji": meta.get("emoji", SPONGE),
                "cost": int(meta["cost"]),
                "pin": meta.get("pin") or cosmetic_pin_text(meta),
                "theme": None,
                "on_sale": False,
                "full_cost": int(meta["cost"]),
            }
            for tid, meta in SHOP_PINS.items()
            if tid in SHOP_BOOST_KEYS
        ]
    if kind == "titles":
        bundle_id = daily_bundle_title_id()
        items = []
        for tid, meta in SHOP_TITLES.items():
            full = int(meta["cost"])
            on_sale = tid == bundle_id and full > 0
            cost = max(1, int(round(full * SHOP_BUNDLE_DISCOUNT))) if on_sale else full
            items.append(
                {
                    "kind": "title",
                    "id": tid,
                    "label": meta["label"],
                    "emoji": meta.get("emoji", SPONGE),
                    "cost": cost,
                    "pin": meta.get("pin") or cosmetic_pin_text(meta),
                    "theme": None,
                    "on_sale": on_sale,
                    "full_cost": full,
                }
            )
        items.sort(
            key=lambda it: (0 if it.get("on_sale") else 1, int(it.get("cost") or 0), it["label"])
        )
        return items

    bundle_id = daily_bundle_pin_id()
    items: list[dict] = []
    for tid, meta in SHOP_PINS.items():
        if tid in SHOP_BOOST_KEYS:
            continue
        full = int(meta["cost"])
        on_sale = tid == bundle_id and full > 0
        cost = max(1, int(round(full * SHOP_BUNDLE_DISCOUNT))) if on_sale else full
        items.append(
            {
                "kind": "pin",
                "id": tid,
                "label": meta["label"],
                "emoji": meta.get("emoji", WAVE),
                "cost": cost,
                "full_cost": full,
                "on_sale": on_sale,
                "theme": meta.get("theme") or "ocean",
                "pin": meta.get("pin") or cosmetic_pin_text(meta),
            }
        )
    # Deal of the day first, then cheaper pins.
    items.sort(key=lambda it: (0 if it.get("on_sale") else 1, int(it.get("cost") or 0), it["label"]))
    return items


def shop_item_owned(stats: dict, item: dict) -> bool:
    if item["kind"] == "boost":
        return False  # Consumables can always be bought again
    if item["kind"] == "title":
        return item["id"] in (stats.get("owned_titles") or [])
    return item["id"] in owned_pin_ids(stats)


def shop_item_equipped(stats: dict, item: dict) -> bool:
    if item["kind"] == "title":
        return stats.get("title") == item["id"]
    return shop_item_owned(stats, item)


def shop_item_status_text(stats: dict, item: dict) -> str:
    if item["kind"] == "boost":
        return "🔮 Power-Up"
    owned = shop_item_owned(stats, item)
    if item["kind"] == "pin":
        return "🟢 Owned" if owned else "🔒 Locked"
    if shop_item_equipped(stats, item):
        return "✨ Equipped"
    if owned:
        return "🟢 Owned"
    return "🔒 Locked"


def shop_item_price_text(item: dict) -> str:
    cost = int(item["cost"])
    if cost <= 0:
        return "FREE"
    if item.get("on_sale") and int(item.get("full_cost") or 0) > cost:
        return f"~~{int(item['full_cost'])}~~ **{cost}** {SPONGE} DEAL"
    return format_sponges(cost)


def shop_select_emoji(raw: Any) -> str | None:
    """Discord SelectOption emoji must be a unicode emoji or PartialEmoji — never plain text."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Reject ASCII/word labels that are not emoji (e.g. legacy typo "Squid").
    if text.isascii() and text.isalpha():
        return None
    return text


def shop_item_can_buy(stats: dict, item: dict) -> bool:
    if item["kind"] != "boost" and shop_item_owned(stats, item):
        return False
    cost = int(item["cost"])
    return cost <= 0 or int(stats.get("coins") or 0) >= cost


def shop_filter_catalog(
    items: list[dict], stats: dict, filt: str
) -> list[dict]:
    """filt: all | afford | owned | ocean | crew"""
    if filt == "owned":
        return [it for it in items if shop_item_owned(stats, it)]
    if filt == "afford":
        return [it for it in items if shop_item_can_buy(stats, it)]
    if filt in ("ocean", "crew"):
        return [it for it in items if (it.get("theme") or "ocean") == filt]
    return list(items)


def shop_page_embed(
    *,
    stats: dict,
    kind: str,
    page_items: list[dict],
    selected: dict | None,
    page: int,
    pages: int,
    filt: str,
    filtered_total: int,
) -> discord.Embed:
    """Mobile-first paginated shop embed with active boosts and inventory status."""
    tab_title = {"boosts": "🔮 Power-Ups", "pins": "🎨 Border Pins", "titles": "👑 Titles"}.get(kind, "🔮 Power-Ups")
    filter_label = {
        "all": "All",
        "afford": "Can buy",
        "owned": "Owned",
        "ocean": "Ocean",
        "crew": "Crew",
    }.get(filt, "All")

    embed = paper_embed(f"{SPONGE} Krusty Shop · {tab_title}")

    boost_charges = int(stats.get("xp_boost_charges") or 0)
    boost_str = (
        f"🔮 **2x Boost:** {boost_charges} games"
        if boost_charges > 0
        else "🔮 **2x Boost:** none"
    )
    snack_charges = int(stats.get("krabby_snack_charges") or 0)
    snack_str = f"🍟 **Snack:** {snack_charges}" if snack_charges > 0 else ""
    spatula_charges = int(stats.get("golden_spatula_charges") or 0)
    spatula_str = f"🥇 **Spatula:** {spatula_charges}" if spatula_charges > 0 else ""
    gary_charges = int(stats.get("gary_wisdom_charges") or 0)
    gary_str = f"🐌 **Gary:** {gary_charges}" if gary_charges > 0 else ""
    shields = int(stats.get("streak_shields") or 0)
    eq_title = SHOP_TITLES[stats.get("title")]["label"] if stats.get("title") in SHOP_TITLES else "Civilian"
    extra_boosts = " · ".join(x for x in (snack_str, spatula_str, gary_str) if x)

    status_banner = (
        f"💰 **Pocket:** {format_sponges(stats.get('coins', 0))}\n"
        f"{boost_str} · 🛡️ **Shields:** {shields}"
        + (f"\n{extra_boosts}" if extra_boosts else "")
        + f"\n👑 **Title:** {eq_title} · 🎨 **Pins:** {len(owned_pin_emojis(stats))}\n"
    )

    deal = None
    if kind == "pins":
        deal = daily_bundle_pin()
    elif kind == "titles":
        deal = daily_bundle_title()
    deal_line = ""
    if deal:
        deal_line = (
            f"🏷️ **Deal of the day:** {deal['emoji']} **{deal['label']}** — "
            f"{shop_item_price_text(deal)} (ends next UTC midnight)\n"
        )

    lines: list[str] = []
    selected_id = (selected or {}).get("id")
    for it in page_items:
        mark = "▶ " if it["id"] == selected_id else "• "
        status = shop_item_status_text(stats, it)
        if status == "🔒 Locked" and shop_item_can_buy(stats, it):
            status = "⚡ Can Buy"
        price = shop_item_price_text(it)
        sale_mark = " 🔥" if it.get("on_sale") else ""
        lines.append(f"{mark}**{it['label']}**{sale_mark} — `{price}` ({status})")

    if not lines:
        lines.append("_No items match this filter._")

    embed.description = (
        f"{status_banner}{deal_line}\n"
        f"─── *Page **{page + 1}/{max(1, pages)}** ({filtered_total} items) · Filter: **{filter_label}*** ───\n\n"
        + "\n".join(lines)
    )

    if selected:
        status = shop_item_status_text(stats, selected)
        if status == "🔒 Locked" and shop_item_can_buy(stats, selected):
            status = "⚡ Can Buy"

        detail = f"**{selected['label']}** ({shop_item_price_text(selected)} · {status})"
        if selected["id"] == "xp_boost":
            detail += (
                "\n⚡ *Best all-rounder: 2× career XP **and** sponges on win (3 games).* "
                "Use Snack or Spatula if you only want one."
            )
        elif selected["id"] == "streak_shield":
            detail += "\n🛡️ *Covers missed **daily** days only — challenges never reset your streak.*"
        elif selected["id"] == "gary_wisdom":
            detail += (
                f"\n🐌 *{GARY_WISDOM_HINT_BONUS} free hints first (no sponge cost) "
                f"for {GARY_WISDOM_GAMES_PER_PURCHASE} games. After that, paid hints "
                f"are unlimited at {format_sponges(HINT_SPONGE_COST)} each.*"
            )
        elif selected["id"] == "krabby_snack":
            detail += "\n🍟 *+25% pocket sponges only — career XP unchanged (3 wins).*"
        elif selected["id"] == "golden_spatula":
            detail += "\n🥇 *+50% career XP only — sponges unchanged (3 wins).*"
        elif selected["kind"] == "title":
            sample = titled_header_line("Easy", selected.get("pin") or "Civilian", emoji=str(selected.get("emoji") or ""))
            detail += f"\nHeader flair: `{sample}`"
            if selected.get("on_sale"):
                detail += "\n🔥 *Today's title deal — 50% off!*"
        else:
            theme = (selected.get("theme") or "ocean").title()
            detail += f"\nBorder pin sticker: {selected['emoji']} · Theme: **{theme}**"
            if selected.get("on_sale"):
                detail += "\n🔥 *Today's bundle deal — 50% off!*"
            detail += "\nGift owned pins with `/giftpin` or the **Gift** button."

        embed.add_field(
            name="🔍 Selected Item",
            value=detail,
            inline=False,
        )

    return embed


def apply_shop_equip(bot: "SudokuBot", guild_id: int, user_id: int, item: dict) -> dict:
    """Equip an owned title. Pins have no equip slot."""
    gstats = guild_stats(bot.data, guild_id)
    stats = user_stats(gstats, user_id)
    if item["kind"] == "pin":
        if item["id"] not in owned_pin_ids(stats):
            return {"ok": False, "message": "You don't own this pin yet — Buy it first."}
        push_cosmetics_sync(user_id, guild_id, stats)
        return {
            "ok": True,
            "message": f"**{item['label']}** is already on your border when you play.",
        }

    tid = item["id"]
    if tid not in SHOP_TITLES:
        return {"ok": False, "message": "Unknown title."}
    if tid not in stats["owned_titles"]:
        return {"ok": False, "message": "You don't own this title yet — Buy it first."}
    stats["title"] = tid
    save_data(bot.data)
    push_cosmetics_sync(user_id, guild_id, stats)
    return {
        "ok": True,
        "message": f"Equipped **{item['label']}**. Active boards pick it up on the next move.",
    }


def apply_shop_purchase(bot: "SudokuBot", guild_id: int, user_id: int, item: dict) -> dict:
    """Buy + auto-equip (titles) or add border pin. Returns {ok, bought, message, label, cost}."""
    gstats = guild_stats(bot.data, guild_id)
    stats = user_stats(gstats, user_id)
    cost = int(item["cost"])

    if item["kind"] == "title":
        tid = item["id"]
        if tid not in SHOP_TITLES:
            return {"ok": False, "bought": False, "message": "Unknown title."}
        if tid in stats["owned_titles"]:
            return {"ok": False, "bought": False, "message": "Already owned — use Equip."}
        if stats["coins"] < cost:
            return {
                "ok": False,
                "bought": False,
                "message": (
                    f"Need **{format_sponges(cost)}** "
                    f"(you have {format_sponges(stats['coins'])})."
                ),
            }
        stats["coins"] -= cost
        stats["sponges_spent"] = int(stats.get("sponges_spent") or 0) + cost
        stats["owned_titles"].append(tid)
        stats["title"] = tid
        save_data(bot.data)
        push_cosmetics_sync(user_id, guild_id, stats)
        return {
            "ok": True,
            "bought": True,
            "label": item["label"],
            "cost": cost,
            "message": f"Bought **{item['label']}**!",
        }

    tid = item["id"]
    if tid == "streak_shield":
        if stats["coins"] < cost:
            return {
                "ok": False,
                "bought": False,
                "message": (
                    f"Need **{format_sponges(cost)}** "
                    f"(you have {format_sponges(stats['coins'])})."
                ),
            }
        stats["coins"] -= cost
        stats["sponges_spent"] = int(stats.get("sponges_spent") or 0) + cost
        stats["streak_shields"] = int(stats.get("streak_shields") or 0) + 1
        save_data(bot.data)
        return {
            "ok": True,
            "bought": True,
            "label": item["label"],
            "cost": cost,
            "message": f"Bought **{item['label']}**! (Shields owned: **{stats['streak_shields']}** 🛡️)",
        }

    if tid == "xp_boost":
        if stats["coins"] < cost:
            return {
                "ok": False,
                "bought": False,
                "message": (
                    f"Need **{format_sponges(cost)}** "
                    f"(you have {format_sponges(stats['coins'])})."
                ),
            }
        stats["coins"] -= cost
        stats["sponges_spent"] = int(stats.get("sponges_spent") or 0) + cost
        stats["xp_boost_charges"] = (
            int(stats.get("xp_boost_charges") or 0) + REWARD_BOOST_GAMES_PER_PURCHASE
        )
        save_data(bot.data)
        return {
            "ok": True,
            "bought": True,
            "label": item["label"],
            "cost": cost,
            "message": (
                f"Bought **🔮 Puff's Crystal Ball**! 🔮 **2x XP & Sponges active for next "
                f"{stats['xp_boost_charges']} games!**"
            ),
        }

    if tid == "gary_wisdom":
        if stats["coins"] < cost:
            return {
                "ok": False,
                "bought": False,
                "message": (
                    f"Need **{format_sponges(cost)}** "
                    f"(you have {format_sponges(stats['coins'])})."
                ),
            }
        stats["coins"] -= cost
        stats["sponges_spent"] = int(stats.get("sponges_spent") or 0) + cost
        stats["gary_wisdom_charges"] = (
            int(stats.get("gary_wisdom_charges") or 0) + GARY_WISDOM_GAMES_PER_PURCHASE
        )
        save_data(bot.data)
        return {
            "ok": True,
            "bought": True,
            "label": item["label"],
            "cost": cost,
            "message": (
                f"Bought **{item['label']}**! Next game: "
                f"**{GARY_WISDOM_HINT_BONUS} free hints**, then unlimited paid hints "
                f"({stats['gary_wisdom_charges']} game(s) queued)."
            ),
        }

    if tid == "krabby_snack":
        if stats["coins"] < cost:
            return {
                "ok": False,
                "bought": False,
                "message": (
                    f"Need **{format_sponges(cost)}** "
                    f"(you have {format_sponges(stats['coins'])})."
                ),
            }
        stats["coins"] -= cost
        stats["sponges_spent"] = int(stats.get("sponges_spent") or 0) + cost
        stats["krabby_snack_charges"] = (
            int(stats.get("krabby_snack_charges") or 0) + REWARD_BOOST_GAMES_PER_PURCHASE
        )
        save_data(bot.data)
        return {
            "ok": True,
            "bought": True,
            "label": item["label"],
            "cost": cost,
            "message": (
                f"Bought **{item['label']}**! **+25% sponges** on your next "
                f"{stats['krabby_snack_charges']} wins."
            ),
        }

    if tid == "golden_spatula":
        if stats["coins"] < cost:
            return {
                "ok": False,
                "bought": False,
                "message": (
                    f"Need **{format_sponges(cost)}** "
                    f"(you have {format_sponges(stats['coins'])})."
                ),
            }
        stats["coins"] -= cost
        stats["sponges_spent"] = int(stats.get("sponges_spent") or 0) + cost
        stats["golden_spatula_charges"] = (
            int(stats.get("golden_spatula_charges") or 0) + REWARD_BOOST_GAMES_PER_PURCHASE
        )
        save_data(bot.data)
        return {
            "ok": True,
            "bought": True,
            "label": item["label"],
            "cost": cost,
            "message": (
                f"Bought **{item['label']}**! **+50% career XP** on your next "
                f"{stats['golden_spatula_charges']} wins."
            ),
        }

    if tid not in SHOP_PINS:
        return {"ok": False, "bought": False, "message": "Unknown pin."}
    owned = owned_pin_ids(stats)
    if tid in owned:
        return {"ok": False, "bought": False, "message": "Already owned — it's on your border."}
    if stats["coins"] < cost:
        return {
            "ok": False,
            "bought": False,
            "message": (
                f"Need **{format_sponges(cost)}** "
                f"(you have {format_sponges(stats['coins'])})."
            ),
        }
    stats["coins"] -= cost
    stats["sponges_spent"] = int(stats.get("sponges_spent") or 0) + cost
    owned.append(tid)
    stats["owned_pins"] = owned
    stats["owned_themes"] = owned  # legacy mirror
    save_data(bot.data)
    push_cosmetics_sync(user_id, guild_id, stats)
    return {
        "ok": True,
        "bought": True,
        "label": item["label"],
        "cost": cost,
        "message": f"Bought pin **{item['label']}**!",
    }


def apply_gift_pin(
    bot: "SudokuBot",
    guild_id: int,
    from_user_id: int,
    to_user_id: int,
    pin_id: str,
) -> dict:
    """Transfer an owned paid pin from one player to another in the same guild."""
    if from_user_id == to_user_id:
        return {"ok": False, "message": "You already own that pin — pick someone else."}
    pin_id = resolve_pin_id(pin_id) or pin_id
    meta = SHOP_PINS.get(pin_id)
    if not meta or pin_id in SHOP_BOOST_KEYS:
        return {"ok": False, "message": "Unknown pin."}
    if int(meta.get("cost") or 0) <= 0:
        return {"ok": False, "message": "Free starter pins can't be gifted."}

    gstats = guild_stats(bot.data, guild_id)
    donor = user_stats(gstats, from_user_id)
    recv = user_stats(gstats, to_user_id)
    donor_owned = owned_pin_ids(donor)
    recv_owned = owned_pin_ids(recv)
    if pin_id not in donor_owned:
        return {"ok": False, "message": "You don't own that pin."}
    if pin_id in recv_owned:
        return {
            "ok": False,
            "message": f"They already have **{meta['label']}** — pick another gift.",
        }

    donor_owned = [p for p in donor_owned if p != pin_id]
    recv_owned = list(recv_owned) + [pin_id]
    donor["owned_pins"] = donor_owned
    donor["owned_themes"] = donor_owned
    recv["owned_pins"] = recv_owned
    recv["owned_themes"] = recv_owned
    save_data(bot.data)
    push_cosmetics_sync(from_user_id, guild_id, donor)
    push_cosmetics_sync(to_user_id, guild_id, recv)
    label = meta["label"]
    emoji = meta.get("emoji", WAVE)
    return {
        "ok": True,
        "label": label,
        "emoji": emoji,
        "pin_id": pin_id,
        "message": f"Gifted {emoji} **{label}**!",
    }


def shop_preview_file(stats: dict, item: dict) -> discord.File:
    """Easy board preview for a Pins catalog item (Lagoon colors + border pins)."""
    board, given, solution = make_puzzle("easy")
    title_id = equipped_title_id(stats)
    pins = list(owned_pin_emojis(stats))
    # Show the browsed pin even if not owned yet
    if item.get("kind") == "pin":
        emoji = item.get("emoji")
        if emoji and emoji not in pins:
            pins = pins + [emoji]
    image = render_board(
        board,
        given,
        solution=solution,
        difficulty="Easy",
        title_id=title_id,
        pin_emojis=pins,
        pin_seed=21,
    )
    return board_to_file(image)


class GiftPinUserView(discord.ui.View):
    """Ephemeral user picker to gift a pin from the shop (or /giftpin)."""

    def __init__(
        self,
        shop_view: "KrustyShopView | None",
        *,
        pin_id: str,
        pin_label: str,
        bot: "SudokuBot | None" = None,
        owner_id: int | None = None,
        guild_id: int | None = None,
    ):
        super().__init__(timeout=120)
        self.shop_view = shop_view
        self.pin_id = pin_id
        self.pin_label = pin_label
        self.bot = bot or (shop_view.bot if shop_view else None)
        self.owner_id = owner_id if owner_id is not None else (shop_view.owner_id if shop_view else 0)
        self.guild_id = guild_id if guild_id is not None else (shop_view.guild_id if shop_view else 0)
        select = discord.ui.UserSelect(
            placeholder="Who gets this pin?",
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_pick
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This gift picker isn't yours.",
                ephemeral=True,
            )
            return False
        return True

    async def on_pick(self, interaction: discord.Interaction) -> None:
        if not interaction.data:
            await interaction.response.defer()
            return
        values = interaction.data.get("values") or []
        if not values:
            await interaction.response.send_message("No player selected.", ephemeral=True)
            return
        try:
            to_id = int(values[0])
        except (TypeError, ValueError):
            await interaction.response.send_message("Invalid player.", ephemeral=True)
            return
        if interaction.guild is None or self.bot is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        member = interaction.guild.get_member(to_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(to_id)
            except discord.HTTPException:
                member = None
        if member is None or member.bot:
            await interaction.response.send_message(
                "Pick a real player in this server.",
                ephemeral=True,
            )
            return

        result = apply_gift_pin(
            self.bot, self.guild_id, self.owner_id, to_id, self.pin_id
        )
        if not result.get("ok"):
            await interaction.response.send_message(result["message"], ephemeral=True)
            return

        donor_stats = user_stats(guild_stats(self.bot.data, self.guild_id), self.owner_id)
        recv_stats = user_stats(guild_stats(self.bot.data, self.guild_id), to_id)
        await sync_cosmetics_to_activity_sessions(
            self.owner_id,
            self.guild_id,
            title_id=equipped_title_id(donor_stats),
            pin_emojis=owned_pin_emojis(donor_stats),
        )
        await sync_cosmetics_to_activity_sessions(
            to_id,
            self.guild_id,
            title_id=equipped_title_id(recv_stats),
            pin_emojis=owned_pin_emojis(recv_stats),
        )

        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

        emoji = result.get("emoji", WAVE)
        label = result.get("label", self.pin_label)
        await interaction.response.edit_message(
            content=f"🎁 Gifted {emoji} **{label}** to {member.mention}!",
            view=self,
        )

        if self.shop_view is not None:
            # Refresh shop: item may no longer be owned
            if self.shop_view.selected_id == self.pin_id:
                self.shop_view.selected_id = None
                self.shop_view._ensure_selection()
            self.shop_view._rebuild()
            try:
                if self.shop_view.message is not None:
                    await self.shop_view.message.edit(
                        embed=self.shop_view.build_embed(),
                        view=self.shop_view,
                        attachments=[],
                    )
            except discord.HTTPException:
                pass

        announce = (
            f"🎁 {interaction.user.mention} gifted {emoji} **{label}** "
            f"to {member.mention}!"
        )
        try:
            if interaction.channel is not None:
                await interaction.channel.send(announce)
        except discord.HTTPException:
            pass


class KrustyShopView(discord.ui.View):
    """Paginated catalog with Select + filters: Titles/Pins, Buy/Equip/Preview."""

    def __init__(
        self,
        bot: "SudokuBot",
        *,
        owner_id: int,
        guild_id: int,
        kind: str = "titles",
        filt: str = "all",
        page: int = 0,
        selected_id: str | None = None,
    ):
        super().__init__(timeout=600)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.kind = kind if kind in ("boosts", "titles", "pins") else "boosts"
        self.filt = filt if filt in ("all", "afford", "owned", "ocean", "crew") else "all"
        if self.kind != "pins" and self.filt in ("ocean", "crew"):
            self.filt = "all"
        self.page = max(0, page)
        self.selected_id = selected_id
        self.message: discord.Message | None = None
        self._ensure_selection()
        self._rebuild()

    def _stats(self) -> dict:
        gstats = guild_stats(self.bot.data, self.guild_id)
        return user_stats(gstats, self.owner_id)

    def catalog(self) -> list[dict]:
        return shop_catalog(self.kind)

    def filtered_catalog(self) -> list[dict]:
        return shop_filter_catalog(self.catalog(), self._stats(), self.filt)

    def page_count(self) -> int:
        total = len(self.filtered_catalog())
        return max(1, (total + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)

    def page_items(self) -> list[dict]:
        items = self.filtered_catalog()
        if not items:
            self.page = 0
            return []
        self.page %= self.page_count()
        start = self.page * SHOP_PAGE_SIZE
        return items[start : start + SHOP_PAGE_SIZE]

    def selected_item(self) -> dict | None:
        items = self.filtered_catalog()
        if not items:
            return None
        if self.selected_id:
            for it in items:
                if it["id"] == self.selected_id:
                    return it
        return items[0]

    def _ensure_selection(self) -> None:
        """Pick a sensible selected item when opening or after filter/tab changes."""
        stats = self._stats()
        items = self.filtered_catalog()
        if not items:
            self.selected_id = None
            self.page = 0
            return

        # Keep selection if still visible under filter
        if self.selected_id and any(it["id"] == self.selected_id for it in items):
            self._sync_page_to_selected(items)
            return

        # Prefer equipped title / first affordable / first item
        if self.kind == "titles":
            eq = equipped_title_id(stats)
            if eq and any(it["id"] == eq for it in items):
                self.selected_id = eq
                self._sync_page_to_selected(items)
                return
        for it in items:
            if shop_item_can_buy(stats, it):
                self.selected_id = it["id"]
                self._sync_page_to_selected(items)
                return
        self.selected_id = items[0]["id"]
        self._sync_page_to_selected(items)

    def _sync_page_to_selected(self, items: list[dict]) -> None:
        if not self.selected_id:
            self.page = 0
            return
        for i, it in enumerate(items):
            if it["id"] == self.selected_id:
                self.page = i // SHOP_PAGE_SIZE
                return
        self.page = 0

    def build_embed(self) -> discord.Embed:
        items = self.filtered_catalog()
        page_items = self.page_items()
        selected = self.selected_item()
        return shop_page_embed(
            stats=self._stats(),
            kind=self.kind,
            page_items=page_items,
            selected=selected,
            page=self.page,
            pages=self.page_count(),
            filt=self.filt,
            filtered_total=len(items),
        )

    def _rebuild(self) -> None:
        self.clear_items()
        stats = self._stats()
        page_items = self.page_items()
        selected = self.selected_item()
        owned = shop_item_owned(stats, selected) if selected else False

        # Row 0 — 3 Mobile Category Tabs
        boosts_btn = discord.ui.Button(
            label="🔮 Power-Ups",
            style=discord.ButtonStyle.primary if self.kind == "boosts" else discord.ButtonStyle.secondary,
            row=0,
        )
        titles_btn = discord.ui.Button(
            label="👑 Titles",
            style=discord.ButtonStyle.primary if self.kind == "titles" else discord.ButtonStyle.secondary,
            row=0,
        )
        pins_btn = discord.ui.Button(
            label="🎨 Pins",
            style=discord.ButtonStyle.primary if self.kind == "pins" else discord.ButtonStyle.secondary,
            row=0,
        )
        boosts_btn.callback = self.on_boosts
        titles_btn.callback = self.on_titles
        pins_btn.callback = self.on_pins
        self.add_item(boosts_btn)
        self.add_item(titles_btn)
        self.add_item(pins_btn)

        # Row 1 — filters (pins get Ocean/Crew themes)
        if self.kind == "pins":
            filter_defs = (
                ("all", "All"),
                ("ocean", "Ocean"),
                ("crew", "Crew"),
                ("afford", "Buy"),
                ("owned", "Owned"),
            )
        else:
            filter_defs = (
                ("all", "All"),
                ("afford", "Can buy"),
                ("owned", "Owned"),
            )
        for key, label in filter_defs:
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if self.filt == key else discord.ButtonStyle.secondary,
                row=1,
            )
            btn.callback = self._filter_cb(key)
            self.add_item(btn)

        # Row 2 — select current page items
        if page_items:
            options: list[discord.SelectOption] = []
            for it in page_items:
                status = shop_item_status_text(stats, it)
                if status == "🔒 Locked" and shop_item_can_buy(stats, it):
                    status = "⚡ Can Buy"
                if it.get("on_sale"):
                    status = f"🔥 {status}"
                price = shop_item_price_text(it)
                desc = f"{price} · {status}"[:100]
                label = it["label"][:100]
                options.append(
                    discord.SelectOption(
                        label=label,
                        value=it["id"],
                        description=desc,
                        emoji=shop_select_emoji(it.get("emoji")),
                        default=(it["id"] == (selected or {}).get("id")),
                    )
                )
            select = discord.ui.Select(
                placeholder="Choose an item…",
                options=options,
                row=2,
                min_values=1,
                max_values=1,
            )
            select.callback = self.on_select
            self.add_item(select)

        # Row 3 — page nav (page index on ▶ to cut clicks)
        pages = self.page_count()
        prev_btn = discord.ui.Button(
            label="◀",
            style=discord.ButtonStyle.secondary,
            row=3,
            disabled=pages <= 1 or self.page <= 0,
        )
        next_btn = discord.ui.Button(
            label=f"{self.page + 1}/{pages} ▶" if pages > 1 else "▶",
            style=discord.ButtonStyle.secondary,
            row=3,
            disabled=pages <= 1 or self.page >= pages - 1,
        )
        prev_btn.callback = self.on_prev
        next_btn.callback = self.on_next
        self.add_item(prev_btn)
        self.add_item(next_btn)

        # Row 4 — actions
        if selected is None:
            return
        if owned:
            if selected["kind"] == "pin":
                action = discord.ui.Button(
                    label="Owned ✓",
                    style=discord.ButtonStyle.success,
                    row=4,
                    disabled=True,
                )
                self.add_item(action)
                gift = discord.ui.Button(
                    label="Gift",
                    style=discord.ButtonStyle.primary,
                    row=4,
                )
                gift.callback = self.on_gift
                self.add_item(gift)
            else:
                action = discord.ui.Button(
                    label="Equip",
                    style=discord.ButtonStyle.success,
                    row=4,
                    disabled=shop_item_equipped(stats, selected),
                )
                action.callback = self.on_equip
                self.add_item(action)
        else:
            cost = int(selected["cost"])
            sale = "🔥 " if selected.get("on_sale") else ""
            action = discord.ui.Button(
                label=(
                    f"{sale}Buy ({cost} {SPONGE})"
                    if cost
                    else "Claim FREE"
                ),
                style=discord.ButtonStyle.danger,
                row=4,
            )
            action.callback = self.on_buy
            self.add_item(action)

        if self.kind == "pins":
            preview = discord.ui.Button(
                label="Preview",
                style=discord.ButtonStyle.secondary,
                row=4,
            )
            preview.callback = self.on_preview
            self.add_item(preview)

    def _filter_cb(self, key: str):
        async def _cb(interaction: discord.Interaction) -> None:
            self.filt = key
            self.page = 0
            self.selected_id = None
            self._ensure_selection()
            await self._refresh(interaction)

        return _cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This Krusty Shop ticket isn't yours — open `/shop`.",
                ephemeral=True,
            )
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        # Acknowledge immediately so Discord never hits the 3-second timeout.
        # After defer(), edit_original_response() updates the ephemeral shop message.
        if not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
        self._rebuild()
        try:
            await interaction.edit_original_response(
                embed=self.build_embed(), view=self, attachments=[]
            )
        except discord.HTTPException as exc:
            print(f"KrustyShopView refresh failed: {type(exc).__name__}: {exc}")
            # Keep the in-memory view usable — try a minimal rebuild without select emojis.
            try:
                for child in list(self.children):
                    if isinstance(child, discord.ui.Select):
                        for opt in child.options:
                            opt.emoji = None
                await interaction.edit_original_response(
                    embed=self.build_embed(), view=self, attachments=[]
                )
            except discord.HTTPException as exc2:
                print(f"KrustyShopView refresh retry failed: {type(exc2).__name__}: {exc2}")
                try:
                    await interaction.followup.send(
                        "Couldn't refresh the shop — run `/shop` again.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass

    async def on_boosts(self, interaction: discord.Interaction) -> None:
        self.kind = "boosts"
        self.page = 0
        self.selected_id = None
        self._ensure_selection()
        await self._refresh(interaction)

    async def on_titles(self, interaction: discord.Interaction) -> None:
        self.kind = "titles"
        self.page = 0
        self.selected_id = None
        self._ensure_selection()
        await self._refresh(interaction)

    async def on_pins(self, interaction: discord.Interaction) -> None:
        self.kind = "pins"
        self.page = 0
        self.selected_id = None
        self._ensure_selection()
        await self._refresh(interaction)

    async def on_prev(self, interaction: discord.Interaction) -> None:
        self.page = (self.page - 1) % self.page_count()
        page_items = self.page_items()
        if page_items:
            self.selected_id = page_items[0]["id"]
        await self._refresh(interaction)

    async def on_next(self, interaction: discord.Interaction) -> None:
        self.page = (self.page + 1) % self.page_count()
        page_items = self.page_items()
        if page_items:
            self.selected_id = page_items[0]["id"]
        await self._refresh(interaction)

    async def on_select(self, interaction: discord.Interaction) -> None:
        if not interaction.data or "values" not in interaction.data:
            await interaction.response.defer()
            return
        values = interaction.data.get("values") or []
        if values:
            self.selected_id = str(values[0])
        await self._refresh(interaction)

    async def on_equip(self, interaction: discord.Interaction) -> None:
        item = self.selected_item()
        if not item:
            await interaction.response.send_message("Nothing selected.", ephemeral=True)
            return
        result = apply_shop_equip(self.bot, self.guild_id, self.owner_id, item)
        if result.get("ok"):
            stats = self._stats()
            await sync_cosmetics_to_activity_sessions(
                self.owner_id,
                self.guild_id,
                title_id=equipped_title_id(stats),
                pin_emojis=owned_pin_emojis(stats),
            )
        self._rebuild()
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self, attachments=[]
        )
        await interaction.followup.send(result["message"], ephemeral=True)

    async def on_buy(self, interaction: discord.Interaction) -> None:
        item = self.selected_item()
        if not item:
            await interaction.response.send_message("Nothing selected.", ephemeral=True)
            return
        bought_id = item["id"]
        result = apply_shop_purchase(self.bot, self.guild_id, self.owner_id, item)
        if result.get("ok"):
            # Keep the purchased item selected and visible (show Owned).
            self.selected_id = bought_id
            if self.filt == "afford" and item.get("kind") in ("pin", "title"):
                self.filt = "owned" if item["kind"] == "pin" else "all"
            self._sync_page_to_selected(self.filtered_catalog())
            stats = self._stats()
            await sync_cosmetics_to_activity_sessions(
                self.owner_id,
                self.guild_id,
                title_id=equipped_title_id(stats),
                pin_emojis=owned_pin_emojis(stats),
            )
        self._rebuild()
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self, attachments=[]
        )
        if not result["ok"]:
            await interaction.followup.send(result["message"], ephemeral=True)
            return
        who = interaction.user.mention
        cost = int(result.get("cost") or 0)
        pocket = format_sponges(self._stats().get("coins", 0))
        owned_note = " · **Owned ✓**" if item.get("kind") == "pin" else ""
        announce = (
            f"{SPONGE} {who} bought **{result['label']}** "
            f"(−{cost} {SPONGE}) · pocket now **{pocket}**!{owned_note}"
        )
        try:
            if interaction.channel is not None:
                await interaction.channel.send(announce)
            else:
                await interaction.followup.send(announce)
        except discord.HTTPException:
            await interaction.followup.send(result["message"], ephemeral=True)

    async def on_gift(self, interaction: discord.Interaction) -> None:
        item = self.selected_item()
        if not item or item.get("kind") != "pin":
            await interaction.response.send_message("Select a pin first.", ephemeral=True)
            return
        if not shop_item_owned(self._stats(), item):
            await interaction.response.send_message("You don't own that pin.", ephemeral=True)
            return
        meta = SHOP_PINS.get(item["id"]) or {}
        if int(meta.get("cost") or 0) <= 0:
            await interaction.response.send_message(
                "Free starter pins can't be gifted.",
                ephemeral=True,
            )
            return
        view = GiftPinUserView(self, pin_id=item["id"], pin_label=item["label"])
        await interaction.response.send_message(
            f"🎁 Gift **{item['label']}** — pick a player:",
            view=view,
            ephemeral=True,
        )

    async def on_preview(self, interaction: discord.Interaction) -> None:
        if self.kind != "pins":
            await interaction.response.send_message(
                "Preview is for Pins — switch tabs.",
                ephemeral=True,
            )
            return
        item = self.selected_item()
        if not item:
            await interaction.response.send_message("Nothing selected.", ephemeral=True)
            return
        stats = self._stats()
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            file = shop_preview_file(stats, item)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                f"Couldn't render preview: {exc}", ephemeral=True
            )
            return
        await interaction.followup.send(
            content=f"{BUBBLE} Preview · **{item['label']}** (not a real game)",
            file=file,
            ephemeral=True,
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message is None:
            return
        try:
            embed = self.build_embed()
            embed.set_footer(text=f"{SPONGE} Shop closed — run /shop again")
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

def start_health_server_early() -> None:
    """Health + Activity static + OAuth/Mongo APIs on the same PORT."""
    from activity_http import start_unified_http_server

    start_unified_http_server(lambda: bot)


class SudokuBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.data = load_data()

    async def setup_hook(self) -> None:
        await match_store.connect()
        kind = type(match_store).__name__
        print(f"Challenge match store: {kind}")
        await restore_leaderboard_from_mongo(self)

        print("Slash tree: testboard uses autocomplete (no static pin choices).")
        self._log_slash_payload_limits()
        # Prefer guild sync. Global sync must preserve the Activities Entry Point
        # command (type 4) or Discord returns 50240.
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            try:
                guild_synced = await self.tree.sync(guild=guild)
                print(f"Synced {len(guild_synced)} slash command(s) to guild {DISCORD_GUILD_ID}.")
                # Remove global slash duplicates; keep only Activity Entry Point.
                await self._clear_global_slash_keep_entrypoint()
            except (app_commands.CommandSyncFailure, discord.Forbidden, discord.HTTPException) as exc:
                print(f"Guild command sync failed (continuing): {exc}")
                print(
                    "Hint: DISCORD_GUILD_ID tem de ser o servidor onde o bot está "
                    "(Developer Mode → clique direito no servidor → Copy Server ID)."
                )
                await self._sync_globals_preserving_entrypoint()
        else:
            await self._sync_globals_preserving_entrypoint()

    async def _clear_global_slash_keep_entrypoint(self) -> None:
        """Delete global CHAT_INPUT commands so guild copies aren't duplicated."""
        try:
            existing = await self.http.get_global_commands(self.application_id)
        except discord.HTTPException as exc:
            print(f"List global commands failed: {exc}")
            return
        removed = 0
        for cmd in existing:
            if int(cmd.get("type") or 0) == 4:
                continue  # keep Activity Entry Point / Launch
            cmd_id = cmd.get("id")
            if not cmd_id:
                continue
            try:
                await self.http.delete_global_command(self.application_id, int(cmd_id))
                removed += 1
            except discord.HTTPException as exc:
                print(f"Delete global /{cmd.get('name')} failed: {exc}")
        print(f"Removed {removed} global slash command(s); Entry Point kept.")

    async def _sync_globals_preserving_entrypoint(self) -> None:
        """Bulk-upsert slash commands without deleting the Activity Entry Point."""
        try:
            existing = await self.http.get_global_commands(self.application_id)
            entry_points = [cmd for cmd in existing if int(cmd.get("type") or 0) == 4]
            payload = [cmd.to_dict(self.tree) for cmd in self.tree.get_commands()]
            for ep in entry_points:
                kept = {
                    "name": ep.get("name") or "launch",
                    "type": 4,
                    "description": ep.get("description") or "",
                }
                if ep.get("id"):
                    kept["id"] = ep["id"]
                if ep.get("handler") is not None:
                    kept["handler"] = ep["handler"]
                if ep.get("integration_types") is not None:
                    kept["integration_types"] = ep["integration_types"]
                if ep.get("contexts") is not None:
                    kept["contexts"] = ep["contexts"]
                payload.append(kept)
            synced = await self.http.bulk_upsert_global_commands(self.application_id, payload)
            print(
                f"Synced {len(synced)} global command(s) "
                f"(kept {len(entry_points)} Activity Entry Point)."
            )
        except (app_commands.CommandSyncFailure, discord.HTTPException) as exc:
            print(f"Global command sync failed (continuing): {exc}")

    def _log_slash_payload_limits(self) -> None:
        """Warn before Discord rejects option choice lists over 25."""
        for cmd in self.tree.get_commands():
            try:
                payload = cmd.to_dict(self.tree)
            except Exception as exc:  # noqa: BLE001
                print(f"slash payload build failed for {getattr(cmd, 'name', cmd)}: {exc}")
                continue
            for opt in payload.get("options") or []:
                n = len(opt.get("choices") or [])
                if n > 25:
                    print(
                        f"WARNING: /{payload.get('name')} option '{opt.get('name')}' "
                        f"has {n} choices (Discord max 25)"
                    )


bot = SudokuBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """Never leave a slash command hanging on an uncaught exception."""
    root = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    print(f"app command error: {root}")
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need **Administrator** permission for that command."
    else:
        msg = "Something went wrong — try again in a moment."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


STATUS_ROTATION = [
    discord.Game(name=f"{SPONGE} /play · I'm ready!"),
    discord.Game(name=f"{WAVE} /daily · Pineapple puzzle"),
    discord.Game(name=f"{JELLY} /challenge · Jellyfish race"),
    discord.Game(name=f"{SPONGE} /shop · titles & pins"),
    discord.Game(name=f"📅 /weekly · bonus sponges"),
]
_status_i = 0


@tasks.loop(seconds=40)
async def rotate_status():
    global _status_i
    await bot.change_presence(activity=STATUS_ROTATION[_status_i % len(STATUS_ROTATION)])
    _status_i += 1


_last_announced_daily_date: str | None = None


async def broadcast_daily_announcement(target_channel_id: int | None = None) -> int:
    """Broadcast a Bikini Bottom embed announcing today's Daily Sudoku."""
    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    weekday = datetime.now(timezone.utc).weekday()
    diff_key = DAILY_WEEKDAY_DIFFICULTY.get(weekday, "medium")
    meta = DIFFICULTY_TIERS.get(diff_key, {})
    label = meta.get("label", "Medium")

    embed = paper_embed(f"{PINEAPPLE} New Daily Sudoku Available! ({now_date})")
    embed.description = (
        f"{WAVE} **Ahoy, Bikini Bottom residents!**\n\n"
        f"Today's Daily Sudoku is now being served at the Krusty Krab! 🍔"
    )
    embed.add_field(name="Difficulty", value=f"**{label}**", inline=True)
    embed.add_field(name="Daily Bonus", value=f"**+{DAILY_BONUS} Sponges {SPONGE}**", inline=True)
    deal = daily_bundle_pin(now_date)
    title_deal = daily_bundle_title(now_date)
    deal_bits = []
    if deal:
        deal_bits.append(
            f"🎨 {deal['emoji']} **{deal['label']}** — {shop_item_price_text(deal)}"
        )
    if title_deal:
        deal_bits.append(
            f"👑 {title_deal['emoji']} **{title_deal['label']}** — {shop_item_price_text(title_deal)}"
        )
    if deal_bits:
        embed.add_field(
            name="🏷️ Shop Deals of the Day",
            value=(
                "\n".join(deal_bits)
                + f"\nOpen `/shop` → **Pins** / **Titles** (50% off until next UTC midnight)"
            ),
            inline=False,
        )
    embed.add_field(
        name="📅 Weekly Goals",
        value="Check `/weekly` — clear dailies, boards, and a challenge for bonus sponges.",
        inline=False,
    )
    embed.add_field(
        name="How to Play",
        value="Type `/daily` in chat or launch the game using the **Activity** button in Discord!",
        inline=False,
    )

    channels_to_notify: set[int] = set()
    if target_channel_id:
        channels_to_notify.add(target_channel_id)
    elif DAILY_ANNOUNCE_CHANNEL_ID:
        channels_to_notify.add(DAILY_ANNOUNCE_CHANNEL_ID)
    elif ACTIVITY_WATCH_CHANNEL_ID:
        channels_to_notify.add(ACTIVITY_WATCH_CHANNEL_ID)

    try:
        guilds_data = bot.data.get("guilds", {}) if hasattr(bot, "data") else {}
        for g_id, g_info in guilds_data.items():
            if isinstance(g_info, dict) and g_info.get("daily_channel_id"):
                try:
                    channels_to_notify.add(int(g_info["daily_channel_id"]))
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass

    sent_count = 0
    for ch_id in channels_to_notify:
        if not ch_id:
            continue
        try:
            ch = bot.get_channel(ch_id)
            if ch is None:
                ch = await bot.fetch_channel(ch_id)
            if ch:
                await ch.send(embed=embed)
                sent_count += 1
        except Exception as exc:
            print(f"[DailyAnnouncement] Failed to send to channel {ch_id}: {exc}")
    return sent_count


@tasks.loop(minutes=3)
async def prune_idle_activity_watch_announcements():
    """Remove channel 'is playing' posts when the board has been idle too long.

    Keeps the Activity session/board so the player can still resume with `/play`.
    For admin wipe of idle sessions (no resume), use `/z-admin clearstale`.
    """
    try:
        stale = await match_store.list_idle_activity_watch_sessions(
            WATCH_IDLE_HIDE_SEC
        )
    except Exception as exc:  # noqa: BLE001
        print(f"prune_idle_activity_watch list failed: {exc}")
        return
    for session in stale:
        sid = str(session.get("_id") or "")
        if not sid:
            continue
        try:
            await end_activity_watch(bot, sid, force=True)
            print(f"pruned idle activity watch announcement for {sid}")
        except Exception as exc:  # noqa: BLE001
            print(f"prune_idle_activity_watch end failed for {sid}: {exc}")


@prune_idle_activity_watch_announcements.before_loop
async def _wait_prune_idle_watch_ready():
    await bot.wait_until_ready()


@tasks.loop(minutes=2)
async def check_daily_announcement():
    global _last_announced_daily_date
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_announced = (
        bot.data.get("last_announced_daily_date")
        if hasattr(bot, "data") and isinstance(bot.data, dict)
        else _last_announced_daily_date
    )

    if last_announced != today_str:
        _last_announced_daily_date = today_str
        if hasattr(bot, "data") and isinstance(bot.data, dict):
            bot.data["last_announced_daily_date"] = today_str
            save_data(bot.data)
        print(f"[DailyAnnouncement] New UTC day detected: {today_str}. Broadcasting daily announcement...")
        sent = await broadcast_daily_announcement()
        print(f"[DailyAnnouncement] Broadcast complete. Sent to {sent} channel(s).")


@check_daily_announcement.before_loop
async def _wait_daily_announce_ready():
    await bot.wait_until_ready()


# Slash commands: Discord sorts A–Z in the picker. Admin tools live under z-admin (sorts last).
admin_group = app_commands.Group(
    name="z-admin",
    description="Bot admin tools (server staff)",
)


@admin_group.command(
    name="setdailychannel",
    description="Set or clear channel for automatic Daily Sudoku announcements",
)
@app_commands.describe(channel="Channel to announce the Daily Sudoku at 00:00 UTC (leave empty to disable)")
@app_commands.checks.has_permissions(administrator=True)
async def setdailychannel_cmd(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    gstats = guild_stats(bot.data, interaction.guild.id)
    if channel is None:
        gstats["daily_channel_id"] = None
        save_data(bot.data)
        await interaction.response.send_message("Disabled automatic Daily Sudoku announcements in this server.", ephemeral=True)
    else:
        gstats["daily_channel_id"] = channel.id
        save_data(bot.data)
        await interaction.response.send_message(f"Daily Sudoku announcements set to channel {channel.mention}! {PINEAPPLE}", ephemeral=True)


async def reply_ephemeral(interaction: discord.Interaction, content: str) -> None:
    """Send an ephemeral reply whether or not the interaction was already deferred."""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def require_bot_admin(interaction: discord.Interaction) -> bool:
    """Ephemeral deny unless the caller is a hard-coded bot admin. True = allowed."""
    if is_bot_admin(interaction.user.id):
        return True
    await reply_ephemeral(
        interaction,
        "Only the bot admins can use this command.",
    )
    return False


async def restore_challenge_games_from_match(bot: "SudokuBot", match: dict) -> bool:
    """Rebuild in-memory challenge boards when a match is active but games were lost on restart."""
    mid = match.get("_id")
    if not mid or match.get("status") != "active":
        return False
    guild_id = int(match.get("guild_id") or 0)
    if not guild_id:
        return False
    given = match.get("given")
    solution = match.get("solution")
    template = match.get("board_template")
    if not given or not solution:
        return False

    start_time = float(match.get("start_time") or time.time())
    diff = match.get("difficulty") or DEFAULT_DIFFICULTY
    restored_any = False

    for slot, player in match_player_entries(match):
        if player.get("forfeit") or player.get("finished_time") is not None:
            continue
        uid = int(player.get("user_id") or 0)
        if not uid:
            continue
        key = challenge_game_key(mid, uid)
        if key in games:
            restored_any = True
            continue

        raw_board = player.get("current_board") or template
        if not raw_board:
            continue
        # Prefer parent text channel for Activity launch; keep thread_id separately.
        channel_id = match.get("channel_id") or player.get("thread_id")
        if not channel_id:
            continue

        board = normalize_board(copy_grid(raw_board))
        pstats = user_stats(guild_stats(bot.data, guild_id), uid)
        owner_name = player.get("name") or pstats.get("name") or "Unknown"
        games[key] = new_game_state(
            mode="challenge",
            board=board,
            given=given,
            solution=solution,
            owner_id=uid,
            owner_name=owner_name,
            owner_title=equipped_title_id(pstats),
            channel_id=int(channel_id),
            guild_id=guild_id,
            match_id=mid,
            player_slot=slot,
            difficulty=diff,
            started_at=start_time,
            pin_emojis=owned_pin_emojis(pstats),
        )
        if player.get("thread_id"):
            games[key]["thread_id"] = int(player["thread_id"])
        games[key]["hints_used"] = int(player.get("hints_used") or 0)
        games[key]["hints_gary_used"] = int(player.get("hints_gary_used") or 0)
        if int(player.get("gary_wisdom_bonus") or 0) > 0:
            games[key]["gary_wisdom_bonus"] = int(player["gary_wisdom_bonus"])
        await persist_game(key, games[key])
        restored_any = True
        print(f"Rehydrated challenge game {serialize_game_key(key)} from match {mid}")

    return restored_any


async def resolve_challenge_launch_channel(
    bot: "SudokuBot",
    game: dict,
    match_id: str | None = None,
) -> discord.TextChannel | None:
    """Parent text channel where Play-in-Activity must be posted (not private threads)."""
    channel = await resolve_channel(bot, game.get("channel_id"))
    if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.TextChannel):
        return channel.parent
    if isinstance(channel, discord.TextChannel):
        return channel

    mid = match_id or game.get("match_id")
    if mid:
        try:
            match = await match_store.get_match(str(mid))
        except Exception:
            match = None
        if match:
            home = await resolve_channel(bot, match.get("channel_id"))
            if isinstance(home, discord.Thread) and isinstance(home.parent, discord.TextChannel):
                return home.parent
            if isinstance(home, discord.TextChannel):
                return home

    if ACTIVITY_WATCH_CHANNEL_ID:
        home = await resolve_channel(bot, ACTIVITY_WATCH_CHANNEL_ID)
        if isinstance(home, discord.TextChannel):
            return home
    return None


async def reattach_challenge_launch_panels(bot: "SudokuBot", match_id: str) -> int:
    """Re-post one shared Activity Play button for a rehydrated challenge match."""
    match_games: list[tuple[tuple, dict]] = []
    for key, game in list(games.items()):
        if not (
            isinstance(key, tuple)
            and len(key) >= 3
            and key[0] == "ch"
            and key[1] == match_id
            and game.get("mode") == "challenge"
        ):
            continue
        match_games.append((key, game))
    if not match_games:
        return 0

    sample_game = match_games[0][1]
    channel = await resolve_challenge_launch_channel(bot, sample_game, match_id)
    if channel is None:
        print(
            f"Challenge match {match_id} launch channel missing "
            f"(was {sample_game.get('channel_id')}) — keeping match, use /quit"
        )
        return len(match_games)

    for key, game in match_games:
        game["channel_id"] = channel.id

    launch_view = ChallengeLaunchActivityView()
    launch_message_id = None
    try:
        match = await match_store.get_match(str(match_id))
    except Exception:  # noqa: BLE001
        match = None
    if match and match.get("launch_message_id"):
        launch_message_id = int(match["launch_message_id"])
    elif sample_game.get("message_id"):
        launch_message_id = int(sample_game["message_id"])

    reattached = False
    if launch_message_id:
        try:
            msg = await channel.fetch_message(launch_message_id)
            await msg.edit(view=launch_view)
            reattached = True
        except discord.HTTPException:
            reattached = False

    if not reattached:
        try:
            tier = difficulty_label(sample_game.get("difficulty"))
            mentions = " ".join(
                f"<@{g.get('owner_id')}>" for _, g in match_games if g.get("owner_id")
            )
            launch_msg = await channel.send(
                f"🏁 Speedrun · **{tier}**\n"
                f"{mentions}\n"
                "Tap **Play in Activity** below (this channel — not a thread)!",
                view=launch_view,
            )
            launch_message_id = launch_msg.id
            try:
                await match_store.update_match(
                    str(match_id), {"launch_message_id": launch_message_id}
                )
            except Exception as exc:  # noqa: BLE001
                print(f"challenge launch_message_id save failed: {exc}")
            print(
                f"challenge shared launch re-posted in #{getattr(channel, 'name', channel.id)} "
                f"for match {match_id}"
            )
        except discord.HTTPException as exc:
            print(f"challenge launch re-post failed for match {match_id}: {exc}")
            for key, game in match_games:
                await persist_game(key, game)
            return len(match_games)

    for key, game in match_games:
        game["message_id"] = launch_message_id
        await persist_game(key, game)
    return len(match_games)


async def restore_persisted_sessions(bot: "SudokuBot") -> None:
    """Reload active boards after a bot restart and reattach controls."""
    try:
        docs = await match_store.list_active_games()
    except Exception as exc:  # noqa: BLE001
        print(f"restore list failed: {exc}")
        return

    restored = 0
    dropped = 0
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
        # Only preserve rewarded if it was explicitly saved — never infer from solved board.
        if not is_solved(game.get("board") or [], game.get("solution")):
            game.pop("rewarded", None)

        # Rehydrate cosmetics from current inventory (themes→pins era safe)
        try:
            owner_id = int(game.get("owner_id"))
            guild_id = int(game.get("guild_id"))
            pstats = user_stats(guild_stats(bot.data, guild_id), owner_id)
            game["pin_emojis"] = owned_pin_emojis(pstats)
            if not game.get("owner_title"):
                game["owner_title"] = equipped_title_id(pstats)
        except (TypeError, ValueError):
            game.setdefault("pin_emojis", [])

        channel = await resolve_channel(bot, game.get("channel_id"))

        # Challenge boards live in Activity; one shared Play button is reattached
        # per match after all boards are loaded (see active_matches loop below).
        if game.get("mode") == "challenge":
            mid = game.get("match_id")
            if not mid and isinstance(key, tuple) and len(key) >= 2:
                mid = key[1]
            match_doc = None
            if mid:
                try:
                    match_doc = await match_store.get_match(str(mid))
                except Exception as exc:  # noqa: BLE001
                    print(f"restore challenge get_match failed for {mid}: {exc}")
                    match_doc = None
            # Drop boards for settled races / finished players so they never block /play.
            if (
                not match_doc
                or match_doc.get("status") == "finished"
                or match_doc.get("rewards_applied")
            ):
                print(
                    f"Dropping settled/orphan challenge session "
                    f"{serialize_game_key(key)} (match={mid})"
                )
                await drop_persisted_game(key, game)
                dropped += 1
                continue
            try:
                owner_id = int(game.get("owner_id") or 0)
            except (TypeError, ValueError):
                owner_id = 0
            player_done = False
            for _slot, player in match_player_entries(match_doc):
                if int(player.get("user_id") or 0) != owner_id:
                    continue
                if player.get("forfeit") or player.get("finished_time") is not None:
                    player_done = True
                break
            else:
                # Owner missing from roster
                player_done = True
            if player_done:
                print(
                    f"Dropping finished-player challenge session "
                    f"{serialize_game_key(key)} (match={mid})"
                )
                await drop_persisted_game(key, game)
                dropped += 1
                continue
            launch_ch = await resolve_challenge_launch_channel(bot, game, mid)
            if launch_ch is not None and game.get("channel_id") != launch_ch.id:
                print(
                    f"Challenge session {serialize_game_key(key)} remapped launch "
                    f"{game.get('channel_id')} → {launch_ch.id}"
                )
                game["channel_id"] = launch_ch.id
            elif launch_ch is None:
                print(
                    f"Challenge session {serialize_game_key(key)} launch channel missing "
                    f"(was {game.get('channel_id')}) — keeping match, use /quit"
                )
            # Normalize key so later remove_game(challenge_game_key(...)) always hits.
            if owner_id and mid:
                key = challenge_game_key(str(mid), owner_id)
                game["match_id"] = str(mid)
                game["owner_id"] = owner_id
            games[key] = game
            restored += 1
            continue

        if channel is None or not game.get("message_id"):
            # Dead panel (deleted thread / lost DM) — don't block /challenge forever
            print(
                f"Dropping unrecoverable session {serialize_game_key(key)} "
                f"(channel={game.get('channel_id')})"
            )
            await drop_persisted_game(key, game)
            dropped += 1
            continue

        games[key] = game
        if game.get("mode") == "daily":
            try:
                await sync_daily_watch_session(key, game)
            except Exception as exc:  # noqa: BLE001
                print(f"sync_daily_watch_session on restore failed: {exc}")
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
        except discord.HTTPException as exc:
            # Keep in memory so /quit still works; player may Refresh later
            print(f"reattach panel failed for {serialize_game_key(key)}: {exc}")

    # Abandon / repair challenge matches after restart
    try:
        active_matches = await match_store.list_matches(status="active")
    except Exception as exc:  # noqa: BLE001
        print(f"list active matches failed: {exc}")
        active_matches = []
    try:
        settling_matches = await match_store.list_matches(status="settling")
    except Exception as exc:  # noqa: BLE001
        print(f"list settling matches failed: {exc}")
        settling_matches = []

    stale_after = float(ACTIVITY_BLOCKING_MAX_AGE_SEC)  # 2h
    seen_ids: set[str] = set()
    for match in list(active_matches) + list(settling_matches):
        mid = match.get("_id")
        if not mid or str(mid) in seen_ids:
            continue
        seen_ids.add(str(mid))
        try:
            fresh = await match_store.get_match(mid)
        except Exception:
            fresh = match
        if not fresh:
            continue
        if fresh.get("rewards_applied") or fresh.get("status") == "finished":
            await purge_challenge_games_for_match(str(mid), fresh)
            continue

        age = time.time() - float(fresh.get("start_time") or 0)
        if age > stale_after:
            print(f"challenge match {mid} stale ({int(age)}s) — settling abandoned")
            for slot, player in match_player_entries(fresh):
                if player.get("forfeit") or player.get("finished_time") is not None:
                    continue
                try:
                    await match_store.try_claim_player_forfeit(mid, slot)
                except Exception as ff_exc:  # noqa: BLE001
                    print(f"stale forfeit {mid}/{slot} failed: {ff_exc}")
                uid = int(player.get("user_id") or 0)
                if uid:
                    ck = challenge_game_key(str(mid), uid)
                    if ck in games:
                        await remove_game(ck)
            try:
                fresh = await match_store.get_match(mid)
            except Exception:
                fresh = None
            if fresh:
                await settle_challenge_match(
                    bot,
                    fresh,
                    reason="restart — stale match",
                    settle_stale_after_sec=0,
                )
            continue

        any_live = any(
            isinstance(k, tuple) and len(k) >= 3 and k[0] == "ch" and k[1] == mid
            for k in games
        )
        if fresh and challenge_ready_to_settle(fresh):
            await settle_challenge_match(
                bot,
                fresh,
                reason="restart — ready to settle",
                settle_stale_after_sec=0,
            )
            continue
        if fresh.get("status") == "settling":
            # Mid-settle crash leftover — only retry if ready, else return to active.
            if challenge_ready_to_settle(fresh):
                await settle_challenge_match(
                    bot,
                    fresh,
                    reason="restart — resume settling",
                    settle_stale_after_sec=0,
                )
            else:
                try:
                    await match_store.update_match(
                        mid, {"status": "active", "settle_started_at": None}
                    )
                except Exception as reset_exc:  # noqa: BLE001
                    print(f"reset settling match {mid} failed: {reset_exc}")
            continue
        if not any_live:
            rehydrated = await restore_challenge_games_from_match(bot, fresh)
            if rehydrated:
                await reattach_challenge_launch_panels(bot, mid)
                schedule_challenge_live_update(mid)
                print(f"challenge match {mid} rehydrated after restart")
            else:
                print(
                    f"challenge match {mid} has no live boards after restart "
                    f"(could not rehydrate); auto-forfeiting unfinished players"
                )
                for slot, player in match_player_entries(fresh):
                    if player.get("forfeit") or player.get("finished_time") is not None:
                        continue
                    try:
                        await match_store.try_claim_player_forfeit(mid, slot)
                    except Exception as ff_exc:  # noqa: BLE001
                        print(f"restart forfeit {mid}/{slot} failed: {ff_exc}")
                try:
                    fresh = await match_store.get_match(mid)
                except Exception:
                    fresh = None
                if fresh and challenge_ready_to_settle(fresh):
                    await settle_challenge_match(
                        bot,
                        fresh,
                        reason="restart — abandoned",
                        settle_stale_after_sec=0,
                    )
                else:
                    schedule_challenge_live_update(mid)
        else:
            # Live boards may still point at a dead private thread — remap Play to home.
            await reattach_challenge_launch_panels(bot, mid)
            schedule_challenge_live_update(mid)

    print(
        f"Restored {restored} active game panel(s); dropped {dropped} unrecoverable; "
        f"{len(games)} session(s) in memory."
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Activity watch channel id: {ACTIVITY_WATCH_CHANNEL_ID or 'unset'}")
    if not os.getenv("MONGODB_URI", "").strip():
        print(
            "WARNING: MONGODB_URI unset — in-memory store only; "
            "do not run multiple bot instances (locks/claims are not shared)."
        )
    if not rotate_status.is_running():
        rotate_status.start()
    if not check_daily_announcement.is_running():
        check_daily_announcement.start()
    if not prune_idle_activity_watch_announcements.is_running():
        prune_idle_activity_watch_announcements.start()
    await bot.change_presence(activity=STATUS_ROTATION[0])
    await restore_persisted_sessions(bot)
    await restore_challenge_watch_views(bot)
    await restore_activity_play_watch_views(bot)
    bot.add_view(ChallengeLaunchActivityView())


@admin_group.command(
    name="testboard",
    description="Preview board pins/cosmetics (dev sample — not a real game)",
)
@app_commands.describe(
    title="Sample title id (default: Goofy Goober) — type to search",
    pin="Extra border pin id (default: Coral) — type to search",
)
async def testboard_cmd(
    interaction: discord.Interaction,
    title: str | None = None,
    pin: str | None = None,
):
    """Ephemeral preview so you can check cosmetic pins without starting a game."""
    if not await require_bot_admin(interaction):
        return
    title_id = title if title in SHOP_TITLES else "sudoku_pro"
    pin_id = pin if pin in SHOP_PINS else "coral"
    # Fake a small collection of owned cosmetics so the border fills with emoji pins
    sample_pins = [
        SHOP_TITLES[title_id]["emoji"],
        SHOP_PINS[pin_id]["emoji"],
        SHOP_TITLES["legend"]["emoji"],
        SHOP_TITLES["neptune"]["emoji"],
        SHOP_PINS["crab"]["emoji"],
        SHOP_TITLES["dutchman"]["emoji"],
    ]
    # Dedupe while preserving order
    seen: set[str] = set()
    pin_emojis = []
    for e in sample_pins:
        if e not in seen:
            pin_emojis.append(e)
            seen.add(e)
    board, given, solution = make_puzzle("easy")
    # Sprinkle a few pencil marks so notes are visible in the preview
    for r, c, marks in ((0, 1, [2, 5]), (4, 4, [1, 3, 7]), (8, 7, [4, 9])):
        if not given[r][c] and cell_value(board, r, c) == 0:
            board[r][c]["pencil_marks"] = marks
    image = render_board(
        board,
        given,
        solution=solution,
        difficulty="Easy",
        title_id=title_id,
        selected=(4, 4),
        highlight_box=4,
        pin_emojis=pin_emojis,
        pin_seed=42,
    )
    await interaction.response.send_message(
        content=(
            f"{BUBBLE} **Pin preview** (not a real game)\n"
            f"Border pins: {' '.join(pin_emojis)}\n"
            f"Board colors stay Lagoon Classic."
        ),
        file=board_to_file(image),
        ephemeral=True,
    )


def _catalog_autocomplete(
    current: str, catalog: dict[str, dict]
) -> list[app_commands.Choice[str]]:
    """Discord allows at most 25 autocomplete / choice results."""
    cur = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []
    for tid, meta in catalog.items():
        label = str(meta.get("label") or meta.get("pin") or tid)
        pin = str(meta.get("pin") or tid)
        hay = f"{tid} {label} {pin}".lower()
        if cur and cur not in hay:
            continue
        out.append(app_commands.Choice(name=label[:100], value=tid))
        if len(out) >= 25:
            break
    return out


@testboard_cmd.autocomplete("title")
async def testboard_title_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    _ = interaction
    return _catalog_autocomplete(current, SHOP_TITLES)


@testboard_cmd.autocomplete("pin")
async def testboard_pin_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    _ = interaction
    return _catalog_autocomplete(current, SHOP_PINS)


async def _launch_activity_window(
    interaction: discord.Interaction,
    *,
    preferred_diff_index: int | None = None,
) -> None:
    """Open the Embedded App Activity (Wordle-style game window).

    If preferred_diff_index is given, persist it so the Activity starts at that
    tier. An in-progress /play board is kept only when it already matches the
    chosen difficulty; otherwise the old board (including activity:0 orphans)
    is cleared so Medium leftovers cannot shadow Very Easy, etc.
    """
    # Write diff preference before launching so the Activity picks it up on load.
    if preferred_diff_index is not None and interaction.guild is not None:
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        session_id = f"activity:{guild_id}:{user_id}"
        try:
            idx = max(0, min(len(DIFF_KEYS_LIST) - 1, int(preferred_diff_index)))
        except (TypeError, ValueError):
            idx = DIFF_KEYS_LIST.index(DEFAULT_DIFFICULTY)
        diff_key = DIFF_KEYS_LIST[idx]
        existing, existing_sid = await lookup_user_activity_session(guild_id, user_id)
        has_board = bool(
            existing and (existing.get("board") or existing.get("solution"))
        )
        existing_kind = (existing or {}).get("session_kind") or "play"
        # Old sessions may lack difficulty fields — treat as medium (historical default).
        existing_key = session_difficulty_key(existing) or DEFAULT_DIFFICULTY
        same_play_board = (
            has_board
            and existing_kind == "play"
            and existing_key == diff_key
        )
        if same_play_board:
            # Resume the matching in-progress /play puzzle.
            pass
        else:
            # Preference / fresh start — never touch watch flags here. Clearing
            # them without deleting the Discord message orphans "is playing" posts.
            pref: dict = {
                "diff_index": idx,
                "difficulty": diff_key,
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "session_kind": "play",
                "board": None,
                "given": None,
                "solution": None,
                "won_at": None,
                "filled": 0,
                "elapsed": 0,
                "hints_used": 0,
                "hints_gary_used": 0,
                "gary_wisdom_bonus": 0,
            }
            await match_store.merge_activity_session(session_id, pref)
            # Orphan activity:0 boards win over preference-only primary in lookup —
            # drop stale play orphans so the new /play choice is not shadowed.
            orphan_id = f"activity:0:{user_id}"
            if orphan_id != session_id:
                try:
                    orphan = await match_store.get_activity_session(orphan_id)
                    if orphan and (orphan.get("session_kind") or "play") == "play":
                        await match_store.delete_activity_session(orphan_id)
                except Exception as orphan_exc:  # noqa: BLE001
                    print(f"clear play orphan after /play pref failed: {orphan_exc}")
            # If lookup pointed at a non-canonical play doc with a board, clear it too.
            if (
                existing_sid
                and existing_sid not in (session_id, orphan_id)
                and existing_kind == "play"
                and has_board
            ):
                try:
                    await match_store.merge_activity_session(
                        existing_sid,
                        {
                            "board": None,
                            "given": None,
                            "solution": None,
                            "won_at": None,
                            "filled": 0,
                            "diff_index": idx,
                            "difficulty": diff_key,
                            "session_kind": "play",
                        },
                    )
                except Exception as alt_exc:  # noqa: BLE001
                    print(f"clear alt play session after /play pref failed: {alt_exc}")
    try:
        await interaction.response.launch_activity()
        print(
            f"launch_activity ok user={interaction.user} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"channel={getattr(interaction.channel, 'id', None)}"
        )
        return
    except Exception as exc:  # noqa: BLE001 — always acknowledge the interaction
        print(f"launch_activity failed: {type(exc).__name__}: {exc}")
        code = getattr(exc, "code", None)
        if code == 50234:
            tip = (
                "A app ainda **não tem Activities/EMBEDDED** ligado.\n"
                "No [Developer Portal](https://discord.com/developers/applications):\n"
                "1. Escolhe a app **Thcoku**\n"
                "2. **Activities → URL Mappings**: `/` → `sudoku-squarepants.onrender.com` "
                "(sem `https://`)\n"
                "3. Também `/pyscript` → `pyscript.net` e `/jsdelivr` → `cdn.jsdelivr.net`\n"
                "4. **Activities → Settings** → ativa **Enable Activities**\n"
                "5. Reinicia o Discord e tenta `/play` outra vez"
            )
        else:
            tip = (
                "Não consegui abrir a janela da Activity.\n"
                "Confirma **Activities → Enable** e URL Mapping `/` → "
                "`sudoku-squarepants.onrender.com`.\n"
                "Ou inicia a Activity num **canal de voz** (ícone Actividades)."
            )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(tip, ephemeral=True)
        else:
            await interaction.response.send_message(tip, ephemeral=True)
    except discord.HTTPException as send_exc:
        print(f"launch_activity fallback reply failed: {send_exc}")


@bot.tree.command(
    name="play",
    description="Pick a difficulty, then open the Thcoku game window",
)
@app_commands.describe(difficulty="Required — choose the level before the game opens")
@app_commands.choices(difficulty=DIFFICULTY_CHOICES)
async def play_cmd(
    interaction: discord.Interaction,
    difficulty: app_commands.Choice[str],
):
    if interaction.guild is not None:
        # Allow reopening an in-progress /play Activity (resume). Only daily
        # shares the same window and must be finished or quit first.
        blocking = await get_blocking_activity_session(
            interaction.guild.id,
            interaction.user.id,
            kinds={"daily"},
        )
        if blocking:
            await interaction.response.send_message(
                "Finish or `/quit` today's **daily** first — it uses the same game window.",
                ephemeral=True,
            )
            return
        daily_block = await daily_attempt_blocks_modes(
            interaction.guild.id, interaction.user.id
        )
        if daily_block:
            await interaction.response.send_message(daily_block, ephemeral=True)
            return
        if await reconcile_challenge_game_for_user(interaction.user.id):
            await interaction.response.send_message(
                "Finish your speedrun challenge first.",
                ephemeral=True,
            )
            return
        block = await challenge_blocks_user(interaction.user.id)
        if block:
            await interaction.response.send_message(block, ephemeral=True)
            return
        sk = solo_key(interaction.guild.id, interaction.user.id)
        if sk in games:
            existing = games[sk]
            await interaction.response.send_message(
                f"Finish your **{existing.get('mode', 'solo')}** game first (`/quit`).",
                ephemeral=True,
            )
            return
    diff_idx = difficulty_index(difficulty.value)
    await _launch_activity_window(interaction, preferred_diff_index=diff_idx)


@bot.tree.command(
    name="watch",
    description="Spectate active /play, /daily, and challenge races",
)
async def watch_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    try:
        sessions = await match_store.list_activity_sessions(
            guild_id,
            max_age_sec=WATCH_LIST_MAX_AGE_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"/watch list_activity_sessions failed: {exc}")
        sessions = []

    # Prefer sessions that still have a board to spectate.
    playable = [
        s for s in sessions
        if activity_session_spectatable(s)
        and activity_session_watch_visible(s)
    ]

    # Also surface active challenge matches in this guild.
    challenge_matches: list[dict] = []
    try:
        matches = await match_store.list_matches(status="active")
    except Exception:
        matches = []
    for match in matches:
        if int(match.get("guild_id") or 0) != guild_id:
            continue
        challenge_matches.append(match)

    if not playable and not challenge_matches:
        await interaction.followup.send(
            "Nobody is playing right now. Start with `/play`, `/daily`, or `/challenge`.",
            ephemeral=True,
        )
        return

    embed = (
        build_activity_live_embed(playable, interaction.guild)
        if playable
        else paper_embed("Live challenges", description="Pick a race below to spectate.")
    )
    if challenge_matches:
        challenge_lines: list[str] = []
        for m in challenge_matches[:5]:
            player_sessions = await activity_sessions_for_challenge(m)
            suffix = format_challenge_watchers_suffix(
                m, player_sessions, interaction.guild
            )
            challenge_lines.append(
                f"🏁 **{difficulty_label(m.get('difficulty'))}** · "
                f"{len(match_player_entries(m))} players{suffix}"
            )
        embed.add_field(
            name="Challenges",
            value="\n".join(challenge_lines),
            inline=False,
        )
    view: ActivityWatchMenuView | None = None
    if playable or challenge_matches:
        view = build_activity_watch_view(
            guild_id,
            interaction.channel_id,
            bot,
            playable,
        )
        view.rebuild_challenge_buttons(challenge_matches)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="help", description="I'm ready! How to play Bikini Bottom Sudoku")
async def help_cmd(interaction: discord.Interaction):
    tiers = " · ".join(
        f"{meta['label']} ×{meta['multiplier']:.2f}"
        for meta in DIFFICULTY_TIERS.values()
    )
    embed = paper_embed(f"{SPONGE} Sudoku · Bikini Bottom")
    embed.description = (
        f"{WAVE} Ahoy, neighbor!\n"
        f"Fill **1–9** in every row, column, and box.\n"
        f"Earn **sponges** {SPONGE} — no duplicate numbers, only vibes."
    )
    embed.add_field(
        name=f"{BUBBLE} Play",
        value=(
            "`/play` — pick a **difficulty**, then open the game window\n"
            "`/daily` — one pineapple puzzle a day\n"
            "`/challenge` — race your pals on the same puzzle\n"
            "`/watch` — spectate active `/play`, `/daily`, and challenge races\n"
            "`/recover` — reopen a saved puzzle (e.g. almost finished)\n"
            "`/cleargame` — abandon a stuck puzzle · `/quit` — leave any active game"
        ),
        inline=False,
    )
    embed.add_field(name="① Cell", value="Tap a square on the board", inline=True)
    embed.add_field(name="② Number", value="1–9 on the pad", inline=True)
    embed.add_field(name="③ Edit", value="Clear cell · Reset board · Notes", inline=True)
    embed.add_field(
        name=f"{JELLY} Rules",
        value=(
            "Red cells = clash in row / column / box.\n"
            "**Pencil mode** = draft notes · solve to earn XP in chat."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{XP} XP & {SPONGE} Sponges",
        value=(
            f"**XP** ranks the leaderboard (never spent).\n"
            f"**Sponges** buy cosmetics in `/shop`:\n"
            f"· **Titles** — header flair on your board\n"
            f"· **Pins** — emoji stickers on the border\n"
            f"· Open `/shop` → pick from the menu → **Buy** / **Equip** "
            f"(filter All / Can buy / Owned · pages ◀ ▶)\n"
            f"· Daily **50% off** one pin + one title (UTC midnight)\n"
            f"· `/weekly` — 3 weekly goals for bonus sponges (resets Monday UTC)\n"
            f"Solve **{format_xp(BASE_WIN_REWARD, signed=True)}** + "
            f"**{format_sponges(BASE_WIN_REWARD, signed=True)}** · "
            f"Daily **+{DAILY_BONUS}** each · "
            f"Streak **+{STREAK_BONUS_PER}**/lvl · "
            f"Challenge win **×{CHALLENGE_WIN_MULT:g}** · "
            f"loss **{format_sponges(CHALLENGE_LOSER_COINS, signed=True)}** (sponges only)\n"
            f"**Hints** cost **{format_sponges(HINT_SPONGE_COST)}** from pocket sponges "
            f"(not career XP) — **no limit** while you can pay. "
            f"**Gary's Wisdom** in `/shop` grants {GARY_WISDOM_HINT_BONUS} free hints/game first.\n"
            f"{tiers}"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{PINEAPPLE} More",
        value="`/shop` · `/weekly` · `/stats` · `/achievements` · `/leaderboard` · `/recover` · `/cleargame` · `/quit`",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    if interaction.guild is None or challenge_home_channel(interaction.channel) is None:
        await interaction.response.send_message(
            "Use this in a server text channel (or its thread).",
            ephemeral=True,
        )
        return

    left = challenge_cooldown_remaining(interaction.user.id)
    if left > 0:
        await interaction.response.send_message(
            f"Challenge cooldown — try again in **{left}s**.",
            ephemeral=True,
        )
        return

    ch_key = await reconcile_challenge_game_for_user(interaction.user.id)
    if ch_key is not None:
        await interaction.response.send_message(
            "You already have an active challenge — leave it first?",
            view=ConfirmQuitView(ch_key, bot, None),
            ephemeral=True,
        )
        return
    # Finished race still settling, or unfinished race only in Mongo.
    busy = await challenge_blocks_user(interaction.user.id)
    if busy:
        if "waiting for other players" in busy:
            await interaction.response.send_message(busy, ephemeral=True)
            return
        ch_key = await ensure_challenge_game_for_user(bot, interaction.user.id)
        if ch_key is not None:
            await interaction.response.send_message(
                "You already have an active challenge — leave it first?",
                view=ConfirmQuitView(ch_key, bot, None),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(busy, ephemeral=True)
        return
    sk_self = solo_key(interaction.guild.id, interaction.user.id)
    if sk_self in games:
        await interaction.response.send_message(
            "Finish your solo/daily game first (`/quit`).",
            ephemeral=True,
        )
        return
    blocking_msg = await activity_blocks_challenge(
        interaction.guild.id, interaction.user.id
    )
    if blocking_msg:
        await interaction.response.send_message(blocking_msg, ephemeral=True)
        return
    daily_block = await daily_attempt_blocks_modes(
        interaction.guild.id, interaction.user.id
    )
    if daily_block:
        await interaction.response.send_message(daily_block, ephemeral=True)
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
        if await reconcile_challenge_game_for_user(uid):
            await interaction.response.send_message(
                f"<@{uid}> already has an active challenge — they need `/quit` first.",
                ephemeral=True,
            )
            return
        block = await challenge_blocks_user(uid)
        if block:
            await interaction.response.send_message(
                f"<@{uid}> — {block}",
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
        block_msg = await activity_blocks_challenge(interaction.guild.id, uid)
        if block_msg:
            await interaction.response.send_message(block_msg, ephemeral=True)
            return
        daily_block = await daily_attempt_blocks_modes(interaction.guild.id, uid)
        if daily_block:
            await interaction.response.send_message(daily_block, ephemeral=True)
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
        f"**{tier}** speedrun (**{n} players**). Everyone must Accept — "
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
    daily = get_guild_daily(bot.data, guild_id)
    day = daily["date"]
    uid = str(user_id)

    if await reconcile_challenge_game_for_user(user_id):
        await interaction.response.send_message(
            "Finish your speedrun challenge first.",
            ephemeral=True,
        )
        return
    block = await challenge_blocks_user(user_id)
    if block:
        await interaction.response.send_message(block, ephemeral=True)
        return
    sk = solo_key(guild_id, user_id)
    if sk in games:
        existing = games[sk]
        if is_solved(existing.get("board") or [], existing.get("solution")):
            await interaction.response.defer()
            await close_solved_session(bot, sk, existing, interaction.user, guild_id)
            # Fall through — allow a new daily only if today's slot is free
        elif existing.get("mode") == "daily":
            # Migrate old chat-based daily to Activity!
            session_id = f"activity:{guild_id}:{user_id}"
            day_key = existing.get("daily_date") or day
            diff_key = daily_difficulty_for_date(str(day_key))
            try:
                diff_index = DIFF_KEYS_LIST.index(diff_key)
            except ValueError:
                diff_index = difficulty_index(diff_key)
            doc = {
                "_id": session_id,
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "difficulty": diff_key,
                "diff_index": diff_index,
                "elapsed": int(time.time() - float(existing.get("started_at") or time.time())),
                "board": existing.get("board"),
                "given": existing.get("given"),
                "solution": existing.get("solution"),
                "filled": game_filled_count(existing),
                "name": interaction.user.display_name,
                "channel_id": str(interaction.channel_id),
                "session_kind": "daily",
                "daily_date": day_key,
                "started_at": existing.get("started_at") or time.time(),
                "last_move_at": time.time(),
                "hints_used": int(existing.get("hints_used") or 0),
                "hints_gary_used": int(existing.get("hints_gary_used") or 0),
            }
            if int(existing.get("gary_wisdom_bonus") or 0) > 0:
                doc["gary_wisdom_bonus"] = int(existing["gary_wisdom_bonus"])
            else:
                gstats = guild_stats(bot.data, guild_id)
                pstats = user_stats(gstats, user_id)
                attach_gary_wisdom_to_session(
                    pstats, doc, existing=None, same_puzzle=False
                )
                save_data(bot.data)
            await match_store.upsert_activity_session(doc)
            await remove_game(sk)
            await _launch_activity_window(interaction)
            return
        else:
            await interaction.response.send_message(
                f"Finish your **{existing['mode']}** game first (**Quit** / `/quit`).",
                ephemeral=True,
            )
            return

    async def _deny_already_done(detail: str) -> None:
        await reply_ephemeral(
            interaction,
            f"{PINEAPPLE} You've already **{detail}** today's daily (`{day}`).\n"
            f"Only **one** pineapple puzzle per day — play more with `/play`.",
        )

    if uid in daily["results"]:
        r = daily["results"][uid]
        if r.get("in_progress"):
            existing, _sid = await lookup_user_activity_session(guild_id, user_id)
            if existing and existing.get("session_kind") == "daily":
                existing, repaired = ensure_daily_session_schedule(existing)
                if repaired:
                    try:
                        await match_store.upsert_activity_session(existing)
                    except Exception as exc:  # noqa: BLE001
                        print(f"/daily in_progress repair failed: {exc}")
                await _launch_activity_window(interaction)
                return
            # Orphan lock (no recoverable session) — treat as forfeit, not a free retry
            await finish_forfeit(
                bot.data,
                guild_id,
                interaction.user,
                {
                    "mode": "daily",
                    "daily_date": day,
                    "started_at": time.time(),
                },
            )
            await _deny_already_done("used (session lost)")
            return
        else:
            if r.get("won"):
                detail = "cleared"
            elif r.get("forfeit"):
                detail = "used (quit)"
            else:
                detail = "used"
            await _deny_already_done(detail)
            return

    # Durable Mongo claim (survives local wipe / redeploy) — fail-closed on store errors.
    try:
        if await match_store.has_daily_forfeit(guild_id, user_id, day):
            daily["results"][uid] = {
                "won": False,
                "forfeit": True,
                "name": interaction.user.display_name,
            }
            save_data(bot.data)
            await _deny_already_done("used (quit)")
            return
    except Exception as exc:  # noqa: BLE001
        print(f"has_daily_forfeit failed (fail-closed): {exc}")
        await reply_ephemeral(
            interaction,
            "Couldn't verify today's daily status right now — try again in a moment.",
        )
        return

    try:
        if await match_store.has_daily_claim(guild_id, user_id, day):
            daily["results"][uid] = {
                "won": True,
                "name": interaction.user.display_name,
            }
            save_data(bot.data)
            await _deny_already_done("cleared")
            return
    except Exception as exc:  # noqa: BLE001
        print(f"has_daily_claim failed (fail-closed): {exc}")
        await reply_ephemeral(
            interaction,
            "Couldn't verify today's daily status right now — try again in a moment.",
        )
        return

    existing_session, _existing_sid = await lookup_user_activity_session(
        guild_id, user_id
    )
    if (
        existing_session
        and existing_session.get("session_kind") == "daily"
        and (existing_session.get("daily_date") or day) == day
        and not existing_session.get("won_at")
    ):
        existing_session, repaired = ensure_daily_session_schedule(existing_session)
        if repaired:
            try:
                await match_store.upsert_activity_session(existing_session)
            except Exception as exc:  # noqa: BLE001
                print(f"/daily repair schedule upsert failed: {exc}")
        daily["results"][uid] = {
            "won": False,
            "in_progress": True,
            "name": interaction.user.display_name,
        }
        save_data(bot.data)
        await _launch_activity_window(interaction)
        return

    # Don't silently overwrite an in-progress /play Activity session.
    play_session = await get_blocking_activity_session(
        guild_id, user_id, kinds={"play"}
    )
    if play_session:
        await reply_ephemeral(
            interaction,
            "You have an active `/play` game open. Finish it or `/quit` first, then start `/daily`.",
        )
        return

    session_id = f"activity:{guild_id}:{user_id}"
    try:
        board, given, solution, diff_key = make_daily_puzzle(
            guild_id, daily["date"], user_id
        )
        try:
            diff_index = DIFF_KEYS_LIST.index(diff_key)
        except ValueError:
            diff_index = difficulty_index(diff_key)
        # Lock only after puzzle+session prep can succeed
        daily["results"][uid] = {
            "won": False,
            "in_progress": True,
            "name": interaction.user.display_name,
        }
        save_data(bot.data)
        doc = {
            "_id": session_id,
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "difficulty": diff_key,
            "diff_index": diff_index,
            "elapsed": 0,
            "board": board,
            "given": given,
            "solution": solution,
            "filled": game_filled_count({"board": board}),
            "name": interaction.user.display_name,
            "channel_id": str(interaction.channel_id),
            "session_kind": "daily",
            "daily_date": daily["date"],
            "started_at": time.time(),
            "last_move_at": time.time(),
            "hints_used": 0,
            "hints_gary_used": 0,
        }
        gstats = guild_stats(bot.data, guild_id)
        pstats = user_stats(gstats, user_id)
        attach_gary_wisdom_to_session(
            pstats, doc, existing=None, same_puzzle=False
        )
        save_data(bot.data)
        await match_store.upsert_activity_session(doc)
        # Drop leftover /play sessions so they cannot shadow this daily on Activity load.
        for play_sid in (f"activity:0:{user_id}",):
            if play_sid == session_id:
                continue
            try:
                play_doc = await match_store.get_activity_session(play_sid)
                if play_doc and (play_doc.get("session_kind") or "play") == "play":
                    await match_store.delete_activity_session(play_sid)
            except Exception as orphan_exc:  # noqa: BLE001
                print(f"clear play orphan after /daily failed: {orphan_exc}")
        # Also clear a same-guild /play if somehow still present under another id.
        try:
            other = await match_store.find_activity_session_by_user_id(user_id)
            if (
                other
                and (other.get("session_kind") or "play") == "play"
                and str(other.get("_id") or "") != session_id
            ):
                await match_store.delete_activity_session(str(other["_id"]))
        except Exception as other_exc:  # noqa: BLE001
            print(f"clear extra play after /daily failed: {other_exc}")
        await _launch_activity_window(interaction)
    except Exception as exc:
        daily["results"].pop(uid, None)
        save_data(bot.data)
        try:
            await match_store.delete_activity_session(session_id)
        except Exception as del_exc:  # noqa: BLE001
            print(f"/daily rollback delete session failed: {del_exc}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"Couldn't start the daily board: {exc}. Try again.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Couldn't start the daily board: {exc}. Try again.",
                ephemeral=True,
            )


@admin_group.command(
    name="resetdaily",
    description="Clear today's daily so everyone can play again (this server)",
)
async def resetdaily_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    if not await require_bot_admin(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    daily = get_guild_daily(bot.data, guild_id)
    day = str(daily.get("date") or utc_today())
    prior_results = dict(daily.get("results") or {})
    n_results = len(prior_results)

    daily["results"] = {}
    save_data(bot.data)

    claims_cleared = 0
    try:
        claims_cleared = await match_store.clear_daily_completions_for_day(guild_id, day)
    except Exception as exc:  # noqa: BLE001
        print(f"resetdaily clear_daily_completions failed: {exc}")

    sessions_cleared = 0
    seen_sids: set[str] = set()
    try:
        sessions = await match_store.list_activity_sessions(
            guild_id, max_age_sec=86400 * 2
        )
    except Exception as exc:  # noqa: BLE001
        print(f"resetdaily list_activity_sessions failed: {exc}")
        sessions = []

    async def _clear_daily_session(sid: str, session: dict | None = None) -> None:
        nonlocal sessions_cleared
        if sid in seen_sids:
            return
        seen_sids.add(sid)
        doc = session
        if doc is None:
            try:
                doc = await match_store.get_activity_session(sid)
            except Exception as exc:  # noqa: BLE001
                print(f"resetdaily get session failed for {sid}: {exc}")
                return
        if not doc:
            return
        if (doc.get("session_kind") or "") != "daily":
            return
        session_day = str(doc.get("daily_date") or "")
        if session_day and session_day != day:
            return
        try:
            await end_activity_watch(bot, sid, force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"resetdaily end_watch failed for {sid}: {exc}")
        try:
            if await clear_activity_session(bot, sid):
                sessions_cleared += 1
        except Exception as exc:  # noqa: BLE001
            print(f"resetdaily clear session failed for {sid}: {exc}")

    for session in sessions:
        sid = str(session.get("_id") or "")
        if sid:
            await _clear_daily_session(sid, session)

    # Orphan keys activity:0:{uid} are missed by guild-scoped list_activity_sessions.
    orphan_uids = {str(uid) for uid in prior_results}
    for session in sessions:
        uid = session.get("user_id")
        if uid is not None:
            orphan_uids.add(str(uid))
    for uid_str in orphan_uids:
        try:
            uid_int = int(uid_str)
        except (TypeError, ValueError):
            continue
        for sid in (f"activity:{guild_id}:{uid_int}", f"activity:0:{uid_int}"):
            await _clear_daily_session(sid)

    games_cleared = 0
    for key, game in list(games.items()):
        if not key or key[0] != guild_id:
            continue
        if game.get("mode") != "daily":
            continue
        if str(game.get("daily_date") or day) != day:
            continue
        await remove_game(key)
        games_cleared += 1

    await interaction.followup.send(
        f"{PINEAPPLE} Daily **`{day}`** reset for this server.\n"
        f"• Cleared **{n_results}** local result(s)\n"
        f"• Cleared **{claims_cleared}** durable claim(s)\n"
        f"• Cleared **{sessions_cleared}** Activity daily session(s)\n"
        f"• Cleared **{games_cleared}** in-memory daily game(s)\n\n"
        f"Everyone can run `/daily` again. "
        f"**Note:** XP/sponges already awarded are not removed.",
        ephemeral=True,
    )


@admin_group.command(
    name="fixdaily",
    description="Unblock a user stuck with 'session lost' on today's daily",
)
@app_commands.describe(user="The user to unblock")
async def fixdaily_cmd(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    if not await require_bot_admin(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    daily = get_guild_daily(bot.data, guild_id)
    day = str(daily.get("date") or utc_today())
    uid = str(user.id)

    entry = daily.get("results", {}).get(uid)
    if not entry:
        await interaction.followup.send(
            f"{user.mention} has no daily entry for today (`{day}`).",
            ephemeral=True,
        )
        return

    if not entry.get("in_progress"):
        await interaction.followup.send(
            f"{user.mention}'s daily for `{day}` is already finalised (`{'won' if entry.get('won') else 'forfeit'}`). Nothing to fix.",
            ephemeral=True,
        )
        return

    # Remove the stuck in_progress lock
    daily["results"].pop(uid, None)
    save_data(bot.data)

    # Also clear any orphaned Activity session and durable claim
    sid = daily_watch_session_id(guild_id, user.id)
    existing = None
    try:
        existing = await match_store.get_activity_session(sid)
    except Exception:
        existing = None
    # If they still have today's daily at the wrong tier, repair in place (no streak hit).
    if (
        existing
        and existing.get("session_kind") == "daily"
        and str(existing.get("daily_date") or day) == day
        and not existing.get("won_at")
    ):
        existing, repaired = ensure_daily_session_schedule(existing)
        if repaired:
            try:
                await match_store.upsert_activity_session(existing)
            except Exception as exc:  # noqa: BLE001
                print(f"fixdaily schedule repair failed: {exc}")
            daily["results"][uid] = {
                "won": False,
                "in_progress": True,
                "name": user.display_name,
            }
            save_data(bot.data)
            await interaction.followup.send(
                f"{PINEAPPLE} Repaired {user.mention}'s daily for `{day}` to "
                f"**{difficulty_label(existing.get('difficulty'))}**. "
                "They can reopen the Activity / run `/daily` — streak untouched.",
                ephemeral=True,
            )
            return

    try:
        await end_activity_watch(bot, sid, force=True)
    except Exception as _exc:  # noqa: BLE001
        pass
    await clear_activity_session(bot, sid)
    # Clear leftover /play that can mask the daily after reopen.
    for play_sid in (f"activity:0:{user.id}",):
        try:
            play_doc = await match_store.get_activity_session(play_sid)
            if play_doc and (play_doc.get("session_kind") or "play") == "play":
                await match_store.delete_activity_session(play_sid)
        except Exception:
            pass
    try:
        await match_store.clear_daily_completions_for_user(guild_id, user.id, day)
    except Exception as exc:  # noqa: BLE001
        print(f"fixdaily clear_daily_completions_for_user failed: {exc}")

    await interaction.followup.send(
        f"{PINEAPPLE} Unblocked {user.mention} for today's daily (`{day}`). "
        "They can now run `/daily` again — streak untouched.",
        ephemeral=True,
    )


@admin_group.command(
    name="setstreak",
    description="Set a player's daily calendar streak (admin)",
)
@app_commands.describe(
    user="Player to update",
    streak="New streak value (e.g. 3)",
)
async def setstreak_cmd(
    interaction: discord.Interaction, user: discord.Member, streak: app_commands.Range[int, 0, 365]
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    if not await require_bot_admin(interaction):
        return

    guild_id = interaction.guild.id
    daily = get_guild_daily(bot.data, guild_id)
    day = str(daily.get("date") or utc_today())
    entry = (daily.get("results") or {}).get(str(user.id)) or {}
    won_today = bool(entry.get("won"))

    gstats = guild_stats(bot.data, guild_id)
    stats = user_stats(gstats, user.id)
    stats["name"] = user.display_name
    apply_manual_streak(stats, streak=int(streak), day=day, won_today=won_today)
    save_data(bot.data)
    try:
        await match_store.save_leaderboard(bot.data)
    except Exception as exc:  # noqa: BLE001
        print(f"setstreak save_leaderboard failed: {exc}")

    await interaction.response.send_message(
        f"{STAR} Set {user.mention} streak to **{int(streak)}** "
        f"(last day `{stats.get('last_streak_day')}`, "
        f"{'won today' if won_today else 'not won today'}).",
        ephemeral=True,
    )


@admin_group.command(
    name="clearsolvetime",
    description="Clear a player's career best/longest solve times (admin)",
)
@app_commands.describe(user="Player whose best/longest times to wipe")
async def clearsolvetime_cmd(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    if not await require_bot_admin(interaction):
        return

    gstats = guild_stats(bot.data, interaction.guild.id)
    stats = user_stats(gstats, user.id)
    stats["name"] = user.display_name
    before_best = stats.get("best_time")
    before_long = stats.get("longest_time")
    clear_solve_times(stats)
    save_data(bot.data)
    try:
        await match_store.save_leaderboard(bot.data)
    except Exception as exc:  # noqa: BLE001
        print(f"clearsolvetime save_leaderboard failed: {exc}")

    best_txt = format_time(before_best) if before_best is not None else "—"
    long_txt = format_time(before_long) if before_long is not None else "—"
    await interaction.response.send_message(
        f"{BUBBLE} Cleared {user.mention} solve times "
        f"(was best **{best_txt}** · longest **{long_txt}**).",
        ephemeral=True,
    )


@admin_group.command(
    name="clearstale",
    description="Delete idle Activity sessions (ends watch + no resume)",
)
async def clearstale_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    if not await require_bot_admin(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    try:
        sessions = await match_store.list_activity_sessions(
            guild_id,
            max_age_sec=WATCH_LIST_MAX_AGE_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"clearstale list_activity_sessions failed: {exc}")
        sessions = []

    cleared = 0
    skipped_live_watch = 0
    seen_sids: set[str] = set()
    now = time.time()

    async def _clear_idle(sid: str, session: dict | None = None) -> None:
        nonlocal cleared, skipped_live_watch
        if sid in seen_sids:
            return
        seen_sids.add(sid)
        doc = session
        if doc is None:
            try:
                doc = await match_store.get_activity_session(sid)
            except Exception as exc:  # noqa: BLE001
                print(f"clearstale get session failed for {sid}: {exc}")
                return
        if not doc or doc.get("won_at"):
            return
        last = float(doc.get("last_move_at") or doc.get("updated_at") or 0)
        if last > 0 and now - last <= WATCH_IDLE_HIDE_SEC:
            return
        try:
            await end_activity_watch(bot, sid, force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"clearstale end_watch failed for {sid}: {exc}")
        try:
            if await clear_activity_session(bot, sid):
                cleared += 1
            else:
                skipped_live_watch += 1
        except Exception as exc:  # noqa: BLE001
            print(f"clearstale clear session failed for {sid}: {exc}")

    for session in sessions:
        sid = str(session.get("_id") or "")
        if sid:
            await _clear_idle(sid, session)

    # Orphan keys activity:0:{uid} missed by guild-scoped list.
    orphan_uids = {str(s.get("user_id")) for s in sessions if s.get("user_id") is not None}
    for uid_str in orphan_uids:
        try:
            uid_int = int(uid_str)
        except (TypeError, ValueError):
            continue
        orphan_id = f"activity:0:{uid_int}"
        orphan = await match_store.get_activity_session(orphan_id)
        if not orphan:
            continue
        orphan_gid = str(orphan.get("guild_id") or "0")
        if orphan_gid not in ("", "0", str(guild_id)):
            continue
        await _clear_idle(orphan_id, orphan)

    note = (
        f"{BUBBLE} Deleted **{cleared}** idle Activity session(s) "
        f"(watch ended + board removed — cannot resume).\n"
        f"Idle = no moves for {WATCH_IDLE_HIDE_SEC // 60}+ min.\n"
        f"Auto-prune only removes the “is playing” post and keeps the board."
    )
    if skipped_live_watch:
        note += f"\nSkipped **{skipped_live_watch}** (watch message still live)."
    await interaction.followup.send(note, ephemeral=True)


@admin_group.command(
    name="resetchallenge",
    description="Zero challenge_wins; wipe matches + live boards (not sponges/XP)",
)
async def resetchallenge_cmd(interaction: discord.Interaction):
    """Wipe challenge career counters + match history after buggy races."""
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    if not await require_bot_admin(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id

    gstats = guild_stats(bot.data, guild_id)
    users_zeroed = 0
    wins_cleared = 0
    for _uid, stats in gstats.items():
        if not isinstance(stats, dict):
            continue
        prior = int(stats.get("challenge_wins") or 0)
        if prior == 0 and "challenge_wins" not in stats:
            continue
        wins_cleared += prior
        if prior or stats.get("challenge_wins") is not None:
            stats["challenge_wins"] = 0
            users_zeroed += 1
    save_data(bot.data)

    matches_deleted = 0
    try:
        matches_deleted = await match_store.delete_matches_for_guild(guild_id)
    except Exception as exc:  # noqa: BLE001
        print(f"resetchallenge delete_matches failed: {exc}")

    games_cleared = 0
    for key, game in list(games.items()):
        if game.get("mode") != "challenge":
            continue
        try:
            gid = int(game.get("guild_id") or 0)
        except (TypeError, ValueError):
            gid = 0
        if gid != guild_id:
            continue
        await remove_game(key)
        games_cleared += 1

    persisted_cleared = 0
    try:
        docs = await match_store.list_active_games()
    except Exception as exc:  # noqa: BLE001
        print(f"resetchallenge list_active_games failed: {exc}")
        docs = []
    for doc in docs:
        game = doc.get("game") if isinstance(doc.get("game"), dict) else {}
        if game.get("mode") != "challenge":
            continue
        try:
            gid = int(game.get("guild_id") or 0)
        except (TypeError, ValueError):
            gid = 0
        if gid != guild_id:
            continue
        sid = str(doc.get("_id") or "")
        if not sid:
            continue
        try:
            await match_store.delete_active_game(sid)
            persisted_cleared += 1
        except Exception as exc:  # noqa: BLE001
            print(f"resetchallenge delete_active_game failed: {exc}")

    await interaction.followup.send(
        f"{JELLY} Challenge stats reset for this server.\n"
        f"• Zeroed **challenge_wins** on **{users_zeroed}** player(s) "
        f"(removed **{wins_cleared}** recorded win(s))\n"
        f"• Deleted **{matches_deleted}** match document(s)\n"
        f"• Cleared **{games_cleared}** live race(s)\n"
        f"• Cleared **{persisted_cleared}** persisted challenge board(s)\n\n"
        f"Challenge win count starts at **0** again. "
        f"**Note:** sponges/XP/best_time from old races are not rolled back.",
        ephemeral=True,
    )


@admin_group.command(
    name="claimdaily",
    description="Re-announce a daily already won (admin; does not invent wins)",
)
@app_commands.describe(
    member="Player whose completed daily announcement to recover (defaults to you)"
)
async def claimdaily_cmd(interaction: discord.Interaction, member: discord.Member | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    if not await require_bot_admin(interaction):
        return

    target_user = member or interaction.user
    guild_id, user_id = interaction.guild.id, target_user.id
    daily = get_guild_daily(bot.data, guild_id)
    day = daily["date"]
    uid = str(user_id)
    results = daily.setdefault("results", {})
    r = results.get(uid) or {}
    if r.get("forfeit"):
        await interaction.response.send_message(
            "You forfeited today's daily — nothing to claim."
            if target_user.id == interaction.user.id
            else f"{target_user.mention} forfeited today's daily — nothing to claim.",
            ephemeral=True,
        )
        return

    if r.get("in_progress") and not r.get("won"):
        await interaction.response.send_message(
            "Finish today's daily first (`/daily`), then use this if the announcement failed.",
            ephemeral=True,
        )
        return

    if r.get("won") and r.get("announced_debug"):
        await interaction.response.send_message(
            "Today's daily has already been announced for you!"
            if target_user.id == interaction.user.id
            else f"Today's daily has already been announced for {target_user.mention}!",
            ephemeral=True,
        )
        return

    try:
        has_claim = await match_store.has_daily_claim(guild_id, user_id, day)
    except Exception as exc:  # noqa: BLE001
        print(f"claimdaily has_daily_claim failed: {exc}")
        has_claim = False

    mongo_completion: dict | None = None
    try:
        mongo_completion = await match_store.get_daily_completion(guild_id, user_id, day)
    except Exception as exc:  # noqa: BLE001
        print(f"claimdaily get_daily_completion failed: {exc}")

    # Only recover announcements for players who actually completed the daily.
    if not r.get("won") and not has_claim:
        await interaction.response.send_message(
            "No completed daily found for today. Finish `/daily` first.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    board, given, solution, diff_key = make_daily_puzzle(guild_id, day, user_id)
    solved_board = [
        [{"value": solution[r_idx][c_idx], "pencil_marks": []} for c_idx in range(9)]
        for r_idx in range(9)
    ]
    elapsed = int(r.get("time") or r.get("elapsed") or 300)
    if mongo_completion and not mongo_completion.get("forfeit"):
        elapsed = int(mongo_completion.get("elapsed") or elapsed)

    game_state = {
        "mode": "daily",
        "daily_date": day,
        "started_at": time.time() - max(1, elapsed),
        "difficulty": diff_key,
        "board": solved_board,
        "given": given,
        "solution": solution,
    }

    # finish_win_and_announce skips payout when local results already have won=True.
    # If only Mongo has the claim, mark local won first so we never double-pay.
    if has_claim and not r.get("won"):
        mc = mongo_completion or {}
        coins = int(mc.get("coins") or r.get("coins") or 0)
        xp = int(mc.get("xp") or mc.get("coins") or r.get("xp") or coins or 0)
        if coins == 0 and xp == 0:
            preview_streak = preview_daily_calendar_streak(
                user_stats(guild_stats(bot.data, guild_id), user_id),
                day,
            )
            coins = win_reward(
                preview_streak,
                daily=True,
                difficulty=diff_key,
            )
            xp = coins
        results[uid] = {
            **r,
            "won": True,
            "name": target_user.display_name,
            "coins": coins,
            "xp": xp,
            "time": elapsed,
            "elapsed": elapsed,
        }
        save_data(bot.data)

    outcome = await finish_win_and_announce(bot, guild_id, target_user, game_state)

    results[uid] = results.get(uid) or {}
    results[uid]["announced_debug"] = True
    results[uid]["won"] = True
    save_data(bot.data)

    gstats = guild_stats(bot.data, guild_id)
    stats = user_stats(gstats, user_id)
    embed = build_activity_win_embed(
        user_id=user_id,
        difficulty=diff_key,
        elapsed=elapsed,
        coins=int(getattr(outcome, "coins", None) or results[uid].get("coins") or 0),
        xp=int(getattr(outcome, "xp", None) or results[uid].get("xp") or 0),
        streak=max(int(stats.get("streak") or 1), 1),
        is_daily=True,
        user_stats_dict=stats,
    )
    image = await asyncio.to_thread(
        render_board,
        solved_board,
        given,
        solution=solution,
        conflicts=set(),
        difficulty=diff_key,
        title_id=equipped_title_id(stats),
        pin_emojis=owned_pin_emojis(stats),
        pin_seed=user_id,
    )
    file = board_to_file(image)

    try:
        await interaction.channel.send(
            content=f"{target_user.mention} completed today's daily!",
            embed=embed,
            file=file,
        )
        await interaction.followup.send(
            f"Daily announcement recovered and posted for {target_user.mention}!",
            ephemeral=True,
        )
    except discord.HTTPException as exc:
        print(f"claimdaily channel post failed: {exc}")
        await interaction.followup.send(
            "Win recovered, but I couldn't post the board in this channel "
            f"(check permissions). Error: {exc}",
            ephemeral=True,
        )


bot.tree.add_command(admin_group)


@bot.tree.command(name="shop", description="Spend sponges at the Krusty Shop")
async def shop_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    gstats = guild_stats(bot.data, interaction.guild.id)
    stats = user_stats(gstats, interaction.user.id)
    stats["name"] = interaction.user.display_name
    save_data(bot.data)
    view = KrustyShopView(
        bot,
        owner_id=interaction.user.id,
        guild_id=interaction.guild.id,
        kind="boosts",
    )
    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


@bot.tree.command(name="giftpin", description="Gift an owned border pin to another player")
@app_commands.describe(pin="Pin you own", member="Player who receives the pin")
async def giftpin_cmd(
    interaction: discord.Interaction,
    pin: str,
    member: discord.Member,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("Pick a real player.", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You already own that pin — pick someone else.",
            ephemeral=True,
        )
        return

    result = apply_gift_pin(
        bot, interaction.guild.id, interaction.user.id, member.id, pin
    )
    if not result.get("ok"):
        await interaction.response.send_message(result["message"], ephemeral=True)
        return

    gstats = guild_stats(bot.data, interaction.guild.id)
    donor_stats = user_stats(gstats, interaction.user.id)
    recv_stats = user_stats(gstats, member.id)
    await sync_cosmetics_to_activity_sessions(
        interaction.user.id,
        interaction.guild.id,
        title_id=equipped_title_id(donor_stats),
        pin_emojis=owned_pin_emojis(donor_stats),
    )
    await sync_cosmetics_to_activity_sessions(
        member.id,
        interaction.guild.id,
        title_id=equipped_title_id(recv_stats),
        pin_emojis=owned_pin_emojis(recv_stats),
    )

    emoji = result.get("emoji", WAVE)
    label = result.get("label", pin)
    await interaction.response.send_message(
        f"🎁 Gifted {emoji} **{label}** to {member.mention}!",
        ephemeral=True,
    )
    try:
        if interaction.channel is not None:
            await interaction.channel.send(
                f"🎁 {interaction.user.mention} gifted {emoji} **{label}** "
                f"to {member.mention}!"
            )
    except discord.HTTPException:
        pass


@giftpin_cmd.autocomplete("pin")
async def giftpin_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    gstats = guild_stats(bot.data, interaction.guild.id)
    stats = user_stats(gstats, interaction.user.id)
    owned = []
    for tid in owned_pin_ids(stats):
        meta = SHOP_PINS.get(tid)
        if not meta or tid in SHOP_BOOST_KEYS:
            continue
        if int(meta.get("cost") or 0) <= 0:
            continue
        owned.append((tid, meta))
    q = (current or "").casefold()
    choices: list[app_commands.Choice[str]] = []
    for tid, meta in owned:
        label = str(meta.get("label") or tid)
        if q and q not in label.casefold() and q not in tid.casefold():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=tid))
        if len(choices) >= 25:
            break
    return choices


@bot.tree.command(
    name="recover",
    description="Reopen your saved in-progress puzzle in the Activity",
)
async def recover_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    uid = interaction.user.id

    if await reconcile_challenge_game_for_user(uid):
        await interaction.response.send_message(
            "Finish or `/quit` your speedrun challenge first.",
            ephemeral=True,
        )
        return
    block = await challenge_blocks_user(uid)
    if block:
        await interaction.response.send_message(block, ephemeral=True)
        return

    session, _session_id = await lookup_user_activity_session(guild_id, uid)
    if not session or not session.get("board"):
        await interaction.response.send_message(
            f"{BUBBLE} No saved puzzle found. Start with `/play` or `/daily`.",
            ephemeral=True,
        )
        return
    if session.get("won_at"):
        await interaction.response.send_message(
            "That puzzle is already finished — use `/play` for a new game.",
            ephemeral=True,
        )
        return

    filled = int(session.get("filled") or 0)
    kind = session.get("session_kind") or "play"
    if kind == "daily":
        day = str(session.get("daily_date") or utc_today())
        tier = difficulty_label(daily_difficulty_for_date(day))
    else:
        tier = difficulty_label(resolve_session_difficulty(session)[0])
    elapsed = activity_session_elapsed(session)
    print(
        f"/recover user={uid} guild={guild_id} kind={kind} "
        f"filled={filled}/81 elapsed={elapsed}s"
    )
    await _launch_activity_window(interaction)


@bot.tree.command(
    name="cleargame",
    description="Abandon your saved puzzle and remove it from /watch",
)
async def cleargame_cmd(interaction: discord.Interaction):
    """Same as /quit for Activity sessions, with clearer wording for stuck boards."""
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    ch_key = await reconcile_challenge_game_for_user(interaction.user.id)
    if ch_key is not None:
        await interaction.response.send_message(
            "Abandon this speedrun?",
            view=ConfirmQuitView(ch_key, bot, None),
            ephemeral=True,
        )
        return

    session, session_id = await lookup_user_activity_session(
        guild_id, interaction.user.id
    )
    if session and session.get("session_kind") == "daily":
        await interaction.response.send_message(
            "Abandon today's daily? This locks your attempt and resets your streak.",
            view=ConfirmQuitActivityDailyView(
                session_id or daily_watch_session_id(guild_id, interaction.user.id),
                bot,
            ),
            ephemeral=True,
        )
        return
    if session and (session.get("session_kind") or "play") == "play" and (
        session.get("board") or session.get("solution")
    ):
        filled = int(session.get("filled") or 0)
        await interaction.response.send_message(
            f"Abandon this puzzle ({filled}/81)? It will be removed from `/watch`. "
            "Your daily streak is unchanged.",
            view=ConfirmQuitActivityPlayView(
                session_id or daily_watch_session_id(guild_id, interaction.user.id),
                bot,
            ),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"{BUBBLE} No saved puzzle to clear. If `/watch` still shows an old game, "
        "an admin can run `/z-admin clearstale`.",
        ephemeral=True,
    )


@bot.tree.command(name="quit", description="Leave your active Sudoku game or challenge")
async def quit_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    ch_key = await reconcile_challenge_game_for_user(interaction.user.id)
    if ch_key is not None:
        await interaction.response.send_message(
            "Really leave this speedrun?",
            view=ConfirmQuitView(ch_key, bot, None),
            ephemeral=True,
        )
        return

    # Challenge active in Mongo but not yet in memory (e.g. after restart).
    try:
        active_matches = await match_store.list_matches(status="active")
    except Exception as exc:  # noqa: BLE001
        print(f"quit_cmd list_matches failed: {exc}")
        active_matches = []
    for match in active_matches:
        mid = match.get("_id")
        if not mid:
            continue
        for slot, player in match_player_entries(match):
            if int(player.get("user_id") or 0) != int(interaction.user.id):
                continue
            if player.get("forfeit") or player.get("finished_time") is not None:
                continue
            # Rehydrate so ConfirmQuitView can forfeit with a game_key.
            try:
                await restore_challenge_games_from_match(bot, match)
            except Exception as exc:  # noqa: BLE001
                print(f"quit_cmd restore challenge failed: {exc}")
            ch_key = await reconcile_challenge_game_for_user(interaction.user.id)
            if ch_key is not None:
                await interaction.response.send_message(
                    "Really leave this speedrun?",
                    view=ConfirmQuitView(ch_key, bot, None),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Really leave this speedrun?",
                view=ConfirmQuitChallengeMongoView(str(mid), slot, bot),
                ephemeral=True,
            )
            return

    # Check for active daily/play Activity session (primary + orphan).
    session, session_id = await lookup_user_activity_session(
        guild_id, interaction.user.id
    )
    if not session:
        session_id = f"activity:{guild_id}:{interaction.user.id}"
    if session and session.get("session_kind") == "daily":
        await interaction.response.send_message(
            "Quit today's daily? This locks your attempt and resets your streak.",
            view=ConfirmQuitActivityDailyView(session_id, bot),
            ephemeral=True,
        )
        return
    if session and (session.get("session_kind") or "play") == "play" and (
        session.get("board") or session.get("solution")
    ):
        await interaction.response.send_message(
            "Really quit this puzzle? Your daily streak stays safe.",
            view=ConfirmQuitActivityPlayView(session_id, bot),
            ephemeral=True,
        )
        return

    sk = solo_key(guild_id, interaction.user.id)
    if sk in games:
        game = games[sk]
        # Already solved but win UI failed earlier — close + award, don't forfeit
        if is_solved(game.get("board") or [], game.get("solution")):
            await interaction.response.defer(ephemeral=True)
            coins = await close_solved_session(bot, sk, game, interaction.user, guild_id)
            msg = (
                f"{SPONGE} That board was already solved — session closed."
                + (f" Rewards: **{format_sponges(coins, signed=True)}** (see `/stats`)." if coins else "")
            )
            await interaction.followup.send(msg, ephemeral=True)
            return
        if game.get("mode") == "daily":
            prompt = "Quit today's daily? This locks your attempt and resets your streak."
        else:
            prompt = "Really quit this puzzle? Your daily streak stays safe."
        await interaction.response.send_message(
            prompt,
            view=ConfirmQuitView(sk, bot, None),
            ephemeral=True,
        )
        return

    # Orphan daily lock (no live session) — forfeit so the day stays locked
    daily = get_guild_daily(bot.data, guild_id)
    entry = daily.get("results", {}).get(str(interaction.user.id))
    if entry and entry.get("in_progress") and not entry.get("won"):
        await finish_forfeit(
            bot.data,
            guild_id,
            interaction.user,
            {
                "mode": "daily",
                "daily_date": daily.get("date"),
                "started_at": time.time(),
            },
        )
        await drop_persisted_game(sk)
        orphan_sid = daily_watch_session_id(guild_id, interaction.user.id)
        try:
            await end_activity_watch(bot, orphan_sid, force=True)
        except Exception as _exc:  # noqa: BLE001
            pass
        await clear_activity_session(bot, orphan_sid)
        await interaction.response.send_message(
            f"{BUBBLE} Cleared a stuck daily lock (counted as forfeit for today).",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("No game to quit.", ephemeral=True)


@bot.tree.command(
    name="leaderboard",
    description="Bikini Bottom rankings — XP, daily today, shop whales",
)
@app_commands.describe(board="Which leaderboard to show")
@app_commands.choices(
    board=[
        app_commands.Choice(name="XP (career)", value="xp"),
        app_commands.Choice(name="Today's daily", value="daily_today"),
        app_commands.Choice(name="Shop whales", value="whales"),
    ]
)
async def leaderboard_cmd(
    interaction: discord.Interaction,
    board: app_commands.Choice[str] | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    mode = board.value if board else "xp"
    guild_id = interaction.guild.id
    gstats = guild_stats(bot.data, guild_id)

    # Today's daily standings (time-based, not career)
    if mode == "daily_today":
        daily = get_guild_daily(bot.data, guild_id)
        results = daily.get("results") or {}
        if not results:
            await interaction.response.send_message(
                f"{PINEAPPLE} Nobody's cleared today's pineapple yet — be the first with `/daily`!",
                ephemeral=True,
            )
            return
        winners = [(uid, r) for uid, r in results.items() if r.get("won")]
        winners.sort(key=lambda item: item[1].get("time", 10**9))
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, r) in enumerate(winners[:10]):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            stats = user_stats(gstats, int(uid))
            name = (
                display_name(stats)
                if stats.get("name") != "Unknown"
                else r.get("name", uid)
            )
            sponge_bit = ""
            if r.get("coins"):
                sponge_bit = f" · {format_sponges(int(r['coins']), signed=True)}"
            lines.append(
                f"{prefix} **{name}** — {format_time(r.get('time', 0))}{sponge_bit}"
            )
        failed = sum(
            1
            for r in results.values()
            if not r.get("won") and not r.get("in_progress")
        )
        embed = paper_embed(f"{PINEAPPLE} Daily #{daily_puzzle_number(daily['date'])}")
        embed.description = (
            f"{WAVE} Fastest clearers of today's pineapple puzzle.\n"
            f"*{interaction.guild.name}*"
        )
        embed.add_field(name="Date", value=f"`{daily['date']}`", inline=True)
        embed.add_field(name=f"Cleared {STAR}", value=str(len(winners)), inline=True)
        embed.add_field(name="Other attempts", value=str(failed), inline=True)
        embed.add_field(
            name="Standings",
            value="\n".join(lines) if lines else "No solves yet — the grill is cold.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, silent=True)
        return

    players = [(uid, user_stats(gstats, int(uid))) for uid, _ in iter_players(gstats)]

    if mode == "whales":
        ranked = sorted(
            players,
            key=lambda item: int(item[1].get("sponges_spent") or 0),
            reverse=True,
        )[:10]
        title = f"{SPONGE} Krusty Shop whales"
        blurb = "Who emptied their pockets at the Krusty Shop? *Squidward is judging you.*"
        fmt = lambda s: (
            f"spent **{int(s.get('sponges_spent') or 0)}** {SPONGE} · "
            f"pocket **{int(s.get('coins') or 0)}**"
        )
        nonempty = lambda s: int(s.get("sponges_spent") or 0) > 0
        empty_msg = (
            f"{BUBBLE} Nobody's emptied their pockets yet — the Krusty Shop is waiting."
        )
    else:
        # Default: career XP (+ pocket on the same line)
        ranked = sorted(players, key=lambda item: item[1].get("xp", 0), reverse=True)[:10]
        title = f"{XP} Career XP"
        blurb = "Who's climbing the ladder? (Shop spend doesn't hurt XP.)"
        fmt = lambda s: (
            f"**{format_xp(s.get('xp', 0))}** · "
            f"{SPONGE} **{int(s.get('coins', 0))}**"
        )
        nonempty = lambda s: s.get("xp", 0) > 0 or s.get("wins", 0) > 0
        empty_msg = f"{BUBBLE} Nobody on this board yet — go earn some XP with `/play`!"
        mode = "xp"

    ranked = [(uid, s) for uid, s in ranked if nonempty(s)]
    if not ranked:
        await interaction.response.send_message(empty_msg, ephemeral=True)
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (_, s) in enumerate(ranked[:10]):
        prefix = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"{prefix} **{display_name(s)}** — {fmt(s)}")
    embed = paper_embed(f"{title}")
    embed.description = f"{blurb}\n*{interaction.guild.name}*"
    field_name = "Top spenders" if mode == "whales" else "Top 10"
    embed.add_field(name=field_name, value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed, silent=True)


@bot.tree.command(
    name="weekly",
    description="This week's Bikini Bottom goals — bonus sponges",
)
@app_commands.describe(member="Peek at a neighbor's weekly progress")
async def weekly_cmd(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    target = member or interaction.user
    gstats = guild_stats(bot.data, interaction.guild.id)
    stats = user_stats(gstats, target.id)
    stats["name"] = target.display_name
    embed = build_weekly_embed(stats, viewer_name=target.display_name)
    save_data(bot.data)
    try:
        await match_store.save_leaderboard(bot.data)
    except Exception as exc:  # noqa: BLE001
        print(f"/weekly save_leaderboard failed: {exc}")
    await interaction.response.send_message(embed=embed, silent=True)


@bot.tree.command(name="stats", description="Your Bikini Bottom Sudoku report card")
@app_commands.describe(member="Peek at a neighbor's stats")
async def stats_cmd(interaction: discord.Interaction, member: discord.Member | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    try:
        target = member or interaction.user
        gstats = guild_stats(bot.data, interaction.guild.id)
        s = user_stats(gstats, target.id)
        s["name"] = target.display_name
        evaluate_user_achievements(s)
        save_data(bot.data)
        embed = build_stats_embed(s)
        await interaction.response.send_message(embed=embed, silent=True)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"/stats failed: {type(exc).__name__}: {exc}")
        msg = f"Couldn't load profile right now (`{type(exc).__name__}`)."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(
    name="achievements",
    description="Browse all badges — unlocked and still locked",
)
@app_commands.describe(member="Peek at a neighbor's achievement progress")
async def achievements_cmd(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    target = member or interaction.user
    gstats = guild_stats(bot.data, interaction.guild.id)
    stats = user_stats(gstats, target.id)
    stats["name"] = target.display_name
    evaluate_user_achievements(stats)
    save_data(bot.data)
    embed = achievement_catalog_embed(stats, viewer_name=target.display_name)
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "SEU_DISCORD_TOKEN_AQUI":
        raise SystemExit(
            "Missing DISCORD_TOKEN. Put it in .env:\n  DISCORD_TOKEN=seu_token_aqui"
        )
    start_health_server_early()
    bot.run(token)
