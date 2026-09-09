"""MediaTranscoder: stream a resolved URL through ffmpeg to 48 kHz PCM.

The transcoder pipes the resolved stream URL through ffmpeg and yields
`Pcm48kFrame` chunks as they are produced, rather than downloading the whole
file, so playback can start quickly and nothing is persisted (ADR-002). Output
is 48 kHz 16-bit little-endian stereo PCM, matching the call boundary; final
fidelity is bounded by the call's Opus encoding downstream.

ffmpeg is spawned as a subprocess and its stdout is read in fixed-size frames.
Stopping the stream terminates ffmpeg so a track can be skipped promptly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from ..audio.frames import CALL_CHANNELS, CALL_FRAME_BYTES, CALL_RATE
from .track import TranscodeError

log = logging.getLogger(__name__)

# Read a whole number of 10 ms call frames per chunk to keep injection smooth.
_FRAMES_PER_READ = 5
_READ_BYTES = CALL_FRAME_BYTES * _FRAMES_PER_READ


class MediaTranscoder:
    """Streams a source URL through ffmpeg into PCM at the call boundary."""

    def __init__(self, *, ffmpeg: str = "ffmpeg") -> None:
        self._ffmpeg = ffmpeg
        self._proc: asyncio.subprocess.Process | None = None

    async def stream(self, url: str) -> AsyncIterator[bytes]:
        """Yield raw 48 kHz stereo S16LE PCM chunks from the source URL.

        Raises TranscodeError if ffmpeg cannot be started. Callers wrap each
        chunk in a `Pcm48kFrame` for the playback path.
        """
        proc = await self._spawn(url)
        self._proc = proc
        assert proc.stdout is not None
        try:
            while True:
                chunk = await proc.stdout.read(_READ_BYTES)
                if not chunk:
                    break
                yield chunk
        finally:
            await self._terminate(proc)
            self._proc = None

    async def stop(self) -> None:
        """Terminate the current ffmpeg process, if any (used on skip/leave)."""
        if self._proc is not None:
            await self._terminate(self._proc)
            self._proc = None

    async def _spawn(self, url: str) -> asyncio.subprocess.Process:
        args = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-i",
            url,
            "-vn",
            "-f",
            "s16le",
            "-ar",
            str(CALL_RATE),
            "-ac",
            str(CALL_CHANNELS),
            "pipe:1",
        ]
        try:
            return await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            raise TranscodeError(f"could not start ffmpeg: {exc}") from exc

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
