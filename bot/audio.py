"""A Discord audio source that plays TTS output while it's still rendering."""

from __future__ import annotations

import threading

import discord
import numpy as np
import soxr

_DISCORD_SAMPLE_RATE = 48000
_FRAME_BYTES = discord.opus.Encoder.FRAME_SIZE  # 20ms of 48kHz stereo s16le
_SILENT_FRAME = b"\x00" * _FRAME_BYTES


class StreamingPCMSource(discord.AudioSource):
    """Fed sentence by sentence from a synthesis thread via `feed()`, and
    read 20ms at a time by discord.py's player thread.

    One source covers a whole message, so sentences play back to back with
    no gap between separate `play()` calls. If the next sentence isn't
    ready yet, `read()` returns silence rather than blocking: blocking
    would make the player send the frames that follow in a burst to catch
    up, which Discord plays back as jitter.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._finished = False
        self.fed_seconds = 0.0

    def feed(self, samples: np.ndarray, sample_rate: int) -> None:
        """Queues float32 mono samples at any sample rate."""
        if sample_rate != _DISCORD_SAMPLE_RATE:
            samples = soxr.resample(samples, sample_rate, _DISCORD_SAMPLE_RATE)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        stereo = np.repeat(pcm, 2).tobytes()
        with self._lock:
            self._buffer += stereo
            self.fed_seconds += len(pcm) / _DISCORD_SAMPLE_RATE

    def finish(self) -> None:
        """Marks the end of input: once the buffer drains, playback ends."""
        with self._lock:
            self._finished = True

    def read(self) -> bytes:
        with self._lock:
            if len(self._buffer) >= _FRAME_BYTES:
                frame = bytes(self._buffer[:_FRAME_BYTES])
                del self._buffer[:_FRAME_BYTES]
                return frame
            if self._finished:
                if not self._buffer:
                    return b""
                frame = bytes(self._buffer).ljust(_FRAME_BYTES, b"\x00")
                self._buffer.clear()
                return frame
        return _SILENT_FRAME

    def is_opus(self) -> bool:
        return False
