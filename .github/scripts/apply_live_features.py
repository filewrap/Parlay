"""One-shot anchor patch: wire the new live-AI features into app.py/runtime.py.

Each edit is idempotent: if the replacement text is already present the edit is
skipped, and a missing anchor is a hard error so a stale script fails loudly
instead of silently doing nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path


def patch(text: str, old: str, new: str, *, label: str) -> str:
    if new in text:
        print(f"skip (already applied): {label}")
        return text
    if old not in text:
        print(f"ANCHOR NOT FOUND: {label}")
        sys.exit(1)
    print(f"applied: {label}")
    return text.replace(old, new, 1)


# --- runtime.py ------------------------------------------------------------
runtime_path = Path("src/parlay/runtime.py")
rt = runtime_path.read_text()

rt = patch(
    rt,
    '        self._store = _SnapshotStore(getattr(config, "activity_db_path", None))\n'
    "        self._callback_tasks: set[asyncio.Task[None]] = set()\n",
    '        self._store = _SnapshotStore(getattr(config, "activity_db_path", None))\n'
    "        self._callback_tasks: set[asyncio.Task[None]] = set()\n"
    "        # Optional parent hooks bound to a chat id in _build: in-call\n"
    "        # participant (join/leave) and active-speaker events.\n"
    "        self.on_participant: Callable[[int, str, int], None] | None = None\n"
    "        self.on_speaker: Callable[[int, int], None] | None = None\n",
    label="runtime: registry hook attributes",
)


rt = patch(
    rt,
    "        bridge = RawAudioBridge(self._client, on_disconnect=disconnected)\n",
    "        def participant(action: str, user_id: int) -> None:\n"
    "            if self.on_participant is not None:\n"
    "                self.on_participant(chat_id, action, user_id)\n"
    "\n"
    "        def speaker(user_id: int) -> None:\n"
    "            if self.on_speaker is not None:\n"
    "                self.on_speaker(chat_id, user_id)\n"
    "\n"
    "        bridge = RawAudioBridge(\n"
    "            self._client,\n"
    "            on_disconnect=disconnected,\n"
    "            on_participant=participant,\n"
    "            on_speaker=speaker,\n"
    "        )\n",
    label="runtime: pass participant/speaker to bridge",
)

runtime_path.write_text(rt)


# --- app.py ----------------------------------------------------------------
app_path = Path("src/parlay/app.py")
ap = app_path.read_text()

ap = patch(
    ap,
    "from .voice.provider import SessionConfiguration\n",
    "from .voice.provider import SessionConfiguration\n"
    "from .voice.templates import (\n"
    "    DEFAULT_TEMPLATE_INDEX,\n"
    "    describe_templates,\n"
    "    get_template,\n"
    "    template_count,\n"
    ")\n",
    label="app: import templates",
)

ap = patch(
    ap,
    '            "queue",\n        ):\n',
    '            "queue",\n            "tem",\n        ):\n',
    label="app: register /tem",
)


ap = patch(
    ap,
    "        self._people_locks: dict[tuple[int, int], asyncio.Lock] = {}\n",
    "        self._people_locks: dict[tuple[int, int], asyncio.Lock] = {}\n"
    "        self._templates: dict[int, int] = {}\n"
    "        self.registry.on_participant = self._on_call_participant\n"
    "        self.registry.on_speaker = self._on_call_speaker\n",
    label="app: template state and registry hooks",
)

ap = patch(
    ap,
    "        runtime.sessions.engage_ai()\n"
    "        default = default_configuration()\n"
    "        config = SessionConfiguration(\n"
    "            model=self.config.gemini_model or default.model,\n"
    "            system_instruction=self.config.gemini_persona or default.system_instruction,\n"
    "            voice=self.config.gemini_voice or default.voice,\n"
    "            response_modality=default.response_modality,\n"
    "        )\n",
    "        runtime.sessions.engage_ai()\n"
    "        default = default_configuration()\n"
    "        template = get_template(self._templates.get(runtime.chat_id, DEFAULT_TEMPLATE_INDEX))\n"
    "        config = SessionConfiguration(\n"
    "            model=self.config.gemini_model or default.model,\n"
    "            system_instruction=template.system_instruction(),\n"
    "            voice=template.voice,\n"
    "            response_modality=default.response_modality,\n"
    "        )\n",
    label="app: build config from template",
)


ap = patch(
    ap,
    "        async def speaking() -> None:\n"
    "            await self.vc.send_call_message(\n"
    '                runtime.chat_id, fmt.status("AI voice is speaking.", "speaking")\n'
    "            )\n",
    "        async def speaking() -> None:\n"
    "            await self.vc.send_call_message(\n"
    '                runtime.chat_id, fmt.status("AI voice is speaking.", "speaking")\n'
    "            )\n"
    "\n"
    "        async def reply_start() -> None:\n"
    "            # Unmute so the AI reply is transmitted while it speaks.\n"
    "            await runtime.bridge.unmute()\n"
    "\n"
    "        async def reply_end() -> None:\n"
    "            # Mute between turns so participants do not hear dead air.\n"
    "            await runtime.bridge.mute()\n",
    label="app: reply-boundary mute hooks",
)


ap = patch(
    ap,
    "        ai = AiVoiceProducer(\n"
    "            FallbackVoiceProvider(ws_provider, sdk_provider),\n"
    "            source=runtime.bridge,\n"
    "            arbiter=runtime.arbiter,\n"
    "            on_loss=lost,\n"
    "            on_speaking=speaking,\n"
    "        )\n",
    "        ai = AiVoiceProducer(\n"
    "            FallbackVoiceProvider(ws_provider, sdk_provider),\n"
    "            source=runtime.bridge,\n"
    "            arbiter=runtime.arbiter,\n"
    "            on_loss=lost,\n"
    "            on_speaking=speaking,\n"
    "            on_reply_start=reply_start,\n"
    "            on_reply_end=reply_end,\n"
    "        )\n",
    label="app: pass reply hooks to producer",
)

ap = patch(
    ap,
    "        runtime.ai = ai\n"
    '        self._spawn(self.vc.send_call_message(runtime.chat_id, fmt.success("AI voice started.")))\n'
    '        return fmt.success("AI voice started in this chat.")\n',
    "        runtime.ai = ai\n"
    "        # Start muted; the reply-start hook unmutes only while the AI speaks.\n"
    "        self._spawn(runtime.bridge.mute())\n"
    '        self._spawn(self.vc.send_call_message(runtime.chat_id, fmt.success("AI voice started.")))\n'
    '        return fmt.success("AI voice started in this chat.")\n'
    "\n"
    "    async def _cmd_tem(self, command: ParsedCommand) -> str:\n"
    "        chat_id = command.chat_id or 0\n"
    "        arg = command.args.strip()\n"
    "        if not arg:\n"
    "            current = self._templates.get(chat_id, DEFAULT_TEMPLATE_INDEX)\n"
    "            return fmt.status(\n"
    '                f"Voice templates (current: {current}):\\n{describe_templates()}\\n"\n'
    '                f"Switch with /tem <1-{template_count()}>.",\n'
    '                "info",\n'
    "            )\n"
    "        if not arg.isdigit() or not 1 <= int(arg) <= template_count():\n"
    '            return fmt.error(f"Pick a template from 1 to {template_count()}.")\n'
    "        index = int(arg)\n"
    "        self._templates[chat_id] = index\n"
    "        template = get_template(index)\n"
    "        runtime = self.registry.get(chat_id)\n"
    '        note = ""\n'
    "        if runtime is not None and runtime.ai is not None:\n"
    '            note = " Restart AI with /stop then /live to apply it."\n'
    "        return fmt.success(\n"
    '            f"Voice template {index} set: {template.label} ({template.voice}).{note}"\n'
    "        )\n"
    "\n"
    "    def _on_call_participant(self, chat_id: int, action: str, user_id: int) -> None:\n"
    '        if action == "joined":\n'
    "            self._spawn(self._announce_join(chat_id, user_id))\n"
    "\n"
    "    def _on_call_speaker(self, chat_id: int, user_id: int) -> None:\n"
    "        self._spawn(self._note_speaker(chat_id, user_id))\n"
    "\n"
    "    async def _announce_join(self, chat_id: int, user_id: int) -> None:\n"
    "        name = await self._display_name(user_id, chat_id)\n"
    "        await self.vc.send_call_message(\n"
    '            chat_id, fmt.status(f"{name} joined the voice chat.", "connected")\n'
    "        )\n"
    "        runtime = self.registry.get(chat_id)\n"
    "        if runtime is not None and runtime.ai is not None:\n"
    '            runtime.ai.note_context(f"{name} abhi voice chat mein aaye hain.")\n'
    "\n"
    "    async def _note_speaker(self, chat_id: int, user_id: int) -> None:\n"
    "        runtime = self.registry.get(chat_id)\n"
    "        if runtime is None or runtime.ai is None:\n"
    "            return\n"
    "        name = await self._display_name(user_id, chat_id)\n"
    "        runtime.ai.note_speaker(name)\n"
    "\n"
    "    async def _display_name(self, user_id: int, chat_id: int) -> str:\n"
    "        candidates = [self.client]\n"
    "        if self.bot is not None and self.bot.client is not None:\n"
    "            candidates.append(self.bot.client)\n"
    "        for client in candidates:\n"
    "            try:\n"
    "                user = await client.get_entity(user_id)\n"
    "            except Exception:\n"
    "                continue\n"
    '            for attr in ("first_name", "username", "title"):\n'
    "                value = getattr(user, attr, None)\n"
    "                if value:\n"
    "                    return str(value)\n"
    "        return str(user_id)\n",
    label="app: /tem command and participant/speaker handlers",
)

app_path.write_text(ap)


# --- tests/test_runtime.py -------------------------------------------------
test_path = Path("tests/test_runtime.py")
tp = test_path.read_text()

tp = patch(
    tp,
    "    def __init__(self, client: object, on_disconnect=None) -> None:\n",
    "    def __init__(\n"
    "        self,\n"
    "        client: object,\n"
    "        on_disconnect=None,\n"
    "        on_participant=None,\n"
    "        on_speaker=None,\n"
    "    ) -> None:\n",
    label="test: FakeBridge accepts participant/speaker hooks",
)

test_path.write_text(tp)
print("done")
