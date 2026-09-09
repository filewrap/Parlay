"""PoTokenProvider: fetches a proof-of-origin token for cookieless YouTube.

YouTube blocks server-side requests that lack a proof-of-origin (PO) token, and
cookie auth gets accounts banned. Parlay runs the bgutil-ytdlp-pot-provider
service alongside the bot and asks it for a fresh token over HTTP, then feeds
that token to yt-dlp for the `mweb`/`web_music` client (ADR-001).

The HTTP call uses the standard library so the runtime needs no extra client
dependency; it runs in a thread so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

from .track import MediaError

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 10.0


class PoTokenError(MediaError):
    """Raised when the PO-token provider cannot supply a token."""


class PoTokenProvider:
    """Client for the bgutil-ytdlp-pot-provider HTTP service."""

    def __init__(self, base_url: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def fetch(self, *, content_binding: str | None = None) -> str:
        """Return a fresh PO token, raising PoTokenError on any failure.

        `content_binding` is the value the provider binds the token to (usually
        a visitor id or video id); when omitted the provider issues a general
        token for the session.
        """
        return await asyncio.to_thread(self._fetch_sync, content_binding)

    def _fetch_sync(self, content_binding: str | None) -> str:
        payload: dict[str, str] = {}
        if content_binding:
            payload["content_binding"] = content_binding
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/get_pot",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise PoTokenError(f"PO-token provider unreachable: {exc}") from exc
        token = body.get("po_token") if isinstance(body, dict) else None
        if not token:
            raise PoTokenError("PO-token provider returned no token")
        return str(token)
