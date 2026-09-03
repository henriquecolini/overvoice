"""Admin slash commands for configuring Overvoice per server."""

from __future__ import annotations

import asyncio
import io

import discord
from discord import app_commands

from .follower import VoiceFollower
from .settings import SettingsStore
from .tts import LANGUAGES, VOICES, TTSCatalog

_PREVIEW_TEXT = {
    "english": "Hello! This is a preview of this voice.",
    "french": "Bonjour ! Ceci est un aperçu de cette voix.",
    "german": "Hallo! Dies ist eine Vorschau dieser Stimme.",
    "italian": "Ciao! Questa è un'anteprima di questa voce.",
    "portuguese": "Olá! Este é um teste desta voz.",
    "spanish": "¡Hola! Esta es una vista previa de esta voz.",
}

_LANGUAGE_CHOICES = [app_commands.Choice(name=lang, value=lang) for lang in LANGUAGES]


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

    @app_commands.command(description="Set the TTS language used for this server")
    @app_commands.choices(language=_LANGUAGE_CHOICES)
    async def language(self, interaction: discord.Interaction, language: app_commands.Choice[str]) -> None:
        self._settings.set_language(interaction.guild_id, language.value)
        await interaction.response.send_message(f"Language set to **{language.value}**.", ephemeral=True)

    @app_commands.command(description="Set the TTS voice used for this server")
    async def voice(self, interaction: discord.Interaction, voice: str) -> None:
        if voice not in VOICES:
            await interaction.response.send_message(
                f"Unknown voice {voice!r}. Use the autocomplete list.", ephemeral=True
            )
            return
        self._settings.set_voice(interaction.guild_id, voice)
        await interaction.response.send_message(f"Voice set to **{voice}**.", ephemeral=True)

    @voice.autocomplete("voice")
    async def _voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return _matching_voice_choices(current)

    @app_commands.command(description="Post a sample clip so you can hear a voice before picking it")
    @app_commands.choices(language=_LANGUAGE_CHOICES)
    async def preview(
        self,
        interaction: discord.Interaction,
        language: app_commands.Choice[str],
        voice: str,
        text: str | None = None,
    ) -> None:
        if voice not in VOICES:
            await interaction.response.send_message(
                f"Unknown voice {voice!r}. Use the autocomplete list.", ephemeral=True
            )
            return

        await interaction.response.defer()
        sample_text = text or _PREVIEW_TEXT.get(language.value, _PREVIEW_TEXT["english"])
        loop = asyncio.get_running_loop()
        wav_bytes = await loop.run_in_executor(
            None, self._tts.synthesize, language.value, voice, sample_text
        )
        clip = discord.File(io.BytesIO(wav_bytes), filename=f"{voice}_{language.value}.wav")
        await interaction.followup.send(
            content=f"**{voice}** ({language.value}): {sample_text}", file=clip
        )

    @preview.autocomplete("voice")
    async def _preview_voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return _matching_voice_choices(current)

    @app_commands.command(description="Generate a spoken clip using this server's configured language and voice")
    async def say(self, interaction: discord.Interaction, text: str) -> None:
        text = text[: self._max_chars]
        settings = self._settings.get(interaction.guild_id)

        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        wav_bytes = await loop.run_in_executor(
            None, self._tts.synthesize, settings.language, settings.voice, text
        )
        clip = discord.File(io.BytesIO(wav_bytes), filename="audio.wav")
        await interaction.followup.send(content=text, file=clip)

    @app_commands.command(description="Show this server's current Overvoice settings")
    async def status(self, interaction: discord.Interaction) -> None:
        settings = self._settings.get(interaction.guild_id)
        tracked = f"<@{settings.tracked_user_id}>" if settings.tracked_user_id else "nobody"
        await interaction.response.send_message(
            f"Tracking: {tracked}\nLanguage: **{settings.language}**\nVoice: **{settings.voice}**",
            ephemeral=True,
        )


def _matching_voice_choices(current: str) -> list[app_commands.Choice[str]]:
    matches = [v for v in VOICES if current.lower() in v.lower()]
    return [app_commands.Choice(name=v, value=v) for v in matches[:25]]
