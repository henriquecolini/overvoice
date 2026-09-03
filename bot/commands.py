"""Admin slash commands for configuring Overvoice per server."""

from __future__ import annotations

import asyncio
import io

import discord
from discord import app_commands

from .follower import VoiceFollower
from .settings import SettingsStore
from .tts import VOICES, TTSCatalog, language_for_voice

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
    """The `/overvoice` command group, restricted to server admins."""

    def __init__(
        self, settings: SettingsStore, tts: TTSCatalog, follower: VoiceFollower, max_chars: int
    ) -> None:
        super().__init__(
            name="overvoice",
            description="Configure Overvoice for this server",
            guild_only=True,
            default_permissions=discord.Permissions(administrator=True),
        )
        self._settings = settings
        self._tts = tts
        self._follower = follower
        self._max_chars = max_chars

    @app_commands.command(description="Follow this user into voice channels and read their messages aloud")
    async def track(self, interaction: discord.Interaction, user: discord.Member) -> None:
        self._settings.set_tracked_user(interaction.guild_id, user.id)
        await self._follower.resync_guild(interaction.guild)
        await interaction.response.send_message(f"Now following {user.mention}.", ephemeral=True)

    @app_commands.command(description="Stop following anyone and leave voice")
    async def untrack(self, interaction: discord.Interaction) -> None:
        self._settings.set_tracked_user(interaction.guild_id, None)
        await self._follower.resync_guild(interaction.guild)
        await interaction.response.send_message("Stopped following anyone.", ephemeral=True)

    @app_commands.command(description="Set the TTS voice used for this server")
    async def voice(self, interaction: discord.Interaction, voice: str) -> None:
        if voice not in VOICES:
            await interaction.response.send_message(
                f"Unknown voice {voice!r}. Use the autocomplete list.", ephemeral=True
            )
            return
        self._settings.set_voice(interaction.guild_id, voice)
        await interaction.response.send_message(
            f"Voice set to **{voice}** ({language_for_voice(voice)}).", ephemeral=True
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
            content=f"**{voice}** ({language}): {sample_text}", file=clip
        )

    @preview.autocomplete("voice")
    async def _preview_voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return _matching_voice_choices(current)

    @app_commands.command(description="Generate a spoken clip using this server's configured voice")
    async def say(self, interaction: discord.Interaction, text: str) -> None:
        text = text[: self._max_chars]
        settings = self._settings.get(interaction.guild_id)

        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        wav_bytes = await loop.run_in_executor(None, self._tts.synthesize, settings.voice, text)
        clip = discord.File(io.BytesIO(wav_bytes), filename="audio.wav")
        await interaction.followup.send(content=text, file=clip)

    @app_commands.command(description="Show this server's current Overvoice settings")
    async def status(self, interaction: discord.Interaction) -> None:
        settings = self._settings.get(interaction.guild_id)
        tracked = f"<@{settings.tracked_user_id}>" if settings.tracked_user_id else "nobody"
        await interaction.response.send_message(
            f"Tracking: {tracked}\nVoice: **{settings.voice}** ({language_for_voice(settings.voice)})",
            ephemeral=True,
        )


def _matching_voice_choices(current: str) -> list[app_commands.Choice[str]]:
    matches = [v for v in VOICES if current.lower() in v.lower()]
    return [app_commands.Choice(name=v, value=v) for v in matches[:25]]
