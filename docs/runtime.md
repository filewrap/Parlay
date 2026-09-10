# Concurrent media runtime

`parlay.runtime` is an integration-ready subsystem. `ParlayApp` does not use it yet.

## Public API

```python
registry = RuntimeRegistry(
    client,
    config,
    on_playback=async_callback,  # (chat_id, snapshot)
    on_closed=async_callback,    # (chat_id, reason)
    on_transport=async_callback, # (chat_id, connected)
)

runtime = await registry.join(chat_or_entity)
runtime = registry.get(chat_id)
snapshot = await registry.command(chat_id, action, payload)
await registry.leave(chat_id, reason="left")
await registry.close()
```

A runtime exposes `sessions`, `bridge`, `arbiter`, `music`, `ai`, `chat_id`,
`generation`, and `call_id`. `ai` starts as `None`; parent integration can attach
one independent AI producer to each runtime.

Supported actions are `play`, `force_play`, `pause`, `resume`, `skip`, `stop`,
`queue`, and `snapshot`. `play` accepts one request string, a list of request
strings, or `{"queue": [...]}`. A play command creates the runtime first, so the
voice chat must already be live. `force_play` replaces the current track and
keeps the pending queue.

## Isolation and limits

Each chat has separate session, bridge, arbiter, music queue, and AI attachment.
Lifecycle and command locks are per chat. A small registry lock protects only
capacity reservations and map updates; it never contains Telegram, resolver,
transcoder, callback, or bridge I/O.

`config.max_concurrent_calls` sets capacity. Missing values default to 4. Values
below 2 are raised to 2. Capacity is reserved before join I/O, so simultaneous
joins cannot overbook it.

One PyTgCalls engine is shared per MTProto client and starts exactly once.
Adapters still join, record, pump, receive updates, and leave independently per
chat. The adapter matches the PyTgCalls 2.3.x API backed by NTgCalls 2.x:
`play(MediaStream(ExternalMedia.AUDIO))`, `record(RecordStream)`, `send_frame`,
`StreamFrames`, `ChatUpdate.Status.LEFT_CALL`, and `leave_call`.

Generation checks reject late disconnect and playback events from an old runtime.
Stopping one runtime does not stop the shared engine or another chat.

## Playback snapshots

```json
{
  "track": {
    "id": "source identifier",
    "title": "Title",
    "source_url": "https://public.original/page",
    "youtube_id": "optional",
    "duration": 123.4
  },
  "status": "playing",
  "position_seconds": 10.25,
  "server_time": 1789060000.0,
  "queue": []
}
```

`track` is `null` while idle. Status is `idle`, `playing`, or `paused`.
`source_url` is original public metadata and never the expiring resolved stream
URL. Position is the duration of PCM accepted by the paced producer. It pauses
when production pauses and resets between tracks.

Music observers run in a separate ordered worker. Playback locks only publish a
latest snapshot and never await an observer. If an observer is slow, intermediate
pending snapshots can coalesce to the newest state. Natural finite-stream end and
auto-advance publish changes.

## Persistence and security

When `config.activity_db_path` exists, the registry writes the latest snapshot to
the `runtime_playback` SQLite table. This is durable metadata for parent recovery
and includes pending queue metadata. It does not restore a Telegram transport,
resolved stream URL, or live playback after restart. Parent integration decides
whether to re-resolve and replay persisted requests.

Direct HTTP and HTTPS requests are resolved before media lookup. Any loopback,
private, link-local, multicast, reserved, or otherwise non-global address is
rejected. Search text and existing public operator sources continue through the
normal resolver.

Callbacks are best-effort. Exceptions are logged and do not stop playback.
`on_closed` and transport callbacks run after lifecycle locks are released.
No live-call or credential test is part of this subsystem.
