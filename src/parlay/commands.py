"""Command registry, parsing, identity checks, and context-aware dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

CONTROL_COMMANDS = ("join", "leave", "start", "stop", "status")
MUSIC_COMMANDS = ("play", "skip", "pause", "resume", "queue")
Handler = Callable[["ParsedCommand"], Awaitable[str]]


@dataclass(frozen=True)
class ParsedCommand:
    """An authorized command and its originating Telegram chat."""

    name: str
    args: str
    sender_id: str
    chat_id: int | None = None
    is_group: bool = False
    is_channel: bool = False

    def join_target(self) -> str | int | None:
        """Explicit target wins; otherwise only use the originating group/channel."""
        if self.args:
            return int(self.args) if self.args.lstrip("-").isdigit() else self.args
        if self.is_group or self.is_channel:
            return self.chat_id
        return None


class CommandHandler:
    """Register known commands and ignore unrelated or unauthorized messages."""

    def __init__(self, operator_id: str, prefix: str = "/") -> None:
        self._operator_id = str(operator_id)
        self._account_id: str | None = None
        self._username: str | None = None
        self._prefix = prefix
        self._handlers: dict[str, Handler] = {}

    def set_operator_id(self, operator_id: str) -> None:
        self._operator_id = str(operator_id)

    def set_account(self, account_id: int, username: str | None = None) -> None:
        self._account_id = str(account_id)
        self._username = username.lower() if username else None

    def register(self, name: str, handler: Handler) -> None:
        self._handlers[name.lower()] = handler

    def is_operator(self, sender_id: object) -> bool:
        return sender_id is not None and str(sender_id) in (
            self._operator_id,
            self._account_id,
        )

    def parse(self, text: str, sender_id: object) -> ParsedCommand | None:
        stripped = text.strip()
        if not stripped.startswith(self._prefix):
            return None
        body = stripped[len(self._prefix) :]
        if not body or body[0].isspace():
            return None
        parts = body.split(maxsplit=1)
        head = parts[0].lower()
        if "@" in head:
            head, recipient = head.split("@", 1)
            if self._username is None or recipient != self._username:
                return None
        if head not in self._handlers:
            return None
        return ParsedCommand(head, parts[1].strip() if len(parts) > 1 else "", str(sender_id))

    async def dispatch(
        self,
        text: str,
        sender_id: object,
        *,
        chat_id: int | None = None,
        is_group: bool = False,
        is_channel: bool = False,
    ) -> str | None:
        if not self.is_operator(sender_id):
            return None
        command = self.parse(text, sender_id)
        if command is None:
            return None
        command = replace(command, chat_id=chat_id, is_group=is_group, is_channel=is_channel)
        return await self._handlers[command.name](command)
