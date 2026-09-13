"""Idempotent patcher for app.py: instant /tem, _engage_ai helper, self-join filter.

Run once in CI, then deleted. Uses regex on method boundaries so it does not
depend on the internal whitespace of the target methods. Generated methods use
comments instead of docstrings so this file stays free of embedded triple
quotes and passes ruff format.
"""

from __future__ import annotations

import pathlib
import re
import sys

PATH = pathlib.Path("src/parlay/app.py")
text = PATH.read_text()

if "_engage_ai" in text:
    print("apply_live_features: already applied; nothing to do")
    sys.exit(0)

NEW_LIVE = """    async def _cmd_live(self, command: ParsedCommand) -> str:
        if not self.config.gemini_api_key and not self.config.gemini_live_token_url:
            return fmt.error(
                "AI voice is disabled. Configure GEMINI_API_KEY or "
                "GEMINI_LIVE_TOKEN_URL to enable it."
            )
        runtime = self.registry.get(command.chat_id or 0)
        if runtime is None:
            return fmt.error("Connect Parlay here with /join before starting AI voice.")
        if runtime.ai is not None:
            return fmt.warning("AI is already running here.")
        runtime.sessions.engage_ai()
        config = self._build_session_config(runtime.chat_id)
        try:
            await self._engage_ai(runtime, config)
        except BaseException:
            runtime.sessions.disengage_ai()
            raise
        self._spawn(self.vc.send_call_message(runtime.chat_id, fmt.success("AI voice started.")))
        return fmt.success("AI voice started in this chat.")

    def _build_session_config(self, chat_id: int) -> SessionConfiguration:
        # Build the provider session config for a chat's selected voice preset.
        default = default_configuration()
        template = get_template(self._templates.get(chat_id, DEFAULT_TEMPLATE_INDEX))
        return SessionConfiguration(
            model=self.config.gemini_model or default.model,
            system_instruction=template.system_instruction(),
            voice=template.voice,
            response_modality=default.response_modality,
        )

    async def _engage_ai(self, runtime: Any, config: SessionConfiguration) -> None:
        # Build the voice provider stack, engage it, and start muted. Shared by
        # /live (first start) and /tem (live voice switch). The caller owns
        # session-state bookkeeping and messaging and cleans up on failure.

        async def lost(reason: str) -> None:
            if self.registry.get(runtime.chat_id) is runtime:
                runtime.ai = None
                if runtime.sessions.active:
                    runtime.sessions.disengage_ai()
                await self._notify_operator("AI voice stopped after a provider failure.")

        async def speaking() -> None:
            await self.vc.send_call_message(
                runtime.chat_id, fmt.status("AI voice is speaking.", "speaking")
            )

        async def reply_start() -> None:
            # Unmute so the AI reply is transmitted while it speaks.
            await runtime.bridge.unmute()

        async def reply_end() -> None:
            # Mute between turns so participants do not hear dead air.
            await runtime.bridge.mute()

        ws_provider = (
            GeminiLiveSocket(
                EphemeralTokenSource(
                    self.config.gemini_live_token_url,
                    ttl_s=self.config.gemini_live_token_ttl,
                ),
                config,
            )
            if self.config.gemini_live_token_url
            else None
        )
        sdk_provider = (
            GeminiVoiceProvider(self.config.gemini_api_key, config)
            if self.config.gemini_api_key
            else None
        )
        ai = AiVoiceProducer(
            FallbackVoiceProvider(ws_provider, sdk_provider),
            source=runtime.bridge,
            arbiter=runtime.arbiter,
            on_loss=lost,
            on_speaking=speaking,
            on_reply_start=reply_start,
            on_reply_end=reply_end,
        )
        await ai.engage()
        runtime.ai = ai
        # Start muted; the reply-start hook unmutes only while the AI speaks.
        self._spawn(runtime.bridge.mute())
"""

NEW_TEM = """    async def _cmd_tem(self, command: ParsedCommand) -> str:
        chat_id = command.chat_id or 0
        arg = command.args.strip()
        if not arg:
            current = self._templates.get(chat_id, DEFAULT_TEMPLATE_INDEX)
            return fmt.status(
                f"Voice templates (current: {current}):\\n{describe_templates()}\\n"
                f"Switch with /tem <1-{template_count()}>.",
                "info",
            )
        if not arg.isdigit() or not 1 <= int(arg) <= template_count():
            return fmt.error(f"Pick a template from 1 to {template_count()}.")
        index = int(arg)
        self._templates[chat_id] = index
        template = get_template(index)
        runtime = self.registry.get(chat_id)
        if runtime is None or runtime.ai is None:
            return fmt.success(
                f"Voice template {index} set: {template.label} ({template.voice})."
            )
        # AI is live: switch the voice instantly by reopening the session with the
        # new config. Gemini fixes the voice at setup, so a clean reopen is the
        # only way to change it; the session-engaged flag stays set throughout.
        await self.vc.send_call_message(
            chat_id,
            fmt.status(f"Switching voice to {template.label} ({template.voice})...", "info"),
        )
        config = self._build_session_config(chat_id)
        old = runtime.ai
        runtime.ai = None
        try:
            await old.disengage()
            await self._engage_ai(runtime, config)
        except BaseException:
            runtime.ai = None
            if runtime.sessions.active:
                runtime.sessions.disengage_ai()
            await self._notify_operator("AI voice failed while switching voice.")
            return fmt.error("Could not switch the voice; AI stopped. Start again with /live.")
        return fmt.success(f"Voice switched to {template.label} ({template.voice}).")
"""

# _cmd_tem holds a reference to `runtime` across an await inside a try/except.
# mypy cannot preserve the not-None narrowing from the early-return guard across
# that boundary, so bind a locally-typed alias it can follow.
NEW_TEM = NEW_TEM.replace(
    "        config = self._build_session_config(chat_id)\n        old = runtime.ai\n",
    "        session_runtime = runtime\n"
    "        config = self._build_session_config(chat_id)\n"
    "        old = session_runtime.ai\n",
).replace(
    "        runtime.ai = None\n        try:\n            await old.disengage()\n"
    "            await self._engage_ai(runtime, config)\n",
    "        session_runtime.ai = None\n        try:\n            await old.disengage()\n"
    "            await self._engage_ai(session_runtime, config)\n",
).replace(
    "        except BaseException:\n            runtime.ai = None\n"
    "            if runtime.sessions.active:\n                runtime.sessions.disengage_ai()\n",
    "        except BaseException:\n            session_runtime.ai = None\n"
    "            if session_runtime.sessions.active:\n"
    "                session_runtime.sessions.disengage_ai()\n",
)

live_pat = re.compile(
    r"    async def _cmd_live\(self, command: ParsedCommand\) -> str:\n"
    r".*?(?=\n    async def _cmd_tem\(self, command: ParsedCommand\) -> str:)",
    re.DOTALL,
)
if not live_pat.search(text):
    print("apply_live_features: _cmd_live anchor not found")
    sys.exit(1)
text = live_pat.sub(lambda m: NEW_LIVE, text, count=1)

tem_pat = re.compile(
    r"    async def _cmd_tem\(self, command: ParsedCommand\) -> str:\n"
    r".*?(?=\n    def _on_call_participant\(self, chat_id: int, action: str, user_id: int\) -> None:)",
    re.DOTALL,
)
if not tem_pat.search(text):
    print("apply_live_features: _cmd_tem anchor not found")
    sys.exit(1)
text = tem_pat.sub(lambda m: NEW_TEM, text, count=1)

old_join = (
    "    async def _announce_join(self, chat_id: int, user_id: int) -> None:\n"
    "        name = await self._display_name(user_id, chat_id)"
)
new_join = (
    "    async def _announce_join(self, chat_id: int, user_id: int) -> None:\n"
    "        if self._account_id is not None and user_id == self._account_id:\n"
    "            return  # never announce the userbot's own join\n"
    "        name = await self._display_name(user_id, chat_id)"
)
if old_join not in text:
    print("apply_live_features: _announce_join anchor not found")
    sys.exit(1)
text = text.replace(old_join, new_join, 1)

PATH.write_text(text)
print("apply_live_features: patched app.py")
