"""
Unified HTTP for Render: health + Activity static + OAuth/Mongo APIs.

Replaces Netlify Functions so bot + Activity share one service URL.
Discord Activity URL Mappings should point at this host (no https://).
"""

from __future__ import annotations

import asyncio
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

CDN_PREFIXES = {
    "/pyscript/": "https://pyscript.net/",
    "/jsdelivr/": "https://cdn.jsdelivr.net/",
}

_CDN_CACHE_DIR = Path(os.getenv("CDN_CACHE_DIR") or "/tmp/thcoku-cdn-cache")


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
        equipped_title_id,
        guild_stats,
        owned_pin_emojis,
        render_board,
        save_data,
        user_stats,
        win_reward,
        win_reward_caption,
    )

    difficulty = body.get("difficulty") or "medium"
    elapsed = max(0, int(body.get("elapsed") or 0))
    guild_id = str(body.get("guild_id") if body.get("guild_id") is not None else "0")
    channel_id_raw = body.get("channel_id")
    display_name = (
        body.get("name")
        or user.get("global_name")
        or user.get("username")
        or "Unknown"
    )
    uid = int(user["id"])

    channel_id_for_panel = str(channel_id_raw) if channel_id_raw else None

    try:
        gid_key = int(guild_id)
    except ValueError:
        gid_key = 0

    gstats = guild_stats(bot.data, gid_key)
    stats = user_stats(gstats, uid)
    stats["name"] = display_name
    stats["wins"] = int(stats.get("wins") or 0) + 1
    stats["games"] = int(stats.get("games") or 0) + 1
    stats["streak"] = int(stats.get("streak") or 0) + 1
    stats["best_streak"] = max(int(stats.get("best_streak") or 0), int(stats["streak"]))
    if stats.get("best_time") is None or elapsed < float(stats["best_time"]):
        stats["best_time"] = float(elapsed)

    coins = win_reward(int(stats["streak"]), daily=False, difficulty=difficulty)
    xp = coins
    stats["coins"] = int(stats.get("coins") or 0) + coins
    stats["xp"] = int(stats.get("xp") or 0) + xp
    stats["activity_wins"] = int(stats.get("activity_wins") or 0) + 1
    stats["last_activity_win_at"] = time.time()

    save_data(bot.data)

    posted = False
    post_error = None
    try:
        board = _normalize_activity_board(body.get("board"))
        given = _normalize_activity_given(body.get("given"), board)
        if board and given and channel_id_raw:
            channel_id = int(channel_id_raw)
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception as exc:  # noqa: BLE001
                    post_error = f"fetch_channel: {exc}"
                    channel = None
            if channel is not None:
                image = render_board(
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
                mm, ss = divmod(elapsed, 60)
                caption = (
                    f"{win_reward_caption(coins, xp)}\n"
                    f"**{display_name}** resolved Activity · "
                    f"{difficulty} · {mm:02d}:{ss:02d}"
                )
                await channel.send(content=caption, file=file)
                posted = True
    except Exception as exc:  # noqa: BLE001
        post_error = str(exc)
        print(f"activity win chat post failed: {exc}")

    try:
        from challenge_store import match_store

        await match_store.delete_activity_session(_activity_session_id(guild_id, uid))
    except Exception as exc:  # noqa: BLE001
        print(f"activity session clear on win failed: {exc}")

    if channel_id_for_panel:
        try:
            from bot import schedule_activity_live_update

            schedule_activity_live_update(guild_id, str(uid), immediate=True)
        except Exception as exc:  # noqa: BLE001
            print(f"activity live panel clear failed: {exc}")

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


async def _save_activity_session(bot: Any, *, user: dict, body: dict) -> dict:
    from challenge_store import match_store

    guild_id = str(body.get("guild_id") if body.get("guild_id") is not None else "0")
    if body.get("clear") or body.get("action") == "clear":
        return await _delete_activity_session(bot, user=user, guild_id=guild_id)

    uid = int(user["id"])
    board = _normalize_activity_board(body.get("board"))
    given = _normalize_activity_given(body.get("given"), board)
    solution = body.get("solution")
    if board is None or given is None:
        return {"ok": False, "error": "invalid_board"}
    if not isinstance(solution, list) or len(solution) != 9:
        return {"ok": False, "error": "invalid_solution"}

    difficulty = body.get("difficulty") or "medium"
    diff_index = int(body.get("diff_index") or 0)
    elapsed = max(0, int(body.get("elapsed") or 0))
    channel_id_raw = body.get("channel_id")
    filled = sum(1 for r in range(9) for c in range(9) if board[r][c]["value"])
    # Don't keep fully solved boards as "continue"
    if filled >= 81:
        await match_store.delete_activity_session(_activity_session_id(guild_id, uid))
        return {"ok": True, "cleared": True}

    doc = {
        "_id": _activity_session_id(guild_id, uid),
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
        "last_move_at": time.time(),
    }
    await match_store.upsert_activity_session(doc)
    try:
        from bot import schedule_activity_live_update

        schedule_activity_live_update(guild_id, str(uid))
    except Exception as exc:  # noqa: BLE001
        print(f"activity live panel schedule failed: {exc}")
    return {"ok": True, "filled": filled, "elapsed": elapsed}


async def _load_activity_session(bot: Any, *, user: dict, guild_id: str) -> dict:
    from challenge_store import match_store

    uid = int(user["id"])
    doc = await match_store.get_activity_session(_activity_session_id(guild_id, uid))
    if not doc:
        return {"ok": True, "session": None}
    return {
        "ok": True,
        "session": {
            "difficulty": doc.get("difficulty") or "medium",
            "diff_index": int(doc.get("diff_index") or 0),
            "elapsed": int(doc.get("elapsed") or 0),
            "board": doc.get("board"),
            "given": doc.get("given"),
            "solution": doc.get("solution"),
            "filled": int(doc.get("filled") or 0),
            "updated_at": doc.get("updated_at"),
        },
    }


async def _delete_activity_session(bot: Any, *, user: dict, guild_id: str) -> dict:
    from challenge_store import match_store

    uid = int(user["id"])
    await match_store.delete_activity_session(_activity_session_id(guild_id, uid))
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

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            headers = {**CORS, **(extra or {})}
            for key, value in headers.items():
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
                        from challenge_store import match_store

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
            try:
                gid_key = int(guild_raw)
            except ValueError:
                gid_key = 0
            try:
                from bot import (
                    SHOP_TITLES,
                    equipped_title_id,
                    guild_stats,
                    owned_pin_emojis,
                    user_stats,
                )

                uid = int(user["id"])
                gstats = guild_stats(bot.data if isinstance(getattr(bot, "data", None), dict) else {}, gid_key)
                stats = user_stats(gstats, uid)
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
                self._send_json(500, {"error": "win_failed", "message": str(exc)})
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
