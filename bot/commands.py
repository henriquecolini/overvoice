"""Admin slash commands for configuring Overvoice per server."""

from __future__ import annotations

import asyncio
import io

import discord
from discord import app_commands

from .follower import VoiceFollower
from .settings import SettingsStore
from .tts import VOICES, TTSCatalog


class OvervoiceGroup(app_commands.Group):
    """The `/overvoice` command group.

    `track` and `untrack` require the Moderate Members permission; `say`
    is open to everyone.
    """

    def __init__(
        self, settings: SettingsStore, tts: TTSCatalog, follower: VoiceFollower, max_chars: int
    ) -> None:
        super().__init__(
            name="overvoice",
            description="Configure Overvoice for this server",
            guild_only=True,
        )
        self._settings = settings
        self._tts = tts
        self._follower = follower
        self._max_chars = max_chars

    @app_commands.command(description="Follow a user and read their messages aloud, or change the voice of one already followed")
    async def track(
        self, interaction: discord.Interaction, user: discord.Member, voice: str | None = None
    ) -> None:
        if not await _require_moderator(interaction):
            return
        if voice is not None and voice not in VOICES:
            await interaction.response.send_message(
                f"Unknown voice {voice!r}. Use the autocomplete list.", ephemeral=True
            )
            return

        already_tracked = user.id in self._settings.get(interaction.guild_id).tracked_users
        self._settings.track_user(interaction.guild_id, user.id, voice)
        await self._follower.resync_guild(interaction.guild)
        picked_voice = self._settings.get(interaction.guild_id).tracked_users[user.id]
        if already_tracked:
            message = f"{user.mention} is followed with voice **{picked_voice}**."
        else:
            message = f"Now following {user.mention} with voice **{picked_voice}**."
        await interaction.response.send_message(message, ephemeral=True)

    @track.autocomplete("voice")
    async def _track_voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return _matching_voice_choices(current)

    @app_commands.command(description="Stop following a user (or everyone, if none is given)")
    async def untrack(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        if not await _require_moderator(interaction):
            return
        if user is None:
            self._settings.untrack_all(interaction.guild_id)
            message = "Stopped following everyone."
        else:
            self._settings.untrack_user(interaction.guild_id, user.id)
            message = f"Stopped following {user.mention}."
        await self._follower.resync_guild(interaction.guild)
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(description="Generate a spoken clip, using your own voice if you're followed")
    async def say(
        self, interaction: discord.Interaction, text: str, voice: str | None = None
    ) -> None:
        if voice is not None and voice not in VOICES:
            await interaction.response.send_message(
                f"Unknown voice {voice!r}. Use the autocomplete list.", ephemeral=True
            )
            return

        text = text[: self._max_chars]
        settings = self._settings.get(interaction.guild_id)
        chosen_voice = voice or settings.tracked_users.get(interaction.user.id, self._settings.default_voice)

        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        wav_bytes = await loop.run_in_executor(None, self._tts.synthesize, chosen_voice, text)
        clip = discord.File(io.BytesIO(wav_bytes), filename="audio.wav")
        await interaction.followup.send(content=text, file=clip)

    @say.autocomplete("voice")
    async def _say_voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return _matching_voice_choices(current)

def _matching_voice_choices(current: str) -> list[app_commands.Choice[str]]:
    matches = [v for v in VOICES if current.lower() in v.lower()]
    return [app_commands.Choice(name=v, value=v) for v in matches[:25]]


async def _require_moderator(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.moderate_members:
        return True
    await interaction.response.send_message(
        "You need the **Moderate Members** permission to use this command.", ephemeral=True
    )
    return False
