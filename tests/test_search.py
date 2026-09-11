"""Public search restricts source URLs and resolves via the public client."""

import pytest

from parlay.media.public_sources import ResolvedStream
from parlay.media.track import MediaSource, StreamSource
from parlay.rooms.service import RoomError
from parlay.search import MediaSearch


class FakePublic:
    def __init__(self, items=None, resolved=None):
        self.items = items or []
        self.resolved = resolved
        self.search_calls = 0

    async def search(self, query, limit=10):
        self.search_calls += 1
        return self.items

    async def resolve(self, video_id):
        return self.resolved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "http://127.0.0.1/x",
        "https://localhost/x",
        "file:///etc/passwd",
        "https://youtube.com.evil.test/watch?v=abcdefghijk",
        "https://youtube.com@evil.test/watch?v=abcdefghijk",
        "//localhost/x",
        "https://www.youtube.com/redirect?q=http://localhost",
    ],
)
async def test_reject_unsafe_sources(query):
    public = FakePublic()
    search = MediaSearch(public=public)
    with pytest.raises(RoomError):
        await search(query)
    assert public.search_calls == 0


@pytest.mark.asyncio
async def test_text_search_uses_public_and_caches():
    public = FakePublic(items=[{"id": "abcdefghijk", "youtube_id": "abcdefghijk", "title": "Song"}])
    search = MediaSearch(public=public)
    first = await search("song")
    second = await search("song")
    assert first[0]["title"] == "Song"
    assert second == first
    assert public.search_calls == 1


@pytest.mark.asyncio
async def test_link_resolves_title_via_public():
    stream = StreamSource(source=MediaSource.INVIDIOUS, stream_url="http://s/a", abr=128.0)
    resolved = ResolvedStream(stream=stream, title="Linked", duration_s=90.0)
    public = FakePublic(resolved=resolved)
    search = MediaSearch(public=public)
    items = await search("https://www.youtube.com/watch?v=abcdefghijk")
    assert items[0]["youtube_id"] == "abcdefghijk"
    assert items[0]["title"] == "Linked"
    assert items[0]["duration"] == 90.0
