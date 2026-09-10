# Listening Rooms backend API

The Linux backend is authoritative for room membership, permissions, playback metadata, queue state, and expiry. The browser must use HTTPS and WSS in production.

## Parent integration

Install `fastapi>=0.115,<1` and `uvicorn>=0.30,<1` in the parent dependency change. This scoped commit does not change `pyproject.toml`.

Construct `RoomService(db_path, playback)` and optionally pass `authority=`, `member=`, `on_reentry=`, and `search=` keyword callbacks. Call `start()` and `stop()` with the parent lifecycle. The bot alone calls `create_personal`, `ensure_group`, `end_group`, and `publish_playback`. There is no HTTP room-creation or group-ownership endpoint.

`playback(chat_id, action, payload)` is async and can return the current playback snapshot. `authority(user_id, chat_id)` and `member(user_id, chat_id)` can be sync or async. Group admission fails closed if `member` is absent. Group settings and close fail closed if `authority` is absent. `on_reentry(owner_id, room_id, user_id)` can be sync or async.

Create the ASGI application with `create_app(service, bot_token, allowed_origins, compass=None, search=None)`. Startup refuses an empty bot token or an empty/non-explicit origin list. Keep bot tokens and Telegram session credentials outside browser configuration.

## Authentication

`POST /api/auth` accepts `{ "init_data": "..." }`. It verifies the Telegram Mini App HMAC exactly as documented by Telegram: `secret_key = HMAC_SHA256(key="WebAppData", message=bot_token)`, then verifies the sorted data-check string with that key. Data older than five minutes or more than 30 seconds in the future is rejected.

The response is `{token, expires_at, user, start_param}`. The opaque token is server-side, user-bound, and valid for 15 minutes. Send it only as `Authorization: Bearer <token>`. Query-string bearer credentials are unsupported.

## Rooms

* `POST /api/rooms/{id}/join` with `{password?}`
* `GET /api/rooms/{id}`
* `POST /api/rooms/{id}/actions` with `{action_id, expected_revision, action, payload}`
* `GET /api/search?q=...` returns `{tracks: [...]}` or 501 if search is absent
* `POST /api/ws-ticket` with `{room_id}` returns a one-time ticket valid for no more than 30 seconds
* `WS /api/rooms/{id}/ws?ticket=...` requires an explicitly allowed `Origin`

Snapshots follow the frontend contract. `pending_reentry` is visible only to the owner. Passwords and hashes are never returned. Actions are durably idempotent by room, user, action ID, and request digest. A stale revision returns HTTP 409 with the current snapshot in the error object.

Personal playback is metadata-only. Search results are resolved server-side. Only HTTPS YouTube page URLs are accepted. The service does not proxy arbitrary media URLs.

WebSocket updates are `{type:"snapshot",snapshot}` and `{type:"presence",players}`. Movement input is `{type:"move",x,z,rotation,seq?}`. The gateway enforces finite values, scene bounds, 10 Hz updates, optional monotonic sequence numbers, and a 25-unit/second speed bound. It continuously checks session expiry and admission, so a kick disconnects receiving sessions. Queues are bounded and slow clients are disconnected.

## Actions

Supported actions are `settings`, `queue_add`, `force_play`, `pause`, `resume`, `skip`, `kick`, `moderator`, `request_reentry`, `approve_reentry`, `leave`, `close`, and `appearance`.

The settings payload supports `capacity` (2 to 15), `password`, `owner_lock`, `queue_all`, `theme`, `tv_size`, and `duration` (300 to 86400 seconds from creation). Group passwords and durations are unsupported. Group authority is independently verified for settings and close.

## Compass

Authenticated routes are:

* `GET /api/compass`
* `POST /api/compass/preferences`
* `POST /api/compass/feedback`
* `POST /api/compass/reset`
* `POST /api/compass/delete` and `DELETE /api/compass`

Synchronous Compass methods run in a worker thread. Async methods are awaited directly.

## Contract differences and parent obligations

The requested snapshot schema lists `password_required` inside `settings`; the backend emits it there and never emits the password. `request_reentry` must remain reachable after a kick. The gateway therefore needs parent/frontend handling that submits this action before normal admitted WebSocket access is restored. `ensure_group` remains an internal service method and cannot trust an HTTP owner ID. The parent bot must inject verified Telegram group callbacks and call it with identities derived from Telegram updates.

The parent must also wire the ASGI server, dependency declarations, bot commands, public HTTPS/WSS termination, and per-chat media registry. Those files are outside this implementation's ownership scope.
