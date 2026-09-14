"""Persistent, per-guild admin-configurable settings."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuildSettings:
    tracked_users: dict[int, str]  # user_id -> voice


class SettingsStore:
    """Tracks which users (and their individual voices) each guild follows.

    Backed by a single JSON file so settings survive restarts without
    requiring a database.
    """

    def __init__(self, path: str, default_voice: str) -> None:
        self._path = Path(path)
        self.default_voice = default_voice
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = {}
        self._load()

    def get(self, guild_id: int) -> GuildSettings:
        entry = self._data.get(str(guild_id), {})
        tracked = entry.get("tracked_users", {})
        return GuildSettings(tracked_users={int(uid): voice for uid, voice in tracked.items()})

    def track_user(self, guild_id: int, user_id: int, voice: str | None = None) -> None:
        entry = self._data.setdefault(str(guild_id), {})
        tracked = entry.setdefault("tracked_users", {})
        tracked[str(user_id)] = voice or tracked.get(str(user_id), self.default_voice)
        self._save()

    def untrack_user(self, guild_id: int, user_id: int) -> None:
        entry = self._data.get(str(guild_id))
        if entry:
            entry.get("tracked_users", {}).pop(str(user_id), None)
            self._save()

    def untrack_all(self, guild_id: int) -> None:
        entry = self._data.get(str(guild_id))
        if entry:
            entry["tracked_users"] = {}
            self._save()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open() as f:
            data = json.load(f)

        migrated = False
        for entry in data.values():
            if "tracked_users" in entry:
                continue
            # Migrate the old single-tracked-user format.
            old_user_id = entry.pop("tracked_user_id", None)
            old_voice = entry.pop("voice", self.default_voice)
            entry["tracked_users"] = {str(old_user_id): old_voice} if old_user_id else {}
            migrated = True

        self._data = data
        if migrated:
            self._save()

    def _save(self) -> None:
        tmp_path = self._path.with_suffix(".tmp")
        with tmp_path.open("w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp_path, self._path)
