"""Timer event entity — source-level contract guards.

No Home Assistant runtime in this env (see the sibling tests), so this pins
what an automation author relies on: the platform is registered, the entity
offers exactly ``started`` and ``completed``, both firmware event names map
to them, the raw bus event keeps its wire names, and the README documents
the entity. A negative control makes sure the map does not swallow events
it should ignore.

Run with:  python3 -m unittest tests/test_timer_event_entity.py
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
EVENT_PY = COMPONENT / "event.py"
CONST_PY = COMPONENT / "const.py"
README = ROOT / "README.md"


def _timer_event_types() -> dict[str, str]:
    """Evaluate TIMER_EVENT_TYPES from event.py without importing HA."""
    tree = ast.parse(EVENT_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "TIMER_EVENT_TYPES" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("TIMER_EVENT_TYPES not found in event.py")


class TimerEventEntityTests(unittest.TestCase):
    def test_event_platform_is_registered(self):
        src = CONST_PY.read_text(encoding="utf-8")
        block = re.search(r"PLATFORMS\s*=\s*\[(.*?)\]", src, re.DOTALL)
        self.assertIsNotNone(block, "PLATFORMS list missing from const.py")
        self.assertIn('"event"', block.group(1))

    def test_both_firmware_events_map_to_the_two_entity_types(self):
        types = _timer_event_types()
        self.assertEqual(types, {"timer_start": "started", "timer_complete": "completed"})

    def test_unrelated_events_are_not_in_the_map(self):
        # Negative control: a button press or an NFC tap must not become a
        # timer event, or every menu press would fire the entity.
        types = _timer_event_types()
        for wire in ("button_press", "nfc_tap", "alarm_start", "menu_opened"):
            self.assertNotIn(wire, types)

    def test_entity_filters_on_its_own_dial(self):
        src = EVENT_PY.read_text(encoding="utf-8")
        self.assertIn('data.get("dial_id") != self.dial_id', src)

    def test_entity_reads_label_and_minutes(self):
        src = EVENT_PY.read_text(encoding="utf-8")
        self.assertIn('payload.get("item_label")', src)
        self.assertIn('payload.get("minutes")', src)

    def test_readme_documents_the_entity_and_both_types(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("## Timers in automations", text)
        self.assertIn("event.<dial>_timer", text)
        for kind in ("`started`", "`completed`"):
            self.assertIn(kind, text)


if __name__ == "__main__":
    unittest.main()
