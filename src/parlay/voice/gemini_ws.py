"""GeminiLiveSocket: VoiceProvider over the raw Gemini Live WebSocket.

An alternative transport to `GeminiVoiceProvider` (which uses the google-genai
SDK and an API key). This one speaks the BidiGenerateContentConstrained
WebSocket directly and authenticates with an ephemeral access token
(`?access_token=`), matching the browser and mobile Live clients. It exists to
make the live engine more robust: a second, dependency-light path that does not
rely on the SDK and can be used as the primary transport with the SDK as a
fallback.

Wire contract (v1alpha, JSON frames):
  * First client frame: {"setup": {...}} then the server replies
    {"setupComplete": {}}.
  * Input: {"realtimeInput": {"mediaChunks": [{"mimeType":
    "audio/pcm;rate=16000", "data": "<base64 PCM>"}]}}.
  * Output: {"serverContent": {"modelTurn": {"parts": [{"inlineData":
    {"data": "<base64 PCM>"}}]}, "turnComplete": bool, "interrupted": bool}}.
  * Resumption: {"sessionResumptionUpdate": {"newHandle": "..."}} is persisted
    and replayed via setup.sessionResumption.handle on reconnect (REQ-AIVP-005).

Same PCM contract as the SDK provider: 16 kHz mono in, 24 kHz mono out.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from websockets.asyncio.client import connect

from .live_token import EphemeralTokenSource
from .provider import ProviderError, ReplyEvent, ResponseModality, SessionConfiguration

log = logging.getLogger(__name__)

GEMINI_INPUT_RATE = 16_000
GEMINI_OUTPUT_RATE = 24_000
_INPUT_MIME = f"audio/pcm;rate={GEMINI_INPUT_RATE}"

_ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained"
)
# How many consecutive reconnect attempts before giving up (REQ-AIVP-005.2).
_MAX_RECONNECTS = 3


class GeminiLiveSocket:
    """Streams audio to Gemini Live over a raw WebSocket and yields its reply."""

    def __init__(self, tokens: EphemeralTokenSource, config: SessionConfiguration) -> None:
        self._tokens = tokens
        self._config = config
        self._ws: Any = None
        self._resume_handle: str | None = None
        self._closed = False

    @property
    def input_rate(self) -> int:
        return GEMINI_INPUT_RATE

    @property
    def output_rate(self) -> int:
        return GEMINI_OUTPUT_RATE

    # --- config ---------------------------------------------------------------
    def _model_name(self) -> str:
        model = self._config.model
        return model if model.startswith("models/") else f"models/{model}"

    def _setup_message(self) -> dict[str, Any]:
        modality = "TEXT" if self._config.response_modality is ResponseModality.TEXT else "AUDIO"
        generation: dict[str, Any] = {"responseModalities": [modality]}
        if self._config.voice:
            generation["speechConfig"] = {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self._config.voice}}
            }
        setup: dict[str, Any] = {"model": self._model_name(), "generationConfig": generation}
        if self._config.system_instruction:
            setup["systemInstruction"] = {"parts": [{"text": self._config.system_instruction}]}
        setup["sessionResumption"] = {"handle": self._resume_handle} if self._resume_handle else {}
        return {"setup": setup}

    # --- lifecycle ------------------------------------------------------------
    async def open(self) -> None:
        self._closed = False
        await self._connect()

    async def _connect(self) -> None:
        try:
            token = await self._tokens.token(force=self._resume_handle is None)
            url = f"{_ENDPOINT}?access_token={token}"
            self._ws = await connect(url, max_size=None)
            await self._ws.send(json.dumps(self._setup_message()))
            await self._await_setup_complete()
        except ProviderError:
            raise
        except Exception as exc:
            self._ws = None
            raise ProviderError(f"could not open Gemini Live socket: {exc}") from exc
        log.info("Gemini Live socket open (model=%s)", self._config.model)

    async def _await_setup_complete(self) -> None:
        ws = self._ws
        if ws is None:
            raise ProviderError("Gemini Live socket not connected")
        message = _decode(await ws.recv())
        if "error" in message:
            raise ProviderError(f"Gemini Live setup failed: {message['error']}")

    async def close(self) -> None:
        self._closed = True
        await self._teardown()

    async def _teardown(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                log.debug("error closing Gemini Live socket", exc_info=True)

    # --- input ----------------------------------------------------------------
    async def send_audio(self, pcm: bytes) -> None:
        ws = self._ws
        if ws is None or self._closed or not pcm:
            return
        frame = {
            "realtimeInput": {
                "mediaChunks": [
                    {"mimeType": _INPUT_MIME, "data": base64.b64encode(pcm).decode("ascii")}
                ]
            }
        }
        try:
            await ws.send(json.dumps(frame))
        except Exception:
            log.debug("live socket send failed; receive loop will reconnect", exc_info=True)

    # --- reply stream ---------------------------------------------------------
    async def events(self) -> AsyncIterator[ReplyEvent]:
        """Yield reply events, reconnecting across session time-limits."""
        reconnects = 0
        while not self._closed:
            try:
                async for event in self._read_once():
                    reconnects = 0
                    yield event
            except ProviderError:
                raise
            except Exception as exc:
                log.warning("Gemini Live socket receive error: %s", exc)
            if self._closed:
                break
            reconnects += 1
            if reconnects > _MAX_RECONNECTS or self._resume_handle is None:
                raise ProviderError("Gemini Live socket lost and could not be resumed")
            await self._teardown()
            await self._connect()
            log.info("Gemini Live socket resumed (attempt %d)", reconnects)

    async def _read_once(self) -> AsyncIterator[ReplyEvent]:
        ws = self._ws
        if ws is None:
            return
        async for raw in ws:
            message = _decode(raw)
            update = message.get("sessionResumptionUpdate")
            if isinstance(update, dict) and update.get("newHandle"):
                self._resume_handle = update["newHandle"]
            content = message.get("serverContent")
            if not isinstance(content, dict):
                continue
            if content.get("interrupted"):
                yield ReplyEvent.interrupted()
            model_turn = content.get("modelTurn")
            if isinstance(model_turn, dict):
                for part in model_turn.get("parts") or []:
                    inline = part.get("inlineData") if isinstance(part, dict) else None
                    data = inline.get("data") if isinstance(inline, dict) else None
                    if data:
                        yield ReplyEvent.audio(base64.b64decode(data))
            if content.get("turnComplete"):
                yield ReplyEvent.turn_complete()


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        message = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProviderError(f"invalid Gemini Live frame: {exc}") from exc
    return message if isinstance(message, dict) else {}
