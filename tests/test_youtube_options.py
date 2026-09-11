"""Exercise real yt-dlp argument parsing and the public-first resolver."""

from importlib.metadata import version

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.youtube import YoutubeIE

from parlay.media.po_token import PoTokenProvider
from parlay.media.public_sources import ResolvedStream
from parlay.media.resolver import TrackResolver
from parlay.media.source_selector import SourceSelector
from parlay.media.track import MediaSource, SourceResolutionError, StreamSource


def test_provider_dependency_and_cookieless_client() -> None:
    assert version("bgutil-ytdlp-pot-provider")
    selector = SourceSelector(PoTokenProvider("http://pot-provider:4416"))
    opts = selector.youtube_options()
    assert opts["extractor_args"]["youtube"]["player_client"] == ["tv", "web_safari"]
    with YoutubeDL(opts) as ydl:
        ie = YoutubeIE(ydl)
        assert ie._configuration_arg("base_url", ie_key="youtubepot-bgutilhttp") == [
            "http://pot-provider:4416"
        ]


@pytest.mark.asyncio
async def test_search_path_resolves_via_public(monkeypatch) -> None:
    class FakePublic:
        async def search(self, query, limit=10):
            return [
                {
                    "id": "HYUpNJJELeE",
                    "youtube_id": "HYUpNJJELeE",
                    "title": "Track",
                    "source_url": "https://www.youtube.com/watch?v=HYUpNJJELeE",
                    "duration": 180,
                }
            ]

        async def resolve(self, video_id):
            stream = StreamSource(source=MediaSource.PIPED, stream_url="http://s/a", abr=128.0)
            return ResolvedStream(stream=stream, title="Track", duration_s=180.0)

    selector = SourceSelector(PoTokenProvider("http://pot:4416"), public=FakePublic())  # type: ignore[arg-type]
    resolved = await TrackResolver(selector).resolve("a track")
    assert resolved.track.title == "Track"
    assert resolved.stream.source is MediaSource.PIPED
    assert resolved.track.webpage_url == "https://www.youtube.com/watch?v=HYUpNJJELeE"


@pytest.mark.asyncio
async def test_search_miss_becomes_not_found(monkeypatch) -> None:
    class EmptyPublic:
        async def search(self, query, limit=10):
            return []

        async def resolve(self, video_id):
            return None

    selector = SourceSelector(PoTokenProvider("http://pot:4416"), public=EmptyPublic())  # type: ignore[arg-type]
    monkeypatch.setattr(SourceSelector, "_ytdlp_search", lambda self, q, n: [])
    from parlay.media.track import TrackNotFoundError

    with pytest.raises(TrackNotFoundError):
        await TrackResolver(selector).resolve("nothing at all")


@pytest.mark.asyncio
async def test_link_all_sources_fail_is_resolution_error(monkeypatch) -> None:
    class EmptyPublic:
        async def resolve(self, video_id):
            return None

    selector = SourceSelector(PoTokenProvider("http://pot:4416"), public=EmptyPublic())  # type: ignore[arg-type]

    def boom(target, opts, *, allow_empty=False):
        raise SourceResolutionError("blocked")

    monkeypatch.setattr(SourceSelector, "_extract", staticmethod(boom))
    with pytest.raises(SourceResolutionError):
        await TrackResolver(selector).resolve("https://www.youtube.com/watch?v=HYUpNJJELeE")
