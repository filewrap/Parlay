"""Membership event types for the Group Membership Audit.

A `MembershipEvent` is the single record produced whenever Parlay's own account
is added to or removed from a chat (REQ-MEM-001). It is provider-agnostic: the
watcher builds it from tolerant attribute reads on the update object, so nothing
here depends on Telethon types and the whole module is unit testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class MembershipAction(StrEnum):
    """Whether the account was added to or removed from a chat."""

    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True)
class Actor:
    """The party that added the account, when it can be determined.

    `unknown()` marks an adder that the event did not carry (AC-MEM-001.2).
    """

    user_id: str | None
    name: str | None = None

    @classmethod
    def unknown(cls) -> Actor:
        return cls(user_id=None, name=None)

    @property
    def is_known(self) -> bool:
        return self.user_id is not None

    @property
    def display(self) -> str:
        """Human-readable label for notifications and the log."""
        if not self.is_known:
            return "an unknown user"
        if self.name:
            return f"{self.name} ({self.user_id})"
        return f"user {self.user_id}"


@dataclass(frozen=True)
class Chat:
    """The chat the account was added to or removed from."""

    chat_id: str
    title: str | None = None

    @property
    def display(self) -> str:
        if self.title:
            return f"{self.title} ({self.chat_id})"
        return f"chat {self.chat_id}"


@dataclass(frozen=True)
class MembershipEvent:
    """An add/remove of Parlay's account, with chat, adder, and time."""

    action: MembershipAction
    chat: Chat
    adder: Actor
    at: datetime

    @classmethod
    def added(cls, chat: Chat, adder: Actor, at: datetime | None = None) -> MembershipEvent:
        return cls(MembershipAction.ADDED, chat, adder, at or datetime.now(UTC))

    @classmethod
    def removed(cls, chat: Chat, at: datetime | None = None) -> MembershipEvent:
        # A removal has no meaningful adder; record the actor as unknown.
        return cls(MembershipAction.REMOVED, chat, Actor.unknown(), at or datetime.now(UTC))

    def as_record(self) -> dict[str, str | None]:
        """Flat, JSON-serializable form for the Audit Log."""
        return {
            "action": str(self.action),
            "chat_id": self.chat.chat_id,
            "chat_title": self.chat.title,
            "adder_id": self.adder.user_id,
            "adder_name": self.adder.name,
            "at": self.at.isoformat(),
        }
