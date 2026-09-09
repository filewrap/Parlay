"""Message presentation layer.

Every Operator-facing and in-call message Parlay sends is routed through this
single module so the style is uniform. Status is conveyed with icon glyphs
(not decorative emojis), and each outcome category has a consistent leading
icon so the category is recognizable at a glance.

Glyphs are plain Unicode symbols, not colorful emoji. Telegram userbots cannot
send colored or inline buttons (Bot API only), so the interface feel comes from
consistent icons and structure.
"""

from __future__ import annotations

from enum import Enum


class Category(str, Enum):
    """Outcome categories, each with a fixed leading icon."""

    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# Leading icon per outcome category.
CATEGORY_ICONS: dict[Category, str] = {
    Category.SUCCESS: "\u2714",   # heavy check mark
    Category.ERROR: "\u2716",     # heavy multiplication x
    Category.WARNING: "\u26a0",   # warning sign
    Category.INFO: "\u2139",      # information source
}

# Transport / status glyphs reused across features.
ICONS: dict[str, str] = {
    "play": "\u25b6",       # black right-pointing triangle
    "pause": "\u23f8",      # double vertical bar
    "stop": "\u23f9",       # black square for stop
    "skip": "\u23ed",       # black right-pointing double triangle with bar
    "queue": "\u2630",      # trigram for heaven (list)
    "speaking": "\U0001f5e3",  # speaking head (status marker, monochrome intent)
    "idle": "\u25cb",       # white circle
    "connected": "\u25cf",  # black circle
}


def _line(icon: str, text: str) -> str:
    return f"{icon} {text}"


def success(text: str) -> str:
    return _line(CATEGORY_ICONS[Category.SUCCESS], text)


def error(text: str) -> str:
    return _line(CATEGORY_ICONS[Category.ERROR], text)


def warning(text: str) -> str:
    return _line(CATEGORY_ICONS[Category.WARNING], text)


def info(text: str) -> str:
    return _line(CATEGORY_ICONS[Category.INFO], text)


def status(text: str, icon_key: str = "info") -> str:
    """Format a status line with a transport/status glyph."""
    icon = ICONS.get(icon_key, CATEGORY_ICONS[Category.INFO])
    return _line(icon, text)
