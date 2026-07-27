"""Shared spectator presence helpers for Activity + Discord bot."""

from __future__ import annotations

import time

SPECTATOR_PRESENCE_TTL_SEC = 15


def prune_watchers(watchers: dict | None) -> dict:
    now = time.time()
    cleaned: dict = {}
    for viewer_id, meta in (watchers or {}).items():
        if not isinstance(meta, dict):
            continue
        if now - float(meta.get("last_seen") or 0) > SPECTATOR_PRESENCE_TTL_SEC:
            continue
        cleaned[str(viewer_id)] = {
            "name": str(meta.get("name") or "Player"),
            "last_seen": float(meta.get("last_seen") or now),
        }
    return cleaned
