"""EphemeralTokenSource: fetch and cache a Gemini Live access token.

The URL-based ("Constrained") Gemini Live transport authenticates with an
ephemeral access token passed as `?access_token=` rather than an API key. A
deployment mints these tokens from its own endpoint (a simple POST that returns
`{"token": "..."}`); this module fetches one, caches it for a configured TTL,
and refreshes it on demand when the cached token has expired.

Kept deliberately small and dependency-free: the POST uses urllib in a worker
thread so the module adds no new runtime dependency and never blocks the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

# Refresh a little before Gemini's ~30 minute ephemeral-token cap.
_DEFAULT_TTL_S = 1500.0
_TIMEOUT_S = 10.0


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
    ) -> None:
        self._url = url
        self._ttl_s = ttl_s if ttl_s > 0 else _DEFAULT_TTL_S
        self._field = field
        self._method = method.upper()
        self._token: str | None = None
        self._expires_at = 0.0

    def _fetch(self) -> str:
        request = urllib.request.Request(self._url, method=self._method)
        if self._method == "POST":
            request.data = b""
        request.add_header("accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
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
