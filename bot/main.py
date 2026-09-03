"""Overvoice entry point: wires up the Discord client and event handlers."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .commands import OvervoiceGroup
from .config import load_config
from .follower import VoiceFollower
from .settings import SettingsStore
from .tts import TTSCatalog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    config = load_config()

    settings = SettingsStore(config.settings_path, config.tts_default_voice)

    logger.info("Loading Kokoro model...")
    tts = TTSCatalog(config.kokoro_model_dir)
    follower = VoiceFollower(settings, tts, config.tts_max_chars, config.tts_debug_dir)

    intents = discord.Intents.default()
    intents.voice_states = True
    intents.members = True
    intents.message_content = True

    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
    bot.tree.add_command(OvervoiceGroup(settings, tts, follower, config.tts_max_chars))

    @bot.event
    async def setup_hook() -> None:
        await bot.tree.sync()

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s", bot.user)
        await follower.on_ready(bot)

    @bot.event
    async def on_guild_join(guild: discord.Guild) -> None:
        logger.info("Joined guild: %s", guild.name)
        await follower.on_guild_join(guild)

    @bot.event
    async def on_voice_state_update(
        member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        await follower.on_voice_state_update(member, before, after)

    @bot.event
    async def on_message(message: discord.Message) -> None:
        await follower.on_message(message)

    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
