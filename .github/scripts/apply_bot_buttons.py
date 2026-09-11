"""Idempotent, anchor-based patch that adds playback control buttons to bot.py."""

from __future__ import annotations

import sys
from pathlib import Path

PATH = Path("src/parlay/bot.py")
text = PATH.read_text()

if "_control_buttons" in text:
    print("already applied")
    sys.exit(0)

replacements: list[tuple[str, str]] = []

# 1) /play now attaches control buttons instead of only Open room.
old_play = (
    '        room = await self.play(sender.id, chat_id, args)\n'
    '        await event.reply(\n'
    '            "Playback updated.",\n'
    '            buttons=Button.url("Open room", self.room_url(str(room["id"]))),\n'
    '        )\n'
)
new_play = (
    '        room = await self.play(sender.id, chat_id, args)\n'
    '        buttons = await self._control_buttons(sender.id, str(room["id"]))\n'
    '        await event.reply("Playback updated.", buttons=buttons)\n'
)
replacements.append((old_play, new_play))

# 2) Route the new control callback kind.
old_dispatch = (
    '            elif record["kind"] == "reentry":\n'
    '                await self._approve_reentry(event, token, record)\n'
)
new_dispatch = (
    '            elif record["kind"] == "reentry":\n'
    '                await self._approve_reentry(event, token, record)\n'
    '            elif record["kind"] == "control":\n'
    '                await self._control(event, token, record)\n'
)
replacements.append((old_dispatch, new_dispatch))

# 3) Add the button builder and control handler ahead of _authorized.
anchor = '    async def _authorized(self, user_id: int, chat_id: int) -> bool:\n'
methods = (
    '    async def _control_buttons(self, owner_id: int, room_id: str) -> list[list[Any]]:\n'
    '        """Build owner-bound pause/resume/skip controls plus an Open room link."""\n'
    '        controls = (("Pause", "pause"), ("Resume", "resume"), ("Skip", "skip"))\n'
    '        row = []\n'
    '        for label, verb in controls:\n'
    '            token = await asyncio.to_thread(\n'
    '                self._state.callback,\n'
    '                owner_id,\n'
    '                "control",\n'
    '                {"room_id": room_id, "verb": verb},\n'
    '            )\n'
    '            row.append(Button.inline(label, self._callback_data(token)))\n'
    '        return [row, [Button.url("Open room", self.room_url(room_id))]]\n'
    '\n'
    '    async def _control(self, event: Any, token: str, record: dict[str, Any]) -> None:\n'
    '        """Issue a room playback action for the pressing owner at the current revision."""\n'
    '        payload = record["payload"]\n'
    '        room_id = str(payload["room_id"])\n'
    '        verb = str(payload["verb"])\n'
    '        snapshot = await self.rooms.snapshot(room_id, record["owner_id"])\n'
    '        await self.rooms.action(\n'
    '            room_id,\n'
    '            record["owner_id"],\n'
    '            f"bot-control:{token}",\n'
    '            int(snapshot["revision"]),\n'
    '            verb,\n'
    '            {},\n'
    '        )\n'
    '        await event.answer(f"{verb.capitalize()} sent.")\n'
    '\n'
)
replacements.append((anchor, methods + anchor))

for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"anchor not unique (count={count}): {old[:60]!r}")
    text = text.replace(old, new)

PATH.write_text(text)
print("applied bot button patch")
