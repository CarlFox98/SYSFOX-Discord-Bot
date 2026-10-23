"""
SYSFOX :: entry point

Boot order (in setup_hook, before the gateway connects):
  1. Open a shared aiohttp session.
  2. Start the HTTP health server (so UptimeRobot can see us immediately).
  3. Register the persistent role panel view (buttons survive restarts).
  4. Load the notification + panel + admin cogs.
  5. Sync slash commands to the configured guild for instant availability.
"""

from __future__ import annotations

import logging
import sys

import aiohttp
import discord
from discord.ext import commands

from config import Config, ConfigError
from health import HealthServer, HealthTracker
from state import State
from cogs.notifications import NotificationsCog
from cogs.panel import PanelCog, RolePanel
from cogs.admin import AdminCog
from cogs.giveaways import setup_giveaways

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sysfox")


class SysfoxBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()  # guilds is enough for our features
        super().__init__(command_prefix="!sysfox ", intents=intents, help_command=None)
        self.config = config
        self.state = State(config.state_path)
        self.tracker = HealthTracker()
        self.health_server = HealthServer(self.tracker, config.health_port)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        # Hard timeout so one hung request can't stall a notification loop.
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

        await self.health_server.start()
        log.info("Health endpoint listening on :%d/health", self.config.health_port)

        # Persistent panel view: must be added before we connect so button
        # clicks on an already-posted panel are handled after a restart.
        self.add_view(RolePanel(self.config))

        await self.add_cog(PanelCog(self, self.config))
        await self.add_cog(AdminCog(self, self.config, self.tracker))
        await self.add_cog(
            NotificationsCog(self, self.config, self.state, self.tracker, self.session)
        )

        await setup_giveaways(self)

        # Sync commands to the guild for instant (non-1-hour-delayed) availability.
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %d", self.config.guild_id)

    async def on_ready(self) -> None:
        self.tracker.guild_count = len(self.guilds)
        log.info("SYSFOX online as %s (%d guild(s))", self.user, len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="Neo's Den // C:\\>"
            )
        )

    async def close(self) -> None:
        await self.health_server.stop()
        if self.session is not None:
            await self.session.close()
        await super().close()


def main() -> int:
    try:
        config = Config.load()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        log.error("Set the missing variable in the bot-hosting.net Startup tab "
                  "(or your local .env) and restart.")
        return 1

    bot = SysfoxBot(config)
    try:
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the token. Check DISCORD_TOKEN.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
