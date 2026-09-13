"""Tests for the ephemeral Gemini Live token source."""

from __future__ import annotations

import json
import urllib.request

import pytest

from parlay.voice.live_token import EphemeralTokenSource, TokenError


class _Resp:
    def __init__(self, body: str) -> None:
        self._b = body.encode()

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *a: object) -> bool:
        return False


class _FakeOpener:
    def __init__(self, body: str) -> None:
        self._body = body
        self.opened: list[object] = []

    def open(self, request, timeout=None):
        self.opened.append(request)
        return _Resp(self._body)


@pytest.mark.asyncio
async def test_token_is_cached_until_ttl(monkeypatch):
    calls: list[int] = []

    def fake_fetch(self):
        calls.append(1)
        return f"tok-{len(calls)}"

    monkeypatch.setattr(EphemeralTokenSource, "_fetch", fake_fetch)
    src = EphemeralTokenSource("https://x/api/live-token", ttl_s=1000)
    assert await src.token() == "tok-1"
    assert await src.token() == "tok-1"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_token_refetched_after_invalidate_or_force(monkeypatch):
    calls: list[int] = []

    def fake_fetch(self):
        calls.append(1)
        return f"tok-{len(calls)}"

    monkeypatch.setattr(EphemeralTokenSource, "_fetch", fake_fetch)
    src = EphemeralTokenSource("https://x/api/live-token", ttl_s=1000)
    assert await src.token() == "tok-1"
    src.invalidate()
    assert await src.token() == "tok-2"
    assert await src.token(force=True) == "tok-3"


def test_fetch_reads_configured_field(monkeypatch):
    fake = _FakeOpener(json.dumps({"token": "abc"}))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: fake)
    src = EphemeralTokenSource("https://x/api/live-token", field="token")
    assert src._fetch() == "abc"


def test_fetch_missing_field_raises(monkeypatch):
    fake = _FakeOpener(json.dumps({"nope": 1}))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: fake)
    src = EphemeralTokenSource("https://x")
    with pytest.raises(TokenError):
        src._fetch()


def test_fetch_warms_up_before_posting(monkeypatch):
    fake = _FakeOpener(json.dumps({"token": "abc"}))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: fake)
    src = EphemeralTokenSource("https://x/api/live-token", warmup_url="https://x/live")
    assert src._fetch() == "abc"
    assert len(fake.opened) == 2
    assert fake.opened[0].full_url == "https://x/live"
    assert fake.opened[1].full_url == "https://x/api/live-token"


def test_headers_include_browser_mimicry():
    src = EphemeralTokenSource("https://host.example/api/live-token")
    headers = src._headers()
    assert headers["origin"] == "https://host.example"
    assert headers["x-requested-with"] == "com.lnkofficial.luviai"
    assert headers["sec-fetch-mode"] == "cors"
    assert "user-agent" in headers
