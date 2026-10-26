"""Press de-duplication and expiry for dial button_press events.

Firmware with the press ledger re-sends a press held through an MQTT outage
(DECK-F0A4, 2026-10-14). HA must never act on the same press twice, and
never on one more than a minute old. Helm and Console apply the same rule.

Run with:  python3 -m pytest tests/test_press_guard.py
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_PY = ROOT / "custom_components" / "deckhand" / "__init__.py"

_spec = importlib.util.spec_from_file_location(
    "deckhand_press_guard", ROOT / "custom_components" / "deckhand" / "_press_guard.py"
)
_pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pg)

NOW = 1_760_430_000.0


def _press(press_id=None, ts=NOW, queued_s=None, type_="button_press"):
    payload = {"item_id": "script.coffee", "item_label": "Coffee"}
    if press_id is not None:
        payload["press_id"] = press_id
    if queued_s is not None:
        payload["queued_s"] = queued_s
    return {"type": type_, "ts": ts, "payload": payload}


class PressGuardTests(unittest.TestCase):
    def setUp(self):
        self.g = _pg.PressGuard()

    def test_first_press_accepted_repeat_dropped(self):
        self.assertEqual(self.g.check("DECK-F0A4", _press("a1b2c3d4-7"), NOW), _pg.ACCEPT)
        self.assertEqual(self.g.check("DECK-F0A4", _press("a1b2c3d4-7"), NOW + 2), _pg.DUPLICATE)

    def test_same_press_id_on_another_dial_is_its_own_press(self):
        self.g.check("DECK-F0A4", _press("a1b2c3d4-7"), NOW)
        self.assertEqual(self.g.check("DECK-45E8", _press("a1b2c3d4-7"), NOW), _pg.ACCEPT)

    def test_distinct_presses_both_fire(self):
        self.assertEqual(self.g.check("D", _press("n-1"), NOW), _pg.ACCEPT)
        self.assertEqual(self.g.check("D", _press("n-2"), NOW), _pg.ACCEPT)

    def test_held_press_within_the_minute_fires(self):
        self.assertEqual(
            self.g.check("D", _press("n-3", ts=NOW - 50, queued_s=50), NOW), _pg.ACCEPT
        )

    def test_press_older_than_a_minute_refused(self):
        self.assertEqual(self.g.check("D", _press("n-4", queued_s=61), NOW), _pg.STALE)
        self.assertEqual(self.g.check("D", _press("n-5", ts=NOW - 70), NOW), _pg.STALE)
        # Its own re-send is a duplicate, not a second action.
        self.assertEqual(self.g.check("D", _press("n-5", ts=NOW - 70), NOW), _pg.DUPLICATE)

    def test_legacy_press_without_press_id_untouched(self):
        old = _press(None, ts=NOW - 3600)
        self.assertEqual(self.g.check("D", old, NOW), _pg.ACCEPT)
        self.assertEqual(self.g.check("D", old, NOW), _pg.ACCEPT)

    def test_unsynced_clock_ts_zero_is_not_stale(self):
        self.assertEqual(self.g.check("D", _press("n-6", ts=0), NOW), _pg.ACCEPT)

    def test_other_event_types_untouched(self):
        ev = _press("n-7", type_="timer_start")
        self.assertEqual(self.g.check("D", ev, NOW), _pg.ACCEPT)
        self.assertEqual(self.g.check("D", ev, NOW), _pg.ACCEPT)

    def test_window_expires(self):
        self.g.check("D", _press("n-8"), NOW)
        later = NOW + _pg.DEDUPE_WINDOW_S + 1
        self.assertEqual(self.g.check("D", _press("n-8", ts=later), later), _pg.ACCEPT)


class QrRequestTests(unittest.TestCase):
    def test_qr_request_is_internal(self):
        ev = {"type": "button_press", "ts": NOW,
              "payload": {"item_id": "wifi_guest", "action": "qr_request"}}
        self.assertTrue(_pg.is_internal_request(ev))

    def test_normal_press_is_not_internal(self):
        self.assertFalse(_pg.is_internal_request(_press("n-9")))
        ev = _press(None)
        ev["payload"]["action"] = "toggle"
        self.assertFalse(_pg.is_internal_request(ev))

    def test_action_only_counts_on_a_button_press(self):
        ev = {"type": "timer_start", "payload": {"action": "qr_request"}}
        self.assertFalse(_pg.is_internal_request(ev))

    def test_top_level_action_is_not_the_contract(self):
        # The firmware nests action under payload; a stray top-level key
        # must not swallow a real press.
        ev = _press(None)
        ev["action"] = "qr_request"
        self.assertFalse(_pg.is_internal_request(ev))

    def test_malformed_envelopes(self):
        for ev in (None, [], {}, {"type": "button_press"},
                   {"type": "button_press", "payload": "qr_request"}):
            self.assertFalse(_pg.is_internal_request(ev))


class WiringTests(unittest.TestCase):
    def test_qr_request_returns_before_anything_fires(self):
        src = INIT_PY.read_text()
        skip = src.index("if is_internal_request(payload):\n            return")
        guard = src.index("press_guard.check(dial_id, payload)")
        fire = src.index('f"{DOMAIN}_dial_event",\n            {\n                "dial_id": dial_id,')
        media = src.index("_dispatch_dial_media_control(hass, payload)")
        self.assertLess(skip, guard)
        self.assertLess(skip, fire)
        self.assertLess(skip, media)

    def test_event_handler_consults_the_guard_before_firing(self):
        src = INIT_PY.read_text()
        guard = src.index("press_guard.check(dial_id, payload)")
        fire = src.index('f"{DOMAIN}_dial_event",\n            {\n                "dial_id": dial_id,')
        media = src.index("_dispatch_dial_media_control(hass, payload)")
        self.assertLess(guard, fire)
        self.assertLess(guard, media)


if __name__ == "__main__":
    unittest.main()
