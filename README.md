# Overvoice

A Discord bot that follows a chosen user into voice channels and speaks
their text messages aloud, using [Pocket TTS](https://kyutai.org/blog/2026-01-13-pocket-tts/)
for fast, fully local, CPU-only speech synthesis. No "User says:" framing —
just the message, spoken.

Overvoice joins whatever voice channel its tracked user is in, and reads
anything they type into *that channel's own text chat* (the chat panel built
into every Discord voice channel). It leaves when they leave, and follows
when they move channels.

Everything is configured per server with admin-only slash commands — no
env vars to edit or restarts needed to change who it follows or what it
sounds like.

## Setup

### 1. Create the bot on Discord

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application named "Overvoice" (or any name), then add a Bot to it.
2. Under **Bot**, enable both privileged intents: **Message Content Intent** and **Server Members Intent**.
3. Copy the bot token.
4. Under **OAuth2 > URL Generator**, select both the `bot` and `applications.commands` scopes, and these bot permissions: View Channel, Connect, Speak, Send Messages, Read Message History. Use the generated URL to invite the bot to your server. (`applications.commands` is required for the `/overvoice` slash commands to register — if you invited an earlier version of the bot without it, just re-open the same invite URL to re-authorize.)

### 2. Configure

```bash
cp .env.example .env
```

Fill in `DISCORD_BOT_TOKEN`. Everything else is optional (see below).

### 3. Run

```bash
docker compose up --build
```

The first start downloads the Pocket TTS model weights into a cached Docker
volume (`hf-cache`), so subsequent restarts start instantly and work offline.

### 4. Configure per server

Once the bot is in your server, an admin runs:

- `/overvoice track user:@someone` — follow this person into voice channels and read their messages
- `/overvoice untrack` — stop following anyone
- `/overvoice language language:portuguese` — set the TTS language for this server
- `/overvoice voice voice:vera` — set the TTS voice (autocompletes as you type)
- `/overvoice preview language:portuguese voice:vera` — post a short sample clip so everyone can hear a voice before picking it
- `/overvoice status` — show the current tracked user, language, and voice

Changes apply immediately — no restart needed.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | Bot token from the Developer Portal |
| `TTS_DEFAULT_LANGUAGE` | `english` | Language for servers that haven't run `/overvoice language` yet |
| `TTS_DEFAULT_VOICE` | `alba` | Voice for servers that haven't run `/overvoice voice` yet |
| `TTS_MAX_CHARS` | `500` | Messages longer than this are truncated before being spoken |
| `TTS_DEBUG_DIR` | unset | If set, saves every generated clip as a `.wav` file there (see `docker-compose.yml`'s `./debug-audio` mount) |
| `SETTINGS_PATH` | `data/guild_settings.json` | Where per-server settings are persisted (see `docker-compose.yml`'s `./data` mount) |

## Local development (without Docker)

Requires Python 3.10+ and `ffmpeg` on your system.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
cp .env.example .env  # fill in DISCORD_BOT_TOKEN
python -m bot.main
```

Iterate against the venv directly for fast turnaround (e.g. calling
`TTSCatalog.synthesize(...)` from a one-off script to inspect generated
audio) — reserve the Docker build for verifying the container itself.
