"""Persistent, per-guild admin-configurable settings."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuildSettings:
    tracked_user_id: int | None
    language: str
    voice: str


class SettingsStore:
    """Tracks which user, language, and voice each guild has configured.

    Backed by a single JSON file so settings survive restarts without
    requiring a database. Guilds that haven't configured anything yet fall
    back to the store's defaults.
    """

    def __init__(self, path: str, default_language: str, default_voice: str) -> None:
        self._path = Path(path)
        self._default_language = default_language
        self._default_voice = default_voice
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = self._load()

    def get(self, guild_id: int) -> GuildSettings:
        entry = self._data.get(str(guild_id), {})
        return GuildSettings(
            tracked_user_id=entry.get("tracked_user_id"),
            language=entry.get("language", self._default_language),
            voice=entry.get("voice", self._default_voice),
        )

    def set_tracked_user(self, guild_id: int, user_id: int | None) -> None:
        self._update(guild_id, tracked_user_id=user_id)

    def set_language(self, guild_id: int, language: str) -> None:
        self._update(guild_id, language=language)

    def set_voice(self, guild_id: int, voice: str) -> None:
        self._update(guild_id, voice=voice)

    def _update(self, guild_id: int, **changes: object) -> None:
        current = asdict(self.get(guild_id))
        current.update(changes)
        self._data[str(guild_id)] = current
        self._save()

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        with self._path.open() as f:
            return json.load(f)

    def _save(self) -> None:
        tmp_path = self._path.with_suffix(".tmp")
        with tmp_path.open("w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp_path, self._path)
