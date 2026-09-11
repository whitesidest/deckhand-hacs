"""Every Deckhand entity follows the dial's heartbeats.

The status handler in ``__init__.py`` REPLACES ``store["dials"][dial_id]``
with a new dict on each heartbeat, so an entity only sees fresh data if it
listens for ``deckhand_status_update``. The Reboot button and Brightness
slider never did. They kept the snapshot they were created with, so they
read "available" forever after startup and "unavailable" forever after HA
re-added them. Renaming ``button.deckhand_deck_3140_reboot_2`` to drop the
``_2`` did exactly that on 2026-09-10.

The listener now lives once in ``DeckhandEntity``. These tests run the real
entity classes against a minimal stand-in for Home Assistant (there is no HA
runtime in this test env), so they exercise behaviour, not source text.

Run with:  python3 -m pytest tests/test_entities_follow_heartbeats.py
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import importlib.machinery
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "deckhand"
PKG = "deckhand_heartbeat_under_test"


# ---------------------------------------------------------------- HA stand-in
class _Anything(type):
    """Metaclass for placeholder HA classes: any class attribute resolves."""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _placeholder(name)


def _placeholder(name):
    def __init__(self, *args, **kwargs):
        self.__dict__.update(kwargs)

    return _Anything(name, (), {"__init__": __init__})


class _Entity:
    """Just enough of homeassistant.helpers.entity.Entity."""

    hass = None

    def __init__(self, *args, **kwargs):
        pass

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, func):
        self._removers = getattr(self, "_removers", []) + [func]

    def async_write_ha_state(self):
        self.writes = getattr(self, "writes", 0) + 1


class _Bus:
    def __init__(self):
        self.listeners: dict[str, list] = {}

    def async_listen(self, event_type, handler):
        self.listeners.setdefault(event_type, []).append(handler)
        return lambda: self.listeners[event_type].remove(handler)

    def fire(self, event_type, data):
        for handler in list(self.listeners.get(event_type, [])):
            handler(types.SimpleNamespace(data=data))


class _Hass:
    def __init__(self):
        self.bus = _Bus()
        self.data = {}


class _HAFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serve any ``homeassistant.*`` import as a module of placeholders."""

    def find_spec(self, fullname, path, target=None):
        if fullname == "homeassistant" or fullname.startswith("homeassistant."):
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        mod = types.ModuleType(spec.name)
        mod.__path__ = []
        mod.__getattr__ = _placeholder
        return mod

    def exec_module(self, module):
        name = module.__name__
        if name == "homeassistant.core":
            module.callback = lambda func: func
        elif name == "homeassistant.helpers.entity":
            module.Entity = _Entity
            module.DeviceInfo = lambda **kwargs: kwargs


def _load_platforms():
    if not any(isinstance(f, _HAFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _HAFinder())
    # A package object pointing at the real directory, WITHOUT executing its
    # __init__.py (which needs a live HA + MQTT).
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[PKG] = pkg
    return {
        name: importlib.import_module(f"{PKG}.{name}")
        for name in ("entity", "button", "number", "sensor", "binary_sensor", "select")
    }


MODS = _load_platforms()
EVENT = "deckhand_status_update"
DIAL = "DECK-3140"


def _seen(ago_s: float) -> str:
    return (datetime.now() - timedelta(seconds=ago_s)).isoformat()


def _data(ago_s: float, **extra) -> dict:
    return {"online": True, "_last_seen": _seen(ago_s), **extra}


def _entry():
    return types.SimpleNamespace(entry_id="entry-1")


def _all_entities(data):
    sensor_def = MODS["sensor"].SENSOR_DEFINITIONS[0]
    return {
        "button": MODS["button"].DeckhandRebootButton(DIAL, dict(data), _entry()),
        "number": MODS["number"].DeckhandBrightnessNumber(DIAL, dict(data), _entry()),
        "sensor": MODS["sensor"].DeckhandSensor(DIAL, dict(data), sensor_def),
        "connectivity": MODS["binary_sensor"].DeckhandConnectivitySensor(DIAL, dict(data)),
        "dnd": MODS["binary_sensor"].DeckhandDndSensor(DIAL, dict(data)),
        "select": MODS["select"].DeckhandThemeSelect(DIAL, dict(data), _entry()),
    }


def _add(entity, hass):
    entity.hass = hass
    asyncio.run(entity.async_added_to_hass())
    return entity


class EntitiesFollowHeartbeats(unittest.TestCase):
    def test_a_readded_button_recovers_on_the_next_heartbeat(self):
        # The founder's case: HA re-adds the entity (rename) holding the
        # creation-time snapshot, whose heartbeat is long stale.
        hass = _Hass()
        button = _add(MODS["button"].DeckhandRebootButton(DIAL, _data(600), _entry()), hass)
        self.assertFalse(button.available, "precondition: the snapshot is stale")

        hass.bus.fire(EVENT, {"dial_id": DIAL, "data": _data(1)})

        self.assertTrue(button.available, "Reboot stayed unavailable after a fresh heartbeat")
        self.assertGreaterEqual(getattr(button, "writes", 0), 1, "state was never re-written")

    def test_the_brightness_slider_recovers_too(self):
        hass = _Hass()
        number = _add(MODS["number"].DeckhandBrightnessNumber(DIAL, _data(600), _entry()), hass)
        hass.bus.fire(EVENT, {"dial_id": DIAL, "data": _data(1)})
        self.assertTrue(number.available)

    def test_a_button_created_fresh_notices_the_dial_going_quiet(self):
        # The other half: before the fix a startup snapshot made the button
        # look available forever. A heartbeat carrying an old timestamp must
        # now be able to take it away.
        hass = _Hass()
        button = _add(MODS["button"].DeckhandRebootButton(DIAL, _data(1), _entry()), hass)
        self.assertTrue(button.available)
        hass.bus.fire(EVENT, {"dial_id": DIAL, "data": _data(600)})
        self.assertFalse(button.available)

    def test_another_dials_heartbeat_is_ignored(self):
        hass = _Hass()
        button = _add(MODS["button"].DeckhandRebootButton(DIAL, _data(600), _entry()), hass)
        hass.bus.fire(EVENT, {"dial_id": "DECK-45E8", "data": _data(1)})
        self.assertFalse(button.available)
        self.assertEqual(getattr(button, "writes", 0), 0)

    def test_every_entity_listens_exactly_once(self):
        # Exactly one listener per entity: none means stale data, two means
        # a platform re-grew its own copy of the handler (double writes).
        for name, entity in _all_entities(_data(1)).items():
            with self.subTest(entity=name):
                hass = _Hass()
                _add(entity, hass)
                self.assertEqual(len(hass.bus.listeners.get(EVENT, [])), 1)

    def test_every_entity_takes_the_new_data(self):
        for name, entity in _all_entities(_data(600)).items():
            with self.subTest(entity=name):
                hass = _Hass()
                _add(entity, hass)
                fresh = _data(1, dnd=False, current_theme="komorebi")
                hass.bus.fire(EVENT, {"dial_id": DIAL, "data": fresh})
                self.assertIs(entity._dial_data, fresh)

    def test_the_theme_select_still_follows_the_applied_theme(self):
        hass = _Hass()
        select = _add(MODS["select"].DeckhandThemeSelect(DIAL, _data(1), _entry()), hass)
        hass.bus.fire(EVENT, {"dial_id": DIAL, "data": _data(1, current_theme="not-in-catalog-xyz")})
        self.assertEqual(select._attr_current_option, "not-in-catalog-xyz")
        self.assertIn("not-in-catalog-xyz", select._attr_options)

    def test_the_connectivity_sensor_still_reports_online(self):
        hass = _Hass()
        conn = _add(MODS["binary_sensor"].DeckhandConnectivitySensor(DIAL, _data(600)), hass)
        self.assertFalse(conn.is_on)
        hass.bus.fire(EVENT, {"dial_id": DIAL, "data": _data(1)})
        self.assertTrue(conn.is_on)


if __name__ == "__main__":
    unittest.main()
