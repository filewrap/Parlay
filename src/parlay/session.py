"""Call session state and lifecycle skeleton.

The CallSessionManager holds the single active Call Session and its lifecycle
(join / leave / status). Audio wiring (capture, playback, AI pipeline) is added
by later work orders; this module owns state and transitions only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

log = logging.getLogger(__name__)


class ConnectionState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass
class CallSession:
    """State for one joined voice chat. At most one is active at a time."""

    chat: str
    state: ConnectionState = ConnectionState.CONNECTING
    ai_engaged: bool = False


class SessionError(RuntimeError):
    """Raised on an invalid session transition (e.g. join while active)."""


@dataclass
class CallSessionManager:
    """Owns the single active Call Session and its lifecycle."""

    _session: CallSession | None = field(default=None, init=False)

    @property
    def active(self) -> bool:
        return self._session is not None

    @property
    def session(self) -> CallSession | None:
        return self._session

    def begin_join(self, chat: str) -> CallSession:
        """Create a Call Session for `chat`. Rejects if one is already active."""
        if self._session is not None:
            raise SessionError("A session is already active.")
        self._session = CallSession(chat=chat)
        log.info("Call session created for %s", chat)
        return self._session

    def mark_connected(self) -> None:
        if self._session is None:
            raise SessionError("No active session to connect.")
        self._session.state = ConnectionState.CONNECTED

    def end(self) -> None:
        """Tear down the active session and release it so a new join can start."""
        if self._session is None:
            raise SessionError("There is nothing to leave.")
        log.info("Call session ended for %s", self._session.chat)
        self._session = None

    def engage_ai(self) -> None:
        if self._session is None:
            raise SessionError("Join a voice chat first.")
        if self._session.ai_engaged:
            raise SessionError("The AI pipeline is already running.")
        self._session.ai_engaged = True

    def disengage_ai(self) -> None:
        if self._session is None:
            raise SessionError("No active session.")
        self._session.ai_engaged = False

    def status_text(self) -> str:
        """Human-readable status line (plain; presentation styling is WO-10)."""
        if self._session is None:
            return "idle: not in a voice chat"
        ai = "engaged" if self._session.ai_engaged else "off"
        return f"in {self._session.chat} ({self._session.state.value}); AI {ai}"
