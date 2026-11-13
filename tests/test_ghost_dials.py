"""Ghost dials: Repairs stop calling them offline; HA can delete them (1.27.0).

The founder's Repairs list showed five dials "offline for 56 hours". Four
were not on the Console this Home Assistant talks to at all (deregistered,
or moved to UAT / prod); they were leftovers in the device registry, re-armed
on every restart and all showing the hours since that restart. And the
device page's Delete was greyed out: the integration had no
``async_remove_config_entry_device``.

These run the REAL ``async_setup_entry`` handlers (status, cmd/config,
lifecycle), the real offline check and the real
``async_remove_config_entry_device`` against the stand-ins from
test_reconfigure_team_id.py.

Run with:  python3 -m pytest tests/test_ghost_dials.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

import test_emoji_strip_every_service as harness
import test_reconfigure_team_id as reconf
from test_repairs_and_climate import _IssueRegistry

MOD = harness.MOD
ISS = sys.modules[f"{harness.PKG}._issues"]
REP = sys.modules[f"{harness.PKG}._repairs"]
DOMAIN = "deckhand"
TEAM = "local"


class GhostDials(unittest.TestCase):
    def setUp(self):
        self.entry = reconf._Entry("entry-home", TEAM)
        self.hass = reconf._Hass([self.entry])
        self.mqtt = reconf._Mqtt()
        self.registry = reconf._Registry()
        reconf.ReconfigureFlow._patch_setup(self, self.mqtt, self.registry)

        self.ir = _IssueRegistry()
        dr = types.SimpleNamespace(
            async_get=lambda hass: self.registry,
            async_entries_for_config_entry=lambda reg, entry_id: [
                d for d in reg.devices.values() if entry_id in d.config_entries
            ],
        )
        for name, value in (("ir", self.ir), ("dr", dr)):
            p = mock.patch.object(ISS, name, value)
            p.start()
            self.addCleanup(p.stop)

        # Capture the periodic offline check instead of scheduling it.
        self.ticks = []

        def _interval(hass, func, interval):
            self.ticks.append(func)
            return lambda: None

        MOD.async_track_time_interval = _interval  # restored by _patch_setup

        # In the registry from earlier sessions. E994 and 4354 are left over
        # from earlier setups and never on this server; 3AC0 is.
        for dial_id in ("DECK-E994", "DECK-4354", "DECK-3AC0"):
            self.registry.async_get_or_create(
                config_entry_id=self.entry.entry_id, identifiers={(DOMAIN, dial_id)})
        asyncio.run(MOD.async_setup_entry(self.hass, self.entry))
        self.store = self.hass.data[DOMAIN][self.entry.entry_id]

    # ------------------------------------------------------------ helpers
    def deliver(self, topic, payload, *, retain=False):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        hits = 0
        for pattern, cb in list(self.mqtt.subs.values()):
            if self.mqtt._matches(pattern, topic):
                cb(types.SimpleNamespace(topic=topic, payload=raw, retain=retain))
                hits += 1
        self.assertTrue(hits, topic)

    def status(self, dial_id, *, retain=False):
        self.deliver(f"deckhand/{TEAM}/dial/{dial_id}/status",
                     {"device_id": dial_id, "online": True, "fw_ver": "0.4.200"}, retain=retain)

    def config(self, dial_id, payload):
        self.deliver(f"deckhand/{TEAM}/dial/{dial_id}/cmd/config", payload, retain=True)

    def check(self, hours):
        """The offline check as if ``hours`` had passed since startup."""
        later = self.store["_watching_since"] + timedelta(hours=hours)
        with mock.patch.object(ISS, "datetime", _FrozenDatetime(later)):
            for tick in self.ticks:
                asyncio.run(tick())

    def issues(self, prefix):
        return sorted(i.removeprefix(prefix) for i in self.ir.issues if i.startswith(prefix))

    def remove(self, dial_or_group):
        device = self.registry.async_get_device({(DOMAIN, dial_or_group)})
        return asyncio.run(MOD.async_remove_config_entry_device(self.hass, self.entry, device))

    # ------------------------------------------------------------ tests
    def test_ghosts_are_not_on_the_server_live_dials_are_offline(self):
        self.assertTrue(self.ticks, "the offline check is not scheduled")
        # On the server: retained config (labelled) and a retained status, but
        # silent since startup.
        self.config("DECK-E77C", {"label": "Office"})
        self.status("DECK-E77C", retain=True)
        # Retained config only (the server has it; no status at all), and a
        # config for a dial HA never registered (nothing to flag).
        self.config("DECK-3AC0", {"label": "Hall", "tz": "UTC0"})
        self.config("DECK-NOPE", {"tz": "UTC0"})

        self.check(0.1)  # inside the startup grace: nothing decided
        self.assertEqual(self.ir.issues, {})

        self.check(56)
        self.assertEqual(self.issues("dial_not_on_server_"), ["DECK-4354", "DECK-E994"])
        self.assertEqual(self.issues("dial_offline_"), ["DECK-3AC0", "DECK-E77C"])
        office = self.ir.issues["dial_offline_DECK-E77C"]
        # A retained status replay is not a report: the wording says the
        # count starts at Home Assistant's startup.
        self.assertEqual(office["translation_key"], "dial_silent_since_start")
        self.assertEqual(office["translation_placeholders"], {"dial": "Office (DECK-E77C)", "hours": "56"})

        # A live heartbeat clears the offline notice...
        self.status("DECK-E77C")
        self.assertNotIn("dial_offline_DECK-E77C", self.ir.issues)
        # ...and once it has gone quiet for a day, the wording counts from it.
        self.check(80)
        self.assertEqual(self.ir.issues["dial_offline_DECK-E77C"]["translation_key"], "dial_offline")

        # The server publishes a ghost's config after all: its notice goes.
        self.config("DECK-4354", {"label": "Back"})
        self.assertNotIn("dial_not_on_server_DECK-4354", self.ir.issues)

    def test_an_emptied_config_is_a_deregistration(self):
        self.config("DECK-E77C", {"label": "Office"})
        self.status("DECK-E77C")
        self.check(30)
        self.assertIn("dial_offline_DECK-E77C", self.ir.issues)

        self.config("DECK-E77C", "")  # Console clear_retained_for_device
        self.assertNotIn("dial_offline_DECK-E77C", self.ir.issues)
        self.assertTrue(self.ir.issues["dial_not_on_server_DECK-E77C"]["is_fixable"])
        # Never auto-deleted: the device stays until the person removes it.
        self.assertIsNotNone(self.registry.async_get_device({(DOMAIN, "DECK-E77C")}))
        self.check(31)
        self.assertNotIn("dial_offline_DECK-E77C", self.ir.issues)

        # An emptied config for a dial HA never knew raises nothing.
        self.config("DECK-ZZZZ", "")
        self.assertNotIn("dial_not_on_server_DECK-ZZZZ", self.ir.issues)

    def test_delete_is_allowed_for_a_ghost_and_cleans_up(self):
        self.check(30)
        self.assertIn("dial_not_on_server_DECK-E994", self.ir.issues)
        self.assertTrue(self.remove("DECK-E994"))
        self.assertNotIn("dial_not_on_server_DECK-E994", self.ir.issues)

    def test_delete_is_refused_for_a_dial_heard_in_the_last_minutes(self):
        self.config("DECK-E77C", {"label": "Office"})
        self.status("DECK-E77C")
        self.assertFalse(self.remove("DECK-E77C"))
        self.assertIn("DECK-E77C", self.store["dials"])
        # Heard six minutes ago: allowed, and the store forgets it.
        self.store["_live_seen"]["DECK-E77C"] = (datetime.now() - timedelta(minutes=6)).isoformat()
        self.store["_fw_gaps"]["DECK-E77C"] = ["dnd"]
        self.ir.issues["firmware_too_old_DECK-E77C"] = {}
        self.assertTrue(self.remove("DECK-E77C"))
        for key in ("dials", "labels", "_fw_gaps", "_live_seen", "_server"):
            self.assertNotIn("DECK-E77C", self.store[key], key)
        self.assertEqual(self.ir.issues, {})

    def test_a_retained_status_alone_does_not_block_delete(self):
        self.status("DECK-E77C", retain=True)
        self.assertTrue(self.remove("DECK-E77C"))

    def test_rooms_are_deletable_only_once_the_server_dropped_them(self):
        self.deliver(f"deckhand/{TEAM}/groups/list",
                     {"groups": [{"id": 12, "name": "Office", "dial_ids": []}]})
        self.assertFalse(self.remove("group:12"))
        # A room only an earlier server published (not in this list).
        self.registry.async_get_or_create(
            config_entry_id=self.entry.entry_id, identifiers={(DOMAIN, "group:99")})
        self.assertTrue(self.remove("group:99"))
        self.assertEqual(self.ir.issues, {})

    def test_lifecycle_deregister_forgets_the_store_too(self):
        self.config("DECK-E77C", {"label": "Office"})
        self.status("DECK-E77C")
        for handler in self.hass.bus.listeners[f"{DOMAIN}_dial_event"]:
            handler(types.SimpleNamespace(data={"dial_id": "DECK-E77C", "event_type": "deregister"}))
        self.assertIsNone(self.registry.async_get_device({(DOMAIN, "DECK-E77C")}))
        self.assertNotIn("DECK-E77C", self.store["dials"])
        self.assertNotIn("DECK-E77C", self.store["labels"])


class _FrozenDatetime:
    """``datetime`` with ``now()`` pinned (for _issues.check_offline)."""

    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def __getattr__(self, name):
        return getattr(datetime, name)


if __name__ == "__main__":
    unittest.main()
