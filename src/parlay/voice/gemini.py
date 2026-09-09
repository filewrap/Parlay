"""GeminiVoiceProvider: VoiceProvider over the Gemini Live API.

This is the ONLY module that touches the `google-genai` SDK. It is isolated the
same way `audio/rawcall.py` isolates ntgcalls: the SDK is imported lazily inside
`open()`, so the pure-Python core and its tests never require the package to be
installed.

Gemini Live contract (google-genai async client):
  * Input: 16-bit LE mono PCM, natively 16 kHz, sent with
    `session.send_realtime_input(audio=Blob(data=..., mime_type="audio/pcm;rate=16000"))`.
  * Output: 16-bit LE mono PCM at 24 kHz, arriving as `server_content.model_turn`
    inline_data parts, with `turn_complete` and `interrupted` control flags.
  * Long sessions: `session_resumption` yields a handle we persist and reuse to
    re-establish the session on time-limit / `go_away` / drop (REQ-AIVP-005).

The session is set up from a `SessionConfiguration` (model, persona, voice,
response modality). When none is supplied the provider applies its own
native-audio audio-response default (REQ-AIVP-007).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from .provider import (
    ProviderError,
    ReplyEvent,
    ResponseModality,
    SessionConfiguration,
)

log = logging.getLogger(__name__)

GEMINI_INPUT_RATE = 16_000
GEMINI_OUTPUT_RATE = 24_000
_INPUT_MIME = f"audio/pcm;rate={GEMINI_INPUT_RATE}"

# Default native-audio model with an audio response modality.
DEFAULT_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

# How many consecutive reconnect attempts before giving up (REQ-AIVP-005.2).
_MAX_RECONNECTS = 3


def default_configuration() -> SessionConfiguration:
    """The default Session Configuration: native-audio model, audio replies.

    Applied when the pipeline engages without an explicit configuration
    (REQ-AIVP-007.2 / REQ-AIVP-007.3).
    """
    return SessionConfiguration(model=DEFAULT_MODEL, response_modality=ResponseModality.AUDIO)


class GeminiVoiceProvider:
    """Streams audio to Gemini Live and yields its spoken reply."""

    def __init__(
        self,
        api_key: str,
        config: SessionConfiguration | None = None,
    ) -> None:
        self._api_key = api_key
        self._config = config or default_configuration()
        self._client: Any = None
        self._types: Any = None
        self._cm: Any = None  # the live.connect async context manager
        self._session: Any = None
        self._resume_handle: str | None = None
        self._closed = False

    @property
    def input_rate(self) -> int:
        return GEMINI_INPUT_RATE

    @property
    def output_rate(self) -> int:
        return GEMINI_OUTPUT_RATE

    # --- config ---------------------------------------------------------------
    def _modality(self) -> Any:
        types = self._types
        if self._config.response_modality is ResponseModality.TEXT:
            return types.Modality.TEXT
        return types.Modality.AUDIO

    def _speech_config(self) -> Any:
        """Build a SpeechConfig selecting the configured prebuilt voice, if any."""
        if not self._config.voice:
            return None
        types = self._types
        return types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._config.voice)
            )
        )

    def _build_config(self) -> Any:
        types = self._types
        return types.LiveConnectConfig(
            response_modalities=[self._modality()],
            system_instruction=self._config.system_instruction,
            speech_config=self._speech_config(),
            session_resumption=types.SessionResumptionConfig(handle=self._resume_handle),
        )

    # --- lifecycle ------------------------------------------------------------
    async def open(self) -> None:
        """Import the SDK, build the client, and open the first session."""
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # SDK not installed on this host
            raise ProviderError(f"google-genai is not available: {exc}") from exc
        self._types = types
        try:
            self._client = genai.Client(
                api_key=self._api_key,
                http_options={"api_version": "v1alpha"},
            )
        except Exception as exc:
            raise ProviderError(f"could not construct Gemini client: {exc}") from exc
        self._closed = False
        await self._connect()

    async def _connect(self) -> None:
        """Open a live session, reusing the resume handle when present."""
        try:
            self._cm = self._client.aio.live.connect(model=self._config.model, config=self._build_config())
            self._session = await self._cm.__aenter__()
        except Exception as exc:
            self._session = None
            self._cm = None
            raise ProviderError(f"could not open Gemini Live session: {exc}") from exc
        log.info("Gemini Live session open (model=%s)", self._config.model)

    async def _teardown_session(self) -> None:
        cm, self._cm, self._session = self._cm, None, None
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                log.debug("error closing Gemini session context", exc_info=True)

    async def close(self) -> None:
        self._closed = True
        await self._teardown_session()

    # --- input ----------------------------------------------------------------
    async def send_audio(self, pcm: bytes) -> None:
        session = self._session
        if session is None or self._closed:
            return
        blob = self._types.Blob(data=pcm, mime_type=_INPUT_MIME)
        try:
            await session.send_realtime_input(audio=blob)
        except Exception:
            # A broken send means the socket dropped; the receive loop owns
            # reconnection, so we swallow here to avoid racing it.
            log.debug("send_realtime_input failed; receive loop will reconnect", exc_info=True)

    # --- reply stream ---------------------------------------------------------
    async def events(self) -> AsyncIterator[ReplyEvent]:
        """Yield reply events, reconnecting across session time-limits."""
        reconnects = 0
        while not self._closed:
            try:
                async for event in self._read_once():
                    reconnects = 0  # a clean message resets the failure budget
                    yield event
            except ProviderError:
                raise
            except Exception as exc:
                log.warning("Gemini receive loop error: %s", exc)
            if self._closed:
                break
            # The session ended (time-limit / go_away / drop). Try to resume.
            reconnects += 1
            if reconnects > _MAX_RECONNECTS or self._resume_handle is None:
                raise ProviderError("Gemini session lost and could not be resumed")
            await self._teardown_session()
            try:
                await self._connect()
            except ProviderError:
                raise
            log.info("Gemini Live session resumed (attempt %d)", reconnects)

    async def _read_once(self) -> AsyncIterator[ReplyEvent]:
        """Translate one session's message stream into ReplyEvents.

        Returns when the underlying `receive()` iterator ends, which the outer
        loop treats as a signal to attempt resumption.
        """
        session = self._session
        if session is None:
            return
        async for message in session.receive():
            update = getattr(message, "session_resumption_update", None)
            if update is not None and getattr(update, "new_handle", None):
                self._resume_handle = update.new_handle
            content = getattr(message, "server_content", None)
            if content is None:
                continue
            if getattr(content, "interrupted", False):
                yield ReplyEvent.interrupted()
            model_turn = getattr(content, "model_turn", None)
            if model_turn is not None:
                for part in getattr(model_turn, "parts", []) or []:
                    inline = getattr(part, "inline_data", None)
                    data = getattr(inline, "data", None) if inline is not None else None
                    if data:
                        yield ReplyEvent.audio(data)
            if getattr(content, "turn_complete", False):
                yield ReplyEvent.turn_complete()
