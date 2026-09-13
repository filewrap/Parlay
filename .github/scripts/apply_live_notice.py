"""Idempotent anchor patch: wire an in-call notice into _cmd_live.

Adds an on_speaking handler that posts a real in-call voice-chat message when
the AI first speaks, passes it to AiVoiceProducer, and posts an engage notice
when /live starts. Uses VoiceChatController.send_call_message (no bot
callbacks). Safe to run more than once.
"""

from __future__ import annotations

import sys

PATH = "src/parlay/app.py"


def patch(text: str, old: str, new: str) -> str:
    if new in text:
        return text
    if old not in text:
        sys.exit(f"anchor not found: {old!r}")
    return text.replace(old, new, 1)


def main() -> None:
    with open(PATH, encoding="utf-8") as handle:
        text = handle.read()

    # 1) Add the speaking handler right after the lost() handler.
    old_lost = (
        '                await self._notify_operator("AI voice stopped after a provider failure.")\n'
        "\n"
        "        ws_provider = (\n"
    )
    new_lost = (
        '                await self._notify_operator("AI voice stopped after a provider failure.")\n'
        "\n"
        "        async def speaking() -> None:\n"
        "            await self.vc.send_call_message(\n"
        '                runtime.chat_id, fmt.status("AI voice is speaking.", "speaking")\n'
        "            )\n"
        "\n"
        "        ws_provider = (\n"
    )
    text = patch(text, old_lost, new_lost)

    # 2) Pass on_speaking to the producer.
    old_producer = (
        "            arbiter=runtime.arbiter,\n"
        "            on_loss=lost,\n"
        "        )\n"
    )
    new_producer = (
        "            arbiter=runtime.arbiter,\n"
        "            on_loss=lost,\n"
        "            on_speaking=speaking,\n"
        "        )\n"
    )
    text = patch(text, old_producer, new_producer)

    # 3) Post an in-call engage notice when /live starts.
    old_ret = (
        "        runtime.ai = ai\n"
        '        return fmt.success("AI voice started in this chat.")\n'
    )
    new_ret = (
        "        runtime.ai = ai\n"
        "        self._spawn(\n"
        "            self.vc.send_call_message(\n"
        '                runtime.chat_id, fmt.success("AI voice started.")\n'
        "            )\n"
        "        )\n"
        '        return fmt.success("AI voice started in this chat.")\n'
    )
    text = patch(text, old_ret, new_ret)

    with open(PATH, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("patched", PATH)


if __name__ == "__main__":
    main()
