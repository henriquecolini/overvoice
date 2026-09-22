"""Follows each guild's configured tracked users into voice channels and
speaks their messages, sent in that channel's text chat, aloud."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import discord
import numpy as np

from .audio import StreamingPCMSource
from .settings import SettingsStore
from .text_cleanup import sanitize_message
from .tts import TTSCatalog, to_wav

logger = logging.getLogger(__name__)


class _GuildSession:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()  # (voice, text)
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
        if member.id not in self._settings.get(member.guild.id).tracked_users:
            return
        await self.resync_guild(member.guild)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or not isinstance(message.channel, discord.VoiceChannel):
            return

        settings = self._settings.get(message.guild.id)
        voice = settings.tracked_users.get(message.author.id)
        if voice is None:
            return

        voice_client = message.guild.voice_client
        if voice_client is None or voice_client.channel.id != message.channel.id:
            return  # bot isn't in this voice channel right now; ignore its text chat

        text = sanitize_message(message, self._max_chars)
        if not text:
            return

        session = self._sessions.get(message.guild.id)
        if session:
            session.queue.put_nowait((voice, text))

    async def resync_guild(self, guild: discord.Guild) -> None:
        """Makes the bot's voice connection for this guild match its
        tracked users' current voice states. Called on startup, when the
        bot joins a new guild, on voice state changes, and whenever an
        admin command changes a guild's settings.

        The bot can only be in one voice channel per guild at a time, so
        when tracked users are spread across channels it stays wherever it
        already is as long as a tracked user is still there, and otherwise
        joins whichever channel currently has the most of them."""
        tracked_user_ids = self._settings.get(guild.id).tracked_users
        members_present = [
            member
            for user_id in tracked_user_ids
            if (member := guild.get_member(user_id)) and member.voice and member.voice.channel
        ]

        voice_client = guild.voice_client
        if not members_present:
            if voice_client is not None:
                await self._disconnect(guild)
            return

        current_channel = voice_client.channel if voice_client else None
        if current_channel and any(m.voice.channel.id == current_channel.id for m in members_present):
            target_channel = current_channel
        else:
            target_channel = _busiest_channel(members_present)

        if voice_client is None:
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
        loop = asyncio.get_running_loop()
        try:
            while True:
                voice, text = await session.queue.get()
                voice_client = guild.voice_client
                if voice_client is None:
                    continue

                try:
                    source = StreamingPCMSource()
                    done = asyncio.Event()
                    start_time = time.monotonic()

                    def _after_playback(error: Exception | None) -> None:
                        elapsed = time.monotonic() - start_time
                        if error:
                            logger.error("Playback error after %.2fs: %s", elapsed, error)
                        else:
                            logger.info("Played %r (%.2fs of audio in %.2fs)", text, source.fed_seconds, elapsed)
                        loop.call_soon_threadsafe(done.set)

                    # Start playing right away: the source emits silence until
                    # the first sentence arrives, then each one as it renders.
                    voice_client.play(source, after=_after_playback)
                    await loop.run_in_executor(None, self._render_into, source, voice, text)
                    await done.wait()
                except Exception:
                    # A single message must never permanently kill this session's
                    # playback loop -- log it and keep going with the next one.
                    logger.exception("Playback failed for message: %r", text)
        except asyncio.CancelledError:
            pass

    def _render_into(self, source: StreamingPCMSource, voice: str, text: str) -> None:
        """Runs in a worker thread: synthesizes `text` sentence by sentence
        into `source`, always marking it finished so playback can end."""
        start_time = time.monotonic()
        chunks: list[np.ndarray] = []
        sample_rate = 0
        try:
            for samples, sample_rate in self._tts.stream(voice, text):
                if not chunks:
                    logger.info("First audio for %r after %.0fms", text, (time.monotonic() - start_time) * 1000)
                source.feed(samples, sample_rate)
                chunks.append(samples)
        except Exception:
            logger.exception("TTS synthesis failed for message: %r", text)
        finally:
            source.finish()

        if self._debug_dir and chunks:
            self._save_debug_clip(text, to_wav(np.concatenate(chunks), sample_rate))

    def _save_debug_clip(self, text: str, wav_bytes: bytes) -> None:
        try:
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")[:40] or "clip"
            path = self._debug_dir / f"{int(time.time() * 1000)}_{slug}.wav"
            path.write_bytes(wav_bytes)
            logger.info("Saved debug clip: %s", path)
        except OSError:
            logger.exception("Could not save debug clip for message: %r", text)


def _busiest_channel(members: list[discord.Member]) -> discord.VoiceChannel:
    counts: dict[int, int] = {}
    channels: dict[int, discord.VoiceChannel] = {}
    for member in members:
        channel = member.voice.channel
        counts[channel.id] = counts.get(channel.id, 0) + 1
        channels[channel.id] = channel
    busiest_id = max(counts, key=lambda cid: counts[cid])
    return channels[busiest_id]
