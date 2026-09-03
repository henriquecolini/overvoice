"""Follows each guild's configured tracked user into voice channels and
speaks their messages, sent in that channel's text chat, aloud."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
import wave
from pathlib import Path

import discord

from .settings import SettingsStore
from .text_cleanup import sanitize_message, split_sentences
from .tts import TTSCatalog

logger = logging.getLogger(__name__)


class _GuildSession:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.task: asyncio.Task | None = None


class VoiceFollower:
    def __init__(
        self,
        settings: SettingsStore,
        tts: TTSCatalog,
        max_chars: int,
        debug_dir: str | None = None,
    ) -> None:
        self._settings = settings
        self._tts = tts
        self._max_chars = max_chars
        self._debug_dir = Path(debug_dir) if debug_dir else None
        if self._debug_dir:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[int, _GuildSession] = {}

    async def on_ready(self, client: discord.Client) -> None:
        for guild in client.guilds:
            await self.resync_guild(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.resync_guild(guild)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        tracked_user_id = self._settings.get(member.guild.id).tracked_user_id
        if member.id != tracked_user_id:
            return
        await self.resync_guild(member.guild)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or not isinstance(message.channel, discord.VoiceChannel):
            return

        settings = self._settings.get(message.guild.id)
        if message.author.id != settings.tracked_user_id:
            return

        voice_client = message.guild.voice_client
        if voice_client is None or voice_client.channel.id != message.channel.id:
            return  # bot isn't in this voice channel right now; ignore its text chat

        text = sanitize_message(message, self._max_chars)
        if not text:
            return

        session = self._sessions.get(message.guild.id)
        if session:
            session.queue.put_nowait(text)

    async def resync_guild(self, guild: discord.Guild) -> None:
        """Makes the bot's voice connection for this guild match its
        configured tracked user's current voice state. Called on startup,
        when the bot joins a new guild, on voice state changes, and
        whenever an admin command changes a guild's settings."""
        tracked_user_id = self._settings.get(guild.id).tracked_user_id
        member = guild.get_member(tracked_user_id) if tracked_user_id else None
        target_channel = member.voice.channel if member and member.voice else None

        voice_client = guild.voice_client
        if target_channel is None:
            if voice_client is not None:
                await self._disconnect(guild)
        elif voice_client is None:
            await self._connect(target_channel)
        elif voice_client.channel.id != target_channel.id:
            await voice_client.move_to(target_channel)

    async def _connect(self, channel: discord.VoiceChannel) -> None:
        await channel.connect()
        session = _GuildSession()
        self._sessions[channel.guild.id] = session
        session.task = asyncio.create_task(self._playback_loop(channel.guild, session))

    async def _disconnect(self, guild: discord.Guild) -> None:
        session = self._sessions.pop(guild.id, None)
        if session and session.task:
            session.task.cancel()
        if guild.voice_client:
            await guild.voice_client.disconnect()

    async def _playback_loop(self, guild: discord.Guild, session: _GuildSession) -> None:
        try:
            while True:
                text = await session.queue.get()
                settings = self._settings.get(guild.id)
                chunks = split_sentences(text)

                async for chunk, wav_bytes in self._synthesize_pipelined(settings.voice, chunks):
                    if self._debug_dir:
                        self._save_debug_clip(chunk, wav_bytes)

                    voice_client = guild.voice_client
                    if voice_client is None:
                        break  # bot got disconnected mid-message; drop the rest

                    await self._play(voice_client, chunk, wav_bytes)
        except asyncio.CancelledError:
            pass

    async def _synthesize_pipelined(self, voice: str, chunks: list[str]):
        """Synthesizes each chunk while the previous one is being played,
        so a long message starts speaking after its first sentence instead
        of waiting for the whole thing to render."""
        loop = asyncio.get_running_loop()
        pending = loop.run_in_executor(None, self._tts.synthesize, voice, chunks[0])
        for i, chunk in enumerate(chunks):
            current = pending
            if i + 1 < len(chunks):
                pending = loop.run_in_executor(None, self._tts.synthesize, voice, chunks[i + 1])
            try:
                wav_bytes = await current
            except Exception:
                logger.exception("TTS synthesis failed for message chunk: %r", chunk)
                continue
            yield chunk, wav_bytes

    async def _play(self, voice_client: discord.VoiceClient, text: str, wav_bytes: bytes) -> None:
        loop = asyncio.get_running_loop()
        try:
            with wave.open(io.BytesIO(wav_bytes)) as wav_file:
                expected_duration = wav_file.getnframes() / wav_file.getframerate()

            done = asyncio.Event()
            start_time = time.monotonic()

            def _after_playback(error: Exception | None) -> None:
                elapsed = time.monotonic() - start_time
                if error:
                    logger.error("Playback error after %.2fs: %s", elapsed, error)
                elif elapsed < expected_duration - 0.5:
                    logger.warning(
                        "Playback for %r stopped early: expected %.2fs, played %.2fs",
                        text, expected_duration, elapsed,
                    )
                else:
                    logger.info("Played %r (%.2fs)", text, elapsed)
                loop.call_soon_threadsafe(done.set)

            voice_client.play(
                discord.FFmpegPCMAudio(io.BytesIO(wav_bytes), pipe=True), after=_after_playback
            )
            await done.wait()
        except Exception:
            # A single chunk must never permanently kill this session's
            # playback loop -- log it and keep going with the next one.
            logger.exception("Playback failed for message chunk: %r", text)

    def _save_debug_clip(self, text: str, wav_bytes: bytes) -> None:
        try:
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")[:40] or "clip"
            path = self._debug_dir / f"{int(time.time() * 1000)}_{slug}.wav"
            path.write_bytes(wav_bytes)
            logger.info("Saved debug clip: %s", path)
        except OSError:
            logger.exception("Could not save debug clip for message: %r", text)
