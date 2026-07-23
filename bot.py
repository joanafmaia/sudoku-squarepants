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
from datetime import datetime, timezone
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
    "very_easy": {"label": "Very Easy", "clues": 46, "multiplier": 0.50},
    "easy": {"label": "Easy", "clues": 40, "multiplier": 0.75},
    "medium": {"label": "Medium", "clues": 34, "multiplier": 1.00},
    "hard": {"label": "Hard", "clues": 28, "multiplier": 1.50},
    "very_hard": {"label": "Very Hard", "clues": 24, "multiplier": 2.00},
    "expertttt": {"label": "Expertttt", "clues": 22, "multiplier": 3.00},
}

DIFFICULTY_CHOICES = [
    app_commands.Choice(name=meta["label"], value=key)
    for key, meta in DIFFICULTY_TIERS.items()
]

BASE_WIN_REWARD = 50
DAILY_BONUS = 40
STREAK_BONUS_PER = 5
CHALLENGE_WIN_MULT = 2.0  # extra multiplier for speedrun winners
MAX_CHALLENGE_PLAYERS = 5  # challenger + up to 4 opponents
CHALLENGE_LOSER_COINS = 15
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
    "patrick": {"label": "⭐ Starfish Genius", "cost": 120, "pin": "Starfish", "emoji": "⭐"},
    "solver": {"label": "🍔 Fry Cook", "cost": 220, "pin": "Fry Cook", "emoji": "🍔"},
    "larry": {"label": "💪 Larry Lobster", "cost": 350, "pin": "Larry", "emoji": "💪"},
    "barnacle": {"label": "🦸 Barnacle Boy", "cost": 500, "pin": "Barnacle", "emoji": "🦸"},
    "row_master": {"label": "🚗 Boatmobile Ace", "cost": 700, "pin": "Boatmobile", "emoji": "🚗"},
    "puff": {"label": "⛵ Boating School Grad", "cost": 950, "pin": "Boating Grad", "emoji": "⛵"},
    "dutchman": {"label": "👻 Flying Dutchman", "cost": 1250, "pin": "Dutchman", "emoji": "👻"},
    "sudoku_pro": {"label": "🍦 Goofy Goober", "cost": 1600, "pin": "Goober", "emoji": "🍦"},
    "plankton": {"label": "🦠 Plankton Plotter", "cost": 2100, "pin": "Plankton", "emoji": "🦠"},
    "mermaid": {"label": "🧜 Mermaid Man", "cost": 2700, "pin": "Mermaid Man", "emoji": "🧜"},
    "legend": {"label": "🍍 Pineapple Legend", "cost": 3500, "pin": "Legend", "emoji": "🍍"},
    "neptune": {"label": "👑 King Neptune", "cost": 4500, "pin": "Neptune", "emoji": "👑"},
    # Crew tributes — Bikini Bottom shout-outs
    "darkstriker": {"label": "🦹 Dark Striker", "cost": 900, "pin": "Striker", "emoji": "🦹"},
    "behindyou": {"label": "👀 Behind You", "cost": 1400, "pin": "Shadow", "emoji": "👀"},
    "glock_sheets": {"label": "📊 Glock Sheets", "cost": 1600, "pin": "Sheets", "emoji": "📊"},
    "bookie": {"label": "📚 Book Queen", "cost": 1800, "pin": "Bookie", "emoji": "📚"},
    "stacked": {"label": "😎 Stacked Smooth", "cost": 2000, "pin": "Stacked", "emoji": "😎"},
    "drea_mom": {"label": "🫶 Mama Drea", "cost": 2400, "pin": "Mama", "emoji": "🫶"},
    "hulk_r5": {"label": "🧌 Hulk Command", "cost": 2800, "pin": "Hulk", "emoji": "🧌"},
    "apex_whale": {"label": "🐋 Apex Whale", "cost": 5000, "pin": "Apex", "emoji": "🐋"},
}

# Pins = border stickers only. One free; paid pins scale up so cosmetics stay a chase.
SHOP_PINS = {
    "wave": {"label": "🌊 Wave Pin", "pin": "Wave", "emoji": "🌊", "cost": 0},
    # Former title emojis → buyable border pins
    "pin_jelly": {"label": "🪼 Jelly Pin", "pin": "Jelly", "emoji": "🪼", "cost": 80},
    "pin_star": {"label": "⭐ Star Pin", "pin": "Star", "emoji": "⭐", "cost": 120},
    "pin_burger": {"label": "🍔 Burger Pin", "pin": "Burger", "emoji": "🍔", "cost": 180},
    "pin_flex": {"label": "💪 Flex Pin", "pin": "Flex", "emoji": "💪", "cost": 240},
    "pin_hero": {"label": "🦸 Hero Pin", "pin": "Hero", "emoji": "🦸", "cost": 320},
    "pin_boat": {"label": "🚗 Boat Pin", "pin": "Boat", "emoji": "🚗", "cost": 400},
    "pin_sail": {"label": "⛵ Sail Pin", "pin": "Sail", "emoji": "⛵", "cost": 500},
    "pin_ghost": {"label": "👻 Ghost Pin", "pin": "Ghost", "emoji": "👻", "cost": 620},
    "pin_goober": {"label": "🍦 Goober Pin", "pin": "Goober", "emoji": "🍦", "cost": 760},
    "pin_bug": {"label": "🦠 Bug Pin", "pin": "Bug", "emoji": "🦠", "cost": 920},
    "pin_mermaid": {"label": "🧜 Mermaid Pin", "pin": "Mermaid", "emoji": "🧜", "cost": 1100},
    "pin_pineapple": {"label": "🍍 Pineapple Pin", "pin": "Pineapple", "emoji": "🍍", "cost": 1350},
    "pin_crown": {"label": "👑 Crown Pin", "pin": "Crown", "emoji": "👑", "cost": 1600},
    # Extra unique stickers
    "coral": {"label": "🪸 Coral Pin", "pin": "Coral", "emoji": "🪸", "cost": 100},
    "crab": {"label": "🦀 Crab Pin", "pin": "Crab", "emoji": "🦀", "cost": 160},
    "bubble": {"label": "🫧 Bubble Pin", "pin": "Bubble", "emoji": "🫧", "cost": 220},
    "shell": {"label": "🐚 Shell Pin", "pin": "Shell", "emoji": "🐚", "cost": 300},
    "squid": {"label": "🦑 Squid Pin", "pin": "Squid", "emoji": "🦑", "cost": 380},
    "sandy": {"label": "🐿️ Dome Pin", "pin": "Dome", "emoji": "🐿️", "cost": 480},
    "pearl": {"label": "💎 Pearl Pin", "pin": "Pearl", "emoji": "💎", "cost": 600},
    "anchor": {"label": "⚓ Anchor Pin", "pin": "Anchor", "emoji": "⚓", "cost": 750},
    "shark": {"label": "🦈 Shark Pin", "pin": "Shark", "emoji": "🦈", "cost": 920},
    "bucket": {"label": "🪣 Bucket Pin", "pin": "Bucket", "emoji": "🪣", "cost": 1100},
    "sponge": {"label": "🧽 Sponge Pin", "pin": "Sponge", "emoji": "🧽", "cost": 1350},
    "whirl": {"label": "🌀 Whirlpool Pin", "pin": "Whirlpool", "emoji": "🌀", "cost": 1600},
    # Crew tribute pins
    "pin_goof": {"label": "🦹 Thief Pin", "pin": "Thief", "emoji": "🦹", "cost": 450},
    "pin_shadow": {"label": "👀 Shadow Pin", "pin": "Shadow", "emoji": "👀", "cost": 700},
    "pin_sheets": {"label": "📊 Sheets Pin", "pin": "Sheets", "emoji": "📊", "cost": 850},
    "pin_book": {"label": "📚 Book Pin", "pin": "Book", "emoji": "📚", "cost": 950},
    "pin_smooth": {"label": "😎 Stacked Pin", "pin": "Stacked", "emoji": "😎", "cost": 1050},
    "pin_mama": {"label": "🫶 Mama Pin", "pin": "Mama", "emoji": "🫶", "cost": 1200},
    "pin_hulk": {"label": "🧌 Hulk Pin", "pin": "Hulk", "emoji": "🧌", "cost": 1450},
    "pin_apex": {"label": "🐋 Apex Pin", "pin": "Apex", "emoji": "🐋", "cost": 1800},
}

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
WATCH_ACTIVE_SEC = 45
CHALLENGE_LIVE_DEBOUNCE_SEC = 4.0
ACTIVITY_WATCH_MAX_AGE_SEC = 180
ACTIVITY_WATCH_END_GRACE_SEC = 20
ACTIVITY_LIVE_REFRESH_SEC = 3.5
ACTIVITY_LIVE_SPECTATOR_TIMEOUT_SEC = 900
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
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        # Don't lose the in-memory award if the disk write fails (e.g. ephemeral FS hiccup)
        print(f"save_data failed: {exc}")
    # Mirror to Mongo so Fly.io restarts keep sponges / stats
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    snapshot = deepcopy(data)
    loop.create_task(_mirror_leaderboard_mongo(snapshot))


async def _mirror_leaderboard_mongo(data: dict) -> None:
    try:
        await match_store.save_leaderboard(data)
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
            try:
                stats["best_time"] = float(elapsed)
            except (TypeError, ValueError):
                pass
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
    s.setdefault("streak", 0)
    s.setdefault("best_streak", 0)
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


async def drop_persisted_game(key: tuple) -> None:
    try:
        await match_store.delete_active_game(serialize_game_key(key))
    except Exception as exc:  # noqa: BLE001
        print(f"drop_persisted_game failed: {exc}")


async def remove_game(key: tuple) -> dict | None:
    game = games.pop(key, None)
    await drop_persisted_game(key)
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
                outcome = await finish_win_and_announce(bot, guild_id, user, game)
                coins = int(outcome.coins)
                game["rewarded"] = True
            except Exception as exc:  # noqa: BLE001
                print(f"close_solved_session award failed: {exc}")
    await remove_game(key)
    return coins


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


def cosmetic_pin_text(meta: dict | None, *, fallback: str = "") -> str:
    """Short ASCII-friendly badge text (captions / /testboard)."""
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
        return True
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
                urllib.request.urlretrieve(url, path)
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


def win_reward_caption(coins: int, xp: int | None = None) -> str:
    """Readable win line under the board image (XP + sponges)."""
    line = random.choice(WIN_BANNER_LINES)
    gained_xp = int(coins if xp is None else xp)
    return (
        f"{BUBBLE} **{line} {format_xp(gained_xp, signed=True)} · "
        f"{format_sponges(max(int(coins), 0), signed=True)}!**"
    )


def build_activity_win_embed(
    *,
    user_id: int,
    difficulty: str,
    elapsed: int,
    coins: int,
    xp: int,
    streak: int,
) -> discord.Embed:
    """Channel announcement when someone clears an Activity (/play) puzzle."""
    mention = f"<@{user_id}>"
    tier = difficulty_label(difficulty)
    embed = paper_embed(f"{SPONGE} /play — puzzle solved!")
    embed.description = (
        f"{WAVE} {mention} cleared the board!\n"
        f"{random.choice(WIN_TAUNTS)}"
    )
    embed.add_field(name="Difficulty", value=f"**{tier}**", inline=True)
    embed.add_field(name="Time", value=f"**{format_time(elapsed)}**", inline=True)
    embed.add_field(name="Streak", value=f"{STAR} **{streak}**", inline=True)
    embed.add_field(
        name="Reward",
        value=f"{format_xp(xp, signed=True)} · {format_sponges(coins, signed=True)}",
        inline=False,
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
    return ("ch", match_id, user_id)


def find_challenge_game_for_user(user_id: int) -> tuple | None:
    for key, game in games.items():
        if game.get("mode") == "challenge" and game.get("owner_id") == user_id:
            return key
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
        return WinOutcome(embed=paper_embed("Daily"), coins=0, xp=0, quiet=True)

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
    xp = coins  # career XP mirrors sponge grant; shop spend never reduces XP
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
    embed.add_field(name=f"XP {XP}", value=format_xp(xp, signed=True), inline=True)
    embed.add_field(name=f"Sponges {SPONGE}", value=format_sponges(coins, signed=True), inline=True)
    embed.add_field(name=f"Streak {STAR}", value=str(stats["streak"]), inline=True)
    embed.add_field(name=f"Career XP {XP}", value=format_xp(stats["xp"]), inline=True)
    embed.add_field(name=f"Pocket {SPONGE}", value=format_sponges(stats["coins"]), inline=True)
    if rank is not None:
        embed.add_field(name="Rank", value=f"#{rank}", inline=True)
    else:
        embed.add_field(name="Mode", value=game["mode"].capitalize(), inline=True)
    return WinOutcome(embed=embed, coins=coins, xp=xp, rank=rank, quiet=False)


async def finish_win_and_announce(
    bot: "SudokuBot",
    guild_id: int,
    user: discord.abc.User,
    game: dict,
) -> WinOutcome:
    """Award win; for daily, gate on local `won` first (Mongo is best-effort anti-dupe)."""
    if game.get("mode") != "daily":
        return finish_win(bot.data, guild_id, user, game)

    day = game.get("daily_date") or utc_today()
    elapsed = int(time.time() - game["started_at"])
    tier = difficulty_label(game.get("difficulty"))
    gstats = guild_stats(bot.data, guild_id)
    stats = user_stats(gstats, user.id)

    daily_meta = get_guild_daily(bot.data, guild_id)
    prior = daily_meta.get("results", {}).get(str(user.id)) or {}

    # Already awarded locally today — don't pay twice; still show previous amounts
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
        )

    # Award XP + sponges FIRST — never let a Mongo "already claimed" block a first local payout
    outcome = finish_win(bot.data, guild_id, user, game)

    preview_coins = int(outcome.coins)
    preview_xp = int(outcome.xp)
    try:
        await match_store.try_claim_daily_win(
            guild_id=guild_id,
            user_id=user.id,
            day=day,
            elapsed=elapsed,
            hints=0,
            difficulty=tier,
            coins=preview_coins,
            player_name=getattr(user, "display_name", None) or getattr(user, "name", None),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"try_claim_daily_win failed (local award kept): {exc}")

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
        {
            "current_board": copy_grid(game["board"]),
            "last_move_at": time.time(),
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


async def resolve_channel(
    bot: "SudokuBot",
    channel_id: int | None,
) -> discord.abc.Messageable | None:
    """Cache lookup, then API fetch — private threads often miss cache after restart."""
    if not channel_id:
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(int(channel_id))
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
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

    winner_name = None
    if winner_id is not None:
        for _, p in entries:
            if p.get("user_id") == winner_id:
                winner_name = p.get("name")
                break
    await match_store.update_match(
        match["_id"],
        {
            "status": "finished",
            "winner_id": winner_id,
            "winner_name": winner_name,
            "settle_reason": detail,
        },
    )
    schedule_challenge_live_update(match["_id"], immediate=True)

    for _slot, player in entries:
        key = challenge_game_key(match["_id"], player["user_id"])
        await remove_game(key)

    guild = bot.get_guild(guild_id)
    channel = await resolve_channel(bot, match.get("channel_id"))
    if not isinstance(channel, discord.TextChannel):
        print(f"settle_challenge_match: origin channel missing for match {match.get('_id')}")
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
        wname = getattr(winner_user, "display_name", None) or winner_user.name
        if wname and wname != winner_name:
            await match_store.update_match(match["_id"], {"winner_name": wname})
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

    schedule_challenge_live_update(match_id, immediate=True)

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
        schedule_challenge_live_update(match_id, immediate=True)
        await settle_challenge_match(bot, match, reason="quit")


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
        assert interaction.guild is not None
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
            difficulty=tier,
            player_names=player_names,
        )
        match_id = await match_store.insert_match(doc)
        match = await match_store.get_match(match_id)
        assert match is not None
        start_time = float(match["start_time"])
        slots = match["player_slots"]

        names = " · ".join(m.display_name for m in players)
        destinations: list[tuple[str, discord.Member, discord.abc.Messageable]] = []
        # Open every destination first — avoid half-started matches
        for slot, member in zip(slots, players):
            dest = await open_private_match_channel(
                home,
                member,
                f"sudoku-{len(players)}p-{member.display_name}"[:90],
            )
            thread_id = getattr(dest, "id", None)
            await match_store.update_player(
                match_id, slot, {"thread_id": thread_id, "name": member.display_name}
            )
            destinations.append((slot, member, dest))

        roster = ", ".join(m.mention for m in players)
        for slot, member, dest in destinations:
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
                channel_id=getattr(dest, "id", home.id),
                guild_id=interaction.guild.id,
                match_id=match_id,
                player_slot=slot,
                difficulty=difficulty,
                started_at=start_time,
                pin_emojis=owned_pin_emojis(pstats),
            )
            try:
                await dest.send(
                    f"{member.mention} Speedrun ({len(players)} players) · **{tier}**\n"
                    f"Field: {names} — go!"
                )
                await post_game_panel(dest, key, games[key])
                await persist_game(key, games[key])
            except discord.HTTPException as exc:
                raise RuntimeError(
                    f"Couldn't deliver board to {member.mention} ({exc}). "
                    "Open DMs from server members or grant thread permissions."
                ) from exc

        await interaction.followup.send(
            f"Challenge started ({len(players)}): {roster} · **{tier}**. "
            "Private boards are open — fastest clean solve wins.",
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


def challenge_standings_lines(match: dict, guild: discord.Guild | None) -> list[str]:
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
            elapsed = player.get("elapsed")
            if elapsed is None:
                elapsed = float(player["finished_time"]) - start
            lines.append(f"✅ {mention} — **{format_time(elapsed)}**")
            continue
        filled = challenge_board_filled(player.get("current_board") or [])
        elapsed = time.time() - start
        marker = "🎮 " if player.get("user_id") == active_uid else "▶ "
        lines.append(f"{marker}{mention} — {filled}/{CHALLENGE_BOARD_CELLS} · {format_time(elapsed)}")
    return lines


def build_challenge_live_embed(match: dict, guild: discord.Guild | None) -> discord.Embed:
    tier = difficulty_label(match.get("difficulty"))
    if match.get("status") == "finished":
        title = "Challenge ended"
        footer = "Race over."
    else:
        title = f"Live challenge — {tier}"
        footer = "Fastest clean solve wins · /watch to spectate"
    embed = paper_embed(title)
    embed.description = "\n".join(challenge_standings_lines(match, guild)) or "No players."
    active = challenge_active_player(match)
    if active and match.get("status") != "finished":
        _slot, player = active
        embed.add_field(
            name="Now playing",
            value=f"{challenge_player_mention(guild, player)} is on the board.",
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
    match = await match_store.get_match(match_id)
    if not match or not match.get("live_message_id"):
        return
    channel = await resolve_channel(bot_ref, match.get("channel_id"))
    if channel is None:
        return
    guild = bot_ref.get_guild(int(match.get("guild_id") or 0))
    try:
        msg = await channel.fetch_message(int(match["live_message_id"]))
    except (discord.HTTPException, discord.NotFound):
        return
    embed = build_challenge_live_embed(match, guild)
    finished = match.get("status") == "finished"
    view = None if finished else build_challenge_watch_view(match, bot_ref)
    try:
        await msg.edit(embed=embed, view=view)
        if view is not None:
            view.message = msg
            bot_ref.add_view(view)
    except discord.HTTPException as exc:
        print(f"update_challenge_live_message failed for {match_id}: {exc}")


def schedule_challenge_live_update(match_id: str, *, immediate: bool = False) -> None:
    if immediate:
        asyncio.create_task(update_challenge_live_message(bot, match_id))
        return
    existing = _challenge_live_tasks.get(match_id)
    if existing and not existing.done():
        existing.cancel()

    async def _debounced() -> None:
        try:
            await asyncio.sleep(CHALLENGE_LIVE_DEBOUNCE_SEC)
            await update_challenge_live_message(bot, match_id)
        except asyncio.CancelledError:
            pass
        finally:
            if _challenge_live_tasks.get(match_id) is asyncio.current_task():
                _challenge_live_tasks.pop(match_id, None)

    _challenge_live_tasks[match_id] = asyncio.create_task(_debounced())


async def post_challenge_live_panel(
    bot_ref: "SudokuBot",
    home: discord.TextChannel,
    match: dict,
    guild: discord.Guild,
) -> None:
    match_id = match["_id"]
    embed = build_challenge_live_embed(match, guild)
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
        elapsed = int(session.get("elapsed") or 0)
        tier = difficulty_label(session.get("difficulty"))
        marker = "🎮 " if str(session.get("user_id")) == active_uid else "▶ "
        lines.append(
            f"{marker}{mention} — **{tier}** · {filled}/{CHALLENGE_BOARD_CELLS} · "
            f"{format_time(elapsed)}"
        )
    embed = paper_embed("Live /play")
    embed.description = "\n".join(lines) or "Nobody playing right now."
    if active_uid:
        for session in sessions:
            if str(session.get("user_id")) == active_uid:
                embed.add_field(
                    name="Now playing",
                    value=f"{activity_session_mention(guild, session)} is on the board.",
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


async def notify_activity_play_started(
    bot_ref: "SudokuBot",
    session_id: str,
    *,
    fallback_user: discord.abc.User | None = None,
    force: bool = False,
    watch_channel_id: int | None = None,
) -> None:
    """Post a one-time watch invite when someone starts /play (no live updates)."""
    channel_id = int(watch_channel_id or ACTIVITY_WATCH_CHANNEL_ID or 0)
    if not channel_id:
        print(f"activity watch notify skipped for {session_id}: no watch channel configured")
        return
    if session_id in _activity_notify_inflight:
        print(f"activity watch notify skipped for {session_id}: already in flight")
        return

    session = await match_store.get_activity_session(session_id)
    if not force and await activity_watch_is_live(bot_ref, session):
        print(f"activity watch notify skipped for {session_id}: announcement already live")
        return

    _activity_notify_inflight.add(session_id)
    try:
        session = await match_store.get_activity_session(session_id)
        if not force and await activity_watch_is_live(bot_ref, session):
            print(f"activity watch notify skipped for {session_id}: announcement already live")
            return

        channel = await resolve_channel(bot_ref, channel_id)
        if channel is None:
            print(f"activity watch channel {channel_id} not found for {session_id}")
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
            return

        view = ActivityPlayWatchView(session_id, bot_ref)
        msg = await channel.send(
            content=f"{mention} is playing — you can watch here.",
            view=view,
        )
        view.message = msg
        bot_ref.add_view(view)
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
            },
        )
        print(f"activity watch posted for {session_id} in {channel_id}")
    except discord.HTTPException as exc:
        print(f"notify_activity_play_started failed for {session_id}: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"notify_activity_play_started error for {session_id}: {exc}")
    finally:
        _activity_notify_inflight.discard(session_id)


async def notify_activity_play_from_launch(
    bot_ref: "SudokuBot",
    interaction: discord.Interaction,
) -> None:
    if interaction.guild is None:
        return
    session_id = f"activity:{interaction.guild.id}:{interaction.user.id}"
    watch_channel_id = ACTIVITY_WATCH_CHANNEL_ID
    if not watch_channel_id and isinstance(interaction.channel, discord.abc.GuildChannel):
        watch_channel_id = int(interaction.channel.id)
    print(
        f"activity watch launch notify for {session_id} "
        f"channel={watch_channel_id or 'unset'}"
    )
    existing = await match_store.get_activity_session(session_id)
    if existing:
        await delete_activity_watch_message(bot_ref, existing, session_id=session_id)
        await match_store.merge_activity_session(
            session_id,
            {
                "watch_notified": False,
                "watch_message_id": None,
            },
        )
    await notify_activity_play_started(
        bot_ref,
        session_id,
        fallback_user=interaction.user,
        force=True,
        watch_channel_id=watch_channel_id or None,
    )


async def _notify_activity_play_from_launch_safe(
    bot_ref: "SudokuBot",
    interaction: discord.Interaction,
) -> None:
    try:
        await notify_activity_play_from_launch(bot_ref, interaction)
    except Exception as exc:  # noqa: BLE001
        print(f"notify_activity_play_from_launch failed: {type(exc).__name__}: {exc}")


async def delete_activity_watch_message(
    bot_ref: "SudokuBot",
    session: dict,
    *,
    session_id: str | None = None,
) -> None:
    """Remove the channel watch announcement when the /play session ends."""
    raw_msg = session.get("watch_message_id")
    if not raw_msg:
        return
    sid = session_id or session.get("_id")
    channel_id = session.get("watch_channel_id") or ACTIVITY_WATCH_CHANNEL_ID
    channel = await resolve_channel(bot_ref, int(channel_id))
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(int(raw_msg))
        await msg.delete()
    except discord.HTTPException as exc:
        print(f"delete_activity_watch_message failed: {exc}")
        if sid and (getattr(exc, "code", None) == 10008 or exc.status == 404):
            await match_store.merge_activity_session(
                str(sid),
                {
                    "watch_message_id": None,
                    "watch_notified": False,
                },
            )
            print(f"cleared stale activity watch message id for {sid}")


async def end_activity_watch(bot_ref: "SudokuBot", session_id: str) -> None:
    """Remove the watch announcement but keep the in-progress board for resume."""
    session = await match_store.get_activity_session(session_id)
    if not session:
        return
    posted_at = float(session.get("watch_posted_at") or 0)
    if posted_at and (time.time() - posted_at) < ACTIVITY_WATCH_END_GRACE_SEC:
        print(f"activity watch end ignored (grace) for {session_id}")
        return
    await delete_activity_watch_message(bot_ref, session)
    await match_store.merge_activity_session(
        session_id,
        {
            "watch_notified": False,
            "watch_message_id": None,
        },
    )
    print(f"activity watch ended for {session_id}")


async def clear_activity_session(bot_ref: "SudokuBot", session_id: str) -> None:
    """Delete watch announcement (if any) and drop the persisted session."""
    session = await match_store.get_activity_session(session_id)
    if session:
        await delete_activity_watch_message(bot_ref, session)
    await match_store.delete_activity_session(session_id)


def schedule_activity_play_notify(session_id: str) -> None:
    asyncio.create_task(notify_activity_play_started(bot, session_id))


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
                max_age_sec=ACTIVITY_WATCH_MAX_AGE_SEC,
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
    """Persistent Live button on the channel watch announcement."""

    def __init__(self, session_id: str, bot_ref: "SudokuBot"):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.bot = bot_ref
        self.message: discord.Message | None = None

        live_btn = discord.ui.Button(
            label="Live",
            style=discord.ButtonStyle.primary,
            custom_id=f"watchplay:{session_id}:live",
        )
        live_btn.callback = self._on_live
        self.add_item(live_btn)

    async def _on_live(self, interaction: discord.Interaction) -> None:
        await open_activity_live_spectator(interaction, self.session_id, self.bot)


def build_activity_spectator_payload(session: dict) -> tuple[str, discord.File] | None:
    board_raw = session.get("board")
    given = session.get("given")
    if not board_raw or not isinstance(given, list) or len(given) != 9:
        return None
    board = normalize_board(board_raw)
    name = str(session.get("name") or "Player")
    filled = int(session.get("filled") or 0)
    tier = difficulty_label(session.get("difficulty"))
    elapsed = int(session.get("elapsed") or 0)
    image = render_board(board, given, difficulty=session.get("difficulty"))
    file = board_to_file(image)
    if filled >= CHALLENGE_BOARD_CELLS:
        status = "puzzle complete"
    else:
        status = "live — updates every few seconds"
    content = (
        f"**{name}** — {tier} · {filled}/{CHALLENGE_BOARD_CELLS} · "
        f"{format_time(elapsed)} · {status}"
    )
    return content, file


async def wait_for_activity_board(session_id: str, *, attempts: int = 10) -> dict | None:
    session: dict | None = None
    for attempt in range(attempts):
        session = await match_store.get_activity_session(session_id)
        if not session:
            return None
        if build_activity_spectator_payload(session) is not None:
            return session
        if attempt < attempts - 1:
            await asyncio.sleep(2.0)
    return session


class ActivityPlayLiveSpectatorView(discord.ui.View):
    """Ephemeral auto-refreshing board for /play spectators."""

    def __init__(self, session_id: str, bot_ref: "SudokuBot"):
        super().__init__(timeout=ACTIVITY_LIVE_SPECTATOR_TIMEOUT_SEC)
        self.session_id = session_id
        self.bot = bot_ref
        self.message: discord.Message | None = None
        self._poll_task: asyncio.Task | None = None
        self._closed = False
        self._last_move_at = 0.0
        self._last_filled = -1
        self._last_elapsed = -1

        refresh_btn = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        refresh_btn.callback = self._on_refresh
        self.add_item(refresh_btn)

        stop_btn = discord.ui.Button(
            label="Stop",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        stop_btn.callback = self._on_stop
        self.add_item(stop_btn)

    def start_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    def _stop_polling(self) -> None:
        self._closed = True
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = None

    async def _poll_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(ACTIVITY_LIVE_REFRESH_SEC)
                if self._closed or self.message is None:
                    break
                await self._refresh_message()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            print(f"activity live spectator poll failed for {self.session_id}: {exc}")

    def _session_changed(self, session: dict) -> bool:
        move_at = float(session.get("last_move_at") or session.get("updated_at") or 0)
        filled = int(session.get("filled") or 0)
        elapsed = int(session.get("elapsed") or 0)
        changed = (
            move_at != self._last_move_at
            or filled != self._last_filled
            or elapsed != self._last_elapsed
        )
        if changed:
            self._last_move_at = move_at
            self._last_filled = filled
            self._last_elapsed = elapsed
        return changed

    async def _refresh_message(self, *, force: bool = False) -> bool:
        if self.message is None or self._closed:
            return False
        session = await match_store.get_activity_session(self.session_id)
        if not session:
            self._stop_polling()
            try:
                await self.message.edit(content="This game has ended.", attachments=[], view=None)
            except discord.HTTPException:
                pass
            self.stop()
            return False
        payload = build_activity_spectator_payload(session)
        if payload is None:
            return False
        if not force and not self._session_changed(session):
            return True
        content, file = payload
        try:
            await self.message.edit(content=content, attachments=[file], view=self)
        except discord.HTTPException as exc:
            print(f"activity live spectator edit failed for {self.session_id}: {exc}")
            self._stop_polling()
            self.stop()
            return False
        if int(session.get("filled") or 0) >= CHALLENGE_BOARD_CELLS:
            self._stop_polling()
            self.stop()
        return True

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._refresh_message(force=True)

    async def _on_stop(self, interaction: discord.Interaction) -> None:
        self._stop_polling()
        self.stop()
        try:
            await interaction.response.edit_message(
                content="Stopped watching.",
                attachments=[],
                view=None,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        self._stop_polling()
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


async def open_activity_live_spectator(
    interaction: discord.Interaction,
    session_id: str,
    bot_ref: "SudokuBot",
) -> None:
    await interaction.response.defer(ephemeral=True)
    session = await wait_for_activity_board(session_id)
    if not session:
        await interaction.followup.send("This game has ended.", ephemeral=True)
        return
    payload = build_activity_spectator_payload(session)
    if payload is None:
        name = str(session.get("name") or "Player")
        await interaction.followup.send(
            f"**{name}** is playing, but the board hasn't synced yet — try **Live** again.",
            ephemeral=True,
        )
        return
    content, file = payload
    view = ActivityPlayLiveSpectatorView(session_id, bot_ref)
    view._last_move_at = float(session.get("last_move_at") or session.get("updated_at") or 0)
    view._last_filled = int(session.get("filled") or 0)
    view._last_elapsed = int(session.get("elapsed") or 0)
    msg = await interaction.followup.send(
        content=content,
        file=file,
        view=view,
        ephemeral=True,
    )
    view.message = msg
    view.start_polling()


class ActivityWatchMenuView(discord.ui.View):
    """Ephemeral /watch menu — Live button per active /play session."""

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
        self.clear_items()
        for session in sessions[:5]:
            name = str(session.get("name") or "Player")[:20]
            sid = str(session.get("_id") or "")
            if not sid:
                continue
            btn = discord.ui.Button(
                label=f"Live — {name}",
                style=discord.ButtonStyle.primary,
            )
            btn.callback = self._make_live_cb(sid)
            self.add_item(btn)

    def _make_live_cb(self, session_id: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await open_activity_live_spectator(interaction, session_id, self.bot)

        return _cb


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
        if guild is None or challenge_home_channel(interaction.channel) is None:
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
        # Soft-disable until launch succeeds
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.defer()
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
        guild_id = guild.id if guild is not None else game.get("guild_id")
        if guild_id is None:
            await remove_game(self.game_key)
            return
        embed = finish_forfeit(self.bot.data, int(guild_id), interaction.user, game)
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
            if not game.get("rewarded"):
                outcome = await finish_win_and_announce(
                    self.bot,
                    guild_id,
                    interaction.user,
                    game,
                )
                game["rewarded"] = True
                coins = int(outcome.coins)
                xp = int(outcome.xp)
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
                win_reward_caption(coins, xp)
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

def shop_catalog(kind: str) -> list[dict]:
    """Browseable catalog entries for the Krusty Shop."""
    if kind == "titles":
        return [
            {
                "kind": "title",
                "id": tid,
                "label": meta["label"],
                "emoji": meta.get("emoji", SPONGE),
                "cost": int(meta["cost"]),
                "pin": meta.get("pin") or cosmetic_pin_text(meta),
            }
            for tid, meta in SHOP_TITLES.items()
        ]
    return [
        {
            "kind": "pin",
            "id": tid,
            "label": meta["label"],
            "emoji": meta.get("emoji", WAVE),
            "cost": int(meta["cost"]),
            "pin": meta.get("pin") or cosmetic_pin_text(meta),
        }
        for tid, meta in SHOP_PINS.items()
    ]


SHOP_PAGE_SIZE = 8


def shop_item_owned(stats: dict, item: dict) -> bool:
    if item["kind"] == "title":
        return item["id"] in (stats.get("owned_titles") or [])
    return item["id"] in owned_pin_ids(stats)


def shop_item_equipped(stats: dict, item: dict) -> bool:
    if item["kind"] == "title":
        return stats.get("title") == item["id"]
    # Pins have no equip slot — all owned pins show on the border
    return shop_item_owned(stats, item)


def shop_item_status_text(stats: dict, item: dict) -> str:
    owned = shop_item_owned(stats, item)
    if item["kind"] == "pin":
        return "Owned" if owned else "Locked"
    if shop_item_equipped(stats, item):
        return "Equipped"
    if owned:
        return "Owned"
    return "Locked"


def shop_item_price_text(item: dict) -> str:
    cost = int(item["cost"])
    return "FREE" if cost <= 0 else format_sponges(cost)


def shop_item_can_buy(stats: dict, item: dict) -> bool:
    if shop_item_owned(stats, item):
        return False
    cost = int(item["cost"])
    return cost <= 0 or int(stats.get("coins") or 0) >= cost


def shop_filter_catalog(
    items: list[dict], stats: dict, filt: str
) -> list[dict]:
    """filt: all | afford | owned"""
    if filt == "owned":
        return [it for it in items if shop_item_owned(stats, it)]
    if filt == "afford":
        return [it for it in items if shop_item_can_buy(stats, it)]
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
    """Paginated catalog embed with the selected item highlighted."""
    tab = "Titles" if kind == "titles" else "Pins"
    filter_label = {"all": "All", "afford": "Can buy", "owned": "Owned"}.get(filt, "All")
    embed = paper_embed(f"{SPONGE} Krusty Shop · {tab}")
    hint = (
        "Pick an item in the menu, then **Buy** / **Equip**. "
        "Pins also have **Preview**."
        if kind == "pins"
        else "Pick an item in the menu, then **Buy** or **Equip** for header flair."
    )
    lines: list[str] = []
    selected_id = (selected or {}).get("id")
    for it in page_items:
        mark = "▸ " if it["id"] == selected_id else "· "
        status = shop_item_status_text(stats, it)
        # Affordable but locked → show Can buy instead of Locked
        if status == "Locked" and shop_item_can_buy(stats, it):
            status = "Can buy"
        price = shop_item_price_text(it)
        lines.append(f"{mark}{it['label']} — **{price}** · {status}")
    if not lines:
        lines.append("_No items match this filter._")

    embed.description = (
        f"{hint}\n"
        f"*Filter: **{filter_label}** · Page **{page + 1}/{max(1, pages)}** "
        f"({filtered_total} items)*\n\n"
        + "\n".join(lines)
        + f"\n\n*No refunds. Squidward is watching.*"
    )
    embed.add_field(
        name=f"Pocket {SPONGE}",
        value=f"**{format_sponges(stats.get('coins', 0))}**",
        inline=True,
    )

    if selected:
        status = shop_item_status_text(stats, selected)
        if status == "Locked" and shop_item_can_buy(stats, selected):
            status = "Can buy"
        embed.add_field(
            name="Selected",
            value=f"**{selected['label']}**\n{shop_item_price_text(selected)} · {status}",
            inline=True,
        )
        if selected["kind"] == "title":
            sample = titled_header_line(
                "Easy",
                selected.get("pin") or "Civilian",
                emoji=str(selected.get("emoji") or ""),
            )
            embed.add_field(
                name="What you get",
                value=f"Header flair — e.g. `{sample}`",
                inline=False,
            )
        else:
            embed.add_field(
                name="What you get",
                value=f"Border pin {selected['emoji']} on your boards.",
                inline=False,
            )
    else:
        embed.add_field(name="Selected", value="_Nothing_", inline=True)

    eq_title = (
        SHOP_TITLES[stats["title"]]["label"]
        if stats.get("title") in SHOP_TITLES
        else "Civilian"
    )
    pin_n = len(owned_pin_emojis(stats))
    embed.add_field(
        name="Your look",
        value=f"Title: {eq_title} · Border pins: **{pin_n}**",
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
        sync_pins_to_active_games(user_id, guild_id, owned_pin_emojis(stats))
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
    sync_title_to_active_games(user_id, guild_id, tid)
    sync_pins_to_active_games(user_id, guild_id, owned_pin_emojis(stats))
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
        sync_title_to_active_games(user_id, guild_id, tid)
        sync_pins_to_active_games(user_id, guild_id, owned_pin_emojis(stats))
        return {
            "ok": True,
            "bought": True,
            "label": item["label"],
            "cost": cost,
            "message": f"Bought **{item['label']}**!",
        }

    tid = item["id"]
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
    sync_pins_to_active_games(user_id, guild_id, owned_pin_emojis(stats))
    return {
        "ok": True,
        "bought": True,
        "label": item["label"],
        "cost": cost,
        "message": f"Bought pin **{item['label']}**!",
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
        super().__init__(timeout=180)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.kind = kind if kind in ("titles", "pins") else "titles"
        self.filt = filt if filt in ("all", "afford", "owned") else "all"
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

        # Row 0 — tabs
        titles_btn = discord.ui.Button(
            label="Titles",
            style=discord.ButtonStyle.primary if self.kind == "titles" else discord.ButtonStyle.secondary,
            row=0,
        )
        pins_btn = discord.ui.Button(
            label="Pins",
            style=discord.ButtonStyle.primary if self.kind == "pins" else discord.ButtonStyle.secondary,
            row=0,
        )
        titles_btn.callback = self.on_titles
        pins_btn.callback = self.on_pins
        self.add_item(titles_btn)
        self.add_item(pins_btn)

        # Row 1 — filters
        for key, label in (("all", "All"), ("afford", "Can buy"), ("owned", "Owned")):
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
                if status == "Locked" and shop_item_can_buy(stats, it):
                    status = "Can buy"
                price = shop_item_price_text(it)
                desc = f"{price} · {status}"[:100]
                label = it["label"][:100]
                options.append(
                    discord.SelectOption(
                        label=label,
                        value=it["id"],
                        description=desc,
                        emoji=it.get("emoji") or None,
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

        # Row 3 — page nav
        prev_btn = discord.ui.Button(
            label="◀",
            style=discord.ButtonStyle.secondary,
            row=3,
            disabled=self.page_count() <= 1,
        )
        next_btn = discord.ui.Button(
            label="▶",
            style=discord.ButtonStyle.secondary,
            row=3,
            disabled=self.page_count() <= 1,
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
                    label="Owned",
                    style=discord.ButtonStyle.success,
                    row=4,
                    disabled=True,
                )
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
            action = discord.ui.Button(
                label=f"Buy ({cost} {SPONGE})" if cost else "Claim FREE",
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
        self._rebuild()
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self, attachments=[]
        )

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
        result = apply_shop_purchase(self.bot, self.guild_id, self.owner_id, item)
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
        announce = (
            f"{SPONGE} {who} bought **{result['label']}** "
            f"(−{cost} {SPONGE}) · pocket now **{pocket}**!"
        )
        try:
            if interaction.channel is not None:
                await interaction.channel.send(announce)
            else:
                await interaction.followup.send(announce)
        except discord.HTTPException:
            await interaction.followup.send(result["message"], ephemeral=True)

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


async def reply_ephemeral(interaction: discord.Interaction, content: str) -> None:
    """Send an ephemeral reply whether or not the interaction was already deferred."""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def start_panel(
    interaction: discord.Interaction,
    key: tuple[int, int],
    game: dict,
    *,
    silent: bool = True,
) -> None:
    view = SudokuView(key, bot)
    content, file = board_file_for(game)
    # /play stays silent; /daily can notify the channel
    if interaction.response.is_done():
        msg = await interaction.followup.send(
            content=content, view=view, file=file, silent=silent
        )
        view.message = msg
    else:
        await interaction.response.send_message(
            content=content, view=view, file=file, silent=silent
        )
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
        # Solved boards: keep rewarded so restart doesn't double-pay
        if is_solved(game.get("board") or [], game.get("solution")):
            game["rewarded"] = True
        else:
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
        if channel is None or not game.get("message_id"):
            # Dead panel (deleted thread / lost DM) — don't block /challenge forever
            print(
                f"Dropping unrecoverable session {serialize_game_key(key)} "
                f"(channel={game.get('channel_id')})"
            )
            if game.get("mode") == "challenge" and game.get("match_id") and game.get("player_slot"):
                try:
                    await match_store.update_player(
                        game["match_id"],
                        game["player_slot"],
                        {"forfeit": True, "finished_time": None},
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"forfeit dropped challenge session failed: {exc}")
            await drop_persisted_game(key)
            dropped += 1
            continue

        games[key] = game
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
        # Refresh match after any forfeit marks from dropped sessions
        try:
            fresh = await match_store.get_match(mid)
        except Exception:
            fresh = match
        if fresh and challenge_ready_to_settle(fresh):
            await settle_challenge_match(bot, fresh, reason="restart — no live boards")
        else:
            await match_store.update_match(
                mid,
                {"status": "finished", "settle_reason": "bot restart — abandoned", "winner_id": None},
            )

    print(
        f"Restored {restored} active game panel(s); dropped {dropped} unrecoverable; "
        f"{len(games)} session(s) in memory."
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Activity watch channel id: {ACTIVITY_WATCH_CHANNEL_ID or 'unset'}")
    if not rotate_status.is_running():
        rotate_status.start()
    await bot.change_presence(activity=STATUS_ROTATION[0])
    await restore_persisted_sessions(bot)
    await restore_challenge_watch_views(bot)
    await restore_activity_play_watch_views(bot)


@bot.tree.command(
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


async def _launch_activity_window(interaction: discord.Interaction) -> None:
    """Open the Embedded App Activity (Wordle-style game window)."""
    try:
        await interaction.response.launch_activity()
        print(
            f"launch_activity ok user={interaction.user} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"channel={getattr(interaction.channel, 'id', None)}"
        )
        if interaction.guild is not None:
            asyncio.create_task(_notify_activity_play_from_launch_safe(bot, interaction))
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
    description="Open the Thcoku game window in this channel (like Wordle)",
)
async def play_cmd(interaction: discord.Interaction):
    await _launch_activity_window(interaction)


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
            "`/play` — **opens the game window** (Activity)\n"
            "`/daily` — one pineapple puzzle a day\n"
            "`/challenge` — race your pals on private boards\n"
            "`/watch` — spectate active `/play` games and challenge races"
        ),
        inline=False,
    )
    embed.add_field(name="① Cell", value="Tap a square on the board", inline=True)
    embed.add_field(name="② Number", value="1–9 on the pad", inline=True)
    embed.add_field(name="③ Clear", value="Apagar · Notes = Lápis", inline=True)
    embed.add_field(
        name=f"{JELLY} Rules",
        value=(
            "Red cells = clash in row / column / box.\n"
            "**Lápis** = notes · solve to earn XP in chat."
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
            f"Solve **{format_xp(BASE_WIN_REWARD, signed=True)}** + "
            f"**{format_sponges(BASE_WIN_REWARD, signed=True)}** · "
            f"Daily **+{DAILY_BONUS}** each · "
            f"Streak **+{STREAK_BONUS_PER}**/lvl · "
            f"Challenge win **×{CHALLENGE_WIN_MULT:g}** · "
            f"loss **{format_sponges(CHALLENGE_LOSER_COINS, signed=True)}** (sponges only)\n"
            f"{tiers}"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{PINEAPPLE} More",
        value="`/shop` · `/stats` · `/leaderboard` · `/quit`",
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


@bot.tree.command(
    name="watch",
    description="Spectate active /play games and challenge races",
)
async def watch_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    guild_matches: list[dict] = []
    activity_sessions: list[dict] = []

    try:
        active = await match_store.list_matches(status="active")
        guild_matches = [m for m in active if m.get("guild_id") == guild_id]
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            f"Couldn't list challenge matches ({exc}).",
            ephemeral=True,
        )
        return

    try:
        activity_sessions = await match_store.list_activity_sessions(
            guild_id,
            max_age_sec=ACTIVITY_WATCH_MAX_AGE_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        await interaction.response.send_message(
            f"Couldn't list Activity sessions ({exc}).",
            ephemeral=True,
        )
        return

    if not guild_matches and not activity_sessions:
        await interaction.response.send_message(
            "No active games right now. Start `/play` or `/challenge`.",
            ephemeral=True,
        )
        return

    if activity_sessions and not guild_matches:
        embed = build_activity_live_embed(activity_sessions, interaction.guild)
        view = build_activity_watch_view(guild_id, None, bot, activity_sessions)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return

    if guild_matches and not activity_sessions:
        if len(guild_matches) == 1:
            match = guild_matches[0]
            embed = build_challenge_live_embed(match, interaction.guild)
            view = build_challenge_watch_view(match, bot)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return
        lines: list[str] = []
        for match in guild_matches:
            roster = ", ".join(
                challenge_player_mention(interaction.guild, player)
                for _slot, player in match_player_entries(match)
            )
            tier = difficulty_label(match.get("difficulty"))
            short_id = str(match.get("_id", ""))[:8]
            channel = await resolve_channel(bot, match.get("channel_id"))
            where = f"<#{channel.id}>" if isinstance(channel, discord.TextChannel) else "channel"
            lines.append(f"`{short_id}` · **{tier}** in {where} — {roster}")
        embed = paper_embed("Active challenges")
        embed.description = (
            "\n".join(lines)
            + "\n\nOpen the **Live challenge** panel in those channels, or run `/watch` "
            "when only one race is active."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = paper_embed("Live games")
    embed.add_field(
        name="Activity /play",
        value=build_activity_live_embed(activity_sessions, interaction.guild).description
        or "—",
        inline=False,
    )
    challenge_lines: list[str] = []
    for match in guild_matches:
        roster = ", ".join(
            challenge_player_mention(interaction.guild, player)
            for _slot, player in match_player_entries(match)
        )
        tier = difficulty_label(match.get("difficulty"))
        challenge_lines.append(f"**{tier}** — {roster}")
    embed.add_field(
        name="Challenges",
        value="\n".join(challenge_lines) or "—",
        inline=False,
    )
    embed.set_footer(text="Use the buttons below for /play boards · challenges have channel panels")
    view = build_activity_watch_view(guild_id, None, bot, activity_sessions)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


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
        existing = games[sk]
        if is_solved(existing.get("board") or [], existing.get("solution")):
            await interaction.response.defer()
            await close_solved_session(bot, sk, existing, interaction.user, guild_id)
            # Fall through — allow a new daily only if today's slot is free
        else:
            await interaction.response.send_message(
                f"Finish your **{existing['mode']}** game first (**Quit** / `/quit`).",
                ephemeral=True,
            )
            return

    daily = get_guild_daily(bot.data, guild_id)
    day = daily["date"]
    uid = str(user_id)

    async def _deny_already_done(detail: str) -> None:
        await reply_ephemeral(
            interaction,
            f"{PINEAPPLE} You've already **{detail}** today's daily (`{day}`).\n"
            f"Only **one** pineapple puzzle per day — play more with `/play`.",
        )

    if uid in daily["results"]:
        r = daily["results"][uid]
        if r.get("in_progress"):
            restored = await load_persisted_game(sk)
            if restored and restored.get("mode") == "daily":
                await reply_ephemeral(
                    interaction,
                    "You already started today's daily — continue on your board message "
                    "(use **Refresh** if the buttons timed out).",
                )
                return
            # Orphan lock (restart without recoverable session) — unlock and continue
            daily["results"].pop(uid, None)
            save_data(bot.data)
        else:
            if r.get("won"):
                detail = "cleared"
            elif r.get("forfeit"):
                detail = "used (quit)"
            else:
                detail = "used"
            await _deny_already_done(detail)
            return

    # Durable Mongo claim (survives local wipe / redeploy)
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
        print(f"has_daily_claim failed: {exc}")

    # Lock the attempt immediately so a restart can't grant a second daily
    daily["results"][uid] = {
        "won": False,
        "in_progress": True,
        "name": interaction.user.display_name,
    }
    save_data(bot.data)

    board, given, solution, diff_key = make_daily_puzzle(guild_id, daily["date"], user_id)
    gstats = guild_stats(bot.data, guild_id)
    stats = user_stats(gstats, user_id)
    games[sk] = new_game_state(
        mode="daily",
        board=board,
        given=given,
        solution=solution,
        owner_id=user_id,
        owner_name=interaction.user.display_name,
        owner_title=equipped_title_id(stats),
        channel_id=interaction.channel_id,
        guild_id=guild_id,
        daily_date=daily["date"],
        difficulty=diff_key,
        pin_emojis=owned_pin_emojis(stats),
    )
    try:
        await start_panel(interaction, sk, games[sk], silent=False)
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
        kind="titles",
    )
    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


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
            prompt = "Really quit this puzzle? Streak will reset."
        await interaction.response.send_message(
            prompt,
            view=ConfirmQuitView(sk, bot, None),
            ephemeral=True,
        )
        return

    # Orphan daily lock (no live session) — clear so they aren't stuck
    daily = get_guild_daily(bot.data, guild_id)
    entry = daily.get("results", {}).get(str(interaction.user.id))
    if entry and entry.get("in_progress") and not entry.get("won"):
        daily["results"].pop(str(interaction.user.id), None)
        save_data(bot.data)
        await drop_persisted_game(sk)
        await interaction.response.send_message(
            f"{BUBBLE} Cleared a stuck daily lock. You can try `/daily` or `/play` again.",
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


@bot.tree.command(name="stats", description="Your Bikini Bottom Sudoku report card")
@app_commands.describe(member="Peek at a neighbor's stats")
async def stats_cmd(interaction: discord.Interaction, member: discord.Member | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    target = member or interaction.user
    gstats = guild_stats(bot.data, interaction.guild.id)
    s = user_stats(gstats, target.id)
    s["name"] = target.display_name
    save_data(bot.data)
    best = format_time(s["best_time"]) if s.get("best_time") is not None else "— not yet!"
    title = SHOP_TITLES[s["title"]]["label"] if s.get("title") in SHOP_TITLES else "Civilian"
    streak = int(s.get("streak", 0))
    best_streak = int(s.get("best_streak", 0))
    wins = int(s.get("wins", 0))
    losses = int(s.get("losses", 0))
    games_n = int(s.get("games", 0)) or (wins + losses)
    win_rate = f"{(100 * wins / games_n):.0f}%" if games_n else "—"

    embed = paper_embed(f"{SPONGE} {display_name(s)}")
    embed.description = (
        f"{WAVE} Employee of the month? Maybe.\n"
        f"**Title:** {title} · **Form:** {streak_flavor(streak)}"
    )
    try:
        embed.set_thumbnail(url=target.display_avatar.url)
    except Exception:
        pass

    embed.add_field(name=f"Career XP {XP}", value=f"**{format_xp(s.get('xp', 0))}**", inline=True)
    embed.add_field(name=f"Pocket {SPONGE}", value=f"**{format_sponges(s['coins'])}**", inline=True)
    embed.add_field(
        name=f"Spent {SPONGE}",
        value=f"**{format_sponges(s.get('sponges_spent', 0))}**",
        inline=True,
    )
    embed.add_field(name=f"Streak {STAR}", value=f"**{streak}** (best {best_streak})", inline=True)
    embed.add_field(name="Win rate", value=f"**{win_rate}**", inline=True)
    embed.add_field(name="Wins", value=f"**{wins}**", inline=True)
    embed.add_field(name="Losses", value=f"**{losses}**", inline=True)
    embed.add_field(name="Best time", value=f"**{best}**", inline=True)
    embed.add_field(name=f"Daily {PINEAPPLE}", value=f"**{s.get('daily_wins', 0)}** clears", inline=True)
    embed.add_field(name=f"Challenge {JELLY}", value=f"**{s.get('challenge_wins', 0)}** wins", inline=True)
    embed.add_field(name="Boards played", value=f"**{games_n}**", inline=True)
    await interaction.response.send_message(embed=embed, silent=True)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "SEU_DISCORD_TOKEN_AQUI":
        raise SystemExit(
            "Missing DISCORD_TOKEN. Put it in .env:\n  DISCORD_TOKEN=seu_token_aqui"
        )
    start_health_server_early()
    bot.run(token)
