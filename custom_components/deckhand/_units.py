"""Generated copy of deckhand_sdk/deckhand/units.py — keep in sync.

HACS integrations can't depend on an external PyPI package at runtime
without going through Home Assistant's manifest.json requirements list,
which would force a venv install on every HA instance. Vendoring the
module avoids that — the trade-off is that two repos hold the same
file. The canonical copy lives at:

    deckhand-sdk/deckhand/units.py

TODO(#2 followup ticket): add CI lint that diffs this file against the
SDK source and fails the build if they drift. Until then, any change
to the SDK's units.py MUST be hand-mirrored here in the same PR.

────────────────────────────────────────────────────────────────────
Canonical dial-display unit & value normalization.

This is the single source of truth for how Helm publishers and the HACS
integration must turn a Home Assistant entity state + unit into the
``(value, unit)`` pair that the dial firmware can render. See the SDK
copy for the full rationale.
"""
from __future__ import annotations

from typing import Any

# Glyphs the LVGL Montserrat firmware font can't render. We substitute
# to ASCII before publishing so the unit field still reads as a unit
# rather than tofu. See feedback_dial_font_ascii_only.md for the full
# rationale + glyph list. Keep this table symmetric (Latin-1 µ AND
# Greek μ both map to "u") because HA's source isn't always consistent.
DIAL_UNIT_GLYPH_MAP: dict[str, str] = {
    "µ": "u",   # MICRO SIGN (Latin-1)
    "μ": "u",   # GREEK SMALL LETTER MU
    "²": "2",   # SUPERSCRIPT TWO
    "³": "3",   # SUPERSCRIPT THREE
    "½": "1/2",
    "¼": "1/4",
    "¾": "3/4",
    "°": "*",   # DEGREE SIGN — fall back to * even though firmware
                     # fonts often ship °, because HA pairs ° with
                     # superscripts and we want a consistent shape.
    " ": " ",   # THIN SPACE
    " ": " ",   # NARROW NO-BREAK SPACE
}


def safe_unit_for_dial(unit: str) -> str:
    """Strip codepoints the firmware font can't render.

    Maps known Latin-1 / Greek glyphs to ASCII equivalents (μ→u, ³→3,
    ° → *, etc.) then ASCII-encodes with ``ignore`` to drop anything
    still non-ASCII — defense in depth so an HA integration we haven't
    seen yet can't leak a glyph past the firmware's ``unit_normalize_to``
    (which only knows the Latin-1 supplement and would otherwise let
    Greek/Cyrillic bytes through as tofu rectangles).
    """
    if not unit:
        return ""
    mapped = "".join(DIAL_UNIT_GLYPH_MAP.get(ch, ch) for ch in unit)
    return mapped.encode("ascii", "ignore").decode("ascii")


def format_sensor_value(
    state: Any, attributes: dict | None = None
) -> tuple[str, str]:
    """Format an HA entity state into a display-friendly ``(value, unit)``.

    - ``unknown`` / ``unavailable`` / ``""`` / ``None`` collapses to
      ``("", "")`` so callers can use that as a "skip the publish"
      sentinel — matches the gate ``poll_sensor_watches`` uses.
    - Numeric states get rounded to 1 decimal place for floats;
      integers stay as-is (no trailing ``.0`` on a count). 1 decimal is
      the canonical precision for dial display — the firmware font is
      narrow and 2 decimals push the unit off the edge of common
      sensor faces.
    - Non-numeric states pass through; short strings (≤6 chars) get
      uppercased for readability ("home" → "HOME").
    - The unit comes off ``attributes["unit_of_measurement"]`` and is
      run through ``safe_unit_for_dial``.
    """
    attrs = attributes or {}
    unit = safe_unit_for_dial(attrs.get("unit_of_measurement") or "")
    if state is None:
        return "", ""
    s_state = str(state).strip()
    if s_state == "" or s_state in ("unknown", "unavailable", "none"):
        return "", ""
    try:
        f = float(s_state)
    except (TypeError, ValueError):
        return (s_state.upper() if len(s_state) <= 6 else s_state), unit
    if f == int(f):
        return str(int(f)), unit
    return f"{f:.1f}", unit


__all__ = [
    "DIAL_UNIT_GLYPH_MAP",
    "safe_unit_for_dial",
    "format_sensor_value",
]
