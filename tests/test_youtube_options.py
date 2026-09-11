"""Exercise real yt-dlp argument parsing without network extraction."""

from importlib.metadata import version
from unittest.mock import Mock

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.utils import DownloadError

from parlay.media.po_token import PoTokenProvider
from parlay.media.resolver import TrackResolver
from parlay.media.source_selector import SourceSelector
from parlay.media.track import SourceResolutionError
from parlay.search import MediaSearch


def test_provider_dependency_and_real_argument_parser():
    assert version("bgutil-ytdlp-pot-provider")
    selector = SourceSelector(PoTokenProvider("http://pot-provider:4416"))
    with YoutubeDL(selector.youtube_options()) as ydl:
        ie = YoutubeIE(ydl)
        assert ie._configuration_arg(
            "base_url", ie_key="youtubepot-bgutilhttp"
        ) == ["http://pot-provider:4416"]


def test_metadata_and_search_share_provider_settings(monkeypatch):
    captured = []

    class FakeYdl:
        def __init__(self, opts):
            captured.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            return {"id": "HYUpNJJELeE", "title": "Track"}

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYdl)
    selector = SourceSelector(PoTokenProvider("http://pot-provider:4416"))
    TrackResolver(selector)._probe("https://www.youtube.com/watch?v=HYUpNJJELeE")
    MediaSearch("http://pot-provider:4416")._probe("ytsearch10:track")
    for opts in captured:
        assert opts["extractor_args"] == selector.youtube_options()["extractor_args"]
        assert opts["socket_timeout"] == 10


@pytest.mark.asyncio
async def test_blocked_metadata_becomes_media_error(monkeypatch):
    resolver = TrackResolver(SourceSelector(PoTokenProvider("http://pot-provider:4416")))
    monkeypatch.setattr(resolver, "_probe", Mock(side_effect=DownloadError("Sign in to confirm")))
    with pytest.raises(SourceResolutionError, match="metadata"):
        await resolver.resolve("https://www.youtube.com/watch?v=HYUpNJJELeE")
