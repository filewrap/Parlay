"""EphemeralTokenSource: fetch and cache a Gemini Live access token.

The URL-based ("Constrained") Gemini Live transport authenticates with an
ephemeral access token passed as `?access_token=` rather than an API key. A
deployment mints these tokens from its own endpoint (a simple POST that returns
`{"token": "..."}`); this module fetches one, caches it for a configured TTL,
and refreshes it on demand when the cached token has expired.

Some mint endpoints sit behind Cloudflare bot management, which rejects a bare
request (HTTP 403) unless it carries browser-like headers and the cookies a real
page load would set. This source therefore sends a browser header set and can
first "warm up" by GET-ting a page URL through a shared cookie jar to collect
cookies (notably `__cf_bm`), which it then replays on the token request.

Kept dependency-free: requests use urllib in a worker thread, so the module adds
no runtime dependency and never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import os
import time
import urllib.request
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Refresh a little before Gemini's ~30 minute ephemeral-token cap.
_DEFAULT_TTL_S = 1500.0
_TIMEOUT_S = 10.0

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; realme 5s) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36"
)


class TokenError(RuntimeError):
    """Raised when an ephemeral live token cannot be fetched."""


class EphemeralTokenSource:
    """Fetches and caches an ephemeral Gemini Live token from a mint endpoint."""

    def __init__(
        self,
        url: str,
        *,
        ttl_s: float = _DEFAULT_TTL_S,
        field: str = "token",
        method: str = "POST",
        warmup_url: str | None = None,
        user_agent: str | None = None,
        referer: str | None = None,
    ) -> None:
        self._url = url
        self._ttl_s = ttl_s if ttl_s > 0 else _DEFAULT_TTL_S
        self._field = field
        self._method = method.upper()
        self._warmup_url = warmup_url or _env("GEMINI_LIVE_TOKEN_WARMUP_URL")
        self._user_agent = user_agent or _env("GEMINI_LIVE_TOKEN_USER_AGENT") or _DEFAULT_USER_AGENT
        self._referer = referer or _env("GEMINI_LIVE_TOKEN_REFERER")
        self._token: str | None = None
        self._expires_at = 0.0

    def _origin(self) -> str:
        parts = urlsplit(self._url)
        return f"{parts.scheme}://{parts.netloc}"

    def _headers(self) -> dict[str, str]:
        origin = self._origin()
        return {
            "user-agent": self._user_agent,
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "origin": origin,
            "referer": self._referer or self._warmup_url or f"{origin}/",
            "x-requested-with": "com.lnkofficial.luviai",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Android WebView";v="150"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "priority": "u=1, i",
        }

    def _fetch(self) -> str:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        headers = self._headers()
        if self._warmup_url:
            try:
                warm = urllib.request.Request(self._warmup_url, method="GET", headers=headers)
                with opener.open(warm, timeout=_TIMEOUT_S):
                    pass
            except Exception:
                log.debug("live token warm-up request failed", exc_info=True)
        request = urllib.request.Request(self._url, method=self._method, headers=headers)
        if self._method == "POST":
            request.data = b""
        try:
            with opener.open(request, timeout=_TIMEOUT_S) as response:
                payload: Any = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise TokenError(f"could not fetch live token: {exc}") from exc
        token = payload.get(self._field) if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise TokenError(f"live token response missing '{self._field}' field")
        return token

    async def token(self, *, force: bool = False) -> str:
        """Return a cached token, fetching a fresh one when expired or forced."""
        now = time.monotonic()
        if not force and self._token is not None and now < self._expires_at:
            return self._token
        token = await asyncio.to_thread(self._fetch)
        self._token = token
        self._expires_at = time.monotonic() + self._ttl_s
        return token

    def invalidate(self) -> None:
        """Force the next `token()` call to fetch a fresh token."""
        self._expires_at = 0.0


def _env(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None
