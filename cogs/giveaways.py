"""
SYSFOX :: automated giveaways

Neo picks the prize and pastes the code. SYSFOX does everything else:

  /giveaway start    post a giveaway right now (prize + secret code)
  /giveaway queue    save a prize for later; the next one auto-starts on the
                     configured day each month (default: the 1st, 7 PM ET)
  /giveaway list     what's running, waiting to be claimed, and queued
  /giveaway end      end the running giveaway early and draw now
  /giveaway reroll   drop the current winner and draw someone else
  /giveaway cancel   cancel the running giveaway (no winner)
  /giveaway unqueue  remove a queued prize

Flow:
  1. Post in the giveaways channel with a 🎉 Enter button, pinging the
     Giveaways role. Entering is one click; clicking again lets you leave.
  2. Entry rules are enforced at click time: no bots, not the host, and the
     member must have been in the server for MIN_DAYS (default 7).
  3. When the timer runs out, a winner is drawn with a cryptographically
     random choice. They're pinged in the channel and DM'd.
  4. The winner presses "Claim prize" (in the DM or in the channel) and gets
     the code privately. Nobody else can ever see it.
  5. No claim within CLAIM_HOURS (default 48)? SYSFOX rerolls on its own.
  6. Every step is logged to the mod-log channel. Codes are never logged.

Settings (environment variables, all optional, defaults fit Neo's Den):
  GIVEAWAY_CHANNEL_ID, GIVEAWAY_ROLE_ID, GIVEAWAY_LOG_CHANNEL_ID,
  GIVEAWAY_MIN_DAYS, GIVEAWAY_CLAIM_HOURS, GIVEAWAY_AUTOSTART_DAY,
  GIVEAWAY_AUTOSTART_HOUR, GIVEAWAY_AUTOSTART_DAYS (length of auto-started
  giveaways), GIVEAWAY_TZ, GIVEAWAY_PATH.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import branding

try:  # zoneinfo needs tz data; fall back to UTC math if it's missing
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

log = logging.getLogger("sysfox.giveaways")

GOLD = 0xFFD700


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not a number, using %s", name, raw, default)
        return default


class GiveawaySettings:
    def __init__(self) -> None:
        self.channel_id = _env_int("GIVEAWAY_CHANNEL_ID", 1557446661784084520)
        self.role_id = _env_int("GIVEAWAY_ROLE_ID", 1557446192923803698)
        self.log_channel_id = _env_int("GIVEAWAY_LOG_CHANNEL_ID", 1527054968576671957)
        self.min_days = _env_int("GIVEAWAY_MIN_DAYS", 7)
        self.claim_hours = _env_int("GIVEAWAY_CLAIM_HOURS", 48)
        self.autostart_day = _env_int("GIVEAWAY_AUTOSTART_DAY", 1)
        self.autostart_hour = _env_int("GIVEAWAY_AUTOSTART_HOUR", 19)
        self.autostart_days = _env_int("GIVEAWAY_AUTOSTART_DAYS", 7)
        self.tz_name = os.environ.get("GIVEAWAY_TZ", "America/New_York").strip()
        self.path = os.environ.get("GIVEAWAY_PATH", "data/giveaways.json").strip()

    def now_local(self) -> datetime:
        if ZoneInfo is not None:
            try:
                return datetime.now(ZoneInfo(self.tz_name))
            except Exception:
                pass
        return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
class GiveawayStore:
    """Small JSON store with atomic writes. Codes live only here."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {"giveaways": {}, "queue": [], "last_autostart": ""}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read %s, starting empty", self.path)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)

    @property
    def giveaways(self) -> dict[str, dict[str, Any]]:
        return self.data["giveaways"]

    @property
    def queue(self) -> list[dict[str, Any]]:
        return self.data["queue"]

    def get(self, gid: str) -> dict[str, Any] | None:
        return self.giveaways.get(gid)

    def running(self) -> dict[str, Any] | None:
        for g in self.giveaways.values():
            if g["status"] == "running":
                return g
        return None

    def awaiting_claim(self) -> list[dict[str, Any]]:
        return [g for g in self.giveaways.values() if g["status"] == "awaiting_claim"]

    def prune(self, keep: int = 25) -> None:
        """Keep the file small: drop old finished giveaways and their codes."""
        finished = [
            g for g in self.giveaways.values()
            if g["status"] in ("claimed", "no_winner", "cancelled")
        ]
        finished.sort(key=lambda g: g.get("ends_at", 0))
        for g in finished[:-keep] if len(finished) > keep else []:
            self.giveaways.pop(g["id"], None)
        # A claimed code stays a week so the winner can look it up again.
        cutoff = time.time() - 7 * 86400
        for g in finished:
            if g["status"] != "claimed" or g.get("claimed_at", 0) < cutoff:
                g["code"] = None


# --------------------------------------------------------------------------
# pure logic (kept free of Discord so it can be tested)
# --------------------------------------------------------------------------
def eligible_entrants(g: dict[str, Any]) -> list[int]:
    banned = set(g.get("past_winners", [])) | {g.get("host_id")}
    return [uid for uid in g.get("entrants", []) if uid not in banned]


def draw(g: dict[str, Any], rng: secrets.SystemRandom | None = None) -> int | None:
    pool = eligible_entrants(g)
    if not pool:
        return None
    return (rng or secrets.SystemRandom()).choice(pool)


def joined_long_enough(joined_at: datetime | None, min_days: int, now: float | None = None) -> bool:
    if min_days <= 0:
        return True
    if joined_at is None:
        return False
    now = time.time() if now is None else now
    return (now - joined_at.timestamp()) >= min_days * 86400


# --------------------------------------------------------------------------
# buttons (DynamicItem = persistent across restarts, one per giveaway id)
# --------------------------------------------------------------------------
def _cog(interaction: discord.Interaction) -> "GiveawaysCog | None":
    cog = interaction.client.get_cog("GiveawaysCog")  # type: ignore[union-attr]
    return cog if isinstance(cog, GiveawaysCog) else None


class EnterButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sysfox:gw:enter:(?P<gid>[0-9a-f]{8})",
):
    def __init__(self, gid: str, *, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label="Enter",
                emoji="🎉",
                style=discord.ButtonStyle.success,
                custom_id=f"sysfox:gw:enter:{gid}",
                disabled=disabled,
            )
        )
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["gid"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Giveaways aren't loaded right now.", ephemeral=True)
            return
        await cog.handle_enter(interaction, self.gid)


class ClaimButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sysfox:gw:claim:(?P<gid>[0-9a-f]{8})",
):
    def __init__(self, gid: str, *, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label="Claim prize",
                emoji="🎁",
                style=discord.ButtonStyle.primary,
                custom_id=f"sysfox:gw:claim:{gid}",
                disabled=disabled,
            )
        )
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["gid"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Giveaways aren't loaded right now.", ephemeral=True)
            return
        await cog.handle_claim(interaction, self.gid)


def _view(*items: discord.ui.Item) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for it in items:
        v.add_item(it)
    return v


# --------------------------------------------------------------------------
# the cog
# --------------------------------------------------------------------------
class GiveawaysCog(commands.Cog):
    giveaway = app_commands.Group(
        name="giveaway",
        description="Run Neo's Den giveaways.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.s = GiveawaySettings()
        self.store = GiveawayStore(self.s.path)
        self.lock = asyncio.Lock()
        self._dirty: set[str] = set()  # giveaways whose entry count changed

    async def cog_load(self) -> None:
        self.tick.start()

    def cog_unload(self) -> None:
        self.tick.cancel()

    # --- small helpers ----------------------------------------------------
    def _channel(self, cid: int) -> discord.abc.Messageable | None:
        ch = self.bot.get_channel(cid)
        return ch if isinstance(ch, discord.abc.Messageable) else None

    async def _log(self, text: str) -> None:
        log.info("giveaway: %s", text)
        ch = self._channel(self.s.log_channel_id)
        if ch is None:
            return
        try:
            await ch.send(
                f"🎁 `Giveaway` {text}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Could not write to the giveaway log channel")

    def _embed(self, g: dict[str, Any]) -> discord.Embed:
        status = g["status"]
        lines = [f"**Prize:** {g['prize']}"]
        if g.get("fine_print"):
            lines.append(f"**Fine print:** {g['fine_print']}")
        lines.append("")
        if status == "running":
            lines.append("Press **🎉 Enter** below. Press it again to leave.")
            lines.append(f"**Ends:** <t:{g['ends_at']}:F> (<t:{g['ends_at']}:R>)")
        elif status == "awaiting_claim":
            lines.append(f"**Winner:** <@{g['winner_id']}>, waiting for them to claim.")
        elif status == "claimed":
            lines.append(f"**Winner:** <@{g['winner_id']}> ✅ claimed")
        elif status == "no_winner":
            lines.append("Ended with no eligible entries.")
        elif status == "cancelled":
            lines.append("This giveaway was cancelled.")
        lines.append(f"**Entries:** {len(g.get('entrants', []))}")
        lines.append("")
        lines.append(
            f"Free to enter, one entry each. You need to have been in the den for "
            f"{self.s.min_days}+ days. The winner gets the code privately. Staff will "
            f"never ask for your login or payment info."
        )
        color = GOLD if status == "running" else branding.SILVER
        title = "🎉 GIVEAWAY" if status == "running" else "🎉 GIVEAWAY (ended)"
        return branding.dialog(title, "\n".join(lines), color=color)

    async def _refresh_post(self, g: dict[str, Any]) -> None:
        ch = self._channel(g["channel_id"])
        if ch is None or not g.get("message_id"):
            return
        try:
            msg = await ch.fetch_message(g["message_id"])  # type: ignore[attr-defined]
            await msg.edit(
                embed=self._embed(g),
                view=_view(EnterButton(g["id"], disabled=g["status"] != "running")),
            )
        except discord.HTTPException:
            log.warning("Could not refresh giveaway post %s", g["id"])

    def _new_id(self) -> str:
        while True:
            gid = secrets.token_hex(4)
            if gid not in self.store.giveaways:
                return gid

    # --- starting ---------------------------------------------------------
    async def _start(self, *, prize: str, code: str, fine_print: str, days: int,
                     host_id: int, ping: bool = True) -> dict[str, Any] | str:
        ch = self._channel(self.s.channel_id)
        if ch is None:
            return "I can't find the giveaways channel (GIVEAWAY_CHANNEL_ID)."
        gid = self._new_id()
        g: dict[str, Any] = {
            "id": gid,
            "prize": prize,
            "fine_print": fine_print,
            "code": code,
            "host_id": host_id,
            "channel_id": self.s.channel_id,
            "message_id": None,
            "started_at": int(time.time()),
            "ends_at": int(time.time()) + days * 86400,
            "entrants": [],
            "past_winners": [],
            "winner_id": None,
            "winner_deadline": None,
            "announce_id": None,
            "status": "running",
        }
        content = f"<@&{self.s.role_id}> a new giveaway is up!" if ping and self.s.role_id else None
        msg = await ch.send(
            content=content,
            embed=self._embed(g),
            view=_view(EnterButton(gid)),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        g["message_id"] = msg.id
        self.store.giveaways[gid] = g
        self.store.save()
        await self._log(f"`{gid}` started: **{prize}**, ends <t:{g['ends_at']}:f>, host <@{host_id}>")
        return g

    # --- entering ---------------------------------------------------------
    async def handle_enter(self, interaction: discord.Interaction, gid: str) -> None:
        member = interaction.user
        async with self.lock:
            g = self.store.get(gid)
            if g is None or g["status"] != "running":
                await interaction.response.send_message("This giveaway is closed.", ephemeral=True)
                return
            if not isinstance(member, discord.Member) or member.bot:
                await interaction.response.send_message("Only server members can enter.", ephemeral=True)
                return
            if member.id == g["host_id"]:
                await interaction.response.send_message(
                    "You're running this one, so you can't enter it.", ephemeral=True
                )
                return
            if member.id in g["entrants"]:
                g["entrants"].remove(member.id)
                self.store.save()
                self._dirty.add(gid)
                await interaction.response.send_message(
                    "You've left the giveaway. Press 🎉 again to rejoin.", ephemeral=True
                )
                return
            if not joined_long_enough(member.joined_at, self.s.min_days):
                ready = int(member.joined_at.timestamp()) + self.s.min_days * 86400 if member.joined_at else None
                when = f" You can enter from <t:{ready}:f>." if ready else ""
                await interaction.response.send_message(
                    f"You need to have been in the den for {self.s.min_days} days to enter.{when}",
                    ephemeral=True,
                )
                return
            g["entrants"].append(member.id)
            self.store.save()
            self._dirty.add(gid)
        await interaction.response.send_message(
            f"You're in! 🎉 Winner is drawn <t:{g['ends_at']}:R>. If you win, I'll ping you "
            f"here and DM you a button to claim the prize.",
            ephemeral=True,
        )

    # --- drawing ----------------------------------------------------------
    async def _draw_and_announce(self, g: dict[str, Any], *, reason: str = "") -> None:
        guild = self.bot.get_guild(self.bot.config.guild_id) if hasattr(self.bot, "config") else None  # type: ignore[attr-defined]
        winner: int | None = None
        # Skip anyone who left the server since entering.
        while True:
            winner = draw(g)
            if winner is None:
                break
            if guild is None:
                break
            try:
                await guild.fetch_member(winner)
                break
            except discord.NotFound:
                g["past_winners"].append(winner)  # gone, never pick again
            except discord.HTTPException:
                break

        ch = self._channel(g["channel_id"])
        if winner is None:
            g["status"] = "no_winner"
            g["winner_id"] = None
            self.store.save()
            await self._refresh_post(g)
            if ch is not None:
                await ch.send(f"The **{g['prize']}** giveaway ended with no eligible entries.")
            await self._log(f"`{g['id']}` ended with no eligible winner{reason}")
            return

        g["status"] = "awaiting_claim"
        g["winner_id"] = winner
        g["winner_deadline"] = int(time.time()) + self.s.claim_hours * 3600
        self.store.save()
        await self._refresh_post(g)

        text = (
            f"🎉 Congrats <@{winner}>! You won **{g['prize']}**.\n"
            f"Press **Claim prize** before <t:{g['winner_deadline']}:f> "
            f"(<t:{g['winner_deadline']}:R>) and I'll send you the code privately. "
            f"If it isn't claimed by then, I'll draw someone else."
        )
        if ch is not None:
            try:
                ann = await ch.send(
                    text,
                    view=_view(ClaimButton(g["id"])),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
                g["announce_id"] = ann.id
                self.store.save()
            except discord.HTTPException:
                log.exception("Could not announce winner for %s", g["id"])
        dm_ok = await self._dm_winner(g, winner)
        await self._log(
            f"`{g['id']}` winner drawn{reason}: <@{winner}> from {len(eligible_entrants(g))} eligible. "
            f"DM {'sent' if dm_ok else 'failed (they can claim in the channel)'}."
        )

    async def _dm_winner(self, g: dict[str, Any], winner: int) -> bool:
        try:
            user = self.bot.get_user(winner) or await self.bot.fetch_user(winner)
            await user.send(
                embed=branding.dialog(
                    "🎉 You won a giveaway in Neo's Den!",
                    f"**Prize:** {g['prize']}\n"
                    + (f"**Fine print:** {g['fine_print']}\n" if g.get("fine_print") else "")
                    + f"\nPress **Claim prize** before <t:{g['winner_deadline']}:f> to get your code.\n"
                    "Nobody from staff will ever ask for your login or payment info.",
                    color=GOLD,
                ),
                view=_view(ClaimButton(g["id"])),
            )
            return True
        except discord.HTTPException:
            return False

    # --- claiming ---------------------------------------------------------
    async def handle_claim(self, interaction: discord.Interaction, gid: str) -> None:
        async with self.lock:
            g = self.store.get(gid)
            if g is None:
                await interaction.response.send_message("I can't find that giveaway.", ephemeral=True)
                return
            if interaction.user.id != g.get("winner_id"):
                await interaction.response.send_message(
                    "This prize belongs to the winner. Good luck next time! 🍀", ephemeral=True
                )
                return
            if g["status"] == "claimed" and g.get("code"):
                await interaction.response.send_message(
                    f"Here's your code again: ||`{g['code']}`||", ephemeral=True
                )
                return
            if g["status"] != "awaiting_claim" or not g.get("code"):
                await interaction.response.send_message(
                    "This prize isn't available anymore. Ask an Admin if you think that's wrong.",
                    ephemeral=True,
                )
                return
            g["status"] = "claimed"
            g["claimed_at"] = int(time.time())
            self.store.save()
            code = g["code"]
        await interaction.response.send_message(
            f"🎁 **{g['prize']}**\nYour code: ||`{code}`||\n"
            "Only you can see this message. Copy it somewhere safe. Press Claim again "
            "within a week if you need it again.",
            ephemeral=True,
        )
        await self._refresh_post(g)
        await self._close_announcement(g, f"🎉 <@{g['winner_id']}> claimed **{g['prize']}**. Congrats!")
        await self._log(f"`{g['id']}` claimed by <@{g['winner_id']}>")

    async def _close_announcement(self, g: dict[str, Any], text: str) -> None:
        ch = self._channel(g["channel_id"])
        if ch is None or not g.get("announce_id"):
            return
        try:
            msg = await ch.fetch_message(g["announce_id"])  # type: ignore[attr-defined]
            # Leave the button live so the winner can re-show their code for a week.
            keep = g["status"] == "claimed"
            await msg.edit(
                content=text,
                view=_view(ClaimButton(g["id"], disabled=not keep)),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

    # --- background tick --------------------------------------------------
    @tasks.loop(seconds=60)
    async def tick(self) -> None:
        try:
            await self._tick_once()
        except Exception:  # noqa: BLE001 - the loop must never die
            log.exception("Giveaway tick failed")

    async def _tick_once(self) -> None:
        now = time.time()
        async with self.lock:
            due = [g for g in self.store.giveaways.values() if g["status"] == "running" and now >= g["ends_at"]]
            expired = [g for g in self.store.awaiting_claim() if now >= (g.get("winner_deadline") or 0)]
            dirty = [self.store.get(gid) for gid in self._dirty]
            self._dirty.clear()

        for g in dirty:
            if g is not None and g["status"] == "running":
                await self._refresh_post(g)

        for g in due:
            async with self.lock:
                await self._draw_and_announce(g)

        for g in expired:
            async with self.lock:
                old = g["winner_id"]
                g["past_winners"].append(old)
                await self._close_announcement(
                    g, f"<@{old}> didn't claim **{g['prize']}** in time, so I'm drawing again."
                )
                await self._draw_and_announce(g, reason=f" (reroll, <@{old}> didn't claim)")

        await self._maybe_autostart()

        async with self.lock:
            self.store.prune()
            self.store.save()

    async def _maybe_autostart(self) -> None:
        if not self.store.queue or self.store.running() is not None:
            return
        local = self.s.now_local()
        month_key = local.strftime("%Y-%m")
        if self.store.data.get("last_autostart") == month_key:
            return
        if local.day < self.s.autostart_day or (
            local.day == self.s.autostart_day and local.hour < self.s.autostart_hour
        ):
            return
        async with self.lock:
            item = self.store.queue.pop(0)
            self.store.data["last_autostart"] = month_key
            self.store.save()
            result = await self._start(
                prize=item["prize"], code=item["code"], fine_print=item.get("fine_print", ""),
                days=item.get("days") or self.s.autostart_days, host_id=item["host_id"],
            )
        if isinstance(result, str):
            async with self.lock:
                self.store.queue.insert(0, item)
                self.store.data["last_autostart"] = ""
                self.store.save()
            await self._log(f"auto-start failed: {result}")
        else:
            await self._log(f"`{result['id']}` was auto-started from the queue")

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    # --- slash commands ---------------------------------------------------
    @giveaway.command(name="start", description="Start a giveaway right now.")
    @app_commands.describe(
        prize="What they win, e.g. 'YouTube Premium, 20% off for 6 months'",
        code="The code the winner gets. Only the winner ever sees it.",
        days="How long it runs (1 to 30 days, default 3)",
        fine_print="Optional: region limits, 'discount not free', etc.",
        ping="Ping the Giveaways role (default yes)",
    )
    async def start_cmd(
        self, interaction: discord.Interaction, prize: str, code: str,
        days: app_commands.Range[int, 1, 30] = 3, fine_print: str = "", ping: bool = True,
    ) -> None:
        async with self.lock:
            if self.store.running() is not None:
                await interaction.response.send_message(
                    "A giveaway is already running. End it first, or use `/giveaway queue`.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self._start(
                prize=prize.strip(), code=code.strip(), fine_print=fine_print.strip(),
                days=days, host_id=interaction.user.id, ping=ping,
            )
        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
        else:
            await interaction.followup.send(
                f"Started `{result['id']}` in <#{result['channel_id']}>. It ends "
                f"<t:{result['ends_at']}:R>. I'll draw, deliver the code and reroll on my own.",
                ephemeral=True,
            )

    @giveaway.command(name="queue", description="Save a prize to auto-start next month.")
    @app_commands.describe(
        prize="What they win",
        code="The code the winner gets. Only the winner ever sees it.",
        days="How long it runs once started (default from settings)",
        fine_print="Optional details",
    )
    async def queue_cmd(
        self, interaction: discord.Interaction, prize: str, code: str,
        days: Optional[app_commands.Range[int, 1, 30]] = None, fine_print: str = "",
    ) -> None:
        async with self.lock:
            self.store.queue.append({
                "prize": prize.strip(), "code": code.strip(), "fine_print": fine_print.strip(),
                "days": days, "host_id": interaction.user.id, "queued_at": int(time.time()),
            })
            self.store.save()
            pos = len(self.store.queue)
        await interaction.response.send_message(
            f"Queued **{prize}** at position {pos}. Queued prizes start one per month on day "
            f"{self.s.autostart_day} at {self.s.autostart_hour}:00 ({self.s.tz_name}), whenever "
            f"nothing else is running.",
            ephemeral=True,
        )
        await self._log(f"prize queued by <@{interaction.user.id}>: **{prize}** (position {pos})")

    @giveaway.command(name="list", description="Show running, unclaimed and queued giveaways.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        lines: list[str] = []
        for g in self.store.giveaways.values():
            if g["status"] == "running":
                lines.append(f"🟢 `{g['id']}` **{g['prize']}**: {len(g['entrants'])} entries, ends <t:{g['ends_at']}:R>")
            elif g["status"] == "awaiting_claim":
                lines.append(f"🟡 `{g['id']}` **{g['prize']}**: <@{g['winner_id']}> has until <t:{g['winner_deadline']}:R> to claim")
        if self.store.queue:
            lines.append("")
            lines.append("**Queue**")
            for i, q in enumerate(self.store.queue, 1):
                lines.append(f"`{i}.` {q['prize']}")
        if not lines:
            lines = ["Nothing running or queued."]
        await interaction.response.send_message(
            embed=branding.dialog("Giveaways", "\n".join(lines), color=GOLD), ephemeral=True
        )

    @giveaway.command(name="end", description="End the running giveaway now and draw a winner.")
    async def end_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            g = self.store.running()
            if g is None:
                await interaction.followup.send("Nothing is running.", ephemeral=True)
                return
            g["ends_at"] = int(time.time())
            await self._draw_and_announce(g, reason=f" (ended early by <@{interaction.user.id}>)")
        await interaction.followup.send("Ended and drawn.", ephemeral=True)

    @giveaway.command(name="reroll", description="Drop the current winner and draw someone else.")
    async def reroll_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            pending = self.store.awaiting_claim()
            if not pending:
                await interaction.followup.send("No unclaimed winner to reroll.", ephemeral=True)
                return
            g = pending[-1]
            old = g["winner_id"]
            g["past_winners"].append(old)
            await self._close_announcement(g, f"**{g['prize']}** was rerolled by staff.")
            await self._draw_and_announce(g, reason=f" (manual reroll by <@{interaction.user.id}>)")
        await interaction.followup.send("Rerolled.", ephemeral=True)

    @giveaway.command(name="cancel", description="Cancel the running giveaway with no winner.")
    async def cancel_cmd(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            g = self.store.running()
            if g is None:
                await interaction.response.send_message("Nothing is running.", ephemeral=True)
                return
            g["status"] = "cancelled"
            g["code"] = None
            self.store.save()
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        await self._refresh_post(g)
        await self._log(f"`{g['id']}` cancelled by <@{interaction.user.id}>")

    @giveaway.command(name="unqueue", description="Remove a queued prize by its number in /giveaway list.")
    async def unqueue_cmd(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 50]) -> None:
        async with self.lock:
            if number > len(self.store.queue):
                await interaction.response.send_message("There's no prize with that number.", ephemeral=True)
                return
            item = self.store.queue.pop(number - 1)
            self.store.save()
        await interaction.response.send_message(f"Removed **{item['prize']}** from the queue.", ephemeral=True)
        await self._log(f"queued prize removed by <@{interaction.user.id}>: **{item['prize']}**")


async def setup_giveaways(bot: commands.Bot) -> None:
    """Called from main.setup_hook: registers persistent buttons + the cog."""
    bot.add_dynamic_items(EnterButton, ClaimButton)
    await bot.add_cog(GiveawaysCog(bot))
