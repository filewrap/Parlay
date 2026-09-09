"""Tests for the Media Sourcing Pipeline (REQ-MUS-003).

These exercise the pure logic: fallback order across sources, all-fail
reporting, highest-quality audio selection, YouTube id parsing, and PO-token
parsing. yt-dlp and ffmpeg are never invoked; extraction is faked.
"""

from __future__ import annotations

import pytest

from parlay.media.source_selector import SourceSelector, _youtube_id
from parlay.media.track import (
    MediaSource,
    SourceResolutionError,
    Track,
)


class FakePoTokens:
    async def fetch(self, *, content_binding: str | None = None) -> str:
        return "tok"


def _track() -> Track:
    return Track(title="t", query="a song", webpage_url="https://youtu.be/abcdefghijk")


async def test_fallback_tries_next_source_on_failure() -> None:
    # AC-MUS-003.1/.2: YouTube fails, resolution falls through to Invidious.
    selector = SourceSelector(FakePoTokens())
    tried: list[MediaSource] = []

    async def fake_resolve_one(source: MediaSource, track: Track):
        tried.append(source)
        if source is MediaSource.YOUTUBE:
            raise RuntimeError("blocked")
        from parlay.media.track import StreamSource

        return StreamSource(source=source, stream_url="http://s/x", abr=128.0)

    selector._resolve_one = fake_resolve_one  # type: ignore[method-assign]
    result = await selector.resolve(_track())
    assert tried == [MediaSource.YOUTUBE, MediaSource.INVIDIOUS]
    assert result.source is MediaSource.INVIDIOUS


async def test_all_sources_fail_raises() -> None:
    # AC-MUS-003.3: every source fails -> SourceResolutionError.
    selector = SourceSelector(FakePoTokens())

    async def always_fail(source: MediaSource, track: Track):
        raise RuntimeError("nope")

    selector._resolve_one = always_fail  # type: ignore[method-assign]
    with pytest.raises(SourceResolutionError):
        await selector.resolve(_track())


def test_best_audio_picks_highest_bitrate() -> None:
    # AC-MUS-003.4: highest-bitrate audio-only format wins.
    info = {
        "formats": [
            {"acodec": "opus", "vcodec": "none", "abr": 70, "url": "low"},
            {"acodec": "opus", "vcodec": "none", "abr": 160, "url": "high"},
            {"acodec": "aac", "vcodec": "h264", "abr": 320, "url": "video"},
        ]
    }
    url, abr = SourceSelector._best_audio(info)
    assert url == "high"
    assert abr == 160


def test_best_audio_falls_back_to_info_url() -> None:
    url, abr = SourceSelector._best_audio({"url": "only", "abr": 96})
    assert url == "only"
    assert abr == 96


def test_youtube_id_parsing() -> None:
    assert _youtube_id("abcdefghijk") == "abcdefghijk"
    assert _youtube_id("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert _youtube_id("https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk"
    assert _youtube_id("not a link") is None


async def test_po_token_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from parlay.media import po_token

    provider = po_token.PoTokenProvider("http://prov:4416")

    def fake_fetch_sync(content_binding: str | None) -> str:
        return "the-token"

    monkeypatch.setattr(provider, "_fetch_sync", fake_fetch_sync)
    assert await provider.fetch() == "the-token"


async def test_po_token_error_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from parlay.media import po_token

    provider = po_token.PoTokenProvider("http://prov:4416")

    def fake_fetch_sync(content_binding: str | None) -> str:
        raise po_token.PoTokenError("no token")

    monkeypatch.setattr(provider, "_fetch_sync", fake_fetch_sync)
    with pytest.raises(po_token.PoTokenError):
        await provider.fetch()


if __name__ == "__main__":
    pytest.main([__file__])
