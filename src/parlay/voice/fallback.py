"""FallbackVoiceProvider: try a primary provider, fall back to a secondary.

The live engine prefers the URL/WebSocket transport (ephemeral token) but keeps
the SDK transport (API key) as a safety net. This wrapper delegates the whole
VoiceProvider contract to whichever provider opens successfully: it calls the
primary's open() first and, only if that raises ProviderError, tries the
secondary. Once a session is open the choice is fixed for its lifetime; the two
transports are never swapped mid-session.

Legs whose credentials are missing are passed as None and skipped, so a
deployment that configures only one transport still gets a working provider (and
a clear ProviderError if that one fails).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from .provider import ProviderError, ReplyEvent, VoiceProvider

log = logging.getLogger(__name__)


class FallbackVoiceProvider:
    """Delegates to the first of several providers whose open() succeeds."""

    def __init__(self, primary: VoiceProvider | None, secondary: VoiceProvider | None) -> None:
        self._candidates = [p for p in (primary, secondary) if p is not None]
        if not self._candidates:
            raise ProviderError("no voice transport is configured")
        self._active: VoiceProvider | None = None

    @property
    def input_rate(self) -> int:
        return self._require().input_rate

    @property
    def output_rate(self) -> int:
        return self._require().output_rate

    def _require(self) -> VoiceProvider:
        if self._active is None:
            raise ProviderError("voice provider is not open")
        return self._active

    async def open(self) -> None:
        errors: list[str] = []
        for index, candidate in enumerate(self._candidates):
            try:
                await candidate.open()
            except ProviderError as exc:
                errors.append(str(exc))
                log.warning("voice transport %d failed to open: %s", index + 1, exc)
                continue
            self._active = candidate
            return
        raise ProviderError("all voice transports failed to open: " + "; ".join(errors))

    async def send_audio(self, pcm: bytes) -> None:
        await self._require().send_audio(pcm)

    async def send_context(self, text: str) -> None:
        """Delegate context injection to the active provider if it supports it."""
        active = self._require()
        send = getattr(active, "send_context", None)
        if send is not None:
            await send(text)

    def events(self) -> AsyncIterator[ReplyEvent]:
        return self._require().events()

    async def close(self) -> None:
        active, self._active = self._active, None
        if active is not None:
            await active.close()
