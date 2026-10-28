"""Diagnostics, the Brightness slider, device info and entity names (1.24.0).

Runs the real platform modules against the Home Assistant stand-in from
test_entities_follow_heartbeats.py. Pins:

* diagnostics: team id, each dial's last heartbeat / firmware / ring mount /
  sensor watches / media bindings, with credential- and network-like keys
  redacted, for the whole entry and for one dial or Room device;
* Brightness: shows the night-dim cap the heartbeat reports (``null`` =
  no cap = 100), restores the last value when the dial doesn't report one,
  and is otherwise unknown — never the made-up 80 it used to start at;
* device info: ``hw_version`` and ``suggested_area`` from the heartbeat,
  and no ``configuration_url`` (no LAN-IP link);
* entity names come from translations whose English is exactly the old
  fixed names, so names and entity_ids don't move; the Theme sensor is
  diagnostic.

Run with:  python3 -m pytest tests/test_diagnostics_and_entities.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import unittest
from collections.abc import Mapping

import test_entities_follow_heartbeats as standin

MODS = standin.MODS
DIAL = standin.DIAL
REDACTED = "**REDACTED**"


def _redact(data, to_redact):
    """Same contract as homeassistant.components.diagnostics.async_redact_data."""
    if isinstance(data, list):
        return [_redact(v, to_redact) for v in data]
    if not isinstance(data, Mapping):
        return data
    out = {}
    for key, value in data.items():
        if key in to_redact and value is not None:
            out[key] = REDACTED
        elif isinstance(value, (Mapping, list)):
            out[key] = _redact(value, to_redact)
        else:
            out[key] = value
    return out


def _load_diagnostics():
    importlib.import_module("homeassistant.components.diagnostics").async_redact_data = _redact
    sys.modules.pop(f"{standin.PKG}.diagnostics", None)
    return importlib.import_module(f"{standin.PKG}.diagnostics")


DIAG = _load_diagnostics()

HEARTBEAT = {
    "online": True,
    "fw_ver": "0.4.220",
    "hardware_type": "amoled175",
    "battery_pct": 81,
    "ip": "192.168.1.77",
    "mac": "AA:BB:CC:DD:EE:FF",
    "ssid": "HomeNet",
    "bssid": "11:22:33:44:55:66",
    "mqtt_user": "dial-7c2c",
    "brightness_cap": 30,
    "_last_seen": "2026-10-28T08:00:00",
}


def _hass_and_entry():
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        title="Deckhand (Team 1)",
        data={"team_id": "1"},
        options={"media_player_bindings": [
            {"dial_device_id": "dev-dial", "entity_id": "media_player.office"},
            {"dial_device_id": "dev-other", "entity_id": "media_player.kitchen"},
        ]},
    )
    store = {
        "team_id": "1",
        "dials": {DIAL: dict(HEARTBEAT)},
        "labels": {DIAL: "Office"},
        "themes": [{"slug": "elysian"}, {"slug": "vault"}],
        "groups": {"7": {"id": 7, "name": "Office", "dial_ids": [DIAL]}},
        "_perimeter_mounted": {DIAL: True},
        "_perimeter_feeds": {DIAL: {"team_id": "1", "bindings": [{"id": "lock.front"}], "unsub": None}},
        "_sensor_watch_bindings": {DIAL: {"entity_ids": ["sensor.pool"], "unsub": None}},
    }
    hass = types.SimpleNamespace(data={"deckhand": {"entry-1": store}})
    return hass, entry


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.hass, self.entry = _hass_and_entry()

    def test_entry_diagnostics(self):
        out = asyncio.run(DIAG.async_get_config_entry_diagnostics(self.hass, self.entry))
        self.assertEqual(out["team_id"], "1")
        self.assertEqual(out["theme_catalog_size"], 2)
        (dial,) = out["dials"]
        self.assertEqual(dial["dial_id"], DIAL)
        self.assertEqual(dial["label"], "Office")
        self.assertEqual(dial["fw_ver"], "0.4.220")
        self.assertEqual(dial["hardware_type"], "amoled175")
        self.assertEqual(dial["last_heartbeat"]["battery_pct"], 81)
        self.assertTrue(dial["perimeter_ring"]["mount_retained"])
        self.assertEqual(dial["perimeter_ring"]["live_fed_bindings"], ["lock.front"])
        self.assertEqual(dial["sensor_watches"], ["sensor.pool"])
        self.assertEqual(len(out["media_player_bindings"]), 2)
        json.dumps(out)  # serialisable: no unsub callables leak through

    def test_network_and_credential_fields_are_redacted(self):
        out = asyncio.run(DIAG.async_get_config_entry_diagnostics(self.hass, self.entry))
        hb = out["dials"][0]["last_heartbeat"]
        for key in ("ip", "mac", "ssid", "bssid", "mqtt_user"):
            with self.subTest(key=key):
                self.assertEqual(hb[key], REDACTED)
        self.assertNotIn("192.168.1.77", json.dumps(out))

    def test_device_diagnostics_for_a_dial(self):
        device = types.SimpleNamespace(id="dev-dial", identifiers={("deckhand", DIAL)})
        out = asyncio.run(DIAG.async_get_device_diagnostics(self.hass, self.entry, device))
        self.assertEqual(out["dial"]["dial_id"], DIAL)
        self.assertEqual(
            [b["entity_id"] for b in out["media_player_bindings"]], ["media_player.office"]
        )

    def test_device_diagnostics_for_a_room(self):
        device = types.SimpleNamespace(id="dev-room", identifiers={("deckhand", "group:7")})
        out = asyncio.run(DIAG.async_get_device_diagnostics(self.hass, self.entry, device))
        self.assertEqual(out["room"], {"name": "Office", "dial_ids": [DIAL]})

    def test_a_dial_with_nothing_known_yet(self):
        self.hass.data["deckhand"]["entry-1"] = {"team_id": "1", "labels": {"DECK-NEW1": "New"}}
        out = asyncio.run(DIAG.async_get_config_entry_diagnostics(self.hass, self.entry))
        (dial,) = out["dials"]
        self.assertIsNone(dial["last_heartbeat"])
        self.assertIsNone(dial["perimeter_ring"]["mount_retained"])
        self.assertEqual(dial["sensor_watches"], [])


def _number(data):
    return MODS["number"].DeckhandBrightnessNumber(DIAL, data, types.SimpleNamespace(entry_id="entry-1"))


class BrightnessReflectsTheDial(unittest.TestCase):
    def test_reads_the_reported_cap(self):
        self.assertEqual(_number(standin._data(1, brightness_cap=30))._attr_native_value, 30.0)

    def test_no_cap_is_100(self):
        self.assertEqual(_number(standin._data(1, brightness_cap=None))._attr_native_value, 100.0)

    def test_not_reported_is_unknown_not_80(self):
        self.assertIsNone(_number(standin._data(1))._attr_native_value)

    def test_the_backlight_level_is_not_the_slider(self):
        # brightness_pct dips whenever the dial idles into its dim level.
        self.assertIsNone(_number(standin._data(1, brightness_pct=12))._attr_native_value)

    def test_restores_the_last_value_when_not_reported(self):
        number = _number(standin._data(1))
        number._last_state = types.SimpleNamespace(state="45.0")
        standin._add(number, standin._Hass())
        self.assertEqual(number._attr_native_value, 45.0)

    def test_an_unknown_last_state_stays_unknown(self):
        number = _number(standin._data(1))
        number._last_state = types.SimpleNamespace(state="unavailable")
        standin._add(number, standin._Hass())
        self.assertIsNone(number._attr_native_value)

    def test_a_reported_cap_beats_the_restored_value(self):
        number = _number(standin._data(1, brightness_cap=20))
        number._last_state = types.SimpleNamespace(state="90")
        standin._add(number, standin._Hass())
        self.assertEqual(number._attr_native_value, 20.0)

    def test_follows_the_heartbeat(self):
        hass = standin._Hass()
        number = standin._add(_number(standin._data(1)), hass)
        hass.bus.fire(standin.EVENT, {"dial_id": DIAL, "data": standin._data(1, brightness_cap=10)})
        self.assertEqual(number._attr_native_value, 10.0)
        # A heartbeat without the key leaves the slider where it is.
        hass.bus.fire(standin.EVENT, {"dial_id": DIAL, "data": standin._data(1)})
        self.assertEqual(number._attr_native_value, 10.0)

    def test_junk_is_ignored(self):
        f = MODS["number"].brightness_from_status
        self.assertIsNone(f({"brightness_cap": "30"}))
        self.assertIsNone(f({"brightness_cap": True}))
        self.assertEqual(f({"brightness_cap": 250}), 100.0)


class DeviceInfo(unittest.TestCase):
    def test_hw_version_and_area(self):
        info = MODS["entity"].dial_device_info(DIAL, {**HEARTBEAT, "_room": "Office", "_label": "Desk"})
        self.assertEqual(info["hw_version"], "amoled175")
        self.assertEqual(info["model"], "AMOLED 1.75in")
        self.assertEqual(info["suggested_area"], "Office")
        self.assertEqual(info["name"], f"Desk ({DIAL})")
        self.assertEqual(info["sw_version"], "0.4.220")

    def test_never_a_lan_link(self):
        info = MODS["entity"].dial_device_info(DIAL, HEARTBEAT)
        self.assertNotIn("configuration_url", info)
        self.assertNotIn("suggested_area", info)

    def test_unknown_hardware_has_no_hw_version(self):
        info = MODS["entity"].dial_device_info(DIAL, {"hardware_type": "unknown"})
        self.assertNotIn("hw_version", info)
        self.assertEqual(info["model"], "Deckhand Dial")


# The names entities had before 1.24.0 (fixed _attr_name strings). The
# English translations must reproduce them exactly.
OLD_NAMES = {
    ("binary_sensor", "connectivity"): "Connectivity",
    ("binary_sensor", "do_not_disturb"): "Do Not Disturb",
    ("button", "reboot"): "Reboot",
    ("event", "timer"): "Timer",
    ("number", "brightness"): "Brightness",
    ("select", "theme"): "Theme",
    ("sensor", "battery"): "Battery",
    ("sensor", "wifi_signal"): "WiFi Signal",
    ("sensor", "temperature"): "Temperature",
    ("sensor", "humidity"): "Humidity",
    ("sensor", "ambient_light"): "Ambient Light",
    ("sensor", "theme"): "Theme",
}


class EntityNamesUnchanged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        strings = json.loads((standin.PKG_DIR / "strings.json").read_text(encoding="utf-8"))
        cls.entity = strings["entity"]

    def test_translations_reproduce_the_old_names(self):
        for (platform, key), name in OLD_NAMES.items():
            with self.subTest(platform=platform, key=key):
                self.assertEqual(self.entity[platform][key]["name"], name)

    def test_every_entity_uses_a_translated_name(self):
        entry = types.SimpleNamespace(entry_id="entry-1")
        data = standin._data(1)
        event_mod = importlib.import_module(f"{standin.PKG}.event")
        entities = {
            "binary_sensor": [
                MODS["binary_sensor"].DeckhandConnectivitySensor(DIAL, data),
                MODS["binary_sensor"].DeckhandDndSensor(DIAL, data),
            ],
            "button": [MODS["button"].DeckhandRebootButton(DIAL, data, entry)],
            "event": [event_mod.DeckhandTimerEvent(DIAL, data)],
            "number": [MODS["number"].DeckhandBrightnessNumber(DIAL, data, entry)],
            "select": [MODS["select"].DeckhandThemeSelect(DIAL, data, entry)],
            "sensor": [MODS["sensor"].DeckhandSensor(DIAL, data, d) for d in MODS["sensor"].SENSOR_DEFINITIONS],
        }
        for platform, items in entities.items():
            for entity in items:
                with self.subTest(platform=platform, entity=type(entity).__name__):
                    # An _attr_name would override the translation.
                    self.assertFalse(hasattr(entity, "_attr_name"))
                    self.assertTrue(entity._attr_has_entity_name)
                    self.assertIn(entity._attr_translation_key, self.entity[platform])

    def test_unique_ids_unchanged(self):
        entry = types.SimpleNamespace(entry_id="entry-1")
        data = standin._data(1)
        self.assertEqual(
            MODS["number"].DeckhandBrightnessNumber(DIAL, data, entry)._attr_unique_id,
            f"deckhand_{DIAL}_brightness",
        )
        self.assertEqual(
            {MODS["sensor"].DeckhandSensor(DIAL, data, d)._attr_unique_id
             for d in MODS["sensor"].SENSOR_DEFINITIONS},
            {f"deckhand_{DIAL}_{k}" for k in
             ("battery_pct", "rssi", "temperature_c", "humidity_pct", "ambient_lux", "current_theme")},
        )

    def test_theme_sensor_is_diagnostic(self):
        (theme,) = [d for d in MODS["sensor"].SENSOR_DEFINITIONS if d["key"] == "current_theme"]
        # The stand-in's EntityCategory members are placeholders named after
        # the member, so compare by name.
        self.assertEqual(getattr(theme["entity_category"], "__name__", None), "DIAGNOSTIC")


if __name__ == "__main__":
    unittest.main()
