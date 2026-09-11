"""Idempotent, anchor-based patches applied in CI.

1. Give the control buttons transport glyphs. Telegram Bot API inline buttons
   cannot be recoloured, so the buttons get visual distinction from the same
   monochrome transport glyphs the rest of Parlay uses (presentation.ICONS):
   pause U+23F8, play U+25B6, skip U+23ED.
2. Update the Invidious ranking test to expect the instance-absolute playback
   URL that local=true proxying now produces.

Applied as string replacements so the large bot.py never has to be rebuilt
byte-for-byte.
"""

from __future__ import annotations

import sys
from pathlib import Path

BOT = Path("src/parlay/bot.py")
TEST = Path("tests/test_bot.py")
MEDIA_TEST = Path("tests/test_media_sourcing.py")

BOT_OLD = '        controls = (("Pause", "pause"), ("Resume", "resume"), ("Skip", "skip"))'
BOT_NEW = (
    "        controls = (\n"
    '            ("\\u23f8 Pause", "pause"),\n'
    '            ("\\u25b6 Resume", "resume"),\n'
    '            ("\\u23ed Skip", "skip"),\n'
    "        )"
)

TEST_OLD = '    assert "Pause" in labels and "Skip" in labels and "Open room" in labels'
TEST_NEW = (
    '    joined = " ".join(labels)\n'
    '    assert "Pause" in joined and "Skip" in joined and "Open room" in joined'
)

MEDIA_OLD = '    assert resolved.stream.stream_url == "high"'
MEDIA_NEW = '    assert resolved.stream.stream_url == "https://inv/high"'


def patch(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"{path}: already patched")
        return
    if old not in text:
        sys.exit(f"{path}: anchor not found:\n{old}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"{path}: patched")


def main() -> None:
    patch(BOT, BOT_OLD, BOT_NEW)
    patch(TEST, TEST_OLD, TEST_NEW)
    patch(MEDIA_TEST, MEDIA_OLD, MEDIA_NEW)


if __name__ == "__main__":
    main()
