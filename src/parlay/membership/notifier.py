"""MembershipNotifier: privately alert the Operator about membership changes.

The notifier sends a message only to the Operator's private channel and never
into the chat that triggered the event (ADR-001 / AC-MEM-002.3). It is given a
`post_private` callable so it stays decoupled from Telethon and testable: the
app supplies one that sends to the Operator's own id.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .. import presentation as fmt
from .events import MembershipAction, MembershipEvent

log = logging.getLogger(__name__)

# Delivers one private message to the Operator.
PrivateSender = Callable[[str], Awaitable[None]]


class MembershipNotifier:
    """Sends the Operator a private notification for each membership event."""

    def __init__(self, send_private: PrivateSender) -> None:
        self._send_private = send_private

    async def __call__(self, event: MembershipEvent) -> None:
        await self.notify(event)

    async def notify(self, event: MembershipEvent) -> None:
        """Deliver the private alert naming the chat and adder (REQ-MEM-002)."""
        if event.action is MembershipAction.ADDED:
            if event.adder.is_known:
                text = fmt.info(
                    f"Your account was added to {event.chat.display} by {event.adder.display}."
                )
            else:
                # AC-MEM-002.2: state the adder could not be determined.
                text = fmt.warning(
                    f"Your account was added to {event.chat.display}, "
                    "but the adder could not be determined."
                )
        else:
            text = fmt.info(f"Your account left or was removed from {event.chat.display}.")
        try:
            await self._send_private(text)
        except Exception:
            log.exception("failed to send membership notification to Operator")
