"""R1 executor-arbitration: HACS presence-beacon tests.

Two layers, matching repo convention (no HA runtime in this bare env):

1. **Pure-module** tests of ``_presence`` — the payload builder + the
   ``owns`` capability list. Loaded by path so the package ``__init__``
   (which imports ``homeassistant.*``) never has to load.
2. **Source-level** wiring guards on ``__init__.py`` / ``const.py`` /
   ``manifest.json`` — the same style as ``test_sensor_watches_wiring.py``.
   These pin that the beacon is actually published on setup, re-stamped on
   an interval, and empty-cleared on unload; renaming any of it silently
   breaks arbitration and reintroduces the media double-execute.

Run with:  python3 -m pytest tests/test_hacs_presence.py
       or:  python3 -m unittest tests.test_hacs_presence
"""

from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "deckhand"
INIT_PY = PKG / "__init__.py"
CONST_PY = PKG / "const.py"
MANIFEST = PKG / "manifest.json"

# Load _presence.py by path so we don't trip the package __init__, which
# imports homeassistant.* (absent in this bare test env).
_spec = importlib.util.spec_from_file_location(
    "deckhand_presence", PKG / "_presence.py"
)
_presence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_presence)

build_presence_payload = _presence.build_presence_payload
PRESENCE_OWNS = _presence.PRESENCE_OWNS
PRESENCE_INTERVAL_S = _presence.PRESENCE_INTERVAL_S
PRESENCE_TTL_S = _presence.PRESENCE_TTL_S
hacs_version = _presence.hacs_version


# ── Pure payload / capability logic ─────────────────────────────────

class PresencePayloadTests(unittest.TestCase):
    def test_payload_has_the_full_contract_shape(self):
        p = build_presence_payload("entry-abc", ts=1753718400)
        self.assertEqual(
            set(p),
            {"online", "ts", "interval_s", "version", "entry_id", "owns"},
        )
        self.assertIs(p["online"], True)
        self.assertEqual(p["ts"], 1753718400)
        self.assertEqual(p["interval_s"], PRESENCE_INTERVAL_S)
        self.assertEqual(p["entry_id"], "entry-abc")
        self.assertEqual(p["owns"], ["media_player"])

    def test_ts_is_int_epoch_seconds(self):
        # Wall-clock int is what makes a retained beacon staleness-safe.
        p = build_presence_payload("e", ts=1753718400.9)
        self.assertIsInstance(p["ts"], int)
        self.assertEqual(p["ts"], 1753718400)

    def test_ts_defaults_to_now(self):
        import time

        before = int(time.time())
        p = build_presence_payload("e")
        self.assertGreaterEqual(p["ts"], before)

    def test_owns_is_a_copy_not_the_module_list(self):
        # A caller mutating the payload must not corrupt PRESENCE_OWNS.
        p = build_presence_payload("e", ts=0)
        p["owns"].append("light")
        self.assertEqual(PRESENCE_OWNS, ["media_player"])

    def test_version_defaults_to_manifest_version(self):
        p = build_presence_payload("e", ts=0)
        self.assertEqual(p["version"], hacs_version())

    def test_version_override_is_respected(self):
        p = build_presence_payload("e", ts=0, version="9.9.9")
        self.assertEqual(p["version"], "9.9.9")


class OwnsCapabilityTests(unittest.TestCase):
    def test_r1_owns_media_player_only(self):
        # GUARD: R3 widens this (light/fan/cover/…) only once the SDK mapper
        # exists AND __init__.py actually dispatches those domains. Shipping
        # a wider ``owns`` before that makes Helm/Console defer domains HACS
        # can't execute → dropped actions. Keep it media-only for R1.
        self.assertEqual(PRESENCE_OWNS, ["media_player"])

    def test_ttl_is_three_intervals(self):
        self.assertEqual(PRESENCE_TTL_S, PRESENCE_INTERVAL_S * 3)
        self.assertEqual(PRESENCE_INTERVAL_S, 30)
        self.assertEqual(PRESENCE_TTL_S, 90)


class ManifestTests(unittest.TestCase):
    def test_manifest_version_is_1_9_9(self):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "1.9.9")

    def test_hacs_version_reads_manifest(self):
        self.assertEqual(hacs_version(), "1.9.9")


# ── Source-level wiring guards ──────────────────────────────────────

class PresenceWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.init_src = INIT_PY.read_text(encoding="utf-8")
        cls.const_src = CONST_PY.read_text(encoding="utf-8")

    def test_topic_constant_declared(self):
        self.assertIn(
            'TOPIC_HACS_PRESENCE = "deckhand/{team_id}/hacs/presence"',
            self.const_src,
            "TOPIC_HACS_PRESENCE missing/wrong — Helm+Console subscribe to "
            "this exact per-team topic.",
        )

    def test_beacon_seeded_on_setup(self):
        # The publisher must be defined and awaited once during setup so a
        # running Helm/Console starts deferring immediately (not after one
        # full interval).
        self.assertIn("async def _publish_presence(", self.init_src)
        setup_chunk = self.init_src.split("async def async_setup_entry", 1)[1]
        setup_chunk = setup_chunk.split("\nasync def ", 1)[0]
        self.assertIn(
            "await _publish_presence()",
            setup_chunk,
            "presence beacon is never seeded during async_setup_entry.",
        )

    def test_interval_republish_registered(self):
        pattern = re.compile(
            r"async_track_time_interval\(\s*hass\s*,\s*_publish_presence\s*,",
            re.DOTALL,
        )
        self.assertTrue(
            pattern.search(self.init_src),
            "presence beacon is not re-stamped on an interval — it would go "
            "stale after TTL and Helm would resume even though HACS is alive.",
        )
        # Registered through async_on_unload so the timer is cancelled on
        # teardown (no leaked publisher after reload).
        self.assertIn(
            "entry.async_on_unload(\n        async_track_time_interval(",
            self.init_src,
            "interval publisher must be registered via entry.async_on_unload.",
        )

    def test_beacon_published_retained_qos1(self):
        pub_chunk = self.init_src.split("async def _publish_presence(", 1)[1]
        pub_chunk = pub_chunk.split("\n    await _publish_presence()", 1)[0]
        self.assertIn("presence_topic", pub_chunk)
        self.assertIn("qos=1", pub_chunk)
        self.assertIn("retain=True", pub_chunk)

    def test_empty_clear_on_unload(self):
        unload_chunk = self.init_src.split("async def async_unload_entry", 1)[1]
        unload_chunk = unload_chunk.split("\nasync def ", 1)[0]
        self.assertIn(
            "TOPIC_HACS_PRESENCE.format(team_id=entry.data[CONF_TEAM_ID])",
            unload_chunk,
            "async_unload_entry no longer clears the presence beacon — a "
            "clean HACS shutdown would leave a stale retained 'present' up "
            "to the TTL, delaying Helm's resume.",
        )
        self.assertIn('retain=True', unload_chunk)

    def test_nfc_raw_refire_untouched(self):
        # The gate is additive — the raw deckhand_dial_event re-fire (the
        # automation/NFC contract) and the media dispatch must both survive.
        self.assertIn('hass.bus.async_fire(', self.init_src)
        self.assertIn("_dispatch_dial_media_control(hass, payload)", self.init_src)


if __name__ == "__main__":
    unittest.main()
