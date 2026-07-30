"""
Unified HTTP for Render: health + Activity static + OAuth/Mongo APIs.

Replaces Netlify Functions so bot + Activity share one service URL.
Discord Activity URL Mappings should point at this host (no https://).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from activity_watchers import prune_watchers


async def _activity_challenge_pending_response(uid: int) -> dict | None:
    """Block play/daily Activity HTTP while the user waits on an unsettled race."""
    from bot import challenge_blocks_user

    reason = await challenge_blocks_user(uid)
    if not reason:
        return None
    return {
        "ok": False,
        "error": "challenge_pending",
        "message": reason,
        "challenge": True,
    }


def _activity_last_move_at(
    existing: dict | None, filled: int, *, same_puzzle: bool
) -> float:
    """Only bump last_move_at when the player fills a new cell (not on heartbeat saves)."""
    now = time.time()
    if not existing:
        return now
    if not same_puzzle:
        return now
    prev_filled = int(existing.get("filled") or 0)
    if filled > prev_filled:
        return now
    prev = existing.get("last_move_at")
    if prev is not None:
        return float(prev)
    return now

BotGetter = Callable[[], Any]

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS, HEAD, DELETE",
}

MAX_HINTS_PLAY = 10  # legacy display only — paid hints are unlimited
MAX_HINTS_DAILY = 3  # legacy display only — paid hints are unlimited

CDN_PREFIXES = {
    "/pyscript/": "https://pyscript.net/",
    "/jsdelivr/": "https://cdn.jsdelivr.net/",
}

_CDN_CACHE_DIR = Path(os.getenv("CDN_CACHE_DIR") or "/tmp/thcoku-cdn-cache")
_activity_win_locks: dict[str, asyncio.Lock] = {}
_MAX_ACTIVITY_WIN_LOCKS = 512


def _activity_win_lock(session_id: str) -> asyncio.Lock:
    lock = _activity_win_locks.get(session_id)
    if lock is None:
        if len(_activity_win_locks) >= _MAX_ACTIVITY_WIN_LOCKS:
            stale = [k for k, v in _activity_win_locks.items() if not v.locked()]
            for k in stale[: len(_activity_win_locks) // 2]:
                _activity_win_locks.pop(k, None)
        lock = asyncio.Lock()
        _activity_win_locks[session_id] = lock
    return lock


def _preserve_server_hint_progress(existing: dict | None, doc: dict) -> None:
    """Never let an older autosave roll back a charged hint board."""
    if not existing:
        return
    server_hints = int(existing.get("hints_used") or 0)
    client_hints = int(doc.get("hints_used") or 0)
    server_gary = int(existing.get("hints_gary_used") or 0)
    client_gary = int(doc.get("hints_gary_used") or 0)
    doc["hints_used"] = max(server_hints, client_hints)
    doc["hints_gary_used"] = max(server_gary, client_gary)
    # Only restore the board when the server is ahead on hint count (stale autosave
    # after a charged reveal). Equal hints_used with a emptier board is an intentional Reset.
    if server_hints > client_hints and existing.get("board") is not None:
        doc["board"] = existing["board"]
        try:
            doc["filled"] = int(existing.get("filled") or doc.get("filled") or 0)
        except (TypeError, ValueError):
            pass


async def _lookup_activity_session(
    bot: Any, guild_id: str | int, user_id: str | int
) -> tuple[dict | None, str]:
    """Primary key → orphan activity:0 (same guild or unset) → find-by-user scoped.

    Preference-only primary docs (no board) do not shadow an orphan board.
    """
    from bot import match_store

    uid = int(user_id)
    gid = str(guild_id if guild_id is not None else "0")
    primary = _activity_session_id(gid, uid)
    session = await match_store.get_activity_session(primary)
    primary_has_board = bool(session and (session.get("board") or session.get("solution")))

    orphan_id = _activity_session_id("0", uid)
    orphan = None
    if orphan_id != primary:
        orphan = await match_store.get_activity_session(orphan_id)
        if orphan:
            orphan_gid = str(orphan.get("guild_id") or "0")
            if orphan_gid not in ("", "0", gid):
                orphan = None

    orphan_has_board = bool(orphan and (orphan.get("board") or orphan.get("solution")))

    # Prefer a real board: orphan wins over preference-only primary.
    if primary_has_board:
        return session, primary
    if orphan_has_board:
        return orphan, orphan_id
    if session:
        return session, primary
    if orphan:
        return orphan, orphan_id

    if gid not in ("", "0"):
        found = await match_store.find_activity_session_by_user_id(uid, guild_id=gid)
        if found:
            found_id = str(found.get("_id") or primary)
            return found, found_id
    else:
        # Client still has guild 0 — prefer any real-guild session with a board.
        found = await match_store.find_activity_session_by_user_id(uid)
        if found:
            found_id = str(found.get("_id") or "")
            found_gid = str(found.get("guild_id") or "0")
            if found_id and found_gid not in ("", "0"):
                return found, found_id
    return None, primary


async def _resolve_activity_guild_id(guild_id: str | int, user_id: str | int) -> str:
    """Resolve client guild. Prefer orphan/real session when client sends 0."""
    gid = str(guild_id if guild_id is not None else "0")
    if gid not in ("", "0"):
        return gid
    from bot import match_store

    # Prefer orphan doc that already stored a real guild (partial SDK race).
    orphan = await match_store.get_activity_session(_activity_session_id("0", user_id))
    if orphan and orphan.get("guild_id") and str(orphan["guild_id"]) not in ("", "0"):
        return str(orphan["guild_id"])
    # Session may already live under activity:{real}:{uid} after migration.
    recent = await match_store.find_activity_session_by_user_id(user_id)
    if recent and recent.get("guild_id") and str(recent["guild_id"]) not in ("", "0"):
        return str(recent["guild_id"])
    return "0"


async def _migrate_orphan_session(
    bot: Any, session: dict, orphan_id: str, canonical_id: str, gid_key: int, uid: int
) -> dict:
    """Copy full orphan session to canonical guild key and delete the orphan."""
    from bot import match_store

    if orphan_id == canonical_id:
        return session
    migrated = dict(session)
    migrated["_id"] = canonical_id
    migrated["guild_id"] = str(gid_key)
    migrated["user_id"] = str(uid)
    await match_store.upsert_activity_session(migrated)
    try:
        await match_store.delete_activity_session(orphan_id)
    except Exception as exc:  # noqa: BLE001
        print(f"orphan session delete failed: {exc}")
    return migrated


async def _cleanup_activity_session_after_win(
    bot: Any, session_id: str, uid: int
) -> None:
    """End watch + clear primary and orphan keys after a win (or already_won)."""
    from bot import clear_activity_session

    # clear_activity_session ends the watch first and refuses to drop the doc
    # while the Discord announcement is still live (avoids permanent orphans).
    try:
        cleared = await clear_activity_session(bot, session_id)
        if not cleared:
            print(
                f"activity win clear deferred for {session_id}: watch still live"
            )
            return
        wrong_id = _activity_session_id("0", uid)
        if wrong_id != session_id:
            await clear_activity_session(bot, wrong_id)
    except Exception as exc:  # noqa: BLE001
        print(f"activity win clear session failed: {exc}")


def _proxy_cdn(path: str) -> tuple[int, bytes, str] | None:
    """Fetch PyScript / Pyodide via disk-cached proxy (avoids re-download + OOM)."""
    for prefix, origin in CDN_PREFIXES.items():
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix) :]
        url = origin + rel
        cache_path = _CDN_CACHE_DIR / prefix.strip("/").replace("/", "_") / rel
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            ctype, _ = mimetypes.guess_type(str(cache_path))
            return 200, cache_path.read_bytes(), ctype or "application/octet-stream"

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type") or mimetypes.guess_type(path)[0]
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(data)
                except OSError as exc:
                    print(f"CDN cache write failed: {exc}")
                return 200, data, ctype or "application/octet-stream"
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return int(exc.code), raw, "text/plain; charset=utf-8"
        except Exception as exc:  # noqa: BLE001
            print(f"CDN proxy failed {url}: {exc}")
            return 502, f"cdn_proxy_failed: {exc}".encode(), "text/plain; charset=utf-8"
    return None


def _static_root() -> Path | None:
    raw = (os.getenv("ACTIVITY_STATIC_DIR") or "").strip()
    candidates = []
    if raw:
        candidates.append(Path(raw))
    here = Path(__file__).resolve().parent
    candidates.extend(
        [
            here / "activity_dist",
            here / "activity" / "client" / "dist",
        ]
    )
    for path in candidates:
        if path.is_dir() and (path / "index.html").is_file():
            return path
    return None


def _client_id() -> str:
    return (
        os.getenv("VITE_DISCORD_CLIENT_ID")
        or os.getenv("DISCORD_CLIENT_ID")
        or ""
    ).strip().strip('"').strip("'")


def _client_secret() -> str:
    return (
        (os.getenv("DISCORD_CLIENT_SECRET") or "")
        .strip()
        .strip('"')
        .strip("'")
    )


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _run_coro(bot: Any, coro: Any, timeout: float = 20.0) -> Any:
    loop = getattr(bot, "loop", None)
    if loop is None or not loop.is_running():
        raise RuntimeError("bot_loop_unavailable")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


def _exchange_token(code: str, bot: Any | None = None) -> tuple[int, dict]:
    """Sync wrapper — prefer aiohttp on the bot event loop (avoids Cloudflare 1010)."""
    if bot is not None and getattr(bot, "loop", None) and bot.loop.is_running():
        try:
            return _run_coro(bot, _exchange_token_async(code), timeout=25.0)
        except Exception as exc:  # noqa: BLE001
            print(f"oauth token exchange via bot loop failed: {exc}")

    # Fallback if bot loop is not ready yet (startup race)
    return asyncio.run(_exchange_token_async(code))


async def _exchange_token_async(code: str) -> tuple[int, dict]:
    """
    Exchange OAuth code using aiohttp.

    Cloudflare returns error 1010 for Python urllib's browser signature; discord.py
    already talks to Discord from this host via aiohttp, so we use the same stack.
    """
    import aiohttp

    client_id = _client_id()
    client_secret = _client_secret()
    if not client_id or not client_secret:
        return 500, {"error": "server_misconfigured"}

    redirect_uri = (
        os.getenv("DISCORD_OAUTH_REDIRECT_URI") or "https://127.0.0.1"
    ).strip()
    attempts = [
        {"redirect_uri": redirect_uri},
        {},
    ]
    last: tuple[int, dict] = (502, {"error": "token_exchange_failed"})
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        # Match discord.py-ish identity — avoids Cloudflare 1010 (urllib UA ban).
        "User-Agent": "DiscordBot (https://github.com/Rapptz/discord.py 2.7.1) Python/3.12 aiohttp/3.14",
    }
    url = "https://discord.com/api/v10/oauth2/token"

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for extra in attempts:
            form = {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                **extra,
            }
            try:
                async with session.post(url, data=form, headers=headers) as resp:
                    raw = await resp.text()
                    try:
                        data = json.loads(raw) if raw else {}
                    except Exception:
                        data = {
                            "error": "token_exchange_failed",
                            "status": resp.status,
                            "body": raw[:200].replace("\n", " "),
                        }
                    if resp.status >= 400:
                        print(
                            f"oauth token exchange HTTP {resp.status} "
                            f"(redirect_uri={'yes' if 'redirect_uri' in extra else 'no'}): {data}"
                        )
                        last = (int(resp.status), data if isinstance(data, dict) else {"error": str(data)})
                        continue
                    token = data.get("access_token") if isinstance(data, dict) else None
                    if not token:
                        last = (502, {"error": "no_access_token", "discord": data})
                        continue
                    print(
                        "oauth token exchange ok "
                        f"(redirect_uri={'yes' if 'redirect_uri' in extra else 'no'})"
                    )
                    return 200, {"access_token": token}
            except Exception as exc:  # noqa: BLE001
                print(f"oauth token exchange failed: {exc}")
                last = (502, {"error": "token_exchange_failed", "message": str(exc)})
                continue
    return last


def _discord_user_from_bearer(auth_header: str | None, bot: Any | None = None) -> dict | None:
    if not auth_header:
        return None
    match = auth_header.strip()
    if not match.lower().startswith("bearer "):
        return None
    token = match[7:].strip()
    if not token:
        return None

    async def _fetch() -> dict | None:
        import aiohttp

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "DiscordBot (https://github.com/Rapptz/discord.py 2.7.1) Python/3.12 aiohttp/3.14",
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://discord.com/api/v10/users/@me", headers=headers) as resp:
                if resp.status >= 400:
                    return None
                return await resp.json()

    try:
        if bot is not None and getattr(bot, "loop", None) and bot.loop.is_running():
            return _run_coro(bot, _fetch(), timeout=20.0)
        return asyncio.run(_fetch())
    except Exception as exc:  # noqa: BLE001
        print(f"discord @me failed: {exc}")
        return None


def _collect_top_xp(data: dict, guild_id: str | None, limit: int) -> list[dict]:
    rows: list[dict] = []
    for gid, gstats in (data or {}).items():
        if str(gid).startswith("_"):
            continue
        if not isinstance(gstats, dict):
            continue
        if guild_id is not None and str(gid) != str(guild_id):
            continue
        for uid, stats in gstats.items():
            if str(uid).startswith("_") or not isinstance(stats, dict):
                continue
            rows.append(
                {
                    "guild_id": str(gid),
                    "user_id": str(uid),
                    "name": stats.get("name") or "Unknown",
                    "xp": int(stats.get("xp") or 0),
                    "coins": int(stats.get("coins") or 0),
                    "wins": int(stats.get("wins") or 0),
                    "streak": int(stats.get("streak") or 0),
                    "best_time": (
                        None
                        if stats.get("best_time") is None
                        else float(stats.get("best_time"))
                    ),
                    "longest_time": (
                        float(stats["longest_time"])
                        if stats.get("longest_time") is not None
                        else (
                            None
                            if stats.get("best_time") is None
                            else float(stats.get("best_time"))
                        )
                    ),
                }
            )
    rows.sort(key=lambda r: r["xp"], reverse=True)
    return rows[:limit]


async def _apply_activity_win(bot: Any, *, user: dict, body: dict) -> dict:
    # Local imports avoid circular import at module load.
    from bot import (
        board_to_file,
        build_activity_win_embed,
        win_boost_caption_kwargs,
        equipped_title_id,
        guild_stats,
        owned_pin_emojis,
        render_board,
        save_data,
        user_stats,
        win_reward,
    )

    difficulty = body.get("difficulty") or "medium"
    client_elapsed = max(0, int(body.get("elapsed") or 0))
    # Resolve guild_id — client may send "0" if SDK guildId isn't ready yet; fall back to
    # the user's most-recent activity session to get the real guild.
    guild_id = await _resolve_activity_guild_id(
        body.get("guild_id") if body.get("guild_id") is not None else "0",
        int(user["id"]),
    )
    # channel_id from the body is preferred; fall back to what was stored in the session.
    channel_id_raw = body.get("channel_id")
    display_name = (
        body.get("name")
        or user.get("global_name")
        or user.get("username")
        or "Unknown"
    )
    uid = int(user["id"])

    from bot import ensure_challenge_game_for_user, games, handle_challenge_completion_activity
    ch_key = await ensure_challenge_game_for_user(bot, uid)
    if ch_key:
        game = games[ch_key]
        board = _normalize_activity_board(body.get("board"))
        if board is None:
            board = _normalize_activity_board(game.get("board"))
        given = game.get("given")
        if isinstance(given, list):
            given_norm = _normalize_activity_given(given, board)
        else:
            given_norm = None
        if not _verify_activity_solve(board, solution=game.get("solution"), given=given_norm):
            print(
                f"activity win rejected challenge user={uid}: not_solved "
                f"({_challenge_solve_debug(board, game)})"
            )
            return {"ok": False, "error": "not_solved", "challenge": True}
        if board:
            game["board"] = board
        # Server wall-clock elapsed — ignore spoofable client timer.
        started = float(game.get("started_at") or time.time())
        elapsed = max(0, int(time.time() - started))
        result = await handle_challenge_completion_activity(bot, uid, game, elapsed)
        if not result.get("ok"):
            print(f"activity win rejected challenge user={uid}: {result.get('error')}")
            return result
        result["elapsed"] = elapsed
        return result

    pending = await _activity_challenge_pending_response(uid)
    if pending:
        return pending

    try:
        gid_key = int(guild_id)
    except ValueError:
        gid_key = 0

    from bot import match_store
    session, lookup_id = await _lookup_activity_session(bot, gid_key, uid)
    # One lock per user so orphan vs canonical guild keys cannot race.
    lock_id = f"activity:user:{uid}"

    async with _activity_win_lock(lock_id):
        # Re-fetch under lock — never revive a stale in-memory copy.
        session = await match_store.get_activity_session(lookup_id)
        if not session and gid_key:
            session = await match_store.get_activity_session(
                _activity_session_id(gid_key, uid)
            )
        session_id = lookup_id if session else (
            _activity_session_id(gid_key, uid) if gid_key else lookup_id
        )
        # Prefer client channel on win (SDK may be ready now); keep session as fallback.
        channel_id_raw = body.get("channel_id") or (session or {}).get("channel_id")

        # Require a persisted session — prevents forged win POSTs with no game.
        if not session:
            print(f"activity win rejected user={uid} guild={guild_id}: no_session")
            return {"ok": False, "error": "no_session"}
        if session.get("won_at"):
            await _cleanup_activity_session_after_win(bot, session_id, uid)
            return {"ok": False, "error": "already_won"}

        # Prefer session guild when client still has "0"; never pay into guild 0.
        session_gid = str(session.get("guild_id") or "")
        if gid_key == 0 and session_gid not in ("", "0"):
            try:
                gid_key = int(session_gid)
            except ValueError:
                pass
        if gid_key == 0:
            print(f"activity win rejected user={uid}: guild_required")
            return {"ok": False, "error": "guild_required"}
        # Reject cross-guild orphan attach (session belongs to another server).
        if session_gid not in ("", "0") and str(gid_key) != session_gid:
            print(
                f"activity win rejected user={uid}: guild_mismatch "
                f"client={gid_key} session={session_gid}"
            )
            return {"ok": False, "error": "guild_mismatch"}

        # Canonical session id for this guild (migrate orphan fully).
        canonical_id = _activity_session_id(gid_key, uid)
        if session_id != canonical_id:
            session = await _migrate_orphan_session(
                bot, session, session_id, canonical_id, gid_key, uid
            )
            session_id = canonical_id
            # Re-acquire under canonical lock if we started on orphan.
            if lock_id != canonical_id:
                # Already inside orphan/canonical lock; merges go to canonical.
                pass
        guild_id = str(gid_key)

        board = _normalize_activity_board(body.get("board"))
        given = _normalize_activity_given(session.get("given"), board)
        solution = session.get("solution")
        # Prefer server-stored difficulty over client-supplied (anti-spoof),
        # and keep difficulty in sync with diff_index (Very Easy is index 0).
        from bot import (
            daily_difficulty_for_date,
            resolve_session_difficulty,
            utc_today,
        )

        difficulty, _diff_idx = resolve_session_difficulty(session)
        if session.get("session_kind") == "daily":
            day = str(session.get("daily_date") or utc_today())
            difficulty = daily_difficulty_for_date(day)

        if not _verify_activity_solve(board, solution=solution, given=given):
            print(f"activity win rejected user={uid} guild={guild_id}: not_solved")
            return {"ok": False, "error": "not_solved"}

        started_at = float(session.get("started_at") or 0)
        elapsed = _resolve_active_play_elapsed(session, client_elapsed)
        if started_at <= 0 and session.get("elapsed") is None and client_elapsed <= 0:
            print(
                f"activity win rejected user={uid} guild={guild_id}: "
                "no_server_elapsed"
            )
            return {"ok": False, "error": "invalid_elapsed"}

        # Award under _activity_win_lock first; mark won_at only after a successful payout
        # so a mid-flight failure can be retried (won_at-before-pay bricks the session).
        gstats = guild_stats(bot.data, gid_key)
        stats = user_stats(gstats, uid)

        is_daily = session.get("session_kind") == "daily"
        if is_daily:
            daily_date = session.get("daily_date") or time.strftime("%Y-%m-%d", time.gmtime())
            game_started = started_at or (time.time() - elapsed)

            discord_user = bot.get_user(uid)
            if discord_user is None:
                try:
                    discord_user = await bot.fetch_user(uid)
                except Exception:
                    pass
            if discord_user is None:
                class FakeUser:
                    def __init__(self, uid, name):
                        self.id = uid
                        self.name = name
                        self.display_name = name
                        self.mention = f"<@{uid}>"
                discord_user = FakeUser(uid, display_name)

            game_state = {
                "mode": "daily",
                "daily_date": daily_date,
                "started_at": game_started,
                "elapsed": elapsed,
                "difficulty": difficulty,
                "board": board,
                "given": given,
                "solution": solution,
                "hints_used": int(session.get("hints_used") or 0),
            }

            from bot import finish_win_and_announce

            outcome = await finish_win_and_announce(bot, gid_key, discord_user, game_state)

            if outcome.quiet:
                await match_store.merge_activity_session(
                    session_id, {"won_at": time.time(), "filled": 81}
                )
                await _cleanup_activity_session_after_win(bot, session_id, uid)
                reason = outcome.quiet_reason or (
                    "already_won" if int(outcome.coins) > 0 else "daily_locked"
                )
                return {
                    "ok": True,
                    "already_won": reason == "already_won",
                    "error": reason,
                    "daily": True,
                    "quiet": True,
                    "coins": int(outcome.coins),
                    "xp": int(outcome.xp),
                    "elapsed": elapsed,
                }

            await match_store.merge_activity_session(
                session_id, {"won_at": time.time(), "filled": 81}
            )
            await _cleanup_activity_session_after_win(bot, session_id, uid)

            posted = False
            post_error = None
            if board and given:
                try:
                    channel = await _resolve_win_announce_channel(
                        bot,
                        guild_id=gid_key,
                        session=session,
                        channel_id_raw=channel_id_raw,
                    )
                    if channel is not None:
                        image = render_board(
                            board,
                            given,
                            solution=solution,
                            conflicts=set(),
                            difficulty=difficulty,
                            title_id=equipped_title_id(stats),
                            pin_emojis=owned_pin_emojis(stats),
                            pin_seed=uid,
                        )
                        file = board_to_file(image)
                        announce_embed = build_activity_win_embed(
                            user_id=uid,
                            difficulty=difficulty,
                            elapsed=elapsed,
                            coins=int(outcome.coins),
                            xp=int(outcome.xp),
                            streak=int(stats.get("streak") or 0),
                            is_daily=True,
                            user_stats_dict=stats,
                            **win_boost_caption_kwargs(outcome),
                        )
                        await channel.send(
                            embed=announce_embed,
                            file=file,
                        )
                        posted = True
                        # Prevent /z-admin claimdaily from re-posting the same daily win.
                        try:
                            from bot import get_guild_daily, save_data as _save

                            daily_meta = get_guild_daily(bot.data, gid_key)
                            entry = daily_meta.setdefault("results", {}).setdefault(
                                str(uid), {}
                            )
                            entry["won"] = True
                            entry["announced_debug"] = True
                            _save(bot.data)
                        except Exception as flag_exc:  # noqa: BLE001
                            print(f"activity daily announced_debug set failed: {flag_exc}")
                    else:
                        post_error = "no_announce_channel"
                        print(
                            f"activity daily win: no announce channel user={uid} "
                            f"guild={gid_key} channel_raw={channel_id_raw}"
                        )
                except Exception as exc:
                    post_error = f"send_daily_announce: {exc}"
                    print(f"activity daily win chat post failed: {exc}")

            return {
                "ok": True,
                "daily": True,
                "coins": int(outcome.coins),
                "xp": int(outcome.xp),
                "streak": int(stats.get("streak") or 0),
                "elapsed": elapsed,
                "posted": posted,
                "post_error": post_error,
                **win_boost_caption_kwargs(outcome),
            }

        from bot import award_play_win

        puzzle_key = _play_puzzle_fingerprint(given, board=board, solution=solution)
        if not puzzle_key:
            print(f"activity play win rejected user={uid}: invalid_puzzle fingerprint")
            return {"ok": False, "error": "invalid_puzzle"}

        discord_user = bot.get_user(uid)
        if discord_user is None:
            try:
                discord_user = await bot.fetch_user(uid)
            except Exception:
                pass
        if discord_user is None:
            class FakeUser:
                def __init__(self, uid, name):
                    self.id = uid
                    self.name = name
                    self.display_name = name
                    self.mention = f"<@{uid}>"

            discord_user = FakeUser(uid, display_name)

        game_started = started_at or (time.time() - elapsed)
        game_state = {
            "mode": "play",
            "started_at": game_started,
            "elapsed": elapsed,
            "difficulty": difficulty,
            "board": board,
            "given": given,
            "solution": solution,
        }

        try:
            outcome = await award_play_win(
                bot,
                gid_key,
                discord_user,
                game_state,
                puzzle_key=puzzle_key,
            )
            if outcome is None:
                await match_store.merge_activity_session(
                    session_id, {"won_at": time.time(), "filled": 81}
                )
                await _cleanup_activity_session_after_win(bot, session_id, uid)
                return {"ok": True, "already_won": True, "error": "already_won"}
        except Exception as exc:  # noqa: BLE001
            print(f"activity play win payout failed user={uid}: {exc}")
            return {"ok": False, "error": "payout_failed"}

        stats = user_stats(gstats, uid)
        coins = int(outcome.coins)
        xp = int(outcome.xp)

        # Mark won only after payout succeeded (lock still held → no double-pay).
        await match_store.merge_activity_session(
            session_id, {"won_at": time.time(), "filled": 81}
        )

        posted = False
        post_error = None
        try:
            if board and given:
                channel = await _resolve_win_announce_channel(
                    bot,
                    guild_id=gid_key,
                    session=session,
                    channel_id_raw=channel_id_raw,
                )
                if channel is not None:
                    import asyncio
                    image = await asyncio.to_thread(
                        render_board,
                        board,
                        given,
                        solution=None,
                        conflicts=set(),
                        difficulty=difficulty,
                        title_id=equipped_title_id(stats),
                        pin_emojis=owned_pin_emojis(stats),
                        pin_seed=uid,
                    )
                    file = board_to_file(image)
                    embed = build_activity_win_embed(
                        user_id=uid,
                        difficulty=difficulty,
                        elapsed=elapsed,
                        coins=coins,
                        xp=xp,
                        streak=int(stats["streak"]),
                        user_stats_dict=stats,
                        **win_boost_caption_kwargs(outcome),
                    )
                    await channel.send(embed=embed, file=file)
                    posted = True
                else:
                    post_error = "no_announce_channel"
                    print(
                        f"activity play win: no announce channel user={uid} "
                        f"guild={gid_key} channel_raw={channel_id_raw}"
                    )
        except Exception as exc:  # noqa: BLE001
            post_error = str(exc)
            print(f"activity win chat post failed: {exc}")

        try:
            await _cleanup_activity_session_after_win(bot, session_id, uid)
        except Exception as exc:  # noqa: BLE001
            print(f"activity play win cleanup failed: {exc}")

        result = {
            "ok": True,
            "coins": coins,
            "xp": xp,
            "streak": int(stats["streak"]),
            "career_xp": int(stats["xp"]),
            "pocket": int(stats["coins"]),
            "best_time": stats.get("best_time"),
            "longest_time": stats.get("longest_time"),
            "elapsed": elapsed,
            "difficulty": difficulty,
            "guild_id": guild_id,
            "user_id": str(uid),
            "posted": posted,
            **win_boost_caption_kwargs(outcome),
        }
        if post_error and not posted:
            result["post_error"] = post_error
        return result


def _activity_session_id(guild_id: str | int, user_id: str | int) -> str:
    return f"activity:{guild_id}:{user_id}"


def _play_puzzle_fingerprint(
    given: list[list[bool]] | None,
    *,
    board: list | None = None,
    solution: Any = None,
) -> str | None:
    """Stable id for a /play puzzle from clue positions + clue digits."""
    from bot import play_puzzle_fingerprint

    return play_puzzle_fingerprint(given, board=board, solution=solution)


def _hints_max_for_session(session_kind: str | None, doc: dict | None = None) -> int | None:
    """Paid hints are unlimited. Returns None so the client does not show a hard cap.

    Gary's Wisdom free tips are capped separately via gary_wisdom_bonus / hints_gary_used.
    """
    return None


def _resolve_active_play_elapsed(
    session: dict | None,
    client_elapsed: int | None = None,
    *,
    now: float | None = None,
) -> int:
    """Active screen time for /play and /daily (not wall-clock since /command).

    Uses the higher of stored session.elapsed and the client report, then caps by
    wall-clock since started_at so a spoofed client cannot invent infinite time.
    When the Activity is currently open (timer_running_since), add the live segment.
    """
    ts = time.time() if now is None else float(now)
    stored = max(0, int((session or {}).get("elapsed") or 0))
    client = max(0, int(client_elapsed or 0))
    elapsed = max(stored, client)
    running = (session or {}).get("timer_running_since")
    if running:
        try:
            elapsed = max(elapsed, stored + max(0, int(ts - float(running))))
        except (TypeError, ValueError):
            pass
    started = float((session or {}).get("started_at") or 0)
    if started > 0:
        wall = max(0, int(ts - started))
        # Don't crush active time when started_at was reset later than accrued play.
        if wall >= stored:
            elapsed = min(elapsed, wall)
    return max(0, int(elapsed))


def _client_activity_session(doc: dict, *, strip_solution: bool = True) -> dict:
    """Session payload for the Activity client (solution withheld server-side)."""
    from bot import normalize_solution

    session_kind = doc.get("session_kind") or "play"
    from bot import HINT_SPONGE_COST, hint_gary_free_remaining
    # Frozen active seconds only — client restarts its run clock on load.
    elapsed_display = max(0, int(doc.get("elapsed") or 0))
    difficulty = doc.get("difficulty") or "medium"
    if session_kind == "daily":
        from bot import DIFF_KEYS_LIST, daily_difficulty_for_date, utc_today

        day = str(doc.get("daily_date") or utc_today())
        difficulty = daily_difficulty_for_date(day)
        try:
            diff_index = DIFF_KEYS_LIST.index(difficulty)
        except ValueError:
            from bot import DEFAULT_DIFFICULTY

            diff_index = (
                DIFF_KEYS_LIST.index(DEFAULT_DIFFICULTY)
                if DEFAULT_DIFFICULTY in DIFF_KEYS_LIST
                else 0
            )
    else:
        from bot import resolve_session_difficulty

        # Keep difficulty + diff_index in sync (diff_index wins when present).
        difficulty, diff_index = resolve_session_difficulty(doc)
    payload: dict = {
        "difficulty": difficulty,
        "diff_index": diff_index,
        "elapsed": elapsed_display,
        "board": doc.get("board"),
        "given": doc.get("given"),
        "filled": int(doc.get("filled") or 0),
        "updated_at": doc.get("updated_at"),
        "session_kind": session_kind,
        "daily_date": doc.get("daily_date"),
        "won_at": doc.get("won_at"),
        "hints_used": int(doc.get("hints_used") or 0),
        "hints_max": _hints_max_for_session(session_kind, doc),
        "hints_gary_used": int(doc.get("hints_gary_used") or 0),
        "gary_wisdom_bonus": int(doc.get("gary_wisdom_bonus") or 0),
        "gary_free_left": hint_gary_free_remaining(doc),
        "hint_sponge_cost": HINT_SPONGE_COST,
    }
    started = float(doc.get("started_at") or 0)
    if started > 0:
        payload["started_at"] = started
    if not strip_solution:
        sol = normalize_solution(doc.get("solution"))
        if sol:
            payload["solution"] = sol
    return payload


def _client_spectate_session(doc: dict) -> dict:
    """Read-only board snapshot for Activity spectators (never includes solution)."""
    payload = _client_activity_session(doc, strip_solution=True)
    payload["spectating"] = True
    payload["player_name"] = str(doc.get("name") or "Player")
    payload["player_id"] = str(doc.get("user_id") or "")
    return payload


def _cosmetics_for_user(bot: Any, guild_id: int, user_id: int) -> dict:
    """Title + pin badges for the player being watched (not the spectator)."""
    from bot import SHOP_TITLES, equipped_title_id, guild_stats, owned_pin_emojis, user_stats

    gstats = guild_stats(
        bot.data if isinstance(getattr(bot, "data", None), dict) else {},
        int(guild_id or 0),
    )
    stats = user_stats(gstats, int(user_id))
    tid = equipped_title_id(stats)
    title_meta = SHOP_TITLES.get(tid or "") if tid else None
    title = None
    if title_meta:
        title = {
            "id": tid,
            "label": title_meta.get("label") or "",
            "pin": title_meta.get("pin") or "",
            "emoji": title_meta.get("emoji") or "",
        }
    return {
        "title": title,
        "pins": owned_pin_emojis(stats),
        "seed": int(user_id),
    }


async def _touch_spectator_presence(
    *,
    session_id: str,
    viewer_id: int,
    viewer_name: str,
) -> None:
    from bot import match_store

    session = await match_store.get_activity_session(session_id)
    watchers = prune_watchers((session or {}).get("watchers"))
    watchers[str(viewer_id)] = {
        "name": viewer_name or "Player",
        "last_seen": time.time(),
    }
    await match_store.merge_activity_session(session_id, {"watchers": watchers})


async def _load_activity_watchers(
    bot: Any,
    *,
    user: dict,
    guild_id: str,
) -> dict:
    from bot import match_store

    uid = int(user["id"])
    resolved_guild = await _resolve_activity_guild_id(guild_id, uid)
    session_id = _activity_session_id(resolved_guild, uid)
    session = await match_store.get_activity_session(session_id)
    if not session:
        session = await match_store.find_activity_session_by_user_id(
            uid, guild_id=resolved_guild
        )
    if session:
        session_id = str(session.get("_id") or session_id)
    watchers = prune_watchers((session or {}).get("watchers"))
    if session and watchers != (session.get("watchers") or {}):
        await match_store.merge_activity_session(session_id, {"watchers": watchers})
    rows = [
        {"user_id": viewer_id, "name": meta.get("name") or "Player"}
        for viewer_id, meta in sorted(
            watchers.items(),
            key=lambda item: str(item[1].get("name") or ""),
        )
    ]
    return {"ok": True, "watchers": rows, "count": len(rows)}


async def _consume_spectate_intent(bot: Any, *, user: dict, guild_id: str) -> dict:
    from bot import match_store

    uid = int(user["id"])
    intent = await match_store.consume_spectate_intent(uid)
    if not intent:
        return {"ok": True, "intent": None}
    return {
        "ok": True,
        "intent": {
            "guild_id": str(intent["guild_id"]),
            "target_user_id": str(intent["target_user_id"]),
        },
    }


async def _load_activity_spectate(
    bot: Any,
    *,
    user: dict,
    guild_id: str,
    target_user_id: str,
) -> dict:
    from bot import get_watch_session_for_spectator

    viewer_id = int(user["id"])
    try:
        target_id = int(target_user_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_target"}

    if target_id == viewer_id:
        return {"ok": False, "error": "self_spectate"}

    resolved_guild = await _resolve_activity_guild_id(guild_id, viewer_id)
    session_id = _activity_session_id(resolved_guild, target_id)
    session = await get_watch_session_for_spectator(session_id)

    def _target_cosmetics() -> dict | None:
        try:
            return _cosmetics_for_user(bot, int(resolved_guild), target_id)
        except Exception as exc:  # noqa: BLE001
            print(f"activity spectate cosmetics failed: {exc}")
            return None

    if not session:
        return {
            "ok": True,
            "session": None,
            "ended": True,
            "player_id": str(target_id),
            "player_name": "Player",
            "cosmetics": _target_cosmetics(),
        }

    viewer_name = (
        user.get("global_name")
        or user.get("username")
        or user.get("display_name")
        or "Player"
    )
    try:
        presence_id = str(session.get("_id") or session_id)
        await _touch_spectator_presence(
            session_id=presence_id,
            viewer_id=viewer_id,
            viewer_name=str(viewer_name),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"activity spectator presence failed: {exc}")

    board = session.get("board")
    given = session.get("given")
    cosmetics = _target_cosmetics()
    if not board or not isinstance(given, list) or len(given) != 9:
        waiting_snap = {
            "spectating": True,
            "player_name": str(session.get("name") or "Player"),
            "player_id": str(session.get("user_id") or target_id),
            "filled": int(session.get("filled") or 0),
            "cosmetics": cosmetics,
        }
        return {
            "ok": True,
            "session": waiting_snap,
            "ended": False,
            "waiting": True,
        }

    filled = int(session.get("filled") or 0)
    ended = bool(session.get("won_at")) or filled >= 81
    snap = _client_spectate_session(session)
    if cosmetics:
        snap["cosmetics"] = cosmetics
    return {
        "ok": True,
        "session": snap,
        "ended": ended,
    }


def _pick_hint_cell(
    board: list[list[dict]],
    given: list[list[bool]],
    solution: list[list[int]],
    row: int,
    col: int,
) -> tuple[int, int] | None:
    from bot import cell_value

    def needs_hint(r: int, c: int) -> bool:
        if given[r][c]:
            return False
        return cell_value(board, r, c) != solution[r][c]

    if 0 <= row < 9 and 0 <= col < 9 and needs_hint(row, col):
        return row, col
    for r in range(9):
        for c in range(9):
            if needs_hint(r, c):
                return r, c
    return None


def _activity_user_from_dict(user: dict) -> Any:
    """Minimal user object for finish_forfeit / finish_win from OAuth payload."""

    class ActivityUser:
        def __init__(self, payload: dict) -> None:
            self.id = int(payload["id"])
            self.name = payload.get("username") or "Unknown"
            self.display_name = payload.get("global_name") or self.name
            self.mention = f"<@{self.id}>"

    return ActivityUser(user)


def _channel_belongs_to_guild(channel: Any, guild_id: int) -> bool:
    ch_guild = getattr(channel, "guild", None)
    if ch_guild is not None and int(getattr(ch_guild, "id", 0)) == int(guild_id):
        return True
    parent = getattr(channel, "parent", None)
    if parent is not None:
        p_guild = getattr(parent, "guild", None)
        if p_guild is not None and int(getattr(p_guild, "id", 0)) == int(guild_id):
            return True
    return False


async def _resolve_win_announce_channel(
    bot: Any,
    *,
    guild_id: int,
    session: dict | None,
    channel_id_raw: Any,
) -> Any | None:
    """Always announce wins in the fixed Sudoku channel when configured."""
    from bot import ACTIVITY_WATCH_CHANNEL_ID, DAILY_ANNOUNCE_CHANNEL_ID

    # Primary: dedicated Sudoku channel (watch + daily announce share this id).
    preferred: list[int] = []
    for raw in (ACTIVITY_WATCH_CHANNEL_ID, DAILY_ANNOUNCE_CHANNEL_ID):
        try:
            cid = int(raw or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid and cid not in preferred:
            preferred.append(cid)

    async def _fetch(cid: int):
        channel = bot.get_channel(cid)
        if channel is None:
            try:
                channel = await bot.fetch_channel(cid)
            except Exception:  # noqa: BLE001
                channel = None
        return channel

    for cid in preferred:
        channel = await _fetch(cid)
        if channel is not None:
            return channel

    # Fallback only if the fixed channel is unset / unreachable.
    candidates: list[int] = []

    def _push(raw: Any) -> None:
        if raw is None or raw == "":
            return
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            return
        if cid and cid not in candidates and cid not in preferred:
            candidates.append(cid)

    _push(channel_id_raw)
    _push((session or {}).get("channel_id"))
    _push((session or {}).get("watch_channel_id"))
    try:
        from bot import guild_stats

        gstats = guild_stats(getattr(bot, "data", None) or {}, guild_id)
        _push(gstats.get("daily_channel_id"))
    except Exception:  # noqa: BLE001
        pass

    for cid in candidates:
        channel = await _fetch(cid)
        if channel is not None and _channel_belongs_to_guild(channel, guild_id):
            return channel

    print(
        f"activity win announce channel unresolved guild={guild_id} "
        f"preferred={preferred} candidates={candidates} "
        f"session_channel={(session or {}).get('channel_id')}"
    )
    return None


async def _clear_user_activity_sessions(
    bot: Any, guild_id: str | int, user_id: int
) -> None:
    """Delete activity session docs for a user, resolving guild and orphan keys."""
    from bot import clear_activity_session

    uid = int(user_id)
    resolved = await _resolve_activity_guild_id(guild_id, uid)
    primary = _activity_session_id(resolved, uid)
    await clear_activity_session(bot, primary)
    orphan = _activity_session_id("0", uid)
    if orphan != primary:
        await clear_activity_session(bot, orphan)


async def _save_activity_session(bot: Any, *, user: dict, body: dict) -> dict:
    from bot import match_store

    uid = int(user["id"])
    guild_id = await _resolve_activity_guild_id(
        body.get("guild_id") if body.get("guild_id") is not None else "0",
        uid,
    )
    if body.get("clear") or body.get("action") == "clear":
        from bot import ensure_challenge_game_for_user
        if await ensure_challenge_game_for_user(bot, uid):
            return {"ok": True, "cleared": False, "reason": "active_challenge"}
        return await _delete_activity_session(bot, user=user, guild_id=guild_id)

    session_id = _activity_session_id(guild_id, uid)
    if body.get("challenge_forfeit"):
        try:
            from bot import forfeit_challenge_activity

            forfeited = await forfeit_challenge_activity(bot, uid)
        except Exception as exc:  # noqa: BLE001
            print(f"activity challenge_forfeit failed: {exc}")
            forfeited = False
        if body.get("end_watch"):
            try:
                from bot import end_activity_watch

                await end_activity_watch(
                    bot,
                    session_id,
                    force=bool(body.get("force")),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"activity end_watch after forfeit failed: {exc}")
        return {"ok": True, "challenge_forfeit": forfeited, "watch_ended": bool(body.get("end_watch"))}

    if body.get("end_watch"):
        # Clean up the "is playing" announcement only.
        # Do NOT auto-forfeit challenges here — Discord Activity remounts fire
        # pagehide/freeze right after open and would end 2-player races instantly.
        # Forfeit only via challenge_forfeit (Quit / explicit leave) above.
        try:
            from bot import end_activity_watch

            session, resolved_id = await _lookup_activity_session(bot, guild_id, uid)
            if not session or not session.get("watch_message_id"):
                watch = await match_store.find_activity_watch_session(
                    uid,
                    guild_id=guild_id if guild_id not in ("", "0") else None,
                )
                if not watch and guild_id not in ("", "0"):
                    watch = await match_store.find_activity_watch_session(uid)
                if watch:
                    resolved_id = str(watch.get("_id") or resolved_id)
            await end_activity_watch(
                bot,
                resolved_id,
                force=bool(body.get("force", True)),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"activity end_watch failed: {exc}")
        return {"ok": True, "watch_ended": True, "challenge_forfeit": False}

    board = _normalize_activity_board(body.get("board"))
    given_raw = body.get("given")
    given = _normalize_activity_given(given_raw, board)

    from bot import ensure_challenge_game_for_user, games, sync_challenge_board
    ch_key = await ensure_challenge_game_for_user(bot, uid)
    if ch_key:
        game = games[ch_key]
        if board:
            sanitized = _sanitize_challenge_board_update(game, board)
            if sanitized is None:
                print(
                    f"activity challenge save rejected invalid board user={uid} "
                    f"match={game.get('match_id')}"
                )
                return {"ok": False, "error": "invalid_board", "challenge": True}
            board = sanitized
            game["board"] = board
            game["filled"] = sum(1 for r in range(9) for c in range(9) if board[r][c]["value"])
            started = float(game.get("started_at") or time.time())
            game["elapsed"] = max(0, int(time.time() - started))
            await sync_challenge_board(game)
        return {"ok": True, "filled": game["filled"], "elapsed": game["elapsed"]}

    pending = await _activity_challenge_pending_response(uid)
    if pending:
        return pending

    # Preference-only stub from Activity after "new order" / remount — no board yet.
    if body.get("preference_only"):
        from bot import resolve_session_difficulty

        diff_key, diff_index = resolve_session_difficulty(body)
        session_id = _activity_session_id(guild_id, uid)
        store_guild = str(guild_id)
        existing = None
        if str(guild_id) in ("", "0"):
            existing, looked_up_id = await _lookup_activity_session(bot, guild_id, uid)
            if existing:
                session_id = looked_up_id
                prev = str(existing.get("guild_id") or "")
                if prev not in ("", "0"):
                    store_guild = prev
        await match_store.merge_activity_session(
            session_id,
            {
                "diff_index": diff_index,
                "difficulty": diff_key,
                "guild_id": store_guild,
                "user_id": str(uid),
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
            },
        )
        return {
            "ok": True,
            "preference": True,
            "difficulty": diff_key,
            "diff_index": diff_index,
        }

    solution = body.get("solution")
    if board is None or given is None:
        print(
            f"activity session save invalid_board user={uid} guild={guild_id} "
            f"raw_guild={body.get('guild_id')}"
        )
        return {"ok": False, "error": "invalid_board"}

    # Same orphan-aware lookup as load/win/hint — then migrate onto canonical key.
    existing, looked_up_id = await _lookup_activity_session(bot, guild_id, uid)
    canonical_id = _activity_session_id(guild_id, uid)
    session_id = canonical_id
    if existing and looked_up_id != canonical_id and str(guild_id) not in ("", "0"):
        try:
            gid_int = int(guild_id)
        except ValueError:
            gid_int = 0
        if gid_int:
            existing = await _migrate_orphan_session(
                bot, existing, looked_up_id, canonical_id, gid_int, uid
            )
            looked_up_id = canonical_id
    elif existing and str(guild_id) in ("", "0"):
        # Still no real guild — keep writing the looked-up doc (often activity:0:uid).
        session_id = looked_up_id

    if (not isinstance(solution, list) or len(solution) != 9) and existing:
        solution = existing.get("solution")
    if not isinstance(solution, list) or len(solution) != 9:
        print(
            f"activity session save invalid_solution user={uid} guild={guild_id} "
            f"solution_type={type(solution).__name__}"
        )
        return {"ok": False, "error": "invalid_solution"}

    # Reconcile difficulty + diff_index from one source of truth.
    # Never independently default to medium + 0 (that pair disagrees: Very Easy is 0).
    from bot import resolve_session_difficulty

    difficulty, diff_index = resolve_session_difficulty(
        {
            "difficulty": body.get("difficulty"),
            "diff_index": body.get("diff_index"),
        }
    )
    elapsed = max(0, int(body.get("elapsed") or 0))
    channel_id_raw = body.get("channel_id")
    filled = sum(1 for r in range(9) for c in range(9) if board[r][c]["value"])
    hints_used = max(
        int(existing.get("hints_used") or 0) if existing else 0,
        max(0, int(body.get("hints_used") or 0)),
    )
    hints_gary_used = max(
        int(existing.get("hints_gary_used") or 0) if existing else 0,
        max(0, int(body.get("hints_gary_used") or 0)),
    )

    session_kind = "play"
    daily_date = None
    started_at = time.time() - elapsed
    accepting_client_puzzle = True
    same_puzzle = False
    if existing:
        session_kind = existing.get("session_kind") or "play"
        daily_date = existing.get("daily_date")
        prior_started = float(existing.get("started_at") or 0)
        if prior_started > 0:
            started_at = prior_started
        else:
            started_at = time.time() - elapsed
        # Pin puzzle metadata once authorized. Daily is always immutable.
        # Play: keep solution/difficulty if the given clues still match (same puzzle);
        # a different given matrix means the player started a new game.
        if existing.get("solution") and existing.get("given"):
            existing_given = _normalize_activity_given(existing.get("given"), board)
            same_puzzle = existing_given is not None and existing_given == given
            if session_kind == "daily" or same_puzzle:
                given = existing_given or given
                solution = existing.get("solution")
                difficulty, diff_index = resolve_session_difficulty(existing)
                accepting_client_puzzle = False
                elapsed = _resolve_active_play_elapsed(existing, elapsed)
            elif session_kind == "play":
                # New play puzzle — reset clock, win claim, and hint/Gary counters.
                started_at = time.time() - max(0, int(body.get("elapsed") or 0))
                elapsed = max(0, int(body.get("elapsed") or 0))
                hints_used = 0
                hints_gary_used = 0
        elif session_kind == "daily":
            # Daily session without a full board yet — still accumulate active time.
            elapsed = _resolve_active_play_elapsed(existing, elapsed)
        elif session_kind == "play":
            # Preference stub → first real board: start hint budget fresh.
            hints_used = 0
            hints_gary_used = 0

    if session_kind == "daily":
        from bot import DIFF_KEYS_LIST, daily_difficulty_for_date, utc_today

        day = str(daily_date or (existing or {}).get("daily_date") or utc_today())
        daily_date = day
        difficulty = daily_difficulty_for_date(day)
        try:
            diff_index = DIFF_KEYS_LIST.index(difficulty)
        except ValueError:
            from bot import DEFAULT_DIFFICULTY

            diff_index = (
                DIFF_KEYS_LIST.index(DEFAULT_DIFFICULTY)
                if DEFAULT_DIFFICULTY in DIFF_KEYS_LIST
                else 0
            )

    # First save / new play puzzle: refuse forged or trivial client grids.
    if accepting_client_puzzle and session_kind == "play":
        # Require an explicit 9×9 given mask — never infer clues from a partial board.
        if not (
            isinstance(given_raw, list)
            and len(given_raw) == 9
            and all(isinstance(row, list) and len(row) == 9 for row in given_raw)
        ):
            print(
                f"activity session save rejected puzzle user={uid} guild={guild_id}: "
                "missing_given"
            )
            return {"ok": False, "error": "missing_given"}
        ok_puzzle, puzzle_err = _validate_activity_puzzle(given, solution, board)
        if not ok_puzzle:
            print(
                f"activity session save rejected puzzle user={uid} guild={guild_id}: "
                f"{puzzle_err}"
            )
            return {"ok": False, "error": puzzle_err}

    # Never wipe a just-solved play board here — /activity/win needs the session.
    # Only drop sessions that were already awarded (won_at) so spectators stop seeing them.
    if filled >= 81 and session_kind not in ("daily", "challenge"):
        if existing and existing.get("won_at") and _verify_activity_solve(
            board, solution=solution, given=given
        ):
            try:
                from bot import clear_activity_session

                await clear_activity_session(bot, session_id)
            except Exception as exc:  # noqa: BLE001
                print(f"activity session clear on solve failed: {exc}")
            return {"ok": True, "cleared": True}

    last_move_at = _activity_last_move_at(
        existing, filled, same_puzzle=same_puzzle
    )

    store_guild = str(guild_id)
    if store_guild in ("", "0") and existing:
        prev_guild = str(existing.get("guild_id") or "")
        if prev_guild not in ("", "0"):
            store_guild = prev_guild

    async with _activity_win_lock(session_id):
        # Serialize with hints so a charged reveal cannot be replaced by a stale board.
        try:
            fresh_existing = await match_store.get_activity_session(session_id)
        except Exception as exc:  # noqa: BLE001
            print(f"activity save re-read failed: {exc}")
            fresh_existing = existing
        if fresh_existing:
            existing = fresh_existing
            if session_kind == "play" and not same_puzzle:
                # Never inherit spent hints / Gary usage onto a brand-new puzzle.
                hints_used = 0
                hints_gary_used = 0
            else:
                hints_used = max(
                    int(existing.get("hints_used") or 0),
                    max(0, int(body.get("hints_used") or 0)),
                )
                hints_gary_used = max(
                    int(existing.get("hints_gary_used") or 0),
                    max(0, int(body.get("hints_gary_used") or 0)),
                )
            if session_kind == "daily" or same_puzzle:
                elapsed = _resolve_active_play_elapsed(
                    existing, max(0, int(body.get("elapsed") or 0))
                )

        try:
            client_seq = max(0, int(body.get("save_seq") or 0))
        except (TypeError, ValueError):
            client_seq = 0
        try:
            server_seq = int((existing or {}).get("save_seq") or 0)
        except (TypeError, ValueError):
            server_seq = 0
        if client_seq and server_seq and client_seq < server_seq:
            return {
                "ok": True,
                "stale": True,
                "save_seq": server_seq,
                "filled": int((existing or {}).get("filled") or filled),
                "elapsed": int((existing or {}).get("elapsed") or elapsed),
                "hints_used": int((existing or {}).get("hints_used") or 0),
                "hints_max": None,
                "hints_gary_used": int((existing or {}).get("hints_gary_used") or 0),
                "gary_wisdom_bonus": int((existing or {}).get("gary_wisdom_bonus") or 0),
            }

        timer_active = bool(body.get("timer_active"))
        if timer_active:
            timer_running_since = time.time()
        else:
            timer_running_since = None

        doc = {
            "_id": session_id,
            "guild_id": store_guild,
            "user_id": str(uid),
            "difficulty": difficulty,
            "diff_index": diff_index,
            "elapsed": elapsed,
            "board": board,
            "given": given,
            "solution": solution,
            "filled": filled,
            "name": body.get("name")
            or user.get("global_name")
            or user.get("username")
            or "Unknown",
            "session_kind": session_kind,
            "daily_date": daily_date,
            "started_at": started_at,
            "last_move_at": last_move_at,
            "hints_used": hints_used,
            "hints_gary_used": hints_gary_used,
            "save_seq": max(client_seq, server_seq),
            # Live segment for spectators while Activity is visible; cleared when paused.
            "timer_running_since": timer_running_since,
        }
        if session_kind == "play" and not same_puzzle:
            # Clear old bonus so merge/upsert cannot resurrect a spent Gary charge.
            doc["gary_wisdom_bonus"] = 0
            doc["hints_used"] = 0
            doc["hints_gary_used"] = 0
        elif session_kind == "daily" or same_puzzle:
            _preserve_server_hint_progress(existing, doc)
        try:
            gid_int = int(store_guild) if store_guild not in ("", "0") else 0
        except ValueError:
            gid_int = 0
        if gid_int:
            from bot import attach_gary_wisdom_to_session, guild_stats, save_data, user_stats

            gstats = guild_stats(bot.data, gid_int)
            pstats = user_stats(gstats, uid)
            attach_gary_wisdom_to_session(
                pstats, doc, existing=existing, same_puzzle=same_puzzle
            )
            save_data(bot.data)
        # Keep a known channel_id — never wipe it with null from a save without SDK channel.
        if channel_id_raw:
            doc["channel_id"] = str(channel_id_raw)
        elif existing and existing.get("channel_id"):
            doc["channel_id"] = existing.get("channel_id")
        else:
            doc["channel_id"] = None
        # New play puzzle must drop leftover win/watch state — delete the Discord
        # announcement first so we never orphan an "is playing" message.
        if existing and session_kind == "play":
            existing_given = _normalize_activity_given(existing.get("given"), board)
            same_puzzle = (
                existing.get("given")
                and existing_given is not None
                and existing_given == given
            )
            if not same_puzzle:
                doc["won_at"] = None
                watch_cleared = True
                refreshed = None
                had_message = bool(existing.get("watch_message_id"))
                if had_message or existing.get("watch_notified"):
                    try:
                        from bot import end_activity_watch

                        ended = await end_activity_watch(bot, session_id, force=True)
                        # Re-read — only wipe local flags if Discord message is gone.
                        refreshed = await match_store.get_activity_session(session_id)
                        if refreshed and refreshed.get("watch_message_id"):
                            watch_cleared = False
                            print(
                                f"activity watch still live after new-puzzle end "
                                f"for {session_id}; keeping message id"
                            )
                        elif had_message and not ended:
                            watch_cleared = False
                    except Exception as exc:  # noqa: BLE001
                        watch_cleared = False
                        print(f"activity watch clear on new puzzle failed: {exc}")
                if watch_cleared:
                    source = refreshed if refreshed is not None else existing
                    # Flag set without message_id — keep once-flag so we never re-post
                    # over a possible Discord orphan.
                    if source.get("watch_once_notified") and not source.get("watch_message_id"):
                        doc["watch_once_notified"] = True
                        doc["watch_notified"] = False
                        doc["watch_message_id"] = None
                        doc["watch_posted_at"] = None
                    else:
                        doc["watch_once_notified"] = False
                        doc["watch_notified"] = False
                        doc["watch_message_id"] = None
                        doc["watch_posted_at"] = None
                else:
                    # Keep pointing at the live Discord message; do not post a second one.
                    keep_from = refreshed or existing
                    for watch_key in (
                        "watch_once_notified",
                        "watch_notified",
                        "watch_message_id",
                        "watch_channel_id",
                        "watch_posted_at",
                    ):
                        if keep_from.get(watch_key) is not None:
                            doc[watch_key] = keep_from[watch_key]
                    doc["watch_once_notified"] = True
        # Preserve live watch fields on same-puzzle autosaves (belt + suspenders).
        if existing and "watch_message_id" not in doc:
            for watch_key in (
                "watch_once_notified",
                "watch_notified",
                "watch_message_id",
                "watch_channel_id",
                "watch_posted_at",
            ):
                if existing.get(watch_key) is not None and watch_key not in doc:
                    doc[watch_key] = existing[watch_key]
        await match_store.upsert_activity_session(doc)
    wrong_id = _activity_session_id("0", uid)
    if wrong_id != session_id:
        wrong = await match_store.get_activity_session(wrong_id)
        if wrong:
            # If the orphan holds the live watch announcement, end it first.
            if wrong.get("watch_message_id"):
                try:
                    from bot import end_activity_watch

                    ended = await end_activity_watch(bot, wrong_id, force=True)
                    if not ended:
                        refreshed = await match_store.get_activity_session(wrong_id)
                        if refreshed and refreshed.get("watch_message_id"):
                            print(
                                f"activity orphan watch still live for {wrong_id}; "
                                "skipping delete"
                            )
                            wrong = None
                except Exception as exc:  # noqa: BLE001
                    print(f"activity orphan watch end failed: {exc}")
                    wrong = None
            if wrong is not None:
                await match_store.delete_activity_session(wrong_id)
    current = await match_store.get_activity_session(session_id)
    
    from bot import _activity_notify_inflight
    posted_at = float((current or {}).get("watch_posted_at") or 0)
    in_flight = session_id in _activity_notify_inflight
    watch_live = bool(
        in_flight
        or (current and current.get("watch_notified") and current.get("watch_message_id"))
        or (time.time() - posted_at < 120)
    )
    notify_doc = current or doc
    from bot import activity_session_spectatable

    # Once this open session announced, never post again until end_watch clears the flag.
    # (Do NOT re-allow notify when message_id is missing — that orphans Discord messages.)
    already_notified_once = bool(current and current.get("watch_once_notified"))
    will_notify = (
        not watch_live
        and not already_notified_once
        and activity_session_spectatable(notify_doc)
    )
    print(
        f"activity session save user={uid} guild={guild_id} filled={filled} "
        f"notify={'yes' if will_notify else 'skip'}"
    )
    if will_notify:
        try:
            from bot import notify_activity_play_started

            await notify_activity_play_started(bot, session_id)
        except Exception as exc:  # noqa: BLE001
            print(f"activity play notify failed: {exc}")
    from bot import hint_gary_free_remaining

    saved = current or doc
    return {
        "ok": True,
        "filled": int(saved.get("filled") or filled),
        "elapsed": int(saved.get("elapsed") or elapsed),
        "hints_used": int(saved.get("hints_used") or 0),
        "hints_max": _hints_max_for_session(
            saved.get("session_kind") or session_kind,
            saved,
        ),
        "hints_gary_used": int(saved.get("hints_gary_used") or 0),
        "gary_wisdom_bonus": int(saved.get("gary_wisdom_bonus") or 0),
        "gary_free_left": hint_gary_free_remaining(saved),
        "save_seq": int(saved.get("save_seq") or 0),
    }


async def _load_activity_session(bot: Any, *, user: dict, guild_id: str) -> dict:
    from bot import match_store, clear_activity_session

    uid = int(user["id"])
    from bot import ensure_challenge_game_for_user, games, game_filled_count
    ch_key = await ensure_challenge_game_for_user(bot, uid)
    if ch_key:
        game = games[ch_key]
        from bot import DIFF_KEYS_LIST, difficulty_key_from_label

        diff_key = difficulty_key_from_label(game.get("difficulty") or "medium")
        try:
            diff_index = DIFF_KEYS_LIST.index(diff_key)
        except ValueError:
            diff_index = DIFF_KEYS_LIST.index("medium") if "medium" in DIFF_KEYS_LIST else 0
        return {
            "ok": True,
            "session": _client_activity_session(
                {
                    "difficulty": diff_key,
                    "diff_index": diff_index,
                    "elapsed": max(
                        0,
                        int(time.time() - float(game.get("started_at") or time.time())),
                    ),
                    "board": game.get("board"),
                    "given": game.get("given"),
                    "solution": game.get("solution"),
                    "filled": game_filled_count(game),
                    "session_kind": "challenge",
                    "hints_used": int(game.get("hints_used") or 0),
                    "hints_gary_used": int(game.get("hints_gary_used") or 0),
                    "gary_wisdom_bonus": int(game.get("gary_wisdom_bonus") or 0),
                    "started_at": float(game.get("started_at") or time.time()),
                    "match_id": game.get("match_id"),
                    "player_slot": game.get("player_slot"),
                },
                # Include solution so the Activity celebrates the same grid the server grades.
                strip_solution=False,
            )
            | {
                "match_id": game.get("match_id"),
                "player_slot": game.get("player_slot"),
            },
        }

    pending = await _activity_challenge_pending_response(uid)
    if pending:
        # Soft-block: Activity may remount after you finish while peers still race.
        # Don't hard-error the load — just withhold a play/daily board.
        return {
            "ok": True,
            "session": None,
            "challenge_pending": True,
            "message": pending.get("message"),
        }

    resolved_guild = await _resolve_activity_guild_id(guild_id, uid)
    doc, _sid = await _lookup_activity_session(bot, resolved_guild, uid)
    if not doc:
        return {"ok": True, "session": None}
    return {"ok": True, "session": _client_activity_session(doc, strip_solution=True)}


async def _apply_activity_hint(bot: Any, *, user: dict, body: dict) -> dict:
    from bot import (
        apply_hint_charge,
        ensure_challenge_game_for_user,
        games,
        guild_stats,
        hint_gary_free_remaining,
        match_store,
        normalize_solution,
        save_data,
        user_stats,
    )

    uid = int(user["id"])
    guild_id = await _resolve_activity_guild_id(
        body.get("guild_id") if body.get("guild_id") is not None else "0",
        uid,
    )
    row = int(body.get("row") if body.get("row") is not None else -1)
    col = int(body.get("col") if body.get("col") is not None else -1)
    board = _normalize_activity_board(body.get("board"))
    game: dict | None = None
    session: dict | None = None
    persist_hint: str | None = None

    ch_key = await ensure_challenge_game_for_user(bot, uid)
    stats: dict | None = None
    gid_key = 0
    if ch_key:
        game = games.get(ch_key)
        if not game or board is None:
            return {"ok": False, "error": "invalid_board"}
        solution = normalize_solution(game.get("solution"))
        given = _normalize_activity_given(game.get("given"), board)
        if not solution or not given:
            return {"ok": False, "error": "no_session"}
        session_kind = "challenge"
        hints_used = int(game.get("hints_used") or 0)
        persist_hint = None
        try:
            gid_key = int(game.get("guild_id") or 0)
        except (TypeError, ValueError):
            gid_key = 0
        if gid_key == 0:
            try:
                gid_key = int(guild_id) if str(guild_id) not in ("", "0") else 0
            except ValueError:
                gid_key = 0
        if gid_key:
            stats = user_stats(guild_stats(bot.data, gid_key), uid)
    else:
        pending = await _activity_challenge_pending_response(uid)
        if pending:
            return pending
        session, persist_hint = await _lookup_activity_session(bot, guild_id, uid)
        if not session or board is None:
            return {"ok": False, "error": "no_session"}
        if session.get("won_at"):
            return {"ok": False, "error": "already_won"}
        solution = normalize_solution(session.get("solution"))
        given = _normalize_activity_given(session.get("given"), board)
        if not solution or not given:
            return {"ok": False, "error": "no_session"}
        session_kind = session.get("session_kind") or "play"
        hints_used = int(session.get("hints_used") or 0)
        try:
            gid_key = int(session.get("guild_id") or 0)
        except (TypeError, ValueError):
            gid_key = 0
        if gid_key == 0:
            try:
                gid_key = int(guild_id) if str(guild_id) not in ("", "0") else 0
            except ValueError:
                gid_key = 0
        if gid_key:
            stats = user_stats(guild_stats(bot.data, gid_key), uid)

    charge: dict = {}
    charge_container: dict = {}
    if ch_key and game is not None:
        charge_container = game
    elif session is not None:
        charge_container = session

    async with _activity_win_lock(persist_hint or f"hint:{uid}"):
        # Re-check hint budget under lock (TOCTOU).
        if persist_hint:
            fresh = await match_store.get_activity_session(persist_hint)
            if fresh:
                hints_used = int(fresh.get("hints_used") or 0)
                charge_container = fresh
        elif ch_key and game is not None:
            hints_used = int(game.get("hints_used") or 0)

        # No hard cap on paid hints — only sponges / Gary free / empty board matter.
        picked = _pick_hint_cell(board, given, solution, row, col)
        if picked is None:
            return {
                "ok": False,
                "error": "no_hint_available",
                "hints_used": hints_used,
                "hints_max": None,
                "hints_gary_used": int(charge_container.get("hints_gary_used") or 0),
                "gary_wisdom_bonus": int(charge_container.get("gary_wisdom_bonus") or 0),
                "gary_free_left": hint_gary_free_remaining(charge_container),
            }

        if stats is None:
            return {
                "ok": False,
                "error": "no_guild",
                "message": "Join a server to use hints.",
                "hints_used": hints_used,
                "hints_max": None,
            }

        charge = apply_hint_charge(stats, charge_container)
        if not charge.get("ok"):
            return {
                "ok": False,
                "error": charge.get("error") or "insufficient_sponges",
                "hint_cost": int(charge.get("cost") or 0),
                "pocket": int(charge.get("pocket") or 0),
                "gary_free_left": int(charge.get("gary_free_left") or 0),
                "gary_wisdom_bonus": int(charge_container.get("gary_wisdom_bonus") or 0),
                "hints_used": hints_used,
                "hints_max": None,
                "hints_gary_used": int(charge_container.get("hints_gary_used") or 0),
            }
        save_data(bot.data)

        target_r, target_c = picked
        value = int(solution[target_r][target_c])
        hints_used += 1
        board[target_r][target_c] = {"value": value, "pencil_marks": []}
        filled = sum(1 for r in range(9) for c in range(9) if board[r][c].get("value"))

        if ch_key and game is not None:
            game["hints_used"] = hints_used
            game["board"] = board
            try:
                from bot import sync_challenge_board

                await sync_challenge_board(game)
            except Exception as exc:  # noqa: BLE001
                print(f"challenge hint persist failed: {exc}")
        elif persist_hint:
            await match_store.merge_activity_session(
                persist_hint,
                {
                    "hints_used": hints_used,
                    "hints_gary_used": int(charge_container.get("hints_gary_used") or 0),
                    "gary_wisdom_bonus": int(
                        charge_container.get("gary_wisdom_bonus") or 0
                    ),
                    "board": board,
                    "filled": filled,
                    "last_move_at": time.time(),
                },
            )

    return {
        "ok": True,
        "row": target_r,
        "col": target_c,
        "value": value,
        "hints_used": hints_used,
        "hints_max": None,
        "hints_gary_used": int(charge_container.get("hints_gary_used") or 0),
        "gary_wisdom_bonus": int(charge_container.get("gary_wisdom_bonus") or 0),
        "gary_free_left": hint_gary_free_remaining(charge_container),
        "hint_cost": int(charge.get("cost") or 0),
        "paid_with": charge.get("paid_with"),
        "pocket": int(charge.get("pocket") or 0),
        "session_kind": session_kind,
    }


async def _delete_activity_session(bot: Any, *, user: dict, guild_id: str) -> dict:
    uid = int(user["id"])
    from bot import (
        ensure_challenge_game_for_user,
        finish_forfeit,
        is_solved,
        match_store,
        normalize_solution,
    )

    if await ensure_challenge_game_for_user(bot, uid):
        return {
            "ok": True,
            "cleared": False,
            "reason": "active_challenge",
        }

    resolved = await _resolve_activity_guild_id(guild_id, uid)
    session, session_id = await _lookup_activity_session(bot, resolved, uid)

    if session and not session.get("won_at"):
        try:
            from bot import end_activity_watch

            ended = await end_activity_watch(bot, session_id, force=True)
            if not ended:
                refreshed = await match_store.get_activity_session(session_id)
                if refreshed and refreshed.get("watch_message_id"):
                    return {
                        "ok": False,
                        "error": "watch_still_live",
                        "cleared": False,
                        "message": "Could not remove the watch announcement; try again.",
                    }
        except Exception as exc:  # noqa: BLE001
            print(f"delete session end_watch failed: {exc}")
            refreshed = await match_store.get_activity_session(session_id)
            if refreshed and refreshed.get("watch_message_id"):
                return {
                    "ok": False,
                    "error": "watch_still_live",
                    "cleared": False,
                    "message": "Could not remove the watch announcement; try again.",
                }

        session_gid = str(session.get("guild_id") or "")
        try:
            gid = int(session_gid) if session_gid not in ("", "0") else int(resolved or 0)
        except ValueError:
            gid = 0
        # Prefer client-resolved guild over orphan "0" for streak/stats.
        if gid == 0:
            try:
                gid = int(resolved) if str(resolved) not in ("", "0") else 0
            except ValueError:
                gid = 0
        kind = session.get("session_kind") or "play"
        actor = _activity_user_from_dict(user)
        board = _normalize_activity_board(session.get("board"))
        given = _normalize_activity_given(session.get("given"), board)
        solution = normalize_solution(session.get("solution"))
        solved = bool(
            board and solution and is_solved(board, solution)
        )
        from bot import (
            daily_difficulty_for_date,
            resolve_session_difficulty,
            utc_today,
        )

        difficulty, _diff_idx = resolve_session_difficulty(session)
        if kind == "daily":
            day = str(session.get("daily_date") or utc_today())
            difficulty = daily_difficulty_for_date(day)

        if kind == "daily" and gid:
            if solved:
                from bot import finish_win_and_announce

                game_state = {
                    "mode": "daily",
                    "daily_date": session.get("daily_date"),
                    "started_at": float(session.get("started_at") or time.time()),
                    "difficulty": difficulty,
                    "board": board,
                    "given": given,
                    "solution": solution,
                    "hints_used": int(session.get("hints_used") or 0),
                }
                try:
                    await finish_win_and_announce(bot, gid, actor, game_state)
                except Exception as exc:  # noqa: BLE001
                    print(f"delete session daily win recover failed: {exc}")
                    # Never forfeit a verified solve — leave session for retry.
                    return {
                        "ok": False,
                        "error": "award_failed",
                        "cleared": False,
                        "message": "Board is solved but rewards could not be saved; try again.",
                    }
            else:
                await finish_forfeit(
                    bot.data,
                    gid,
                    actor,
                    {
                        "mode": "daily",
                        "daily_date": session.get("daily_date"),
                        "started_at": float(session.get("started_at") or time.time()),
                        "difficulty": difficulty,
                    },
                )
        elif kind == "play" and gid:
            if solved:
                puzzle_key = _play_puzzle_fingerprint(
                    given, board=board, solution=solution
                )
                if not puzzle_key:
                    print(f"delete play award skipped user={uid}: no fingerprint")
                    return {
                        "ok": False,
                        "error": "invalid_puzzle",
                        "cleared": False,
                        "message": "Board is solved but puzzle could not be fingerprinted; try again.",
                    }
                game_state = {
                    "mode": "play",
                    "started_at": float(session.get("started_at") or time.time()),
                    "difficulty": difficulty,
                }
                from bot import award_play_win

                try:
                    outcome = await award_play_win(
                        bot,
                        gid,
                        actor,
                        game_state,
                        puzzle_key=puzzle_key,
                    )
                    if outcome is None:
                        pass  # already paid — still clear session below
                except Exception as exc:  # noqa: BLE001
                    print(f"delete play award failed: {exc}")
                    return {
                        "ok": False,
                        "error": "award_failed",
                        "cleared": False,
                    }
            else:
                # Abandon mid-game via clear/"new order" — quit play (daily streak kept).
                await finish_forfeit(
                    bot.data,
                    gid,
                    actor,
                    {
                        "mode": "play",
                        "started_at": float(session.get("started_at") or time.time()),
                        "difficulty": difficulty,
                    },
                )

    await _clear_user_activity_sessions(bot, guild_id, uid)
    return {"ok": True, "cleared": True}


def _normalize_activity_board(raw: Any) -> list[list[dict]] | None:
    if not isinstance(raw, list) or len(raw) != 9:
        return None
    board: list[list[dict]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) != 9:
            return None
        out_row: list[dict] = []
        for cell in row:
            if isinstance(cell, dict):
                value = int(cell.get("value") or 0)
                marks = cell.get("pencil_marks") or []
                if not isinstance(marks, list):
                    marks = []
                out_row.append(
                    {
                        "value": value,
                        "pencil_marks": [int(m) for m in marks if str(m).isdigit()],
                    }
                )
            else:
                out_row.append({"value": int(cell or 0), "pencil_marks": []})
        board.append(out_row)
    return board


def _normalize_activity_given(raw: Any, board: list[list[dict]] | None) -> list[list[bool]] | None:
    if board is None:
        return None
    if isinstance(raw, list) and len(raw) == 9:
        given: list[list[bool]] = []
        for r, row in enumerate(raw):
            if not isinstance(row, list) or len(row) != 9:
                return None
            given.append([bool(row[c]) for c in range(9)])
        return given
    # Fallback: treat filled cells as given (solved board still looks fine).
    return [[board[r][c]["value"] != 0 for c in range(9)] for r in range(9)]


def _sanitize_challenge_board_update(
    game: dict, board: list[list[dict]]
) -> list[list[dict]] | None:
    """Return a board safe to persist for a challenge race.

    Clue cells are forced to the solution. Wrong non-clue digits are cleared so a
    forged/local wrong board cannot overwrite good race progress, while still
    allowing mid-race mistakes without failing the whole autosave.
    """
    from bot import cell_value, normalize_solution

    given = game.get("given")
    solution = normalize_solution(game.get("solution"))
    if not given or not solution:
        return None
    out: list[list[dict]] = []
    for r in range(9):
        row_out: list[dict] = []
        for c in range(9):
            cell = board[r][c]
            marks = cell.get("pencil_marks") if isinstance(cell, dict) else []
            if not isinstance(marks, list):
                marks = []
            val = cell_value(board, r, c)
            if val != 0 and (val < 1 or val > 9):
                return None
            if given[r][c]:
                row_out.append({"value": int(solution[r][c]), "pencil_marks": []})
                continue
            if val != 0 and val != solution[r][c]:
                row_out.append(
                    {
                        "value": 0,
                        "pencil_marks": [int(m) for m in marks if str(m).isdigit()],
                    }
                )
                continue
            row_out.append(
                {
                    "value": int(val),
                    "pencil_marks": [int(m) for m in marks if str(m).isdigit()],
                }
            )
        out.append(row_out)
    return out


def _validate_challenge_board_update(game: dict, board: list[list[dict]]) -> bool:
    """True when the board can be sanitized into a valid challenge update."""
    return _sanitize_challenge_board_update(game, board) is not None


def _verify_activity_solve(
    board: list[list[dict]] | None,
    *,
    solution: Any,
    given: list[list[bool]] | None = None,
) -> bool:
    """True only when board is fully solved against the authoritative solution."""
    from bot import cell_value, find_conflicts, filled_count, is_solved, normalize_solution, values_grid

    if board is None:
        return False
    sol = normalize_solution(solution)
    if not sol:
        return False
    # Given clues must still match the solution (client cannot rewrite givens).
    if given is not None:
        for r in range(9):
            for c in range(9):
                if given[r][c] and cell_value(board, r, c) != sol[r][c]:
                    return False
    if is_solved(board, sol):
        return True
    # Unique-puzzle fallback: a conflict-free complete grid that respects clues
    # is the solution even if the stored solution grid was corrupted in memory.
    if filled_count(board) < 81 or find_conflicts(board):
        return False
    grid = values_grid(board)
    if not _is_valid_complete_sudoku(grid):
        return False
    if given is not None:
        for r in range(9):
            for c in range(9):
                if given[r][c] and grid[r][c] != sol[r][c]:
                    return False
        return True
    return False


def _challenge_solve_debug(board: list[list[dict]] | None, game: dict) -> str:
    """Short mismatch summary for logs when challenge win is rejected."""
    from bot import cell_value, filled_count, find_conflicts, normalize_solution, values_grid

    if board is None:
        return "board=None"
    sol = normalize_solution(game.get("solution"))
    filled = filled_count(board)
    conflicts = len(find_conflicts(board))
    if not sol:
        return f"filled={filled} conflicts={conflicts} solution=missing"
    mism = 0
    clue_bad = 0
    given = game.get("given")
    for r in range(9):
        for c in range(9):
            val = cell_value(board, r, c)
            if val != sol[r][c]:
                mism += 1
            if given and given[r][c] and val != sol[r][c]:
                clue_bad += 1
    return (
        f"filled={filled} conflicts={conflicts} mismatches={mism} "
        f"clue_mismatches={clue_bad} valid={_is_valid_complete_sudoku(values_grid(board))}"
    )


def _is_valid_complete_sudoku(grid: list[list[int]]) -> bool:
    """True when grid is a filled 9×9 Sudoku with no row/col/box conflicts."""
    if len(grid) != 9:
        return False
    for r in range(9):
        if len(grid[r]) != 9:
            return False
        for c in range(9):
            v = grid[r][c]
            if not isinstance(v, int) or v < 1 or v > 9:
                return False

    def _unit_ok(vals: list[int]) -> bool:
        return sorted(vals) == list(range(1, 10))

    for r in range(9):
        if not _unit_ok(grid[r]):
            return False
    for c in range(9):
        if not _unit_ok([grid[r][c] for r in range(9)]):
            return False
    for br in range(0, 9, 3):
        for bc in range(0, 9, 3):
            box = [grid[r][c] for r in range(br, br + 3) for c in range(bc, bc + 3)]
            if not _unit_ok(box):
                return False
    return True


def _validate_activity_puzzle(
    given: list[list[bool]] | None,
    solution: Any,
    board: list[list[dict]] | None = None,
) -> tuple[bool, str]:
    """Reject forged/trivial client puzzles before they become the session authority."""
    from bot import _sudoku_count_solutions, normalize_solution

    sol = normalize_solution(solution if isinstance(solution, list) else None)
    if not sol or not _is_valid_complete_sudoku(sol):
        return False, "invalid_solution"
    if given is None:
        return False, "invalid_given"
    clues = 0
    for r in range(9):
        for c in range(9):
            if not given[r][c]:
                continue
            clues += 1
            if board is not None:
                bv = int(board[r][c].get("value") or 0)
                # Given cells on the board must show the solution digit (or be empty).
                if bv and bv != sol[r][c]:
                    return False, "given_mismatch"
    if clues < 17:
        return False, "too_few_clues"
    # Soft upper bound: Very Easy targets ~50 clues; reject near-solved starts.
    if clues > 55:
        return False, "too_many_clues"

    puzzle = [[sol[r][c] if given[r][c] else 0 for c in range(9)] for r in range(9)]
    try:
        nsol = _sudoku_count_solutions(puzzle, limit=2)
    except Exception as exc:  # noqa: BLE001
        print(f"activity puzzle uniqueness check failed: {exc}")
        return False, "uniqueness_check_failed"
    if nsol != 1:
        return False, "not_unique"
    return True, "ok"


def start_unified_http_server(bot_getter: BotGetter) -> None:
    port = int(os.getenv("PORT", "8080") or 8080)
    started_at = time.monotonic()
    ready_grace_s = float(os.getenv("HEALTH_READY_GRACE_S", "90") or 90)
    static_root = _static_root()
    if static_root:
        print(f"Activity static files: {static_root}")
    else:
        print("Activity static files: missing (build activity/client dist)")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _cors_origin(self) -> str | None:
            req_origin = self.headers.get("Origin", "").strip()
            if not req_origin:
                return None
            if (
                ".discordsays.com" in req_origin
                or ".discord.com" in req_origin
                or ".onrender.com" in req_origin
                or "localhost" in req_origin
                or "127.0.0.1" in req_origin
            ):
                return req_origin
            return None

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            cors_origin = self._cors_origin()
            cors_headers = {k: v for k, v in CORS.items() if k != "Access-Control-Allow-Origin"}
            if cors_origin:
                cors_headers["Access-Control-Allow-Origin"] = cors_origin
                cors_headers["Vary"] = "Origin"
            cors_headers.update(extra or {})
            for key, value in cors_headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, status: int, payload: dict) -> None:
            self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode() or "{}")

        def _path_only(self) -> str:
            return urllib.parse.urlparse(self.path).path

        def _health(self) -> None:
            bot = bot_getter()
            ready = False
            user = "-"
            try:
                ready = bool(bot.is_ready())
                if ready and bot.user is not None:
                    user = str(bot.user)
            except Exception:
                pass
            aged_out = (time.monotonic() - started_at) >= ready_grace_s
            status = 200 if ready or not aged_out else 503
            label = "ok" if status == 200 else "not_ready"
            body = f"{label} ready={ready} user={user}".encode()
            self._send(status, body, "text/plain; charset=utf-8", extra={})

        def _serve_static(self, rel: str) -> bool:
            root = static_root
            if root is None:
                return False
            rel = rel.lstrip("/")
            if not rel or rel.endswith("/"):
                rel = (rel + "index.html") if rel else "index.html"
            target = (root / rel).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                self._send_json(403, {"error": "forbidden"})
                return True
            if not target.is_file():
                # SPA fallback only for navigations — never for asset-like paths.
                if "." in Path(rel).name:
                    return False
                index = root / "index.html"
                if index.is_file() and self.command == "GET":
                    data = index.read_bytes()
                    self._send(200, data, "text/html; charset=utf-8")
                    return True
                return False
            data = target.read_bytes()
            ctype, _ = mimetypes.guess_type(str(target))
            self._send(200, data, ctype or "application/octet-stream")
            return True

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(204, b"", "text/plain")

        def do_GET(self) -> None:  # noqa: N802
            path = self._path_only()
            if path == "/health":
                self._health()
                return

            if path in ("/api/leaderboard", "/leaderboard"):
                self._leaderboard()
                return

            if path in ("/api/activity/profile", "/activity/profile", "/api/profile", "/profile"):
                self._activity_profile()
                return

            if path in ("/api/activity/session", "/activity/session"):
                self._activity_session_get()
                return

            if path in ("/api/activity/spectate/pending", "/activity/spectate/pending"):
                self._activity_spectate_pending()
                return

            if path in ("/api/activity/spectate", "/activity/spectate"):
                self._activity_spectate_get()
                return

            if path in ("/api/activity/watchers", "/activity/watchers"):
                self._activity_watchers_get()
                return

            proxied = _proxy_cdn(path)
            if proxied is not None:
                status, body, ctype = proxied
                self._send(status, body, ctype)
                return

            if self._serve_static(path):
                return

            if path == "/":
                self._health()
                return

            self._send_json(404, {"error": "not_found", "path": path})

        def do_POST(self) -> None:  # noqa: N802
            path = self._path_only()
            if path in ("/api/token", "/token"):
                self._token()
                return
            if path in ("/api/activity/win", "/activity/win"):
                self._activity_win()
                return
            if path in ("/api/activity/hint", "/activity/hint"):
                self._activity_hint()
                return
            if path in ("/api/activity/session", "/activity/session"):
                self._activity_session_save()
                return
            self._send_json(404, {"error": "not_found", "path": path})

        def do_DELETE(self) -> None:  # noqa: N802
            path = self._path_only()
            if path in ("/api/activity/session", "/activity/session"):
                self._activity_session_delete()
                return
            self._send_json(404, {"error": "not_found", "path": path})

        def _token(self) -> None:
            try:
                payload = self._read_json()
            except Exception:
                self._send_json(400, {"error": "invalid_json"})
                return
            code = payload.get("code")
            if not code:
                self._send_json(400, {"error": "missing_code"})
                return
            status, data = _exchange_token(str(code), bot=bot_getter())
            self._send_json(status, data)

        def _leaderboard(self) -> None:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            guild_id = (qs.get("guild_id") or [None])[0]
            if guild_id is None or str(guild_id).strip() in ("", "0"):
                self._send_json(
                    400,
                    {
                        "error": "guild_id_required",
                        "top": [],
                        "message": "Pass guild_id to scope the leaderboard to one server.",
                    },
                )
                return
            try:
                limit = min(50, max(1, int((qs.get("limit") or ["10"])[0])))
            except ValueError:
                limit = 10
            bot = bot_getter()
            data: dict = {}
            try:
                if bot.is_ready() and isinstance(getattr(bot, "data", None), dict):
                    data = bot.data
                else:

                    async def _load():
                        from bot import match_store

                        remote = await match_store.load_leaderboard()
                        return remote or {}

                    data = _run_coro(bot, _load())
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "leaderboard_failed", "message": str(exc)})
                return
            top = _collect_top_xp(data, guild_id, limit)
            self._send_json(200, {"top": top, "guild_id": guild_id, "updated": True})

        def _activity_profile(self) -> None:
            bot = bot_getter()
            user = _discord_user_from_bearer(self.headers.get("Authorization"), bot=bot)
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            guild_raw = (qs.get("guild_id") or ["0"])[0]
            uid = int(user["id"])
            try:
                gid_key = int(_run_coro(bot, _resolve_activity_guild_id(guild_raw, uid)))
            except ValueError:
                gid_key = 0
            try:
                from bot import (
                    SHOP_TITLES,
                    HINT_SPONGE_COST,
                    equipped_title_id,
                    evaluate_user_achievements,
                    guild_stats,
                    owned_pin_emojis,
                    user_stats,
                )

                gstats = guild_stats(bot.data if isinstance(getattr(bot, "data", None), dict) else {}, gid_key)
                stats = user_stats(gstats, uid)
                badges = evaluate_user_achievements(stats)
                tid = equipped_title_id(stats)
                title_meta = SHOP_TITLES.get(tid or "") if tid else None
                title = None
                if title_meta:
                    title = {
                        "id": tid,
                        "label": title_meta.get("label") or "",
                        "pin": title_meta.get("pin") or "",
                        "emoji": title_meta.get("emoji") or "",
                    }
                self._send_json(
                    200,
                    {
                        "user_id": str(uid),
                        "guild_id": str(gid_key),
                        "name": stats.get("name")
                        or user.get("global_name")
                        or user.get("username")
                        or "Unknown",
                        "title": title,
                        "pins": owned_pin_emojis(stats),
                        "xp": int(stats.get("xp") or 0),
                        "coins": int(stats.get("coins") or 0),
                        "badges": badges,
                        "streak_shields": int(stats.get("streak_shields") or 0),
                        "hint_sponge_cost": HINT_SPONGE_COST,
                        "gary_wisdom_charges": int(stats.get("gary_wisdom_charges") or 0),
                        "xp_boost_charges": int(stats.get("xp_boost_charges") or 0),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "profile_failed", "message": str(exc)})

        def _activity_win(self) -> None:
            bot = bot_getter()
            user = _discord_user_from_bearer(self.headers.get("Authorization"), bot=bot)
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                body = self._read_json()
            except Exception:
                self._send_json(400, {"error": "invalid_json"})
                return
            try:
                result = _run_coro(bot, _apply_activity_win(bot, user=user, body=body))
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                self._send_json(500, {"error": "win_failed", "message": str(exc)})
                return
            if not result.get("ok"):
                self._send_json(400, result)
                return
            self._send_json(200, result)

        def _activity_hint(self) -> None:
            bot = bot_getter()
            user = _discord_user_from_bearer(self.headers.get("Authorization"), bot=bot)
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                body = self._read_json()
            except Exception:
                self._send_json(400, {"error": "invalid_json"})
                return
            try:
                result = _run_coro(bot, _apply_activity_hint(bot, user=user, body=body))
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "hint_failed", "message": str(exc)})
                return
            if not result.get("ok"):
                self._send_json(400, result)
                return
            self._send_json(200, result)

        def _activity_session_get(self) -> None:
            bot = bot_getter()
            user = _discord_user_from_bearer(self.headers.get("Authorization"), bot=bot)
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            guild_id = (qs.get("guild_id") or ["0"])[0]
            try:
                result = _run_coro(bot, _load_activity_session(bot, user=user, guild_id=str(guild_id)))
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "session_load_failed", "message": str(exc)})
                return
            self._send_json(200, result)

        def _activity_session_save(self) -> None:
            bot = bot_getter()
            user = _discord_user_from_bearer(self.headers.get("Authorization"), bot=bot)
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                body = self._read_json()
            except Exception:
                self._send_json(400, {"error": "invalid_json"})
                return
            try:
                result = _run_coro(bot, _save_activity_session(bot, user=user, body=body))
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "session_save_failed", "message": str(exc)})
                return
            status = 200 if result.get("ok") else 400
            self._send_json(status, result)

        def _activity_session_delete(self) -> None:
            bot = bot_getter()
            user = _discord_user_from_bearer(self.headers.get("Authorization"), bot=bot)
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            guild_id = (qs.get("guild_id") or ["0"])[0]
            try:
                result = _run_coro(bot, _delete_activity_session(bot, user=user, guild_id=str(guild_id)))
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "session_delete_failed", "message": str(exc)})
                return
            self._send_json(200, result)

        def _activity_spectate_pending(self) -> None:
            bot = bot_getter()
            user = _discord_user_from_bearer(self.headers.get("Authorization"), bot=bot)
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            guild_id = (qs.get("guild_id") or ["0"])[0]
            try:
                result = _run_coro(
                    bot, _consume_spectate_intent(bot, user=user, guild_id=str(guild_id))
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "spectate_intent_failed", "message": str(exc)})
                return
            self._send_json(200, result)

        def _activity_spectate_get(self) -> None:
            bot = bot_getter()
            user = _discord_user_from_bearer(self.headers.get("Authorization"), bot=bot)
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            guild_id = (qs.get("guild_id") or ["0"])[0]
            target_user_id = (qs.get("target_user_id") or [""])[0]
            if not str(target_user_id).strip():
                self._send_json(400, {"error": "target_user_id_required"})
                return
            try:
                result = _run_coro(
                    bot,
                    _load_activity_spectate(
                        bot,
                        user=user,
                        guild_id=str(guild_id),
                        target_user_id=str(target_user_id),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "spectate_load_failed", "message": str(exc)})
                return
            if not result.get("ok"):
                self._send_json(400, result)
                return
            self._send_json(200, result)

        def _activity_watchers_get(self) -> None:
            bot = bot_getter()
            user = _discord_user_from_bearer(self.headers.get("Authorization"), bot=bot)
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            guild_id = (qs.get("guild_id") or ["0"])[0]
            try:
                result = _run_coro(
                    bot,
                    _load_activity_watchers(bot, user=user, guild_id=str(guild_id)),
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "watchers_load_failed", "message": str(exc)})
                return
            self._send_json(200, result)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="activity-http", daemon=True)
    thread.start()
    print(f"Unified HTTP listening on 0.0.0.0:{port} (/health, Activity, /api/*)")
    print(
        f"OAuth token exchange redirect_uri="
        f"{(os.getenv('DISCORD_OAUTH_REDIRECT_URI') or 'https://127.0.0.1').strip()}"
    )
