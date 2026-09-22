"""Turns a raw Discord message into clean, speakable text."""

from __future__ import annotations

import re

import discord

_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_URL = re.compile(r"https?://\S+")
_MARKDOWN_CHARS = re.compile(r"(\*\*|\*|__|_|~~|`{1,3}|\|\||^>\s?)", re.MULTILINE)
# A line break is a sentence break (lists, one thought per line); turning
# it into a period lets the TTS split there and start speaking sooner.
_LINE_BREAK = re.compile(r"(?<![.!?…,;:])[ \t]*\n\s*")
_WHITESPACE = re.compile(r"\s+")


def sanitize_message(message: discord.Message, max_chars: int) -> str:
    guild = message.guild
    text = message.content

    text = _USER_MENTION.sub(lambda m: _resolve_user(guild, int(m.group(1))), text)
    text = _ROLE_MENTION.sub(lambda m: _resolve_role(guild, int(m.group(1))), text)
    text = _CHANNEL_MENTION.sub(lambda m: _resolve_channel(guild, int(m.group(1))), text)
    text = _CUSTOM_EMOJI.sub(lambda m: m.group(1), text)
    text = _URL.sub("", text)
    text = _MARKDOWN_CHARS.sub("", text)
    text = _LINE_BREAK.sub(". ", text.strip())
    text = _WHITESPACE.sub(" ", text).strip()

    return text[:max_chars]


def _resolve_user(guild: discord.Guild | None, user_id: int) -> str:
    member = guild.get_member(user_id) if guild else None
    return f"@{member.display_name}" if member else "@someone"


def _resolve_role(guild: discord.Guild | None, role_id: int) -> str:
    role = guild.get_role(role_id) if guild else None
    return f"@{role.name}" if role else "@role"


def _resolve_channel(guild: discord.Guild | None, channel_id: int) -> str:
    channel = guild.get_channel(channel_id) if guild else None
    return f"#{channel.name}" if channel else "#channel"
