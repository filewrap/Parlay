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
  * Context: {"clientContent": {"turns": [{"role": "user", "parts":
    [{"text": "..."}]}], "turnComplete": false}} injects text without forcing a
    reply.
  * Output: {"serverContent": {"modelTurn": {"parts": [{"inlineData":
    {"data": "<base64 PCM>"}}]}, "turnComplete": bool, "interrupted": bool}}.
  * Resumption: {"sessionResumptionUpdate": {"newHandle": "..."}} is persisted
    and replayed via setup.sessionResumption.handle on reconnect (REQ-AIVP-005).

Same PCM contract as the SDK provider: 16 kHz mono in, 24 kHz mono out.

Logging: to diagnose "socket open but silent" cases, this module logs the setup
frame, periodic outbound audio totals, and a summary of every inbound frame. Set
the `parlay.voice.gemini_ws` logger to DEBUG for per-frame detail.
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
# Log an outbound-audio summary every this many chunks, to confirm mic flow
# without spamming a line per 20 ms frame.
_SEND_LOG_EVERY = 100


class GeminiLiveSocket:
    """Streams audio to Gemini Live over a raw WebSocket and yields its reply."""

    def __init__(self, tokens: EphemeralTokenSource, config: SessionConfiguration) -> None:
        self._tokens = tokens
        self._config = config
        self._ws: Any = None
        self._resume_handle: str | None = None
        self._closed = False
        self._sent_chunks = 0
        self._sent_bytes = 0
        self._recv_audio_chunks = 0
        self._recv_audio_bytes = 0

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
            setup = self._setup_message()
            log.info(
                "Gemini Live sending setup (model=%s, modality=%s, voice=%s, resume=%s)",
                setup["setup"].get("model"),
                self._config.response_modality.name,
                self._config.voice or "<default>",
                bool(self._resume_handle),
            )
            log.debug("Gemini Live setup frame: %s", json.dumps(setup))
            await self._ws.send(json.dumps(setup))
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
        log.info("Gemini Live setup response frame keys: %s", sorted(message.keys()))
        if "error" in message:
            raise ProviderError(f"Gemini Live setup failed: {message['error']}")
        if "setupComplete" not in message:
            log.warning(
                "Gemini Live first frame was not setupComplete: %s",
                _summarize(message),
            )

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            log.info(
                "Gemini Live closing (sent %d chunks/%d bytes, recv %d audio chunks/%d bytes)",
                self._sent_chunks,
                self._sent_bytes,
                self._recv_audio_chunks,
                self._recv_audio_bytes,
            )
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
            return
        self._sent_chunks += 1
        self._sent_bytes += len(pcm)
        if self._sent_chunks == 1:
            log.info("Gemini Live first outbound audio chunk sent (%d bytes)", len(pcm))
        elif self._sent_chunks % _SEND_LOG_EVERY == 0:
            log.info(
                "Gemini Live outbound audio: %d chunks, %d bytes total",
                self._sent_chunks,
                self._sent_bytes,
            )

    async def send_context(self, text: str) -> None:
        """Inject a text turn as context without forcing a reply.

        `turnComplete` is false so the model incorporates the text (e.g. who is
        now speaking) but does not treat it as a prompt to answer. Best-effort:
        a send failure is swallowed and the receive loop owns reconnection.
        """
        ws = self._ws
        if ws is None or self._closed or not text:
            return
        frame = {
            "clientContent": {
                "turns": [{"role": "user", "parts": [{"text": text}]}],
                "turnComplete": False,
            }
        }
        try:
            await ws.send(json.dumps(frame))
        except Exception:
            log.debug("live socket context send failed", exc_info=True)

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
            log.debug("Gemini Live inbound frame keys: %s", sorted(message.keys()))
            update = message.get("sessionResumptionUpdate")
            if isinstance(update, dict) and update.get("newHandle"):
                self._resume_handle = update["newHandle"]
                log.info("Gemini Live session resumption handle updated")
            if message.get("goAway"):
                log.warning("Gemini Live goAway received: %s", message["goAway"])
            content = message.get("serverContent")
            if not isinstance(content, dict):
                # Surface anything that is neither serverContent nor a known
                # control frame, so unexpected shapes are visible.
                if not (update or "usageMetadata" in message or "goAway" in message):
                    log.info("Gemini Live non-content frame: %s", _summarize(message))
                continue
            if content.get("interrupted"):
                log.info("Gemini Live turn interrupted")
                yield ReplyEvent.interrupted()
            model_turn = content.get("modelTurn")
            if isinstance(model_turn, dict):
                for part in model_turn.get("parts") or []:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        log.info("Gemini Live text part: %s", text[:200])
                    inline = part.get("inlineData")
                    data = inline.get("data") if isinstance(inline, dict) else None
                    if data:
                        pcm = base64.b64decode(data)
                        self._recv_audio_chunks += 1
                        self._recv_audio_bytes += len(pcm)
                        if self._recv_audio_chunks == 1:
                            log.info(
                                "Gemini Live first inbound audio part (%d bytes, mime=%s)",
                                len(pcm),
                                inline.get("mimeType") if isinstance(inline, dict) else "?",
                            )
                        yield ReplyEvent.audio(pcm)
            if content.get("turnComplete"):
                log.info(
                    "Gemini Live turnComplete (recv %d audio chunks/%d bytes this session)",
                    self._recv_audio_chunks,
                    self._recv_audio_bytes,
                )
                yield ReplyEvent.turn_complete()


def _summarize(message: dict[str, Any]) -> str:
    """Render a compact, log-safe view of a frame without dumping raw audio."""
    try:
        raw = json.dumps(message)
    except (TypeError, ValueError):
        return f"<unserializable frame keys={sorted(message.keys())}>"
    if len(raw) > 500:
        return raw[:500] + "...(truncated)"
    return raw


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        message = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProviderError(f"invalid Gemini Live frame: {exc}") from exc
    return message if isinstance(message, dict) else {}
