"""Text the dial can actually draw.

Mirrors ``strip_emoji_for_dial`` in Helm (``helm/apps/utils/dial_units.py``).
HACS publishes ``cmd/announce`` straight to the broker, never through Helm's
REST API, so Helm's publisher-side stripping cannot reach announcements sent
from Home Assistant — this is that same rule on this side of the wire. Keep
the two range lists identical.

No dial font has any emoji glyph, so an emoji reaches the screen as a tofu
box (a 💧 in a Home Assistant announcement, 2026-09-10). But the dial renders
far more than ASCII: a Noto fallback behind every Montserrat size draws
Latin-1, Latin Extended-A, typographic punctuation, Cyrillic, and at the
smaller sizes CJK punctuation, hiragana and katakana. So this removes a
known-UNRENDERABLE set rather than keeping a known-renderable one — a gap here
leaves a tofu, whereas a gap in an allow-list would silently delete real words.
"""

from __future__ import annotations

import re

_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # emoji & pictographs, incl. flags + skin tones
    (0x2600, 0x27BF),  # Misc Symbols + Dingbats
    (0x2300, 0x23FF),  # Misc Technical
    (0x2B00, 0x2BFF),  # Misc Symbols and Arrows
    (0xFE00, 0xFE0F),  # variation selectors
    (0x200D, 0x200D),  # zero-width joiner
    (0x20E3, 0x20E3),  # combining enclosing keycap
    (0xE0020, 0xE007F),  # tag characters
)
_EMOJI_RE = re.compile("[" + "".join(f"\\U{lo:08X}-\\U{hi:08X}" for lo, hi in _EMOJI_RANGES) + "]")
_SPACE_RUN_RE = re.compile(r"[ \t]{2,}")
_SPACE_AROUND_NEWLINE_RE = re.compile(r"[ \t]*\n[ \t]*")


def strip_emoji_for_dial(text):
    """Remove emoji — and only emoji. Gaps left behind are closed; line
    breaks are kept; text with no emoji comes back exactly as given."""
    if not text:
        return ""
    if not isinstance(text, str):
        # Leave non-strings for the caller's own validation to reject,
        # rather than failing here with a less useful TypeError.
        return text
    stripped = _EMOJI_RE.sub("", text)
    if stripped == text:
        return text
    stripped = _SPACE_AROUND_NEWLINE_RE.sub("\n", stripped)
    return _SPACE_RUN_RE.sub(" ", stripped).strip()
