"""
SYSFOX :: health monitoring

Two jobs:
  1. HealthTracker  - records each notification loop's last success/failure and
     consecutive-failure count, and decides when a loop has "degraded".
  2. HealthServer   - a tiny aiohttp server exposing GET /health as JSON so an
     external monitor (UptimeRobot) can watch the bot and alert if it stops
     responding. This is the external safety net that catches a wiped or crashed
     deployment even when the bot itself can't shout for help.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from aiohttp import web

log = logging.getLogger("sysfox.health")


@dataclass
class LoopHealth:
    name: str
    fail_threshold: int
    consecutive_failures: int = 0
    total_failures: int = 0
    last_ok_ts: float | None = None
    last_error: str | None = None
    degraded: bool = False

    def record_success(self) -> bool:
        """Returns True if this success cleared a previously-degraded state."""
        recovered = self.degraded
        self.consecutive_failures = 0
        self.last_ok_ts = time.time()
        self.last_error = None
        self.degraded = False
        return recovered

    def record_failure(self, error: str) -> bool:
        """Returns True if this failure just crossed the degradation threshold."""
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = error
        newly_degraded = (
            not self.degraded
            and self.consecutive_failures >= self.fail_threshold
        )
        if self.consecutive_failures >= self.fail_threshold:
            self.degraded = True
        return newly_degraded

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "degraded": self.degraded,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "last_error": self.last_error,
            "seconds_since_ok": (
                round(time.time() - self.last_ok_ts) if self.last_ok_ts else None
            ),
        }


class HealthTracker:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.loops: dict[str, LoopHealth] = {}
        self.guild_count = 0

    def register(self, name: str, fail_threshold: int) -> LoopHealth:
        lh = LoopHealth(name=name, fail_threshold=fail_threshold)
        self.loops[name] = lh
        return lh

    @property
    def any_degraded(self) -> bool:
        return any(l.degraded for l in self.loops.values())

    def snapshot(self) -> dict:
        return {
            "status": "degraded" if self.any_degraded else "ok",
            "uptime_seconds": round(time.time() - self.started_at),
            "guilds": self.guild_count,
            "loops": {name: lh.as_dict() for name, lh in self.loops.items()},
        }


class HealthServer:
    def __init__(self, tracker: HealthTracker, port: int) -> None:
        self._tracker = tracker
        self._port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(middlewares=[_errors_middleware])
        app.router.add_get("/health", self._handle)
        app.router.add_get("/", self._root)

        # Internet scanners send junk requests all day. aiohttp rejects those
        # before they reach a handler and logs a full traceback for each one,
        # which made the console look like the bot was crashing. Keep that
        # logger quiet; real handler errors are logged by the middleware.
        logging.getLogger("aiohttp.server").setLevel(logging.CRITICAL)

        # access_log=None: don't print a line for every scanner hit.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="0.0.0.0", port=self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _root(self, _request: web.Request) -> web.Response:
        return web.Response(text="SYSFOX is running. Health: /health\n")

    async def _handle(self, _request: web.Request) -> web.Response:
        snap = self._tracker.snapshot()
        # Public endpoint: drop raw error text so scanners can't read internals.
        # (/status in Discord still shows everything.)
        for lh in snap.get("loops", {}).values():
            lh.pop("last_error", None)
        # Always 200 so UptimeRobot treats "reachable" as alive; the JSON body
        # still carries the degraded flag for anyone who inspects it.
        return web.json_response(snap)


@web.middleware
async def _errors_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise  # 404s etc. pass through untouched
    except Exception:  # noqa: BLE001
        log.exception("Health server handler failed")
        return web.Response(status=500, text="error")
