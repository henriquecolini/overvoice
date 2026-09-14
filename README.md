# Overvoice

A Discord bot that follows chosen users into voice channels and speaks
their text messages aloud, using [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)
for fast, fully local, CPU-only speech synthesis. No "User says:" framing —
just the message, spoken, each tracked user in their own chosen voice.

Overvoice joins whatever voice channel its tracked users are in, and reads
anything they type into *that channel's own text chat* (the chat panel built
into every Discord voice channel). Since a bot can only be in one voice
channel per server at a time, it stays put as long as any tracked user is
still there, and otherwise joins whichever channel currently has the most
of them.

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

The first start downloads the Kokoro model weights into `./models`, so
subsequent restarts start instantly and work offline.

### 4. Configure per server

Once the bot is in your server, an admin runs:

- `/overvoice track user:@someone [voice:pf_dora]` — follow this person into voice channels and read their messages, in the given voice (defaults to the server's default voice; autocompletes as you type — see `bot/tts.py`'s `VOICES` list for every option, language is implied by the voice, e.g. `pf_dora` speaks Brazilian Portuguese, `af_bella` speaks American English)
- `/overvoice untrack [user:@someone]` — stop following one person, or everyone if no user is given
- `/overvoice voice user:@someone voice:pf_dora` — change the voice for someone already being followed
- `/overvoice preview voice:pf_dora` — post a short sample clip so everyone can hear a voice before picking it
- `/overvoice say text:hello there [voice:pf_dora]` — anyone can post a spoken clip of arbitrary text, defaulting to their own voice if they're followed
- `/overvoice status` — list everyone currently being followed and their voice

Multiple people can be followed at once, each with their own voice — the
bot just can't be in two voice channels simultaneously (a Discord
limitation), so it follows whichever channel has the most tracked people
in it. Changes apply immediately — no restart needed.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | Bot token from the Developer Portal |
| `TTS_DEFAULT_VOICE` | `af_heart` | Voice for servers that haven't run `/overvoice voice` yet |
| `TTS_MAX_CHARS` | `500` | Messages longer than this are truncated before being spoken |
| `TTS_DEBUG_DIR` | unset | If set, saves every generated clip as a `.wav` file there (see `docker-compose.yml`'s `./debug-audio` mount) |
| `SETTINGS_PATH` | `data/guild_settings.json` | Where per-server settings are persisted (see `docker-compose.yml`'s `./data` mount) |
| `KOKORO_MODEL_DIR` | `models/kokoro` | Where Kokoro's model weights are downloaded to (see `docker-compose.yml`'s `./models` mount) |

## Local development (without Docker)

Requires Python 3.10+, `ffmpeg`, and `espeak-ng` on your system (`dnf install espeak-ng` / `apt install espeak-ng`).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in DISCORD_BOT_TOKEN
python -m bot.main
```

Iterate against the venv directly for fast turnaround (e.g. calling
`TTSCatalog.synthesize(...)` from a one-off script to inspect generated
audio) — reserve the Docker build for verifying the container itself.

## Why Kokoro instead of Pocket TTS

The bot originally used [Pocket TTS](https://kyutai.org/blog/2026-01-13-pocket-tts/),
which sounds great but is a flow/transformer model with an EOS detector
that decides when speech ends — and that detector can misfire on short,
context-free chat messages ("oi", "kkkkk", single-word reactions), cutting
them short. Kokoro is non-autoregressive, so short and long utterances are
equally stable. That version is preserved on the `pocket-tts` branch.
