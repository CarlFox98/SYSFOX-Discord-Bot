"""
SYSFOX :: persistent state

Remembers which items have already been announced so a restart never re-spams the
server, and remembers the current live-stream message so it can be edited when the
stream ends.

State is a small JSON file. On bot-hosting.net the starter tier keeps disk between
restarts; a *reinstall* (git re-clone) wipes it, in which case the notification
loops "prime" on first run (mark whatever is currently live/published as already
seen) so users don't get a burst of stale pings.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any


class State:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "seen_clips": [],       # list of clip ids
            "seen_videos": [],      # list of youtube video ids (long-form + shorts)
            "live_message_id": None,  # discord message id of the current go-live post
            "live_stream_id": None,   # twitch stream id currently announced
            "primed": {},           # {"clips": bool, "youtube": bool} first-run guards
            "live_meta": {},        # current stream: started_at, peak viewers
        }
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data.update(loaded)
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable state is non-fatal; we start fresh and re-prime.
            pass

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self._path)  # atomic on POSIX

    # --- seen-item helpers (bounded so the file can't grow forever) ---
    def _remember(self, bucket: str, item_id: str, cap: int = 200) -> None:
        with self._lock:
            items: list[str] = self._data.setdefault(bucket, [])
            if item_id in items:
                return
            items.append(item_id)
            if len(items) > cap:
                del items[: len(items) - cap]
            self._flush()

    def has_seen(self, bucket: str, item_id: str) -> bool:
        with self._lock:
            return item_id in self._data.get(bucket, [])

    def remember_clip(self, clip_id: str) -> None:
        self._remember("seen_clips", clip_id)

    def remember_video(self, video_id: str) -> None:
        self._remember("seen_videos", video_id)

    # --- priming (first-run seed so we don't announce backlog) ---
    def is_primed(self, key: str) -> bool:
        with self._lock:
            return bool(self._data.get("primed", {}).get(key, False))

    def set_primed(self, key: str) -> None:
        with self._lock:
            self._data.setdefault("primed", {})[key] = True
            self._flush()

    def seed_seen(self, bucket: str, ids: list[str]) -> None:
        with self._lock:
            existing: list[str] = self._data.setdefault(bucket, [])
            for i in ids:
                if i not in existing:
                    existing.append(i)
            self._flush()

    # --- live-stream message tracking ---
    @property
    def live_message_id(self) -> int | None:
        with self._lock:
            return self._data.get("live_message_id")

    @property
    def live_stream_id(self) -> str | None:
        with self._lock:
            return self._data.get("live_stream_id")

    def set_live(self, message_id: int | None, stream_id: str | None) -> None:
        with self._lock:
            self._data["live_message_id"] = message_id
            self._data["live_stream_id"] = stream_id
            self._flush()

    # --- extra details about the current stream (survive a restart mid-stream) ---
    @property
    def live_meta(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get("live_meta") or {})

    def set_live_meta(self, meta: dict[str, Any]) -> None:
        with self._lock:
            self._data["live_meta"] = dict(meta)
            self._flush()

    # --- read-only summary for /state ---
    def stats(self) -> dict[str, Any]:
        with self._lock:
            try:
                size = os.path.getsize(self._path)
            except OSError:
                size = 0
            return {
                "seen_clips": len(self._data.get("seen_clips", [])),
                "seen_videos": len(self._data.get("seen_videos", [])),
                "primed": dict(self._data.get("primed", {})),
                "live_stream_id": self._data.get("live_stream_id"),
                "file_bytes": size,
            }
