"""Tests for the Media Sourcing Pipeline (REQ-MUS-003).

These exercise the pure logic: public-first resolution and search, the
yt-dlp fallback selection, highest-quality audio selection, YouTube id
parsing, and the PO-token provider's extractor-args and health check.
Network calls and yt-dlp are faked; nothing touches the network.
"""

from __future__ import annotations

import pytest

from parlay.media.public_sources import PublicSourceClient, ResolvedStream
from parlay.media.source_selector import SourceSelector, _youtube_id
from parlay.media.track import MediaSource, StreamSource, Track


class FakePoTokens:
    def extractor_args(self) -> dict[str, dict[str, list[str]]]:
        return {"youtubepot-bgutilhttp": {"base_url": ["http://prov:4416"]}}


def _track() -> Track:
    return Track(title="t", query="a song", webpage_url="https://youtu.be/abcdefghijk")


async def test_resolve_prefers_public_source() -> None:
    # AC-MUS-003.1: the cookieless public path resolves without touching yt-dlp.
    class FakePublic:
        async def resolve(self, video_id: str) -> ResolvedStream:
            stream = StreamSource(source=MediaSource.INVIDIOUS, stream_url="http://s/x", abr=160.0)
            return ResolvedStream(stream=stream, title="Song", duration_s=200.0)

    selector = SourceSelector(FakePoTokens(), public=FakePublic())  # type: ignore[arg-type]
    stream = await selector.resolve(_track())
    assert stream.source is MediaSource.INVIDIOUS
    assert stream.stream_url == "http://s/x"


async def test_resolve_falls_back_to_ytdlp_when_public_fails(monkeypatch) -> None:
    # AC-MUS-003.2: public instances all fail, yt-dlp (tv client) resolves.
    class FakePublic:
        async def resolve(self, video_id: str) -> None:
            return None

    selector = SourceSelector(FakePoTokens(), public=FakePublic())  # type: ignore[arg-type]

    def fake_extract(target, opts, *, allow_empty=False):
        assert opts["extractor_args"]["youtube"]["player_client"] == ["tv", "web_safari"]
        return {"formats": [{"acodec": "opus", "vcodec": "none", "abr": 128, "url": "yt"}]}

    monkeypatch.setattr(SourceSelector, "_extract", staticmethod(fake_extract))
    stream = await selector.resolve(_track())
    assert stream.source is MediaSource.YOUTUBE
    assert stream.stream_url == "yt"


async def test_search_prefers_public_then_ytdlp(monkeypatch) -> None:
    class FakePublic:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query: str, limit: int = 10) -> list[dict]:
            self.calls += 1
            return []

    public = FakePublic()
    selector = SourceSelector(FakePoTokens(), public=public)  # type: ignore[arg-type]

    def fake_search(self, query, limit):
        return [{"id": "abcdefghijk", "youtube_id": "abcdefghijk", "title": "x"}]

    monkeypatch.setattr(SourceSelector, "_ytdlp_search", fake_search)
    items = await selector.search("song", 5)
    assert public.calls == 1
    assert items[0]["youtube_id"] == "abcdefghijk"


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


def test_invidious_bitrate_ranking() -> None:
    client = PublicSourceClient()
    data = {
        "title": "Song",
        "lengthSeconds": 210,
        "adaptiveFormats": [
            {"type": "audio/webm; codecs=opus", "bitrate": "70000", "url": "low"},
            {"type": "audio/mp4; codecs=mp4a", "bitrate": "160000", "url": "high"},
            {"type": "video/mp4", "bitrate": "900000", "url": "video"},
        ],
    }
    client._get_json = lambda url: data  # type: ignore[method-assign]
    resolved = client._invidious_stream("https://inv", "abcdefghijk")
    assert resolved is not None
    assert resolved.stream.stream_url == "https://inv/high"
    assert resolved.stream.source is MediaSource.INVIDIOUS
    assert resolved.title == "Song"
    assert resolved.duration_s == 210.0


def test_po_token_extractor_args_point_at_base_url() -> None:
    from parlay.media import po_token

    provider = po_token.PoTokenProvider("http://prov:4416/")
    args = provider.extractor_args()
    assert args == {"youtubepot-bgutilhttp": {"base_url": ["http://prov:4416"]}}


async def test_po_token_ping_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from parlay.media import po_token

    provider = po_token.PoTokenProvider("http://prov:4416")
    monkeypatch.setattr(provider, "_ping_sync", lambda: None)
    await provider.ping()


async def test_po_token_ping_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from parlay.media import po_token

    provider = po_token.PoTokenProvider("http://prov:4416")

    def fake_ping_sync() -> None:
        raise po_token.PoTokenError("unreachable")

    monkeypatch.setattr(provider, "_ping_sync", fake_ping_sync)
    with pytest.raises(po_token.PoTokenError):
        await provider.ping()


if __name__ == "__main__":
    pytest.main([__file__])
