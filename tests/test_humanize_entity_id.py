"""A raw entity id never leaves the integration as display text.

``cmd/sensor_value`` labels and perimeter ``friendly_name`` used to fall
back to "" and to the raw id respectively; the dial prints an empty sensor
label as the id in capitals and paints the perimeter name on the ring
(Helm parity, 2026-09-22).
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "deckhand"


def _load_dial_text():
    spec = importlib.util.spec_from_file_location("deckhand_dial_text", ROOT / "dial_text.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HumanizeEntityIdTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_dial_text()

    def test_shapes(self):
        h = self.mod.humanize_entity_id
        self.assertEqual(h("sensor.office_temperature"), "Office Temperature")
        self.assertEqual(h("binary_sensor.front-door"), "Front Door")
        self.assertEqual(h("  scene.movie_night "), "Movie Night")
        self.assertEqual(h(""), "")
        self.assertEqual(h(None), "")

    def test_the_two_fallback_sites_use_it(self):
        src = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(
            '"label": str(label or state.attributes.get("friendly_name") or humanize_entity_id(entity_id))[:64]',
            src,
            "cmd/sensor_value label fallback changed shape",
        )
        perimeter = re.search(r'"friendly_name": (.+),\n', src)
        self.assertIsNotNone(perimeter)
        self.assertIn("humanize_entity_id(bid)", perimeter.group(1))
        self.assertNotIn('raw.get("friendly_name", bid)', src)
