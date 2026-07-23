"""Competitive challenge match persistence (MongoDB + in-memory fallback)."""

from __future__ import annotations

import os
import time
import uuid
from copy import deepcopy
from typing import Any


def _clone(obj: Any) -> Any:
    return deepcopy(obj)


class MatchStore:
    """Async store for speedrun matches, daily claims, and active game sessions."""

    async def connect(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    async def insert_match(self, doc: dict) -> str:
        raise NotImplementedError

    async def get_match(self, match_id: str) -> dict | None:
        raise NotImplementedError

    async def update_match(self, match_id: str, fields: dict) -> dict | None:
        raise NotImplementedError

    async def update_player(self, match_id: str, slot: str, fields: dict) -> dict | None:
        raise NotImplementedError

    async def list_matches(self, *, status: str) -> list[dict]:
        raise NotImplementedError

    async def upsert_active_game(self, doc: dict) -> None:
        raise NotImplementedError

    async def delete_active_game(self, game_id: str) -> None:
        raise NotImplementedError

    async def list_active_games(self) -> list[dict]:
        raise NotImplementedError

    async def try_claim_daily_win(
        self,
        *,
        guild_id: int,
        user_id: int,
        day: str,
        elapsed: int,
        hints: int,
        difficulty: str,
        coins: int,
        player_name: str | None = None,
    ) -> bool:
        """Atomically claim today's daily win. True = first claim (award + announce)."""
        raise NotImplementedError

    async def has_daily_claim(self, guild_id: int, user_id: int, day: str) -> bool:
        """True if this user already claimed today's daily win in durable storage."""
        return False

    async def count_daily_wins(self, guild_id: int, day: str) -> int:
        raise NotImplementedError

    async def save_leaderboard(self, data: dict) -> None:
        """Persist sponges / stats / daily board so Render redeploys don't wipe them."""
        return None

    async def load_leaderboard(self) -> dict | None:
        return None

    async def list_daily_completions(self) -> list[dict]:
        return []

    async def upsert_activity_session(self, doc: dict) -> None:
        """Save in-progress Activity puzzle (keyed by guild+user)."""
        raise NotImplementedError

    async def get_activity_session(self, session_id: str) -> dict | None:
        raise NotImplementedError

    async def delete_activity_session(self, session_id: str) -> None:
        raise NotImplementedError

    async def list_activity_sessions(
        self, guild_id: str, active_within_seconds: int = 300
    ) -> list[dict]:
        """Return public summaries of players active in the last N seconds."""
        return []


class MemoryMatchStore(MatchStore):
    def __init__(self) -> None:
        self._docs: dict[str, dict] = {}
        self._daily: dict[str, dict] = {}
        self._active: dict[str, dict] = {}
        self._activity: dict[str, dict] = {}
        self._leaderboard: dict | None = None

    async def connect(self) -> None:
        return None

    async def insert_match(self, doc: dict) -> str:
        match_id = doc.get("_id") or str(uuid.uuid4())
        payload = _clone(doc)
        payload["_id"] = match_id
        self._docs[match_id] = payload
        return match_id

    async def get_match(self, match_id: str) -> dict | None:
        doc = self._docs.get(match_id)
        return _clone(doc) if doc else None

    async def update_match(self, match_id: str, fields: dict) -> dict | None:
        doc = self._docs.get(match_id)
        if not doc:
            return None
        doc.update(fields)
        return _clone(doc)

    async def update_player(self, match_id: str, slot: str, fields: dict) -> dict | None:
        doc = self._docs.get(match_id)
        if not doc or slot not in doc:
            return None
        doc[slot].update(fields)
        return _clone(doc)

    async def list_matches(self, *, status: str) -> list[dict]:
        return [_clone(d) for d in self._docs.values() if d.get("status") == status]

    async def upsert_active_game(self, doc: dict) -> None:
        payload = _clone(doc)
        game_id = payload.get("_id")
        if not game_id:
            raise ValueError("active game needs _id")
        payload["updated_at"] = time.time()
        self._active[game_id] = payload

    async def delete_active_game(self, game_id: str) -> None:
        self._active.pop(game_id, None)

    async def list_active_games(self) -> list[dict]:
        return [_clone(d) for d in self._active.values()]

    async def upsert_activity_session(self, doc: dict) -> None:
        payload = _clone(doc)
        sid = payload.get("_id")
        if not sid:
            raise ValueError("activity session needs _id")
        payload["updated_at"] = time.time()
        self._activity[sid] = payload

    async def get_activity_session(self, session_id: str) -> dict | None:
        doc = self._activity.get(session_id)
        return _clone(doc) if doc else None

    async def delete_activity_session(self, session_id: str) -> None:
        self._activity.pop(session_id, None)

    async def list_activity_sessions(
        self, guild_id: str, active_within_seconds: int = 300
    ) -> list[dict]:
        cutoff = time.time() - active_within_seconds
        results = []
        for doc in self._activity.values():
            if str(doc.get("guild_id")) != str(guild_id):
                continue
            if (doc.get("updated_at") or 0) < cutoff:
                continue
            results.append({
                "user_id": str(doc.get("user_id", "")),
                "name": doc.get("name") or "Unknown",
                "difficulty": doc.get("difficulty") or "medium",
                "filled": int(doc.get("filled") or 0),
                "elapsed": int(doc.get("elapsed") or 0),
                "updated_at": doc.get("updated_at"),
            })
        results.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
        return results

    def _daily_key(self, guild_id: int, user_id: int, day: str) -> str:
        return f"{guild_id}:{day}:{user_id}"

    async def try_claim_daily_win(
        self,
        *,
        guild_id: int,
        user_id: int,
        day: str,
        elapsed: int,
        hints: int,
        difficulty: str,
        coins: int,
        player_name: str | None = None,
    ) -> bool:
        key = self._daily_key(guild_id, user_id, day)
        if key in self._daily:
            return False
        self._daily[key] = {
            "_id": key,
            "guild_id": guild_id,
            "user_id": user_id,
            "name": player_name or "Unknown",
            "date": day,
            "elapsed": elapsed,
            "hints": hints,
            "difficulty": difficulty,
            "coins": coins,
            "claimed_at": time.time(),
        }
        return True

    async def has_daily_claim(self, guild_id: int, user_id: int, day: str) -> bool:
        return self._daily_key(guild_id, user_id, day) in self._daily

    async def count_daily_wins(self, guild_id: int, day: str) -> int:
        return sum(
            1
            for doc in self._daily.values()
            if doc.get("guild_id") == guild_id and doc.get("date") == day
        )

    async def save_leaderboard(self, data: dict) -> None:
        self._leaderboard = _clone(data)

    async def load_leaderboard(self) -> dict | None:
        return _clone(self._leaderboard) if self._leaderboard is not None else None

    async def list_daily_completions(self) -> list[dict]:
        return [_clone(d) for d in self._daily.values()]


class MongoMatchStore(MatchStore):
    def __init__(self, uri: str, db_name: str = "sudoku") -> None:
        self.uri = uri
        self.db_name = db_name
        self._client = None
        self._col = None
        self._daily = None
        self._active = None
        self._activity = None
        self._leaderboard = None

    async def connect(self) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        self._client = AsyncIOMotorClient(self.uri)
        db = self._client[self.db_name]
        self._col = db["challenge_matches"]
        self._daily = db["daily_completions"]
        self._active = db["active_games"]
        self._activity = db["activity_sessions"]
        self._leaderboard = db["leaderboard"]
        await self._col.create_index("status")
        await self._daily.create_index([("guild_id", 1), ("date", 1)])
        await self._active.create_index("updated_at")
        await self._activity.create_index("updated_at")

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()

    async def insert_match(self, doc: dict) -> str:
        payload = _clone(doc)
        match_id = payload.get("_id") or str(uuid.uuid4())
        payload["_id"] = match_id
        await self._col.insert_one(payload)
        return match_id

    async def get_match(self, match_id: str) -> dict | None:
        return await self._col.find_one({"_id": match_id})

    async def update_match(self, match_id: str, fields: dict) -> dict | None:
        await self._col.update_one({"_id": match_id}, {"$set": fields})
        return await self.get_match(match_id)

    async def update_player(self, match_id: str, slot: str, fields: dict) -> dict | None:
        set_fields = {f"{slot}.{k}": v for k, v in fields.items()}
        await self._col.update_one({"_id": match_id}, {"$set": set_fields})
        return await self.get_match(match_id)

    async def list_matches(self, *, status: str) -> list[dict]:
        cursor = self._col.find({"status": status})
        return await cursor.to_list(length=500)

    async def upsert_active_game(self, doc: dict) -> None:
        payload = _clone(doc)
        game_id = payload.get("_id")
        if not game_id:
            raise ValueError("active game needs _id")
        payload["updated_at"] = time.time()
        await self._active.replace_one({"_id": game_id}, payload, upsert=True)

    async def delete_active_game(self, game_id: str) -> None:
        await self._active.delete_one({"_id": game_id})

    async def list_active_games(self) -> list[dict]:
        cursor = self._active.find({})
        return await cursor.to_list(length=500)

    async def upsert_activity_session(self, doc: dict) -> None:
        if self._activity is None:
            await self.connect()
        payload = _clone(doc)
        sid = payload.get("_id")
        if not sid:
            raise ValueError("activity session needs _id")
        payload["updated_at"] = time.time()
        await self._activity.replace_one({"_id": sid}, payload, upsert=True)

    async def get_activity_session(self, session_id: str) -> dict | None:
        if self._activity is None:
            await self.connect()
        return await self._activity.find_one({"_id": session_id})

    async def delete_activity_session(self, session_id: str) -> None:
        if self._activity is None:
            await self.connect()
        await self._activity.delete_one({"_id": session_id})

    async def list_activity_sessions(
        self, guild_id: str, active_within_seconds: int = 300
    ) -> list[dict]:
        if self._activity is None:
            await self.connect()
        cutoff = time.time() - active_within_seconds
        cursor = self._activity.find(
            {"guild_id": str(guild_id), "updated_at": {"$gte": cutoff}},
            sort=[("updated_at", -1)],
        )
        docs = await cursor.to_list(length=50)
        return [
            {
                "user_id": str(doc.get("user_id", "")),
                "name": doc.get("name") or "Unknown",
                "difficulty": doc.get("difficulty") or "medium",
                "filled": int(doc.get("filled") or 0),
                "elapsed": int(doc.get("elapsed") or 0),
                "updated_at": doc.get("updated_at"),
            }
            for doc in docs
        ]

    async def try_claim_daily_win(
        self,
        *,
        guild_id: int,
        user_id: int,
        day: str,
        elapsed: int,
        hints: int,
        difficulty: str,
        coins: int,
        player_name: str | None = None,
    ) -> bool:
        from pymongo.errors import DuplicateKeyError

        if self._daily is None:
            await self.connect()
        if self._daily is None:
            raise RuntimeError("Mongo daily collection not connected")

        doc = {
            "_id": f"{guild_id}:{day}:{user_id}",
            "guild_id": guild_id,
            "user_id": user_id,
            "name": player_name or "Unknown",
            "date": day,
            "elapsed": elapsed,
            "hints": hints,
            "difficulty": difficulty,
            "coins": coins,
            "claimed_at": time.time(),
        }
        try:
            await self._daily.insert_one(doc)
            return True
        except DuplicateKeyError:
            return False

    async def has_daily_claim(self, guild_id: int, user_id: int, day: str) -> bool:
        if self._daily is None:
            await self.connect()
        if self._daily is None:
            return False
        doc = await self._daily.find_one({"_id": f"{guild_id}:{day}:{user_id}"})
        return doc is not None

    async def count_daily_wins(self, guild_id: int, day: str) -> int:
        return await self._daily.count_documents({"guild_id": guild_id, "date": day})

    async def save_leaderboard(self, data: dict) -> None:
        if self._leaderboard is None:
            await self.connect()
        await self._leaderboard.replace_one(
            {"_id": "main"},
            {"_id": "main", "data": _clone(data), "updated_at": time.time()},
            upsert=True,
        )

    async def load_leaderboard(self) -> dict | None:
        if self._leaderboard is None:
            await self.connect()
        doc = await self._leaderboard.find_one({"_id": "main"})
        if not doc or not isinstance(doc.get("data"), dict):
            return None
        return _clone(doc["data"])

    async def list_daily_completions(self) -> list[dict]:
        if self._daily is None:
            await self.connect()
        cursor = self._daily.find({})
        return await cursor.to_list(length=5000)


def create_match_store() -> MatchStore:
    uri = os.getenv("MONGODB_URI", "").strip()
    if uri:
        db = os.getenv("MONGODB_DB", "sudoku").strip() or "sudoku"
        return MongoMatchStore(uri, db_name=db)
    return MemoryMatchStore()


def _player_blob(user_id: int, template: list, *, name: str | None = None) -> dict:
    return {
        "user_id": user_id,
        "name": name or "Unknown",
        "current_board": _clone(template),
        "thread_id": None,
        "finished_time": None,
        "forfeit": False,
        "elapsed": None,
    }


def match_player_entries(match: dict) -> list[tuple[str, dict]]:
    """Return (slot, player_dict) for every competitor in the match."""
    slots = match.get("player_slots")
    if isinstance(slots, list) and slots:
        return [(s, match[s]) for s in slots if isinstance(match.get(s), dict)]
    out: list[tuple[str, dict]] = []
    for s in ("player_1", "player_2"):
        if isinstance(match.get(s), dict):
            out.append((s, match[s]))
    return out


def new_match_document(
    *,
    guild_id: int,
    channel_id: int,
    player_ids: list[int],
    board: list,
    given: list,
    solution: list,
    difficulty: str,
    player_names: list[str] | None = None,
) -> dict:
    if len(player_ids) < 2:
        raise ValueError("Challenge needs at least 2 players")
    start_time = time.time()
    template = _clone(board)
    slots = [f"player_{i}" for i in range(1, len(player_ids) + 1)]
    names = list(player_names or [])
    while len(names) < len(player_ids):
        names.append("Unknown")
    doc: dict = {
        "_id": str(uuid.uuid4()),
        "guild_id": guild_id,
        "channel_id": channel_id,
        "status": "active",
        "difficulty": difficulty,
        "solution": _clone(solution),
        "given": _clone(given),
        "board_template": template,
        "start_time": start_time,
        "player_slots": slots,
        "player_ids": list(player_ids),
        "player_names": names[: len(player_ids)],
        "winner_id": None,
        "winner_name": None,
    }
    for slot, uid, pname in zip(slots, player_ids, names):
        doc[slot] = _player_blob(uid, template, name=pname)
    return doc
