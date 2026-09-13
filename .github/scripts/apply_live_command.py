"""Idempotent, anchor-based patch applied in CI.

1. Userbot: move AI voice engage from /start to a new /live command; /start now
   returns a short 'configured and running' status message.
2. Companion bot: refresh the /help text (mentions /live) and attach an inline
   button menu to both /start and /help. Inline buttons come only from the bot;
   the userbot is a user account and cannot send them.

Applied as string replacements so the large app.py/bot.py are not rebuilt
byte-for-byte.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP = Path("src/parlay/app.py")
BOT = Path("src/parlay/bot.py")

# --- app.py: register the new /live command ---
APP_REG_OLD = '            "start",\n            "stop",\n'
APP_REG_NEW = '            "start",\n            "live",\n            "stop",\n'

# --- app.py: /start becomes status, AI logic moves to /live ---
APP_START_OLD = (
    '    async def _cmd_start(self, command: ParsedCommand) -> str:\n'
    '        if not self.config.gemini_api_key:\n'
    '            return fmt.error("AI voice is disabled. Configure GEMINI_API_KEY to enable it.")\n'
)
APP_START_NEW = (
    '    async def _cmd_start(self, command: ParsedCommand) -> str:\n'
    '        ai_state = "ready" if self.config.gemini_api_key else "not configured"\n'
    '        return fmt.status(\n'
    '            "Parlay is configured and running. "\n'
    '            "Commands: /join to connect to the voice chat, /play to play music, "\n'
    '            "/live to chat with the AI, /stop to stop. "\n'
    '            f"AI voice is {ai_state}.",\n'
    '            "connected",\n'
    '        )\n'
    '\n'
    '    async def _cmd_live(self, command: ParsedCommand) -> str:\n'
    '        if not self.config.gemini_api_key:\n'
    '            return fmt.error("AI voice is disabled. Configure GEMINI_API_KEY to enable it.")\n'
)

# --- bot.py: refreshed help text ---
BOT_HELP_OLD = (
    '_HELP = (\n'
    '    "Parlay companion commands:\\n"\n'
    '    "/room [duration] - create a personal room (default 2h; 5m to 24h)\\n"\n'
    '    "/play <query> - play in this Telegram group\\n"\n'
    '    "/compass on|off|reset|delete|count [1-10]|suggestions\\n"\n'
    '    "/start - allow private bot delivery\\n"\n'
    '    "/help - show this help"\n'
    ')'
)
BOT_HELP_NEW = (
    '_HELP = (\n'
    '    "Parlay companion commands:\\n"\n'
    '    "/room [duration] - create a personal room (default 2h; 5m to 24h)\\n"\n'
    '    "/play <query> - play a track in your Telegram group\\n"\n'
    '    "/live - chat live with the AI in the group voice chat\\n"\n'
    '    "/compass on|off|reset|delete|count [1-10]|suggestions\\n"\n'
    '    "/start - allow private bot delivery\\n"\n'
    '    "/help - show this help"\n'
    ')'
)

# --- bot.py: attach a button menu to /start ---
BOT_START_OLD = (
    '        await asyncio.to_thread(self._state.set_eligible, sender.id, True)\n'
    '        await event.reply("Private delivery is available. Compass stays off until /compass on.")\n'
)
BOT_START_NEW = (
    '        await asyncio.to_thread(self._state.set_eligible, sender.id, True)\n'
    '        await event.reply(\n'
    '            "Private delivery is available. Compass stays off until /compass on.",\n'
    '            buttons=self._menu_buttons(),\n'
    '        )\n'
)

# --- bot.py: buttons on /help + the _menu_buttons helper ---
BOT_HELP_FN_OLD = (
    '    async def _command_help(self, event: Any, args: str) -> None:\n'
    '        await event.reply(_HELP)\n'
)
BOT_HELP_FN_NEW = (
    '    async def _command_help(self, event: Any, args: str) -> None:\n'
    '        await event.reply(_HELP, buttons=self._menu_buttons())\n'
    '\n'
    '    def _menu_buttons(self) -> list[list[Any]]:\n'
    '        """Inline menu for /start and /help (bot-only; user accounts cannot send buttons)."""\n'
    '        rows: list[list[Any]] = [[Button.switch_inline("Play music", "play ", same_peer=True)]]\n'
    '        url = str(getattr(self.config, "mini_app_url", "") or "")\n'
    '        if url.startswith("https://"):\n'
    '            rows.append([Button.url("Open Parlay", url)])\n'
    '        return rows\n'
)


def patch(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"{path}: already patched for a segment")
        return
    if old not in text:
        sys.exit(f"{path}: anchor not found:\n{old}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"{path}: patched a segment")


def main() -> None:
    patch(APP, APP_REG_OLD, APP_REG_NEW)
    patch(APP, APP_START_OLD, APP_START_NEW)
    patch(BOT, BOT_HELP_OLD, BOT_HELP_NEW)
    patch(BOT, BOT_START_OLD, BOT_START_NEW)
    patch(BOT, BOT_HELP_FN_OLD, BOT_HELP_FN_NEW)


if __name__ == "__main__":
    main()
