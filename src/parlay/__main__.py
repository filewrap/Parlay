"""Entry point: `python -m parlay` or the `parlay` console script."""

from __future__ import annotations

import asyncio
import logging

from .app import ParlayApp
from .client import AuthorizationError
from .config import ConfigError, load_config
from .logging_setup import setup_logging

log = logging.getLogger("parlay")


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    setup_logging(config.log_level)
    app = ParlayApp(config)
    try:
        asyncio.run(app.run())
    except AuthorizationError as exc:
        log.error("Authorization error: %s", exc)
        return 3
    except KeyboardInterrupt:
        log.info("Shutting down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
