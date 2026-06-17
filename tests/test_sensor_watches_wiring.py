"""Regression tests pinning the sensor_watches subscription wiring.

The HACS sensor_watches realtime path depends on three pieces being
present in ``__init__.py``: the topic subscription, the JSON-parsing
handler, and the listener-reconciliation helper. Removing or renaming
any of them silently breaks realtime sensor pushes — the dial falls
back to the 60s server poll and the user sees laggy sensor faces.

These tests do not exercise runtime behaviour (no HA + MQTT broker in
the test env). They guard the *contract* the way
``test_service_schema.py`` does for apply_overlay.

Run with:  python3 -m pytest tests/test_sensor_watches_wiring.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_PY = ROOT / "custom_components" / "deckhand" / "__init__.py"
CONST_PY = ROOT / "custom_components" / "deckhand" / "const.py"


class SensorWatchesWiringTests(unittest.TestCase):
    """The three load-bearing pieces of the realtime path."""

    @classmethod
    def setUpClass(cls):
        cls.init_src = INIT_PY.read_text(encoding="utf-8")
        cls.const_src = CONST_PY.read_text(encoding="utf-8")

    def test_topic_constant_declared(self):
        # The wildcard ``+`` is what lets HACS handle all of a team's
        # dials with a single subscription — must stay as-is.
        self.assertIn(
            'TOPIC_SENSOR_WATCHES = "deckhand/{team_id}/dial/+/sensor_watches"',
            self.const_src,
            "TOPIC_SENSOR_WATCHES constant missing or wrong shape — "
            "Helm/Console publish to this exact topic.",
        )

    def test_subscription_registered(self):
        # The wiring point: an mqtt.async_subscribe call against the
        # sensor_watches topic with the new handler.
        pattern = re.compile(
            r"mqtt\.async_subscribe\s*\(\s*hass\s*,\s*sensor_watches_topic\s*,",
            re.DOTALL,
        )
        self.assertTrue(
            pattern.search(self.init_src),
            "sensor_watches MQTT subscription is missing from "
            "async_setup_entry — the realtime push path is unwired.",
        )

    def test_handler_function_defined(self):
        self.assertIn(
            "def _handle_sensor_watches(",
            self.init_src,
            "_handle_sensor_watches callback is missing.",
        )
        # The handler MUST clear listeners on empty payload — otherwise
        # tearing down a sensor face leaks subscriptions for entities
        # the dial no longer cares about.
        self.assertIn(
            "_set_sensor_watch_listeners(hass, entry, dial_id, team_id, [])",
            self.init_src,
            "_handle_sensor_watches no longer clears listeners on empty "
            "payload — sensor-face teardown will leak state listeners.",
        )

    def test_listener_set_helper_warms_lut(self):
        # Without the one-shot push on bind, the dial would render "—"
        # until the first state_change event fires, which for slow
        # sensors (battery %, freezer temp) could be hours.
        self.assertIn(
            "def _set_sensor_watch_listeners(",
            self.init_src,
            "_set_sensor_watch_listeners helper is missing.",
        )
        helper_chunk = self.init_src.split("def _set_sensor_watch_listeners(", 1)[1]
        helper_chunk = helper_chunk.split("\ndef ", 1)[0]
        self.assertIn(
            "_push_sensor_value_for_entity",
            helper_chunk,
            "_set_sensor_watch_listeners no longer warms the LUT — "
            "first frame after face mount will show '—' until the next "
            "HA state change.",
        )
        self.assertIn(
            "async_track_state_change_event",
            helper_chunk,
            "_set_sensor_watch_listeners no longer registers state-change "
            "listeners — realtime sensor pushes will not fire.",
        )

    def test_store_field_separate_from_overlay(self):
        # The persistent face listeners MUST live in their own store
        # field so they coexist with the apply_overlay transient binding.
        # If both paths share ``_sensor_bindings`` the overlay's
        # last-writer-wins reset would tear down the persistent listeners.
        self.assertIn(
            '"_sensor_watch_bindings"',
            self.init_src,
            "_sensor_watch_bindings store field is missing or renamed — "
            "the realtime path will collide with apply_overlay.",
        )

    def test_unload_cleans_up_listeners(self):
        # async_unload_entry MUST unsub the sensor_watch bindings or
        # reloading the integration will leak listeners that publish to
        # MQTT topics whose ConfigEntry is gone.
        unload_chunk = self.init_src.split("async def async_unload_entry", 1)[1]
        unload_chunk = unload_chunk.split("\nasync def ", 1)[0]
        self.assertIn(
            '"_sensor_watch_bindings"',
            unload_chunk,
            "async_unload_entry no longer cleans up sensor_watch bindings — "
            "reloading the integration will leak HA state listeners.",
        )


if __name__ == "__main__":
    unittest.main()
