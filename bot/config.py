"""Environment-based configuration for the bot."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    discord_token: str
    tts_default_voice: str
    tts_max_chars: int
    tts_debug_dir: str | None
    settings_path: str
    kokoro_model_dir: str
    piper_model_dir: str


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    return Config(
        discord_token=_require("DISCORD_BOT_TOKEN"),
        tts_default_voice=os.environ.get("TTS_DEFAULT_VOICE", "kokoro_en-us_female_heart"),
        tts_max_chars=int(os.environ.get("TTS_MAX_CHARS", "500")),
        tts_debug_dir=os.environ.get("TTS_DEBUG_DIR") or None,
        settings_path=os.environ.get("SETTINGS_PATH", "data/guild_settings.json"),
        kokoro_model_dir=os.environ.get("KOKORO_MODEL_DIR", "models/kokoro"),
        piper_model_dir=os.environ.get("PIPER_MODEL_DIR", "models/piper"),
    )
