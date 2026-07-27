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

BotGetter = Callable[[], Any]

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS, HEAD, DELETE",
}

MAX_HINTS_PLAY = 10
MAX_HINTS_DAILY = 3

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


async def _lookup_activity_session(
    bot: Any, guild_id: str | int, user_id: str | int
) -> tuple[dict | None, str]:
    """Primary key → orphan activity:0 (same guild or unset) → find-by-user scoped."""
    from bot import match_store

    uid = int(user_id)
    gid = str(guild_id if guild_id is not None else "0")
    primary = _activity_session_id(gid, uid)
    session = await match_store.get_activity_session(primary)
    if session:
        return session, primary

    orphan_id = _activity_session_id("0", uid)
    if orphan_id != primary:
        orphan = await match_store.get_activity_session(orphan_id)
        if orphan:
            orphan_gid = str(orphan.get("guild_id") or "0")
            # Only accept orphan if it belongs to this guild or has no real guild yet.
            if orphan_gid in ("", "0", gid):
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
    from bot import clear_activity_session, end_activity_watch

    try:
        await end_activity_watch(bot, session_id, force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"activity win end_watch failed: {exc}")
    try:
        await clear_activity_session(bot, session_id)
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
                }
            )
    rows.sort(key=lambda r: r["xp"], reverse=True)
    return rows[:limit]


async def _apply_activity_win(bot: Any, *, user: dict, body: dict) -> dict:
    # Local imports avoid circular import at module load.
    from bot import (
        board_to_file,
        build_activity_win_embed,
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

    from bot import find_challenge_game_for_user, games, handle_challenge_completion_activity
    ch_key = find_challenge_game_for_user(uid)
    if not ch_key:
        # Mongo fallback after restart — rehydrate then complete.
        try:
            from bot import match_store, match_player_entries, restore_challenge_games_from_match

            active = await match_store.list_matches(status="active")
            for match in active:
                for slot, player in match_player_entries(match):
                    if player.get("user_id") != uid:
                        continue
                    if player.get("forfeit") or player.get("finished_time") is not None:
                        continue
                    await restore_challenge_games_from_match(bot, match)
                    ch_key = find_challenge_game_for_user(uid)
                    break
                if ch_key:
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"activity challenge win rehydrate failed: {exc}")
    if ch_key:
        game = games[ch_key]
        board = _normalize_activity_board(body.get("board"))
        given = game.get("given")
        if isinstance(given, list):
            given_norm = _normalize_activity_given(given, board)
        else:
            given_norm = None
        if not _verify_activity_solve(board, solution=game.get("solution"), given=given_norm):
            print(f"activity win rejected challenge user={uid}: not_solved")
            return {"ok": False, "error": "not_solved"}
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

    try:
        gid_key = int(guild_id)
    except ValueError:
        gid_key = 0

    from bot import match_store
    session, lookup_id = await _lookup_activity_session(bot, gid_key, uid)
    # Lock on canonical guild key when known, else lookup id.
    lock_id = (
        _activity_session_id(gid_key, uid)
        if gid_key
        else lookup_id
    )

    async with _activity_win_lock(lock_id):
        # Re-fetch under lock — never revive a stale in-memory copy.
        session = await match_store.get_activity_session(lookup_id)
        if not session and lookup_id != lock_id:
            session = await match_store.get_activity_session(lock_id)
        session_id = lookup_id if session else lock_id
        # Prefer session-stored channel; client channel_id only if same guild.
        channel_id_raw = (session or {}).get("channel_id") or body.get("channel_id")

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
        # Prefer server-stored difficulty over client-supplied (anti-spoof).
        difficulty = session.get("difficulty") or difficulty

        if not _verify_activity_solve(board, solution=solution, given=given):
            print(f"activity win rejected user={uid} guild={guild_id}: not_solved")
            return {"ok": False, "error": "not_solved"}

        started_at = float(session.get("started_at") or 0)
        if started_at > 0:
            elapsed = max(0, int(time.time() - started_at))
        elif session.get("elapsed") is not None:
            elapsed = max(0, int(session.get("elapsed") or 0))
        else:
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
                return {
                    "ok": True,
                    "already_won": int(outcome.coins) > 0,
                    "error": "already_won" if int(outcome.coins) > 0 else "daily_locked",
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
            share_text = None
            for field in outcome.embed.fields:
                if field.name == "Share":
                    raw = str(field.value or "").strip()
                    share_text = raw.strip("`").strip()
                    break
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
                            share_text=share_text,
                        )
                        await channel.send(
                            embed=announce_embed,
                            file=file,
                        )
                        posted = True
                        # Prevent /claimdaily from re-posting the same daily win.
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
                except Exception as exc:
                    post_error = f"send_daily_announce: {exc}"

            return {
                "ok": True,
                "daily": True,
                "coins": int(outcome.coins),
                "xp": int(outcome.xp),
                "streak": int(stats.get("streak") or 0),
                "elapsed": elapsed,
                "posted": posted,
                "post_error": post_error,
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
                    )
                    await channel.send(embed=embed, file=file)
                    posted = True
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
            "elapsed": elapsed,
            "difficulty": difficulty,
            "guild_id": guild_id,
            "user_id": str(uid),
            "posted": posted,
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


def _hints_max_for_session(session_kind: str | None) -> int:
    return MAX_HINTS_DAILY if session_kind == "daily" else MAX_HINTS_PLAY


def _client_activity_session(doc: dict, *, strip_solution: bool = True) -> dict:
    """Session payload for the Activity client (solution withheld server-side)."""
    from bot import normalize_solution

    session_kind = doc.get("session_kind") or "play"
    started = float(doc.get("started_at") or 0)
    if started > 0:
        elapsed_display = max(0, int(time.time() - started))
    else:
        elapsed_display = int(doc.get("elapsed") or 0)
    payload: dict = {
        "difficulty": doc.get("difficulty") or "medium",
        "diff_index": int(doc.get("diff_index") or 0),
        "elapsed": elapsed_display,
        "board": doc.get("board"),
        "given": doc.get("given"),
        "filled": int(doc.get("filled") or 0),
        "updated_at": doc.get("updated_at"),
        "session_kind": session_kind,
        "daily_date": doc.get("daily_date"),
        "won_at": doc.get("won_at"),
        "hints_used": int(doc.get("hints_used") or 0),
        "hints_max": _hints_max_for_session(session_kind),
    }
    if started > 0:
        payload["started_at"] = started
    if not strip_solution:
        sol = normalize_solution(doc.get("solution"))
        if sol:
            payload["solution"] = sol
    return payload


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
    """Resolve a win-announce channel that belongs to the puzzle's guild."""
    candidates: list[int] = []
    for raw in (channel_id_raw, (session or {}).get("channel_id")):
        if raw is None or raw == "":
            continue
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid not in candidates:
            candidates.append(cid)

    for cid in candidates:
        channel = bot.get_channel(cid)
        if channel is None:
            try:
                channel = await bot.fetch_channel(cid)
            except Exception:  # noqa: BLE001
                channel = None
        if channel is not None and _channel_belongs_to_guild(channel, guild_id):
            return channel
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
        from bot import find_challenge_game_for_user, games
        if find_challenge_game_for_user(uid):
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
        forfeited = False
        from bot import find_challenge_game_for_user

        # Only forfeit when a challenge race is actually active for this user.
        if find_challenge_game_for_user(uid) or body.get("challenge_forfeit"):
            try:
                from bot import forfeit_challenge_activity

                forfeited = await forfeit_challenge_activity(bot, uid)
            except Exception as exc:  # noqa: BLE001
                print(f"activity end_watch challenge forfeit failed: {exc}")
        try:
            from bot import end_activity_watch

            await end_activity_watch(
                bot,
                session_id,
                force=bool(body.get("force")),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"activity end_watch failed: {exc}")
        return {"ok": True, "watch_ended": True, "challenge_forfeit": forfeited}

    board = _normalize_activity_board(body.get("board"))
    given_raw = body.get("given")
    given = _normalize_activity_given(given_raw, board)

    from bot import find_challenge_game_for_user, games, sync_challenge_board
    ch_key = find_challenge_game_for_user(uid)
    if ch_key:
        game = games[ch_key]
        if board:
            if not _validate_challenge_board_update(game, board):
                print(
                    f"activity challenge save rejected invalid board user={uid} "
                    f"match={game.get('match_id')}"
                )
                return {"ok": False, "error": "invalid_board"}
            game["board"] = board
            game["filled"] = sum(1 for r in range(9) for c in range(9) if board[r][c]["value"])
            started = float(game.get("started_at") or time.time())
            game["elapsed"] = max(0, int(time.time() - started))
            await sync_challenge_board(game)
        return {"ok": True, "filled": game["filled"], "elapsed": game["elapsed"]}
    solution = body.get("solution")
    if board is None or given is None:
        print(
            f"activity session save invalid_board user={uid} guild={guild_id} "
            f"raw_guild={body.get('guild_id')}"
        )
        return {"ok": False, "error": "invalid_board"}

    existing = await match_store.get_activity_session(session_id)
    if (not isinstance(solution, list) or len(solution) != 9) and existing:
        solution = existing.get("solution")
    if not isinstance(solution, list) or len(solution) != 9:
        print(
            f"activity session save invalid_solution user={uid} guild={guild_id} "
            f"solution_type={type(solution).__name__}"
        )
        return {"ok": False, "error": "invalid_solution"}

    difficulty = body.get("difficulty") or "medium"
    diff_index = int(body.get("diff_index") or 0)
    elapsed = max(0, int(body.get("elapsed") or 0))
    channel_id_raw = body.get("channel_id")
    filled = sum(1 for r in range(9) for c in range(9) if board[r][c]["value"])
    hints_used = max(
        int(existing.get("hints_used") or 0) if existing else 0,
        max(0, int(body.get("hints_used") or 0)),
    )

    session_kind = "play"
    daily_date = None
    started_at = time.time()
    accepting_client_puzzle = True
    if existing:
        session_kind = existing.get("session_kind") or "play"
        daily_date = existing.get("daily_date")
        started_at = existing.get("started_at") or time.time()
        # Pin puzzle metadata once authorized. Daily is always immutable.
        # Play: keep solution/difficulty if the given clues still match (same puzzle);
        # a different given matrix means the player started a new game.
        if existing.get("solution") and existing.get("given"):
            existing_given = _normalize_activity_given(existing.get("given"), board)
            same_puzzle = existing_given is not None and existing_given == given
            if session_kind == "daily" or same_puzzle:
                given = existing_given or given
                solution = existing.get("solution")
                difficulty = existing.get("difficulty") or difficulty
                if existing.get("diff_index") is not None:
                    diff_index = int(existing.get("diff_index") or 0)
                accepting_client_puzzle = False
            elif session_kind == "play":
                # New play puzzle — reset the session clock and any stale win claim.
                started_at = time.time()

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

    doc = {
        "_id": session_id,
        "guild_id": guild_id,
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
        "channel_id": str(channel_id_raw) if channel_id_raw else None,
        "session_kind": session_kind,
        "daily_date": daily_date,
        "started_at": started_at,
        "last_move_at": time.time(),
        "hints_used": hints_used,
    }
    # New play puzzle must drop a leftover won_at from a prior failed clear.
    if existing and session_kind == "play":
        existing_given = _normalize_activity_given(existing.get("given"), board)
        same_puzzle = (
            existing.get("given")
            and existing_given is not None
            and existing_given == given
        )
        if not same_puzzle:
            doc["won_at"] = None
            doc["watch_once_notified"] = False
            doc["watch_notified"] = False
            doc["watch_message_id"] = None
    await match_store.upsert_activity_session(doc)
    wrong_id = _activity_session_id("0", uid)
    if wrong_id != session_id:
        wrong = await match_store.get_activity_session(wrong_id)
        if wrong:
            await match_store.delete_activity_session(wrong_id)
    current = await match_store.get_activity_session(session_id)
    
    from bot import _activity_notify_inflight
    posted_at = float((current or {}).get("watch_posted_at") or 0)
    in_flight = session_id in _activity_notify_inflight
    watch_live = bool(
        in_flight
        or (current and current.get("watch_notified") and current.get("watch_message_id"))
        or (time.time() - posted_at < 60)
    )
    print(
        f"activity session save user={uid} guild={guild_id} filled={filled} "
        f"notify={'skip' if watch_live else 'yes'}"
    )
    # Skip re-notification if the player already got one for this game session.
    # watch_once_notified is set by notify_activity_play_started and only cleared when
    # the session doc itself is deleted (new game, win, or quit).
    already_notified_once = bool(current and current.get("watch_once_notified"))
    if not watch_live and not already_notified_once:
        try:
            from bot import notify_activity_play_started

            await notify_activity_play_started(bot, session_id)
        except Exception as exc:  # noqa: BLE001
            print(f"activity play notify failed: {exc}")
    return {"ok": True, "filled": filled, "elapsed": elapsed}


async def _load_activity_session(bot: Any, *, user: dict, guild_id: str) -> dict:
    from bot import match_store, clear_activity_session

    uid = int(user["id"])
    from bot import find_challenge_game_for_user, games, game_filled_count
    ch_key = find_challenge_game_for_user(uid)
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
                    "started_at": float(game.get("started_at") or time.time()),
                    "match_id": game.get("match_id"),
                    "player_slot": game.get("player_slot"),
                },
                strip_solution=True,
            )
            | {
                "match_id": game.get("match_id"),
                "player_slot": game.get("player_slot"),
            },
        }

    resolved_guild = await _resolve_activity_guild_id(guild_id, uid)
    doc, _sid = await _lookup_activity_session(bot, resolved_guild, uid)
    if not doc:
        return {"ok": True, "session": None}
    return {"ok": True, "session": _client_activity_session(doc, strip_solution=True)}


async def _apply_activity_hint(bot: Any, *, user: dict, body: dict) -> dict:
    from bot import (
        find_challenge_game_for_user,
        games,
        match_store,
        normalize_solution,
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

    ch_key = find_challenge_game_for_user(uid)
    if ch_key:
        game = games.get(ch_key)
        if not game or board is None:
            return {"ok": False, "error": "invalid_board"}
        solution = normalize_solution(game.get("solution"))
        given = _normalize_activity_given(game.get("given"), board)
        session_kind = "challenge"
        hints_used = int(game.get("hints_used") or 0)
        max_hints = MAX_HINTS_PLAY
        persist_hint = None
    else:
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
        max_hints = _hints_max_for_session(session_kind)

    async with _activity_win_lock(persist_hint or f"hint:{uid}"):
        # Re-check hint budget under lock (TOCTOU).
        if persist_hint:
            fresh = await match_store.get_activity_session(persist_hint)
            if fresh:
                hints_used = int(fresh.get("hints_used") or 0)
        elif ch_key and game is not None:
            hints_used = int(game.get("hints_used") or 0)

        if hints_used >= max_hints:
            return {
                "ok": False,
                "error": "hints_exhausted",
                "hints_used": hints_used,
                "hints_max": max_hints,
            }

        picked = _pick_hint_cell(board, given, solution, row, col)
        if picked is None:
            return {"ok": False, "error": "no_hint_available"}

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
        "hints_max": max_hints,
        "session_kind": session_kind,
    }


async def _delete_activity_session(bot: Any, *, user: dict, guild_id: str) -> dict:
    uid = int(user["id"])
    from bot import (
        find_challenge_game_for_user,
        finish_forfeit,
        is_solved,
        match_store,
        normalize_solution,
    )

    if find_challenge_game_for_user(uid):
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

            await end_activity_watch(bot, session_id, force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"delete session end_watch failed: {exc}")

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

        if kind == "daily" and gid:
            if solved:
                from bot import finish_win_and_announce

                game_state = {
                    "mode": "daily",
                    "daily_date": session.get("daily_date"),
                    "started_at": float(session.get("started_at") or time.time()),
                    "difficulty": session.get("difficulty"),
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
                finish_forfeit(
                    bot.data,
                    gid,
                    actor,
                    {
                        "mode": "daily",
                        "daily_date": session.get("daily_date"),
                        "started_at": float(session.get("started_at") or time.time()),
                        "difficulty": session.get("difficulty"),
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
                    "difficulty": session.get("difficulty"),
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
                # Abandon mid-game via clear/"new order" — same streak wipe as /quit.
                finish_forfeit(
                    bot.data,
                    gid,
                    actor,
                    {
                        "mode": "play",
                        "started_at": float(session.get("started_at") or time.time()),
                        "difficulty": session.get("difficulty"),
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


def _validate_challenge_board_update(game: dict, board: list[list[dict]]) -> bool:
    """Reject forged challenge edits (given cells rewritten, invalid digits)."""
    from bot import cell_value, normalize_solution

    given = game.get("given")
    solution = normalize_solution(game.get("solution"))
    old_board = game.get("board")
    if not given or not solution or not old_board:
        return False
    for r in range(9):
        for c in range(9):
            val = cell_value(board, r, c)
            if val != 0 and (val < 1 or val > 9):
                return False
            if given[r][c]:
                if val != solution[r][c]:
                    return False
                if cell_value(old_board, r, c) != val:
                    return False
    return True


def _verify_activity_solve(
    board: list[list[dict]] | None,
    *,
    solution: Any,
    given: list[list[bool]] | None = None,
) -> bool:
    """True only when board is fully solved against the authoritative solution."""
    from bot import cell_value, is_solved, normalize_solution

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
    return is_solved(board, sol)


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
