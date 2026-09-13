"""Idempotent anchor patch: wire the URL live transport into config + /live."""

from __future__ import annotations

import sys
from pathlib import Path

APP = Path("src/parlay/app.py")
CFG = Path("src/parlay/config.py")

APP_IMPORT_OLD = "from .voice.provider import SessionConfiguration\n"
APP_IMPORT_NEW = (
    "from .voice.fallback import FallbackVoiceProvider\n"
    "from .voice.gemini_ws import GeminiLiveSocket\n"
    "from .voice.live_token import EphemeralTokenSource\n"
    "from .voice.provider import SessionConfiguration\n"
)

APP_STATE_OLD = '        ai_state = "ready" if self.config.gemini_api_key else "not configured"\n'
APP_STATE_NEW = (
    "        ai_state = (\n"
    '            "ready"\n'
    "            if self.config.gemini_api_key or self.config.gemini_live_token_url\n"
    '            else "not configured"\n'
    "        )\n"
)

APP_GUARD_OLD = (
    "    async def _cmd_live(self, command: ParsedCommand) -> str:\n"
    "        if not self.config.gemini_api_key:\n"
    '            return fmt.error("AI voice is disabled. Configure GEMINI_API_KEY to enable it.")\n'
)
APP_GUARD_NEW = (
    "    async def _cmd_live(self, command: ParsedCommand) -> str:\n"
    "        if not self.config.gemini_api_key and not self.config.gemini_live_token_url:\n"
    "            return fmt.error(\n"
    '                "AI voice is disabled. Configure GEMINI_API_KEY or "\n'
    '                "GEMINI_LIVE_TOKEN_URL to enable it."\n'
    "            )\n"
)

APP_PROVIDER_OLD = (
    "        ai = AiVoiceProducer(\n"
    "            GeminiVoiceProvider(self.config.gemini_api_key, config),\n"
    "            source=runtime.bridge,\n"
    "            arbiter=runtime.arbiter,\n"
    "            on_loss=lost,\n"
    "        )\n"
)
APP_PROVIDER_NEW = (
    "        ws_provider = (\n"
    "            GeminiLiveSocket(\n"
    "                EphemeralTokenSource(\n"
    "                    self.config.gemini_live_token_url,\n"
    "                    ttl_s=self.config.gemini_live_token_ttl,\n"
    "                ),\n"
    "                config,\n"
    "            )\n"
    "            if self.config.gemini_live_token_url\n"
    "            else None\n"
    "        )\n"
    "        sdk_provider = (\n"
    "            GeminiVoiceProvider(self.config.gemini_api_key, config)\n"
    "            if self.config.gemini_api_key\n"
    "            else None\n"
    "        )\n"
    "        ai = AiVoiceProducer(\n"
    "            FallbackVoiceProvider(ws_provider, sdk_provider),\n"
    "            source=runtime.bridge,\n"
    "            arbiter=runtime.arbiter,\n"
    "            on_loss=lost,\n"
    "        )\n"
)

CFG_FIELD_OLD = "    youtube_api_key: str | None = None\n"
CFG_FIELD_NEW = (
    "    youtube_api_key: str | None = None\n"
    "    gemini_live_token_url: str | None = None\n"
    "    gemini_live_token_ttl: float = 1500.0\n"
)

CFG_LOAD_OLD = '        youtube_api_key=_optional("YOUTUBE_API_KEY"),\n'
CFG_LOAD_NEW = (
    '        youtube_api_key=_optional("YOUTUBE_API_KEY"),\n'
    '        gemini_live_token_url=_optional("GEMINI_LIVE_TOKEN_URL"),\n'
    '        gemini_live_token_ttl=float(_optional("GEMINI_LIVE_TOKEN_TTL") or 1500.0),\n'
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
    patch(APP, APP_IMPORT_OLD, APP_IMPORT_NEW)
    patch(APP, APP_STATE_OLD, APP_STATE_NEW)
    patch(APP, APP_GUARD_OLD, APP_GUARD_NEW)
    patch(APP, APP_PROVIDER_OLD, APP_PROVIDER_NEW)
    patch(CFG, CFG_FIELD_OLD, CFG_FIELD_NEW)
    patch(CFG, CFG_LOAD_OLD, CFG_LOAD_NEW)


if __name__ == "__main__":
    main()
