"""SQLite persistence for Compass catalog, consent, events, and exposures."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class CompassStore:
    """Small synchronous store. Async callers run operations in a worker thread."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    track_id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL,
                    source_url TEXT NOT NULL, tags_json TEXT NOT NULL,
                    source TEXT NOT NULL, rights_note TEXT NOT NULL, discovered_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                    recommendation_count INTEGER NOT NULL DEFAULT 10,
                    quiet_start INTEGER, quiet_end INTEGER, timezone TEXT NOT NULL DEFAULT 'UTC',
                    dislikes_since_reset INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, track_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, context_json TEXT NOT NULL, created_at REAL NOT NULL,
                    FOREIGN KEY(track_id) REFERENCES tracks(track_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS events_user_time ON events(user_id, created_at);
                CREATE TABLE IF NOT EXISTS exposures (
                    exposure_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                    track_id TEXT NOT NULL, model_version TEXT NOT NULL, exposed_at REAL NOT NULL,
                    feedback_event_id TEXT UNIQUE,
                    UNIQUE(user_id, track_id),
                    FOREIGN KEY(track_id) REFERENCES tracks(track_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_type TEXT NOT NULL, slot TEXT NOT NULL, completed_at REAL NOT NULL,
                    PRIMARY KEY(job_type, slot)
                );
                """
            )

    def set_preferences(
        self,
        user_id: str,
        enabled: bool,
        count: int,
        quiet_start: int | None,
        quiet_end: int | None,
        timezone: str,
    ) -> None:
        with self.connect() as db:
            existing = db.execute(
                "SELECT enabled, paused FROM preferences WHERE user_id=?", (user_id,)
            ).fetchone()
            reset = bool(enabled and existing and (not existing["enabled"] or existing["paused"]))
            db.execute(
                """INSERT INTO preferences
                   (user_id, enabled, recommendation_count, quiet_start, quiet_end, timezone)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,
                   recommendation_count=excluded.recommendation_count,
                   quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end,
                   timezone=excluded.timezone,
                   paused=CASE WHEN ? THEN 0 ELSE preferences.paused END,
                   dislikes_since_reset=CASE WHEN ? THEN 0 ELSE preferences.dislikes_since_reset END""",
                (user_id, int(enabled), count, quiet_start, quiet_end, timezone, reset, reset),
            )

    def preference(self, user_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM preferences WHERE user_id=?", (user_id,)).fetchone()

    def enabled_users(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute("SELECT * FROM preferences WHERE enabled=1 AND paused=0"))

    def ingest_track(
        self,
        track_id: str,
        title: str,
        artist: str,
        source_url: str,
        tags: list[str],
        source: str,
        rights_note: str,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET title=excluded.title,
                   artist=excluded.artist, source_url=excluded.source_url,
                   tags_json=excluded.tags_json, source=excluded.source,
                   rights_note=excluded.rights_note""",
                (track_id, title, artist, source_url, json.dumps(tags), source, rights_note, time.time()),
            )

    def record_event(
        self,
        user_id: str,
        track_id: str,
        event_type: str,
        event_id: str,
        context: dict[str, Any],
        require_exposure: bool = False,
    ) -> bool:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM tracks WHERE track_id=?", (track_id,)).fetchone():
                raise ValueError("unknown track_id")
            exposure = None
            if require_exposure:
                exposure = db.execute(
                    """SELECT exposure_id, feedback_event_id FROM exposures
                       WHERE user_id=? AND track_id=?""",
                    (user_id, track_id),
                ).fetchone()
                if not exposure:
                    raise ValueError("feedback requires an actual recommendation exposure")
                if exposure["feedback_event_id"] is not None:
                    return False
            try:
                db.execute(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                    (event_id, user_id, track_id, event_type, json.dumps(context), time.time()),
                )
            except sqlite3.IntegrityError:
                return False
            if exposure:
                db.execute(
                    "UPDATE exposures SET feedback_event_id=? WHERE exposure_id=?",
                    (event_id, exposure["exposure_id"]),
                )
            if event_type in {"dislike", "negative"}:
                db.execute(
                    "INSERT INTO preferences(user_id) VALUES (?) ON CONFLICT DO NOTHING", (user_id,)
                )
                db.execute(
                    """UPDATE preferences SET dislikes_since_reset=dislikes_since_reset+1,
                       paused=CASE WHEN dislikes_since_reset+1>=5 THEN 1 ELSE paused END
                       WHERE user_id=?""",
                    (user_id,),
                )
            return True

    def candidates(self, user_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    """SELECT t.* FROM tracks t
                       WHERE NOT EXISTS (SELECT 1 FROM exposures x
                         WHERE x.user_id=? AND x.track_id=t.track_id)
                       AND NOT EXISTS (SELECT 1 FROM events e
                         WHERE e.user_id=? AND e.track_id=t.track_id
                         AND e.event_type IN ('dislike','negative'))
                       ORDER BY t.discovered_at DESC""",
                    (user_id, user_id),
                )
            )

    def add_exposures(self, user_id: str, track_ids: list[str], version: str) -> None:
        now = time.time()
        with self.connect() as db:
            db.executemany(
                """INSERT OR IGNORE INTO exposures(user_id,track_id,model_version,exposed_at)
                   VALUES (?,?,?,?)""",
                [(user_id, item, version, now) for item in track_ids],
            )

    def training_rows(self) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        with self.connect() as db:
            tracks = list(db.execute("SELECT * FROM tracks ORDER BY track_id"))
            events = list(db.execute("SELECT * FROM events ORDER BY created_at, event_id"))
        return tracks, events

    def claim_job(self, job_type: str, slot: str) -> bool:
        with self.connect() as db:
            try:
                db.execute("INSERT INTO jobs VALUES (?,?,?)", (job_type, slot, time.time()))
            except sqlite3.IntegrityError:
                return False
            return True

    def reset_user(self, user_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE preferences SET dislikes_since_reset=0, paused=0 WHERE user_id=?", (user_id,)
            )
            db.execute("DELETE FROM exposures WHERE user_id=?", (user_id,))

    def delete_user(self, user_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM exposures WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM preferences WHERE user_id=?", (user_id,))
