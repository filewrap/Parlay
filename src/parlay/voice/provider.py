"""Provider-agnostic voice interface (REQ-AIVP-006).

`ProviderSessionManager` depends on this interface, never on Gemini directly.
Swapping to another real-time model means providing a different `VoiceProvider`
implementation without touching capture or playback. A text-only provider hides
its STT/TTS behind this same PCM-in / PCM-out contract.

All PCM crossing this boundary is 16-bit little-endian, mono, at the rate the
implementation declares (`input_rate` / `output_rate`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """Raised when a provider session cannot be opened or is lost unrecoverably."""


class ResponseModality(StrEnum):
    """How the provider should return its reply for a session (REQ-AIVP-007)."""

    AUDIO = "audio"
    TEXT = "text"


@dataclass(frozen=True)
class SessionConfiguration:
    """Provider-agnostic session setup applied when the session opens.

    Carries the model, persona (`system_instruction`), `voice`, and
    `response_modality`. It is provider-agnostic on purpose: each provider maps
    these fields onto its own SDK config, and supplies its own default model
    (this type does not hard-code one).
    """

    model: str
    system_instruction: str | None = None
    voice: str | None = None
    response_modality: ResponseModality = ResponseModality.AUDIO


class ReplyEventKind(StrEnum):
    """Kinds of event a provider emits on its reply stream."""

    AUDIO = "audio"  # a chunk of reply PCM at the provider output rate
    TURN_COMPLETE = "turn_complete"  # the current reply segment finished
    INTERRUPTED = "interrupted"  # the model was interrupted; drop pending audio


@dataclass(frozen=True)
class ReplyEvent:
    """One event from a provider's reply stream.

    `pcm` carries bytes only for AUDIO events and is empty otherwise.
    """

    kind: ReplyEventKind
    pcm: bytes = b""

    @classmethod
    def audio(cls, pcm: bytes) -> ReplyEvent:
        return cls(ReplyEventKind.AUDIO, pcm)

    @classmethod
    def turn_complete(cls) -> ReplyEvent:
        return cls(ReplyEventKind.TURN_COMPLETE)

    @classmethod
    def interrupted(cls) -> ReplyEvent:
        return cls(ReplyEventKind.INTERRUPTED)


@runtime_checkable
class VoiceProvider(Protocol):
    """A real-time voice model accessed as a PCM-in / PCM-out stream.

    Lifecycle: `open()` establishes the session, `send_audio()` streams input
    PCM in low-latency chunks, `events()` yields reply audio and control
    signals until the session ends, and `close()` tears it down. Implementations
    must not persist input or reply audio (REQ-AIVP-006.3).
    """

    @property
    def input_rate(self) -> int:
        """Sample rate (Hz) of the mono PCM the provider expects as input."""
        ...

    @property
    def output_rate(self) -> int:
        """Sample rate (Hz) of the mono reply PCM the provider produces."""
        ...

    async def open(self) -> None:
        """Open the provider session. Raises ProviderError on failure."""
        ...

    async def send_audio(self, pcm: bytes) -> None:
        """Send one low-latency chunk of 16-bit LE mono input PCM."""
        ...

    def events(self) -> AsyncIterator[ReplyEvent]:
        """Yield reply events until the session closes.

        Raises ProviderError if the session is lost and cannot be resumed.
        """
        ...

    async def close(self) -> None:
        """Close the session and release provider resources."""
        ...
