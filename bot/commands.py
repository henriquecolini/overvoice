"""Admin slash commands for configuring Overvoice per server."""

from __future__ import annotations

import asyncio
import io

import discord
from discord import app_commands

from .follower import VoiceFollower
from .settings import SettingsStore
from .tts import VOICES, TTSCatalog, engine_for_voice, language_for_voice

_PREVIEW_TEXT = {
    "en-us": "Hello! This is a preview of this voice.",
    "en-gb": "Hello! This is a preview of this voice.",
    "ja": "こんにちは、これはこの声のプレビューです。",
    "cmn": "你好，这是这个声音的预览。",
    "es": "¡Hola! Esta es una vista previa de esta voz.",
    "fr-fr": "Bonjour ! Ceci est un aperçu de cette voix.",
    "hi": "नमस्ते! यह इस आवाज़ का एक पूर्वावलोकन है।",
    "it": "Ciao! Questa è un'anteprima di questa voce.",
    "pt-br": "Oi! Este é um teste desta voz.",
}


class OvervoiceGroup(app_commands.Group):
    """The `/overvoice` command group.

    Configuration subcommands (track/untrack/voice/preview/status) require
    the Moderate Members permission; `say` is open to everyone.
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

    @app_commands.command(description="Follow this user into voice channels and read their messages aloud")
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

        self._settings.track_user(interaction.guild_id, user.id, voice)
        await self._follower.resync_guild(interaction.guild)
        picked_voice = self._settings.get(interaction.guild_id).tracked_users[user.id]
        await interaction.response.send_message(
            f"Now following {user.mention} with voice **{picked_voice}**.", ephemeral=True
        )

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

    @app_commands.command(description="Change the TTS voice for a user this server already follows")
    async def voice(self, interaction: discord.Interaction, user: discord.Member, voice: str) -> None:
        if not await _require_moderator(interaction):
            return
        if user.id not in self._settings.get(interaction.guild_id).tracked_users:
            await interaction.response.send_message(
                f"{user.mention} isn't being followed. Use `/overvoice track` first.", ephemeral=True
            )
            return
        if voice not in VOICES:
            await interaction.response.send_message(
                f"Unknown voice {voice!r}. Use the autocomplete list.", ephemeral=True
            )
            return
        self._settings.track_user(interaction.guild_id, user.id, voice)
        await interaction.response.send_message(
            f"{user.mention}'s voice set to **{voice}** ({_describe(voice)}).", ephemeral=True
        )

    @voice.autocomplete("voice")
    async def _voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return _matching_voice_choices(current)

    @app_commands.command(description="Post a sample clip so you can hear a voice before picking it")
    async def preview(
        self, interaction: discord.Interaction, voice: str, text: str | None = None
    ) -> None:
        if not await _require_moderator(interaction):
            return
        if voice not in VOICES:
            await interaction.response.send_message(
                f"Unknown voice {voice!r}. Use the autocomplete list.", ephemeral=True
            )
            return

        await interaction.response.defer()
        language = language_for_voice(voice)
        sample_text = text or _PREVIEW_TEXT.get(language, _PREVIEW_TEXT["en-us"])
        loop = asyncio.get_running_loop()
        wav_bytes = await loop.run_in_executor(None, self._tts.synthesize, voice, sample_text)
        clip = discord.File(io.BytesIO(wav_bytes), filename=f"{voice}.wav")
        await interaction.followup.send(
            content=f"**{voice}** ({_describe(voice)}): {sample_text}", file=clip
        )

    @preview.autocomplete("voice")
    async def _preview_voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return _matching_voice_choices(current)

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

    @app_commands.command(description="Show who this server is currently following")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await _require_moderator(interaction):
            return
        tracked = self._settings.get(interaction.guild_id).tracked_users
        if not tracked:
            await interaction.response.send_message("Following nobody right now.", ephemeral=True)
            return
        lines = [f"<@{user_id}>: **{voice}** ({_describe(voice)})" for user_id, voice in tracked.items()]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


def _describe(voice: str) -> str:
    return f"{language_for_voice(voice)}, {engine_for_voice(voice)}"


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
