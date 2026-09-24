"""Behavioural contract for this repo's vendored dial unit normalisation.

``custom_components/deckhand/_units.py`` is a VENDORED COPY of
``deckhand-sdk/src/deckhand_sdk/units.py`` — HACS ships as a
self-contained integration and can't take the SDK as a dependency. The
SDK's docstring says the copies are kept honest by mirroring its vector
table into each consumer's suite; HACS never had one, and the copy drifted.

The drift: the THIN SPACE and NARROW NO-BREAK SPACE keys in
DIAL_UNIT_GLYPH_MAP had both been flattened to a plain ASCII space
somewhere in the copying, so the map read as correct (the comments still
said THIN SPACE / NARROW NO-BREAK SPACE) while mapping a space to a
space, twice, and neither narrow space was handled at all.

That is live here, not latent: ``safe_unit_for_dial`` runs no NFKD pass —
it maps known glyphs and then ascii-encodes with ``ignore`` — so an
unmapped narrow space is DELETED rather than folded. Home Assistant very
commonly emits U+202F inside units, so "21 °C" was reaching the dial as
"21*C" while Helm rendered "21 *C".

The module loads by path so it's exercised without a HA runtime.

Run with:  python3 -m pytest tests/test_dial_units_contract.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

_spec = importlib.util.spec_from_file_location(
    "deckhand_units",
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components" / "deckhand" / "_units.py",
)
_units = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_units)

DIAL_UNIT_GLYPH_MAP = _units.DIAL_UNIT_GLYPH_MAP
format_sensor_value = _units.format_sensor_value
safe_text_for_dial = _units.safe_text_for_dial
safe_unit_for_dial = _units.safe_unit_for_dial

# (text_in, expected_out) — human-facing NAMES, not units.
TEXT_VECTORS = [
    ("Küche", "Kuche"),
    ("Café", "Cafe"),
    ("Björk", "Bjork"),
    ("naïve", "naive"),
    ("Ærø", "AEro"),
    ("Straße", "Strasse"),
    ("Łódź", "Lodz"),
    ("Þór", "Thor"),
    ("Køkken", "Kokken"),
    ("日本語", ""),
    ("Кухня", ""),
    ("", ""),
]

# (unit_in, expected_out)
UNIT_VECTORS = [
    ("°C", "*C"),
    ("°F", "*F"),
    ("µg/m³", "ug/m3"),
    ("μg/m³", "ug/m3"),   # GREEK MU, distinct codepoint from MICRO SIGN
    ("m²", "m2"),
    ("W", "W"),
    ("kWh", "kWh"),
    ("%", "%"),
    # The regression this file was added for.
    ("21 °C", "21 *C"),   # THIN SPACE
    ("21 °C", "21 *C"),   # NARROW NO-BREAK SPACE
    ("", ""),
]

# (state, attributes, expected_value, expected_unit)
VALUE_VECTORS = [
    ("21.5", {"unit_of_measurement": "°C"}, "21.5", "*C"),
    ("42", {"unit_of_measurement": "µg/m³"}, "42", "ug/m3"),
    ("on", None, "ON", ""),
    ("off", None, "OFF", ""),
    ("", {"unit_of_measurement": "W"}, "", ""),
    (None, None, "", ""),
]


class DialUnitContract(unittest.TestCase):
    def test_units_fold_to_ascii(self):
        for raw, expected in UNIT_VECTORS:
            with self.subTest(unit=raw):
                self.assertEqual(safe_unit_for_dial(raw), expected)

    def test_no_unit_output_is_non_ascii(self):
        for raw, _ in UNIT_VECTORS:
            with self.subTest(unit=raw):
                self.assertTrue(safe_unit_for_dial(raw).isascii())

    def test_names_fold_to_ascii(self):
        for raw, expected in TEXT_VECTORS:
            with self.subTest(text=raw):
                self.assertEqual(safe_text_for_dial(raw), expected)

    def test_format_sensor_value_pairs(self):
        for state, attrs, exp_val, exp_unit in VALUE_VECTORS:
            with self.subTest(state=state):
                self.assertEqual(format_sensor_value(state, attrs), (exp_val, exp_unit))

    def test_the_narrow_space_keys_are_real_narrow_spaces(self):
        """Guards the corruption directly, not just its symptom.

        Escapes are used in the source now so a literal narrow space can't
        be silently flattened to U+0020 again by an editor or a paste.
        """
        self.assertIn(" ", DIAL_UNIT_GLYPH_MAP)
        self.assertIn(" ", DIAL_UNIT_GLYPH_MAP)
        self.assertEqual(DIAL_UNIT_GLYPH_MAP[" "], " ")
        self.assertEqual(DIAL_UNIT_GLYPH_MAP[" "], " ")


if __name__ == "__main__":
    unittest.main()
