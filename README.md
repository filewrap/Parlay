# Parlay

A thin bridge between [ntgcalls](https://github.com/pytgcalls/ntgcalls) and Gemini Live, wired into a Telegram voice/music userbot. Parlay runs as your own Telegram account (a userbot), joins a group voice chat, bridges call speech to an AI voice provider, and plays music on request.

This is a personal, single-operator tool. Not a service.

## Status

Early scaffold (WO-1): userbot process, config, operator-gated command handler, call-session skeleton, and one-command startup. Audio capture, the AI pipeline, and music are added in later work orders.

## Requirements

- Docker and Docker Compose
- A Telegram API id/hash from https://my.telegram.org
- A Google Gemini API key

## Setup

```bash
cp .env.example .env
# edit .env and fill in TELEGRAM_API_ID, TELEGRAM_API_HASH, OPERATOR_ID, GEMINI_API_KEY
./start.sh
```

`start.sh` verifies prerequisites and your `.env`, then brings up the bot and the PO-token provider together with Docker Compose.

### First run / login

The first launch needs an interactive Telegram login to create the session file. Run the bot in the foreground once to complete the login prompt:

```bash
docker compose run --rm parlay python -m parlay
```

The session is stored under `./data` and reused on later runs.

## Commands

Only the configured Operator can drive the bot.

| Command | Purpose |
| --- | --- |
| `/join <chat>` | Join a group voice chat |
| `/leave` | Leave the current voice chat |
| `/start` | Engage the AI voice pipeline |
| `/stop` | Disengage the AI voice pipeline |
| `/status` | Report current session state |
| `/play <query or link>` | Play music (later work order) |
| `/skip` `/pause` `/resume` `/queue` | Music transport (later work order) |

## Development

```bash
pip install -e '.[dev]'
ruff check .
mypy
pytest
```
