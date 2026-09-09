"""Tests for the message presentation layer."""

from __future__ import annotations

from parlay import presentation as fmt
from parlay.presentation import CATEGORY_ICONS, Category


def test_each_category_has_a_distinct_leading_icon() -> None:
    icons = list(CATEGORY_ICONS.values())
    assert len(icons) == len(set(icons))
    for category in Category:
        assert category in CATEGORY_ICONS


def test_success_prefixes_check_glyph() -> None:
    out = fmt.success("Joined chat.")
    assert out.startswith(CATEGORY_ICONS[Category.SUCCESS])
    assert "Joined chat." in out


def test_error_prefixes_error_glyph() -> None:
    out = fmt.error("Nothing to leave.")
    assert out.startswith(CATEGORY_ICONS[Category.ERROR])


def test_status_uses_transport_glyph() -> None:
    out = fmt.status("in chat; AI off", "connected")
    assert "in chat; AI off" in out
    # Leading glyph should not be a plain ASCII letter.
    assert not out[0].isascii()


def test_no_emoji_variation_selectors_in_category_icons() -> None:
    # Guard against emoji-style (colorful) variants sneaking in.
    for icon in CATEGORY_ICONS.values():
        assert "\ufe0f" not in icon
