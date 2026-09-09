"""PoTokenProvider: wires yt-dlp to the bgutil PO-token provider service.

YouTube blocks server-side requests that lack a proof-of-origin (PO) token, and
cookie auth gets accounts banned. Parlay runs the bgutil-ytdlp-pot-provider
HTTP server alongside the bot (ADR-001). The correct integration is NOT to
fetch a token by hand: the bgutil yt-dlp *plugin* registers with yt-dlp's PO
Token Provider framework and mints a fresh, content-bound token per video on
its own. yt-dlp only needs to be pointed at the running server through the
plugin's `youtubepot-bgutilhttp:base_url` extractor argument.

This class therefore does two things: it produces the `extractor_args` yt-dlp
needs so the plugin auto-fetches tokens, and it offers a `/ping` health check
so the SourceSelector can tell whether the provider is reachable before it
relies on the YouTube source.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .track import MediaError

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 10.0

# yt-dlp extractor-args key the bgutil HTTP plugin reads its base URL from.
_PLUGIN_ARG_KEY = "youtubepot-bgutilhttp"


class PoTokenError(MediaError):
    """Raised when the PO-token provider service is unreachable."""


class PoTokenProvider:
    """Configures and health-checks the bgutil PO-token HTTP provider."""

    def __init__(self, base_url: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._base_url = base_url.rstrip("/")

        self._timeout_s = timeout_s

    @property
    def base_url(self) -> str:
        return self._base_url

    def extractor_args(self) -> dict[str, list[str]]:
        """Return the extractor-args entry that points yt-dlp at the provider.

        The bgutil plugin then fetches a content-bound PO token per video via
        the running HTTP server, so no token is handled here.
        """
        return {_PLUGIN_ARG_KEY: [f"base_url={self._base_url}"]}

    async def ping(self) -> None:
        """Verify the provider service is reachable, raising PoTokenError if not."""
        await asyncio.to_thread(self._ping_sync)

    def _ping_sync(self) -> None:
        request = urllib.request.Request(f"{self._base_url}/ping", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                body: Any = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise PoTokenError(f"PO-token provider unreachable at {self._base_url}: {exc}") from exc
        if not isinstance(body, dict) or "version" not in body:
            raise PoTokenError("PO-token provider /ping returned an unexpected response")
