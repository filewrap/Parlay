"""Command parsing, operator gating, and dispatch.

The CommandHandler owns the full command set. It parses an incoming message,
verifies it came from the configured Operator, and routes it to a registered
handler. Music command handlers are implemented in later work orders; here they
are registered as not-yet-available so the surface is complete and testable.

Message wording is plain here. A shared presentation layer (WO-10) will style
output later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# Bot Control commands owned end-to-end by this work order.
CONTROL_COMMANDS = ("join", "leave", "start", "stop", "status")
# Music commands: registered and dispatched here; behavior lands in WO-5/WO-6.
MUSIC_COMMANDS = ("play", "skip", "pause", "resume", "queue")

Handler = Callable[["ParsedCommand"], Awaitable[str]]


@dataclass(frozen=True)
class ParsedCommand:
    """A parsed operator command."""

    name: str
    args: str
    sender_id: str


class CommandHandler:
    """Parses, gates, and dispatches operator commands."""

    def __init__(self, operator_id: str, prefix: str = "/") -> None:
        self._operator_id = str(operator_id)
        self._prefix = prefix
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        """Register a handler for a command name."""
        self._handlers[name] = handler

    def is_operator(self, sender_id: object) -> bool:
        """True only for the configured Operator identity."""
        if sender_id is None:
            return False
        return str(sender_id) == self._operator_id

    def parse(self, text: str, sender_id: object) -> Optional[ParsedCommand]:
        """Parse a message into a command, or None if it is not one."""
        if not text:
            return None
        stripped = text.strip()
        if not stripped.startswith(self._prefix):
            return None
        body = stripped[len(self._prefix):]
        if not body:
            return None
        head, _, rest = body.partition(" ")
        return ParsedCommand(name=head.lower(), args=rest.strip(), sender_id=str(sender_id))

    async def dispatch(self, text: str, sender_id: object) -> Optional[str]:
        """Parse, operator-gate, and route a message.

        Returns the reply string, or None when the message should be ignored
        (not a command, or not from the Operator).
        """
        command = self.parse(text, sender_id)
        if command is None:
            return None
        if not self.is_operator(sender_id):
            log.debug("Ignoring command from non-operator sender %s", sender_id)
            return None
        handler = self._handlers.get(command.name)
        if handler is None:
            return f"Unknown command: {command.name}"
        return await handler(command)
