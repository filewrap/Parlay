# Parlay

Parlay runs Telegram music and optional AI voice through a user-account session. A separate companion bot opens shared listening rooms and delivers opt-in Compass recommendations. The Linux backend owns playback and room state. The Next.js Mini App deploys separately.

## Implemented components

- Concurrent per-chat media runtimes with independent queues and playback timelines.
- Telegram activity reconciliation and local SQLite persistence.
- Companion commands, inline room confirmation, callbacks, and recommendation DMs.
- Persistent personal and group rooms, permissions, expiry, moderation, and re-entry.
- Authenticated HTTP/WebSocket room gateway with Telegram launch validation.
- Next.js/Tailwind/Lucide 3D Mini App with human avatars and a visible synchronized YouTube player.
- Trainable NumPy hybrid Compass recommendations, feedback, hourly scheduling, and temporal evaluation.

Implementation and automated tests do not prove live Telegram concurrency or recommendation quality. Release still requires two real simultaneous VCs, two real Mini App users, and evaluation with permitted real interaction data. Browser playback uses supported YouTube embeds; it does not promise hidden/audio-only YouTube playback, lossless restoration, or sample-accurate VC/browser synchronization. Semantic coloured buttons are not enabled on the current broad Telethon version range.

## Linux backend setup

1. Install Docker and Docker Compose.
2. Copy `.env.example` to `.env`.
3. Supply Telegram API credentials and an already authorized **user-account** Telethon session. Startup does not prompt for login. With Docker, put file sessions in `./data` and set `TELEGRAM_SESSION=/app/data/<name>.session`. A valid Telethon StringSession is also supported. Never commit or share credentials.
4. Set `OPERATOR_ID`. Gemini is optional and needed only for AI voice.
5. To enable rooms, set `BOT_TOKEN` for a separate bot and `MINI_APP_URL` for the HTTPS frontend. Keep its bot session separate from the user session.
6. Expose the host's `127.0.0.1:8080` through a public HTTPS reverse proxy with WebSocket upgrade support. Set `ALLOWED_ORIGINS` to exact frontend origins. Use one backend process, not multiple ASGI workers sharing in-memory rooms.
7. Run:

```bash
docker compose up -d --build
docker compose logs -f parlay
```

All local databases and model artifacts default to `./data`, persisted by Compose. Keep this directory private and back it up. Playback metadata survives restart; native audio connections do not. Recovered group connections currently return idle rather than silently replaying an old song.

`MAX_CONCURRENT_CALLS` defaults to 4 and is configurable. This is an admission limit, not a measured hardware guarantee.

## Mini App deployment

```bash
cd web
npm ci
NEXT_PUBLIC_BACKEND_URL=https://YOUR-BACKEND-HOST npm run build
```

Deploy `web/out` to an HTTPS static host, or configure a compatible Next.js host with `web` as its root. `NEXT_PUBLIC_BACKEND_URL` is public configuration and must be supplied at build time. Never place bot tokens or Telegram sessions in frontend variables.

In BotFather, enable inline mode and configure the bot's Main Mini App to the deployed frontend URL. Room links use `https://t.me/<bot>?startapp=<room-id>`. Telegram supplies identity automatically. There is no separate website login.

## Commands

User-account interface is restricted to the account and configured operator:

- `/join` uses the current group/channel; `/join <chat>` accepts an explicit target.
- `/play <song or YouTube link>` operates in the current group/channel.
- `/pause`, `/resume`, `/skip`, `/queue`, `/stop`, `/leave` operate on that chat only.
- `/vc`, `/vcstart`, `/vcstop`, `/status` inspect or control the target chat.
- `/start` engages AI voice in the current connected chat.
- Unknown commands are ignored.

Companion bot:

- `/room [duration]` in reply to a human, for example `/room 2h`.
- Inline `@<bot> room`, followed by creator confirmation.
- `/play <query>` for authorized group playback and the linked room.
- `/start` in private messages permits delivery but does not enable recommendations.
- `/compass on`, `/compass off`, `/compass suggestions`, `/compass count 5`, `/compass reset`, `/compass delete`.

Personal defaults: capacity 2 including owner, configurable through 15; password off; Owner Lock on; owner-only queue; no appointed moderators; lifetime 2 hours from creation, configurable from 5 minutes through 24 hours.

## Compass data and evaluation

Set `YOUTUBE_API_KEY` for hourly collection of up to 100 available chart candidates. Without a key, the engine can learn from the catalog populated by Parlay requests but cannot collect charts. Consent is required before personal events are retained. VC presence alone is never recorded as a person's listening preference.

The 10,000-track test uses synthetic metadata to verify pipeline mechanics. It is not evidence of real recommendation quality.

## Development

```bash
pip install -e '.[dev]'
ruff check .
ruff format --check .
mypy
pytest -q
cd web
npm ci
npm run typecheck
npm test -- --run
npm run build
```

See `docs/rooms-api.md`, `docs/mini-app.md`, `docs/runtime.md`, `docs/bot.md`, and `docs/parlay-ml.md` for contracts and limitations.
