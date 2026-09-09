"""AuditLogStore: append membership events to an Operator-local log.

Each event is written as one JSON line to a file on the Operator's own host. The
file is created with owner-only permissions (0o600) and never exposed to any
chat (ADR-001 / AC-MEM-003.2). Writes run in a worker thread so the event loop
is never blocked by disk IO.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .events import MembershipEvent

log = logging.getLogger(__name__)

# Owner read/write only; the log is Operator-local and never chat-facing.
_LOG_MODE = 0o600


class AuditLogStore:
    """Appends each MembershipEvent to the Operator-local Audit Log."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    async def __call__(self, event: MembershipEvent) -> None:
        await self.append(event)

    async def append(self, event: MembershipEvent) -> None:
        """Append one event to the log (REQ-MEM-003.1)."""
        line = json.dumps(event.as_record(), ensure_ascii=False)
        await asyncio.to_thread(self._write_line, line)

    def _write_line(self, line: str) -> None:
        parent = self._path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        # Open with O_APPEND and a restrictive mode so a freshly created log is
        # owner-only from the first byte.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _LOG_MODE)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        # Enforce owner-only perms even if the file pre-existed with looser ones.
        try:
            os.chmod(self._path, _LOG_MODE)
        except OSError:
            log.debug("could not tighten audit log permissions", exc_info=True)
