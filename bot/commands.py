"""Admin slash commands for configuring Overvoice per server."""

from __future__ import annotations

import asyncio
import io
import re

import discord
from discord import app_commands

from .follower import VoiceFollower
from .settings import SettingsStore
from .tts import VOICES, TTSCatalog, resolve_voice

_VOICE_WORD_SEPARATORS = re.compile(r"[\s(),]+")


class OvervoiceGroup(app_commands.Group):
    """The `/overvoice` command group.

    `track` and `untrack` require the Moderate Members permission, except
    that anyone already followed may use `track` to change their own
    voice; `say` is open to everyone.
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
        already_tracked = user.id in self._settings.get(interaction.guild_id).tracked_users
        # Anyone already followed may change their own voice; following
        # someone new or changing someone else's voice is for moderators.
        changing_own_voice = already_tracked and user.id == interaction.user.id
        if not changing_own_voice and not await _require_moderator(
            interaction,
            "You need the **Moderate Members** permission to follow someone or change "
            "someone else's voice. Once you're followed, you can change your own voice.",
        ):
            return
        if voice is not None:
            voice = resolve_voice(voice)
            if voice is None:
                await _reject_unknown_voice(interaction)
                return

        self._settings.track_user(interaction.guild_id, user.id, voice)
        await self._follower.resync_guild(interaction.guild)
        picked_voice = VOICES[self._settings.get(interaction.guild_id).tracked_users[user.id]]
        if already_tracked:
            message = f"{user.mention} is followed with voice **{picked_voice.display_name}**."
        else:
            message = f"Now following {user.mention} with voice **{picked_voice.display_name}**."
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
        if voice is not None:
            voice = resolve_voice(voice)
            if voice is None:
                await _reject_unknown_voice(interaction)
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
    # Every typed word must start a word of the voice's display name, so
    # "fab", "piper" or "pt female" all narrow the list (and "male" doesn't
    # match "female"). Discord shows at most 25 choices.
    typed = current.lower().split()
    matches = []
    for voice in VOICES.values():
        words = _VOICE_WORD_SEPARATORS.split(voice.display_name.lower())
        if all(any(word.startswith(t) for word in words) for t in typed):
            matches.append(voice)
    return [app_commands.Choice(name=v.display_name, value=v.slug) for v in matches[:25]]


async def _reject_unknown_voice(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "Unknown voice. Pick one from the autocomplete list.", ephemeral=True
    )


async def _require_moderator(
    interaction: discord.Interaction,
    denial: str = "You need the **Moderate Members** permission to use this command.",
) -> bool:
    if interaction.user.guild_permissions.moderate_members:
        return True
    await interaction.response.send_message(denial, ephemeral=True)
    return False
