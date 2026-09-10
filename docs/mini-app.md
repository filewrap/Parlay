# Parlay Telegram Mini App

The frontend is an independently hosted static Next.js export in `web/`. The Linux backend remains authoritative for identity, admission, permissions, room state, playback, queues, expiry, and Telegram voice chat output.

## Deployment

1. Set `NEXT_PUBLIC_BACKEND_URL` to the public HTTPS backend origin before `npm run build`. Do not add a trailing path.
2. Run `npm ci`, `npm audit`, `npm run typecheck`, `npm test`, and `npm run build` in `web/`.
3. Publish `web/out/` on a static host. Configure the backend to allow that exact frontend origin for HTTP and WebSocket traffic.
4. Configure the Telegram bot Mini App URL to the static host URL. The app loads Telegram's official SDK from `https://telegram.org/js/telegram-web-app.js`.

The static bundle contains no bot token, Telethon session, provider credential, or backend bearer token. The bearer token exists only in page memory. A reload performs a fresh `/api/auth` exchange.

## Launch and room selection

The browser sends raw `Telegram.WebApp.initData` to `POST /api/auth`. It does not use `initDataUnsafe` for identity or authorization. The room candidate returned as the server-validated nullable `start_param` is preferred. A `?room=` URL value is only a fallback candidate and never grants admission or permissions. The authenticated user identifier is the numeric `user.id` returned by the backend.

The backend exposes the contract below under `NEXT_PUBLIC_BACKEND_URL`:

- `POST /api/auth` with `{init_data}`.
- `POST /api/rooms/{id}/join` with `{password}` and bearer authentication.
- `GET /api/rooms/{id}` for a current snapshot.
- `POST /api/rooms/{id}/actions` with UUID `action_id`, `expected_revision`, `action`, and `payload`.
- `GET /api/search?q=...`.
- `POST /api/ws-ticket` with `{room_id}`. Tickets are short-lived and are the only credentials placed in a WebSocket URL.
- `WS /api/rooms/{id}/ws?ticket=...` for snapshots, presence, and movement.
- Compass preference, feedback, reset, and delete routes under `/api/compass`.

## Client behavior and backend compatibility

- Snapshot user, owner, chat, presence, moderation target, and pending re-entry identifiers are numeric on the wire.
- Snapshot `server_time` and `expires_at` use epoch seconds.
- Backend failures use `{error:{code,message,snapshot?}}`; framework validation failures can use `detail`. A stale-revision snapshot is applied without blindly retrying the mutation.
- Queue and force-play actions send `{query}`. The backend resolves the first supported search result and accepts only HTTPS YouTube page URLs.
- Appearance sends optional short-string `avatar` and `outfit` fields. The client sends both current selector values.
- Compass quiet-time controls send nullable integer hours from 0 through 23, not browser `HH:mm` strings. Reset and delete use authenticated POST routes.
- `pending_reentry` is always present as an array and is populated only for the owner.
- A kicked person can send `request_reentry` from the last known snapshot while normal snapshot and WebSocket access remain blocked. If the revision is stale, the backend error snapshot is shown and the action is not retried automatically. Owner approval uses a numeric `user_id`.
- Shared controls remain disabled while disconnected, recovering a connection, or after room end. The re-entry request is the only disconnected exception.
- Presence movement is sent at no more than 10 Hz. Remote positions are interpolated in the scene and are never treated as durable room state.
- A settings `duration` is total lifetime in seconds from room creation. The backend does not expose `created_at`, so the client must describe any pre-submit deadline as an estimate and treat the returned `expires_at` as authoritative.

## Media and accessibility

YouTube tracks use the official, visible IFrame Player API. Playback starts after a tap and follows the backend timeline with periodic drift correction. Local mute and volume are personal controls. Users should mute browser playback when Telegram voice chat audio is also audible. Unsupported sources show an honest source link and no simulated playback.

The room supports tap-to-move, WASD, arrow keys, and touch pointer input. Reduced-motion preferences and low CPU counts select lower pixel density and simpler rendering. If WebGL is unavailable, all room and media controls remain available in a lightweight fallback.
