"""Public room search restricts source URLs before contacting extractors."""

from unittest.mock import Mock

import pytest

from parlay.rooms.service import RoomError
from parlay.search import MediaSearch


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
    search = MediaSearch()
    search._probe = Mock()
    with pytest.raises(RoomError):
        await search(query)
    search._probe.assert_not_called()


@pytest.mark.asyncio
async def test_search_prefix_and_cache():
    search = MediaSearch()
    search._probe = Mock(return_value=[{"id": "abcdefghijk", "title": "Song"}])
    await search("song")
    await search("song")
    search._probe.assert_called_once_with("ytsearch10:song")
