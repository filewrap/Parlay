# Parlay Telegram Mini App

The frontend is an independently hosted static Next.js export in `web/`. The Linux backend remains authoritative for identity, admission, permissions, room state, playback, queues, expiry, and Telegram voice chat output.

## Deployment

1. Set `NEXT_PUBLIC_BACKEND_URL` to the public HTTPS backend origin before `npm run build`. Do not add a trailing path.
2. Run `npm ci`, `npm run typecheck`, `npm test`, and `npm run build` in `web/`.
3. Publish `web/out/` on a static host. Configure the backend to allow that exact frontend origin for HTTP and WebSocket traffic.
4. Configure the Telegram bot Mini App URL to the static host URL. The app loads Telegram's official SDK from `https://telegram.org/js/telegram-web-app.js`.

The static bundle contains no bot token, Telethon session, provider credential, or backend bearer token. The bearer token exists only in page memory. A reload performs a fresh `/api/auth` exchange.

## Launch and room selection

The browser sends raw `Telegram.WebApp.initData` to `POST /api/auth`. It does not use `initDataUnsafe` for identity or authorization. The room candidate returned as the server-validated `start_param` is preferred. A `?room=` URL value is only a fallback candidate and never grants admission or permissions.

The backend must expose the contract below under `NEXT_PUBLIC_BACKEND_URL`:

- `POST /api/auth` with `{init_data}`.
- `POST /api/rooms/{id}/join` with optional `{password}` and bearer authentication.
- `GET /api/rooms/{id}` for a current snapshot.
- `POST /api/rooms/{id}/actions` with UUID `action_id`, `expected_revision`, `action`, and `payload`.
- `GET /api/search?q=...`.
- `POST /api/ws-ticket` with `{room_id}`. Tickets are short-lived and are the only credentials placed in a WebSocket URL.
- `WS /api/rooms/{id}/ws?ticket=...` for snapshots, presence, and movement.
- Compass preference, feedback, reset, and delete routes under `/api/compass`.

## Client behavior and contract assumptions

- Snapshot member and user identifiers can be strings or numbers on the wire. The client compares their string forms.
- Snapshot `server_time` and `expires_at` use epoch seconds.
- A settings action sends all editable settings. `duration` is seconds from the time the backend accepts the change. The expiry shown before submit is a browser preview; the returned snapshot is authoritative.
- An empty password is sent when settings are saved. The backend defines whether that keeps or disables password protection and must return only `password_required`, never a password or hash.
- A `401` starts one fresh Telegram `initData` exchange. If Telegram does not provide fresh launch data, the interface asks for a relaunch.
- A `409` fetches and shows a fresh snapshot. The client does not retry the mutation.
- A kicked person can still send `request_reentry`. Owner approval uses `approve_reentry`.
- Shared controls remain disabled while disconnected, recovering a connection, or after room end.
- Presence movement is sent at no more than 10 Hz. Remote positions are interpolated in the scene and are never treated as durable room state.

## Media and accessibility

YouTube tracks use the official, visible IFrame Player API. Playback starts after a tap and follows the backend timeline with periodic drift correction. Local mute and volume are personal controls. Users should mute browser playback when Telegram voice chat audio is also audible. Unsupported sources show an honest source link and no simulated playback.

The room supports tap-to-move, WASD, arrow keys, and touch pointer input. Reduced-motion preferences and low CPU counts select lower pixel density and simpler rendering. If WebGL is unavailable, all room and media controls remain available in a lightweight fallback.
