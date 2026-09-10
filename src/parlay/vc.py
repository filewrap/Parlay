"""Telethon-native voice-chat control.

Every Telegram/MTProto action in this module goes through the one Telethon
client. py-tgcalls performs its own joinGroupCall signaling through the same
client (it wraps the Telethon instance Parlay passes it), so between the two,
all Telegram traffic flows over a single authorized user session.

Raw functions used (Telethon `telethon.tl.functions` / `telethon.tl.types`):
- channels.GetFullChannelRequest / messages.GetFullChatRequest
    -> `full_chat.call` is the chat's active `InputGroupCall`, or None.
- phone.GetGroupCallRequest(call, limit) -> participant count and title.
- phone.CreateGroupCallRequest(peer, random_id) -> start a voice chat.
- phone.DiscardGroupCallRequest(call) -> stop a voice chat.
- channels.GetParticipantRequest(channel, participant) -> own banned rights,
    used for the can-send check before posting into a call chat.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

from telethon.errors import ChatAdminRequiredError
from telethon.tl import functions, types

log = logging.getLogger(__name__)


class VoiceChatError(Exception):
    """A voice-chat control action failed; the message is operator-facing."""


@dataclass(frozen=True)
class VcStatus:
    """Snapshot of a chat's voice-chat state."""

    active: bool
    participants: int = 0
    title: str | None = None


class VoiceChatController:
    """Controls a chat's voice chat over Telethon: status, start, stop, send."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def resolve(self, chat: Any) -> Any:
        """Resolve a chat reference (id, username, link, entity) via Telethon."""
        if isinstance(chat, (types.Chat, types.Channel, types.User)):
            return chat
        try:
            return await self._client.get_entity(chat)
        except (ValueError, TypeError) as exc:
            raise VoiceChatError(f"Cannot resolve chat {chat!r}: {exc}") from exc

    async def get_active_call(self, chat: Any) -> Any | None:
        """Return the chat's active group call (InputGroupCall) or None."""
        entity = await self.resolve(chat)
        if isinstance(entity, types.Channel):
            full = await self._client(functions.channels.GetFullChannelRequest(entity))
        elif isinstance(entity, types.Chat):
            full = await self._client(functions.messages.GetFullChatRequest(entity.id))
        else:
            raise VoiceChatError("Voice chats exist only in groups and channels.")
        return full.full_chat.call

    async def status(self, chat: Any) -> VcStatus:
        """Report whether the chat's voice chat is live and how many are in it."""
        call = await self.get_active_call(chat)
        if call is None:
            return VcStatus(active=False)
        result = await self._client(functions.phone.GetGroupCallRequest(call=call, limit=1))
        info = result.call
        if isinstance(info, types.GroupCallDiscarded):
            return VcStatus(active=False)
        return VcStatus(
            active=True,
            participants=int(getattr(info, "participants_count", 0)),
            title=getattr(info, "title", None),
        )

    async def start(self, chat: Any) -> Any:
        """Start a voice chat in the chat and return its InputGroupCall."""
        entity = await self.resolve(chat)
        if await self.get_active_call(entity) is not None:
            raise VoiceChatError("A voice chat is already active in this chat.")
        peer = await self._client.get_input_entity(entity)
        try:
            await self._client(
                functions.phone.CreateGroupCallRequest(
                    peer=peer, random_id=secrets.randbelow(2**31)
                )
            )
        except ChatAdminRequiredError as exc:
            raise VoiceChatError("Starting a voice chat needs admin rights here.") from exc
        call = await self.get_active_call(entity)
        if call is None:
            raise VoiceChatError("Telegram did not report the new voice chat.")
        return call

    async def stop(self, chat: Any) -> None:
        """Discard the chat's active voice chat."""
        call = await self.get_active_call(chat)
        if call is None:
            raise VoiceChatError("No active voice chat to stop.")
        try:
            await self._client(functions.phone.DiscardGroupCallRequest(call=call))
        except ChatAdminRequiredError as exc:
            raise VoiceChatError("Stopping the voice chat needs admin rights here.") from exc

    async def can_send(self, chat: Any) -> bool:
        """True when this account may send messages in the chat.

        Checks our own participant banned rights first, then the chat's
        default banned rights. In broadcast channels only admins can post.
        """
        entity = await self.resolve(chat)
        if isinstance(entity, types.User):
            return True
        default = getattr(entity, "default_banned_rights", None)
        default_banned = bool(getattr(default, "send_messages", False))
        if isinstance(entity, types.Channel):
            me = await self._client.get_me(input_peer=True)
            try:
                result = await self._client(
                    functions.channels.GetParticipantRequest(channel=entity, participant=me)
                )
            except Exception:
                # Not a participant at all (e.g. a channel we merely follow).
                return False
            participant = result.participant
            if isinstance(participant, types.ChannelParticipantBanned):
                if participant.banned_rights.send_messages:
                    return False
                return not default_banned
            if isinstance(
                participant,
                (types.ChannelParticipantAdmin, types.ChannelParticipantCreator),
            ):
                return True
            if getattr(entity, "broadcast", False):
                return False
            return not default_banned
        return not default_banned

    async def send_message(self, chat: Any, text: str) -> Any:
        """Send a message into the chat after verifying we are allowed to."""
        if not await self.can_send(chat):
            raise VoiceChatError("Parlay is not allowed to send messages in this chat.")
        entity = await self.resolve(chat)
        return await self._client.send_message(entity, text)
