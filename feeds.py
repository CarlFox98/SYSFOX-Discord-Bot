"""
SYSFOX :: feed sources

Pure-ish data fetching, kept separate from Discord concerns:

  * TwitchClient - app-access-token auth, live-stream check, recent clips.
  * YouTubeClient - RSS feed (no API key) + Shorts detection via URL probe.

Every method raises on failure; the callers translate those into health-tracker
signals. YouTube's RSS endpoint and the /shorts/ probe both 404 intermittently
from datacenter IPs, which is why the YouTube loop tolerates more consecutive
failures before flagging degraded (see config thresholds).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import aiohttp
import feedparser

HELIX = "https://api.twitch.tv/helix"
OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"


@dataclass
class Stream:
    id: str
    title: str
    game_name: str
    started_at: str
    thumbnail_url: str
    login: str

    @property
    def url(self) -> str:
        return f"https://twitch.tv/{self.login}"


@dataclass
class Clip:
    id: str
    title: str
    url: str
    thumbnail_url: str
    creator_name: str
    created_at: str


@dataclass
class Video:
    id: str
    title: str
    url: str
    published: str
    thumbnail_url: str
    is_short: bool = False


class TwitchClient:
    def __init__(self, client_id: str, client_secret: str, login: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._login = login
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._broadcaster_id: str | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Client-ID": self._client_id,
            "Authorization": f"Bearer {self._token}",
        }

    async def _ensure_token(self, session: aiohttp.ClientSession) -> None:
        # Refresh a minute before expiry to avoid edge-of-expiry 401s.
        if self._token and time.time() < self._token_expiry - 60:
            return
        params = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials",
        }
        async with session.post(OAUTH_TOKEN_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600))

    async def _ensure_broadcaster_id(self, session: aiohttp.ClientSession) -> str:
        if self._broadcaster_id:
            return self._broadcaster_id
        await self._ensure_token(session)
        async with session.get(
            f"{HELIX}/users",
            headers=self._headers(),
            params={"login": self._login},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        entries = data.get("data", [])
        if not entries:
            raise RuntimeError(f"Twitch user not found: {self._login}")
        self._broadcaster_id = entries[0]["id"]
        return self._broadcaster_id

    async def get_live_stream(self, session: aiohttp.ClientSession) -> Stream | None:
        """Return the current live Stream, or None if the channel is offline."""
        await self._ensure_token(session)
        async with session.get(
            f"{HELIX}/streams",
            headers=self._headers(),
            params={"user_login": self._login},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        entries = data.get("data", [])
        if not entries:
            return None
        s = entries[0]
        if s.get("type") != "live":
            return None
        thumb = (s.get("thumbnail_url") or "").replace("{width}", "1280").replace(
            "{height}", "720"
        )
        return Stream(
            id=s["id"],
            title=s.get("title", "Live now"),
            game_name=s.get("game_name", ""),
            started_at=s.get("started_at", ""),
            thumbnail_url=thumb,
            login=self._login,
        )

    async def get_recent_clips(
        self, session: aiohttp.ClientSession, *, first: int = 10
    ) -> list[Clip]:
        broadcaster_id = await self._ensure_broadcaster_id(session)
        await self._ensure_token(session)
        # Clips created in the last 24h, newest first.
        started = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 24 * 3600)
        )
        async with session.get(
            f"{HELIX}/clips",
            headers=self._headers(),
            params={
                "broadcaster_id": broadcaster_id,
                "first": str(first),
                "started_at": started,
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        clips: list[Clip] = []
        for c in data.get("data", []):
            clips.append(
                Clip(
                    id=c["id"],
                    title=c.get("title", "Untitled clip"),
                    url=c.get("url", ""),
                    thumbnail_url=c.get("thumbnail_url", ""),
                    creator_name=c.get("creator_name", "someone"),
                    created_at=c.get("created_at", ""),
                )
            )
        return clips


class YouTubeClient:
    def __init__(self, feed_url: str) -> None:
        self._feed_url = feed_url

    async def get_recent_videos(
        self, session: aiohttp.ClientSession, *, limit: int = 10
    ) -> list[Video]:
        """Fetch the channel RSS feed and classify each entry as Short or video.

        Raises on network/parse failure so the caller can count it toward the
        (more forgiving) YouTube degradation threshold.
        """
        headers = {"User-Agent": "SYSFOX/1.0 (+https://github.com/CarlFox98)"}
        async with session.get(self._feed_url, headers=headers) as resp:
            resp.raise_for_status()
            raw = await resp.text()

        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
            raise RuntimeError("YouTube feed did not parse and had no entries")

        videos: list[Video] = []
        for entry in parsed.entries[:limit]:
            vid = getattr(entry, "yt_videoid", None) or entry.get("id", "").split(":")[-1]
            if not vid:
                continue
            is_short = await self._is_short(session, vid)
            videos.append(
                Video(
                    id=vid,
                    title=entry.get("title", "New upload"),
                    url=f"https://www.youtube.com/watch?v={vid}",
                    published=entry.get("published", ""),
                    thumbnail_url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    is_short=is_short,
                )
            )
        return videos

    async def _is_short(self, session: aiohttp.ClientSession, video_id: str) -> bool:
        """A video is a Short if its /shorts/ URL resolves without redirecting to
        /watch. This probe can 404/throttle from datacenter IPs; when uncertain we
        default to treating it as a normal video so a notification is never lost.
        """
        url = f"https://www.youtube.com/shorts/{video_id}"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; SYSFOX/1.0)"}
        try:
            async with session.get(
                url, headers=headers, allow_redirects=False
            ) as resp:
                # 200 -> genuine Short. 3xx -> redirected to /watch (long-form).
                return resp.status == 200
        except aiohttp.ClientError:
            return False
