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
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


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
    ).strip()


def _client_secret() -> str:
    return (os.getenv("DISCORD_CLIENT_SECRET") or "").strip()


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _run_coro(bot: Any, coro: Any, timeout: float = 20.0) -> Any:
    loop = getattr(bot, "loop", None)
    if loop is None or not loop.is_running():
        raise RuntimeError("bot_loop_unavailable")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


def _exchange_token(code: str) -> tuple[int, dict]:
    client_id = _client_id()
    client_secret = _client_secret()
    if not client_id or not client_secret:
        return 500, {"error": "server_misconfigured"}
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
        }
    ).encode()
    req = urllib.request.Request(
        "https://discord.com/api/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            return 200, {"access_token": data.get("access_token")}
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode())
        except Exception:
            data = {"error": "token_exchange_failed", "status": exc.code}
        return int(exc.code), data
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": "token_exchange_failed", "message": str(exc)}


def _discord_user_from_bearer(auth_header: str | None) -> dict | None:
    if not auth_header:
        return None
    match = auth_header.strip()
    if not match.lower().startswith("bearer "):
        return None
    token = match[7:].strip()
    if not token:
        return None
    req = urllib.request.Request(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
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
    from bot import guild_stats, save_data, user_stats, win_reward

    difficulty = body.get("difficulty") or "medium"
    elapsed = max(0, int(body.get("elapsed") or 0))
    guild_id = str(body.get("guild_id") if body.get("guild_id") is not None else "0")
    display_name = (
        body.get("name")
        or user.get("global_name")
        or user.get("username")
        or "Unknown"
    )
    uid = int(user["id"])

    # guild_stats expects int guild ids for Discord guilds; activity may use "0"
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
    return {
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
    }


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
                # SPA fallback
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

            if self._serve_static(path):
                return

            if path == "/":
                # No Activity build yet — keep a tiny status page.
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
            status, data = _exchange_token(str(code))
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

        def _activity_win(self) -> None:
            user = _discord_user_from_bearer(self.headers.get("Authorization"))
            if not user or not user.get("id"):
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                body = self._read_json()
            except Exception:
                self._send_json(400, {"error": "invalid_json"})
                return
            bot = bot_getter()
            try:
                result = _run_coro(bot, _apply_activity_win(bot, user=user, body=body))
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "win_failed", "message": str(exc)})
                return
            self._send_json(200, result)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="activity-http", daemon=True)
    thread.start()
    print(f"Unified HTTP listening on 0.0.0.0:{port} (/health, Activity, /api/*)")
