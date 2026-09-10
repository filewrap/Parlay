# Parlay companion bot

`parlay.bot.CompanionBot` is an independent Telethon bot. The existing userbot remains the media account. The companion uses a separate MTProto session and does not call `run_until_disconnected()`.

## Parent contract

```python
bot = CompanionBot(config, rooms, compass, play, authority)
await bot.start()
# Monitor bot.client if the parent needs connection health.
await bot.stop()
```

- `rooms` is the existing `RoomService` contract. The bot calls `create_personal(owner_id, invited_id, duration)`, `snapshot(room_id, user_id)`, and `action(room_id, user_id, action_id, expected_revision, action, payload)`.
- `compass` is the existing `CompassService` contract. Its blocking SQLite/model methods run in worker threads.
- `play(user_id, chat_id, query)` is async and returns a room snapshot containing `id`.
- Optional `authority(user_id, chat_id)` can be synchronous or async. It must validate group ownership or another approved control role. Without it, only `config.operator_id` can use `/play`.
- `notify_reentry(owner_id, room_id, user_id)` is suitable for `RoomService(on_reentry=...)`. It reads an owner snapshot, stores its revision, and sends an owner-bound approval action.
- `deliver(user_id, text, items)` is suitable for `CompassService.start(deliver=...)`. It returns `True` after sending and `False` when the user has not started the bot or Telegram delivery fails.

The companion is optional. A missing bot token must not affect userbot-only startup. If the parent configures and starts `CompanionBot`, `bot_token` is required.

## Configuration

The parent config object supplies these attributes:

| Attribute | Required/default | Purpose |
| --- | --- | --- |
| `api_id`, `api_hash` | required | Telegram application credentials |
| `bot_token` | required when started | BotFather token |
| `bot_session` | `data/parlay-bot` | Separate Telethon session |
| `bot_username` | resolved with `get_me()` when absent | Username used in direct Mini App links |
| `mini_app_url` | required HTTPS URL | Direct static host configured in BotFather |
| `mini_app_short_name` | optional | Reserved for BotFather configuration; the preferred runtime link uses the Main Mini App |
| `bot_db_path` | `data/parlay-bot.sqlite3` | Separate consent and callback SQLite state |
| `operator_id` | existing setting | Fallback `/play` controller when no authority callback is supplied |

Secrets are passed only to Telethon startup. Bot errors do not include credentials or tracebacks in chat.

## Telegram and Mini App setup

Create the bot with BotFather and configure its username, inline mode, and Main Mini App. Set the Main Mini App URL to the same direct HTTPS host as `mini_app_url`. Mini App registration is a BotFather operation. It does not require a second Telegram user login.

Room buttons prefer:

```text
https://t.me/{bot_username}?startapp={room_id}
```

The HTTPS host fallback uses `mini_app_url?room={room_id}`. Telegram `startapp` remains the preferred path because it supplies the signed start parameter described in `docs/mini-app.md`.

## Commands and consent

The bot handles non-forwarded incoming messages only.

- `/room [duration]` creates a personal room. Duration is exactly zero or one compact value in seconds, minutes, or hours, such as `300s`, `30m`, or `2h`. Default is `2h`; range is `5m` through `24h`. A reply invites that verified human sender. Bots and anonymous channel/chat senders are rejected.
- `/play <query>` works only in groups after the authority callback, or fallback operator check, approves the sender. The parent callback performs playback and returns the room snapshot.
- `/start` works in a private chat and records delivery eligibility. It does not enable Compass.
- `/compass on|off|reset|delete|count N|suggestions` controls the existing Compass service. `on` is an explicit private opt-in and requires `/start`. Count is 1 through 10.
- `/help` shows command help.

Blocked private delivery clears bot delivery eligibility and returns `False` to Compass. The Compass scheduler can then disable its own preference using its existing delivery-failure behavior.

## Inline and callback behavior

The exact inline query `room` returns one preview article. It does not create a room. Its confirmation callback is bound to the initiating user. The first valid click creates one room and persists the result; retries edit the inline message to the same room URL. This flow does not depend on BotFather chosen-result feedback and no typing event creates state.

Compass recommendation buttons store opaque callback IDs. Track IDs stay in SQLite rather than Telegram's 64-byte callback payload. Feedback callbacks are user-bound and use a stable event ID, so retries are idempotent. Re-entry approvals are owner-bound and use the snapshot revision captured before delivery.

Unauthorized callbacks are answered without mutation. Successful activation and re-entry callbacks use `CallbackQuery.edit()` with a URL button, which also works for inline-origin messages.

## Telethon API compatibility

The implementation follows Telethon's stable event API:

- [`events.NewMessage(forwards=False)`](https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.NewMessage) excludes forwarded commands.
- [`events.InlineQuery`](https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.InlineQuery) supplies `event.builder`; the result uses the documented `builder.article(title, text=..., buttons=...)` form.
- [`events.CallbackQuery`](https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.CallbackQuery) supports `event.edit()` for normal and inline-origin messages.
- [`Button.inline` and `Button.url`](https://docs.telethon.dev/en/stable/modules/custom.html#telethon.tl.custom.button.Button) create callback and URL buttons.

Telegram added semantic `KeyboardButtonStyle` in API layer 227 ([Telegram button documentation](https://core.telegram.org/api/bots%2Fbuttons)). Telethon's high-level `Button.inline()` and `Button.url()` signatures in the project's supported `telethon>=1.36` range do not consistently expose a style argument. The bot therefore uses plain buttons. It does not pass invented style keywords. A future pinned Telethon layer can add raw styled constructors after compatibility tests.
