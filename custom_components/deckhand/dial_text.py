"""Text the dial can actually draw.

Mirrors ``strip_emoji_for_dial`` in Helm (``helm/apps/utils/dial_units.py``).
HACS publishes ``cmd/announce`` straight to the broker, never through Helm's
REST API, so Helm's publisher-side stripping cannot reach announcements sent
from Home Assistant — this is that same rule on this side of the wire. Keep
the range list identical to Helm's and Console's
(``deckhand-console/backend/services/dial_text.py``); both of their test
suites pin it against this file.

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


# ── Every other service that sends text the dial draws (helm#399) ─────────
#
# 1.14.0 stripped emoji from send_announcement only. The same HA-typed text
# reaches the same dial fonts through a dozen other services (countdown,
# overlay subtitles and home messages, invitations, generic face mounts,
# now-playing, sensor values, menu items, alarm and dial labels) and drew a
# tofu box on every one. The key lists below mirror Console's
# ``services/dial_text.py`` (the third publisher, helm#399), so a face mount
# means the same thing wherever it is published from.
#
# Deliberately lists of DISPLAY keys, not "every string": ids, entity_ids,
# colours, slugs, alarm/schedule/credential names (looked up by name later)
# and action payloads must reach Helm and the dial byte-exact.

# The invitation prompt (cmd/face/invitation/mount, and the quiet-invitation
# request Helm turns into a menu item). ``from_name`` is Helm's sender line.
INVITATION_TEXT_KEYS: tuple[str, ...] = (
    "text",
    "subtitle",
    "accept_label",
    "decline_label",
    "hold_text",
    "accepted_text",
    "from_name",
)

# Any cmd/face/<kind>/mount, from each face's firmware parser: message
# ``text``, clock/sensor ``subtitle_text``, climate ``title``, charge
# ``label`` and its free-text ``range`` / ``eta``, sensor quad/marquee
# ``label``, perimeter ``friendly_name``, plus the invitation prompt.
FACE_MOUNT_TEXT_KEYS: tuple[str, ...] = (
    "label",
    "title",
    "subtitle_text",
    "home_message",
    "friendly_name",
    "range",
    "eta",
) + INVITATION_TEXT_KEYS

# Containers that are never walked into. Each holds strings that go BACK out
# as identifiers or actions: media ``sources`` and art ``scenes`` are selected
# in HA by label, ``hvac_modes`` go back as set_hvac_mode, ``on_accept`` is
# the opaque action the dial echoes on accept, ``control`` / ``action_data``
# are action config.
_NEVER_WALK: frozenset[str] = frozenset({
    "scenes",
    "sources",
    "hvac_modes",
    "on_accept",
    "control",
    "action_data",
})


def _strip_keys(node, keys: frozenset[str]):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in _NEVER_WALK:
                out[key] = value
            elif isinstance(value, str):
                out[key] = strip_emoji_for_dial(value) if key in keys else value
            else:
                out[key] = _strip_keys(value, keys)
        return out
    if isinstance(node, list):
        return [_strip_keys(v, keys) for v in node]
    return node


def strip_emoji_keys(payload, keys) -> dict:
    """Return a copy of ``payload`` with emoji removed from every string
    under one of ``keys``, at any depth (sensor quads, perimeter bindings).
    Identifier containers (_NEVER_WALK) are left untouched, and the caller's
    dict is never mutated — HA hands services their own call data, and a
    payload is often reused across a whole room fan-out."""
    if not isinstance(payload, dict):
        return payload
    return _strip_keys(payload, frozenset(keys))
