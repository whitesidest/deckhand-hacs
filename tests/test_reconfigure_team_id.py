"""Reconfigure moves an entry to a new team IN PLACE (Helm -> Console cutover).

The cutover moves a home from ``deckhand/1/...`` (Helm) to
``deckhand/local/...`` (Deckhand Console). Deleting the entry and adding a
new one would mint new HA device ids for every dial and Room, and every
automation targeting them would break. ``async_step_reconfigure`` rewrites
the entry's team id, title and unique_id and reloads it; the device
identifiers (``dial_id`` and ``group:<id>``) never contained the team id, so
the devices survive.

These run the REAL ``config_flow.py`` and the REAL ``async_setup_entry`` /
``async_unload_entry`` against the Home Assistant stand-in from
test_entities_follow_heartbeats.py, with small fakes for the config-entry
manager, the device registry and MQTT (there is no HA runtime in this env).

Run with:  python3 -m pytest tests/test_reconfigure_team_id.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import unittest
import uuid
from unittest import mock

import test_entities_follow_heartbeats as standin  # installs the HA stand-in
import test_emoji_strip_every_service as harness  # the real __init__ under it

MOD = harness.MOD
DOMAIN = "deckhand"


# ------------------------------------------------------------ voluptuous stub
def _voluptuous_stub():
    """Enough of voluptuous for config_flow's schemas (not installed here)."""
    vol = types.ModuleType("voluptuous")

    class _Marker:
        def __init__(self, schema, default=None, **kwargs):
            self.schema = schema
            self.default = default

    class Schema:
        def __init__(self, schema, **kwargs):
            self.schema = schema

    vol.Schema = Schema
    vol.Required = _Marker
    vol.Optional = _Marker
    vol.All = lambda *a, **k: a
    vol.In = lambda *a, **k: a
    return vol


# ------------------------------------------------------------ ConfigFlow stub
class _FlowBase:
    """Just enough of homeassistant.config_entries.ConfigFlow."""

    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.handler = domain

    def __init__(self):
        self.context: dict = {}
        self.flow_id = "flow-self"
        self.unique_id = None

    def async_show_form(self, *, step_id, data_schema=None, errors=None,
                        description_placeholders=None):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema,
                "errors": errors or {},
                "description_placeholders": description_placeholders}

    def async_abort(self, *, reason, **kwargs):
        return {"type": "abort", "reason": reason}

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        for e in self.hass.config_entries.async_entries(DOMAIN):
            if e.unique_id == self.unique_id:
                raise _AbortFlow("already_configured")


class _AbortFlow(Exception):
    pass


def _load_config_flow():
    ce = importlib.import_module("homeassistant.config_entries")
    stubs = {"ConfigFlow": _FlowBase, "OptionsFlow": object,
             "ConfigEntry": object, "ConfigFlowResult": dict}
    saved = {k: ce.__dict__.get(k) for k in stubs}
    modules = {}
    try:
        importlib.import_module("voluptuous")
    except ImportError:
        modules["voluptuous"] = _voluptuous_stub()
    pkg_name = "deckhand_reconfigure_under_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(standin.PKG_DIR)]
    modules[pkg_name] = pkg
    try:
        for k, v in stubs.items():
            setattr(ce, k, v)
        with mock.patch.dict(sys.modules, modules):
            mod = importlib.import_module(f"{pkg_name}.config_flow")
            importlib.import_module(f"{pkg_name}.const")
    finally:
        for k, v in saved.items():
            if v is None:
                ce.__dict__.pop(k, None)
            else:
                setattr(ce, k, v)
    return mod


CF = _load_config_flow()


# ---------------------------------------------------------------- HA fakes
class _Entry:
    def __init__(self, entry_id, team_id):
        self.entry_id = entry_id
        self.domain = DOMAIN
        self.data = {"team_id": team_id}
        self.options = {}
        self.title = f"Deckhand (Team {team_id})"
        self.unique_id = f"deckhand_{team_id}"
        self._on_unload = []

    def async_on_unload(self, func):
        self._on_unload.append(func)

    def add_update_listener(self, listener):
        return lambda: None

    def run_unload(self):
        while self._on_unload:
            self._on_unload.pop()()


class _Flows:
    def __init__(self):
        self.progress: list[dict] = []
        self.aborted: list[str] = []

    def async_progress_by_handler(self, handler):
        return list(self.progress)

    def async_abort(self, flow_id):
        self.aborted.append(flow_id)


class _ConfigEntries:
    def __init__(self, hass, entries):
        self.hass = hass
        self._entries = {e.entry_id: e for e in entries}
        self.flow = _Flows()
        self.reloads: list[str] = []

    def async_entries(self, domain=None):
        if domain == "mqtt":
            return [object()]
        return list(self._entries.values())

    def async_get_entry(self, entry_id):
        return self._entries.get(entry_id)

    def async_update_entry(self, entry, **changes):
        for k, v in changes.items():
            setattr(entry, k, v)
        return True

    async def async_reload(self, entry_id):
        self.reloads.append(entry_id)
        entry = self._entries[entry_id]
        await MOD.async_unload_entry(self.hass, entry)
        entry.run_unload()
        await MOD.async_setup_entry(self.hass, entry)

    async def async_forward_entry_setups(self, entry, platforms):
        pass

    async def async_unload_platforms(self, entry, platforms):
        return True


class _Device:
    def __init__(self, identifiers, config_entry_id):
        self.id = uuid.uuid4().hex
        self.identifiers = set(identifiers)
        self.config_entries = {config_entry_id}
        self.name = None


class _Registry:
    def __init__(self):
        self.devices: dict[str, _Device] = {}
        self.removed: list[str] = []

    def async_get(self, device_id):
        return self.devices.get(device_id)

    def async_get_device(self, identifiers):
        for d in self.devices.values():
            if d.identifiers & set(identifiers):
                return d
        return None

    def async_get_or_create(self, *, config_entry_id, identifiers, name=None, **kw):
        d = self.async_get_device(identifiers)
        if d is None:
            d = _Device(identifiers, config_entry_id)
            self.devices[d.id] = d
        d.name = name
        return d

    def async_update_device(self, device_id, **kw):
        pass

    def async_remove_device(self, device_id):
        self.removed.append(device_id)
        self.devices.pop(device_id, None)


class _Mqtt:
    """Topic-matching broker double: subscribe, publish, deliver."""

    def __init__(self):
        self.subs: dict[int, tuple[str, object]] = {}
        self.published: list[tuple[str, str, bool]] = []
        self._next = 0

    async def async_subscribe(self, hass, topic, cb, qos=0):
        self._next += 1
        key = self._next
        self.subs[key] = (topic, cb)
        return lambda: self.subs.pop(key, None)

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    @staticmethod
    def _matches(pattern, topic):
        p, t = pattern.split("/"), topic.split("/")
        if len(p) != len(t):
            return False
        return all(a == "+" or a == b for a, b in zip(p, t))

    def deliver(self, topic, payload):
        hits = 0
        for pattern, cb in list(self.subs.values()):
            if self._matches(pattern, topic):
                cb(types.SimpleNamespace(topic=topic, payload=json.dumps(payload)))
                hits += 1
        return hits

    def topics(self):
        return sorted(t for t, _ in self.subs.values())


class _Hass:
    def __init__(self, entries):
        self.data = {}
        self.bus = standin._Bus()
        self.bus.async_fire = lambda *a, **k: None
        self.services = harness._Services()
        self.states = harness._States({})
        self.config_entries = _ConfigEntries(self, entries)

    def async_create_task(self, coro, *args, **kwargs):
        # Run reloads to completion; drop entity-add coroutines (no platforms).
        if getattr(coro, "__qualname__", "").endswith("async_reload"):
            return asyncio.get_running_loop().create_task(coro)
        coro.close()
        return None


# ---------------------------------------------------------------- flow tests
def _flow(hass, entry, *, modern: bool):
    flow = CF.DeckhandConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    if modern:
        # HA 2024.11+ helpers.
        flow._get_reconfigure_entry = lambda: hass.config_entries.async_get_entry(
            flow.context["entry_id"])

        def _update_reload_and_abort(e, *, reason, **kw):
            assert not kw, kw  # we pass nothing version-specific
            hass.async_create_task(hass.config_entries.async_reload(e.entry_id))
            return {"type": "abort", "reason": reason}

        flow.async_update_reload_and_abort = _update_reload_and_abort
    return flow


class ReconfigureFlow(unittest.TestCase):
    def setUp(self):
        self.entry = _Entry("entry-helm", "1")
        self.hass = _Hass([self.entry])
        # Patch out the setup side-effects the reload would hit.
        self.mqtt = _Mqtt()
        self.registry = _Registry()
        self._patch_setup(self, self.mqtt, self.registry)

    @staticmethod
    def _patch_setup(test, mqtt_double, registry):
        patches = {
            "mqtt": mqtt_double,
            "register_album_art_view": lambda hass: None,
            "async_track_time_interval": lambda *a, **k: (lambda: None),
            "dr": types.SimpleNamespace(async_get=lambda hass: registry),
        }
        saved = {k: getattr(MOD, k) for k in patches}
        for k, v in patches.items():
            setattr(MOD, k, v)
        test.addCleanup(lambda: [setattr(MOD, k, v) for k, v in saved.items()])

    def run_flow(self, flow, user_input=None):
        async def _go():
            result = await flow.async_step_reconfigure(user_input)
            # Let any scheduled reload finish.
            await asyncio.sleep(0)
            for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
                await t
            return result
        return asyncio.run(_go())

    def test_shows_form_prefilled_with_current_team(self):
        for modern in (True, False):
            with self.subTest(modern=modern):
                result = self.run_flow(_flow(self.hass, self.entry, modern=modern))
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["step_id"], "reconfigure")
                [marker] = result["data_schema"].schema.keys()
                self.assertEqual((marker.schema, marker.default), ("team_id", "1"))
                self.assertEqual(result["description_placeholders"],
                                 {"current_team_id": "1"})

    def test_changes_team_in_place_and_reloads(self):
        for modern in (True, False):
            with self.subTest(modern=modern):
                entry = _Entry("entry-helm", "1")
                hass = _Hass([entry])
                hass.config_entries.flow.progress = [
                    {"flow_id": "disc-local", "context": {"unique_id": "deckhand_local"}},
                    {"flow_id": "disc-other", "context": {"unique_id": "deckhand_7"}},
                ]
                result = self.run_flow(_flow(hass, entry, modern=modern),
                                       {"team_id": "  local "})
                self.assertEqual(result, {"type": "abort",
                                          "reason": "reconfigure_successful"})
                self.assertEqual(entry.entry_id, "entry-helm")
                self.assertEqual(entry.data, {"team_id": "local"})
                self.assertEqual(entry.title, "Deckhand (Team local)")
                self.assertEqual(entry.unique_id, "deckhand_local")
                self.assertEqual(hass.config_entries.reloads, ["entry-helm"])
                # The "Discovered" card for the new team is dropped, others kept.
                self.assertEqual(hass.config_entries.flow.aborted, ["disc-local"])

    def test_other_entry_data_is_preserved(self):
        self.entry.data = {"team_id": "1", "future_key": "kept"}
        self.run_flow(_flow(self.hass, self.entry, modern=True), {"team_id": "local"})
        self.assertEqual(self.entry.data, {"team_id": "local", "future_key": "kept"})

    def test_team_already_used_by_another_entry_is_refused(self):
        other = _Entry("entry-console", "local")
        hass = _Hass([self.entry, other])
        result = self.run_flow(_flow(hass, self.entry, modern=True), {"team_id": "local"})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"team_id": "team_id_in_use"})
        self.assertEqual(self.entry.data, {"team_id": "1"})
        self.assertEqual(self.entry.unique_id, "deckhand_1")
        self.assertEqual(hass.config_entries.reloads, [])

    def test_keeping_the_same_team_is_not_a_duplicate(self):
        result = self.run_flow(_flow(self.hass, self.entry, modern=True), {"team_id": "1"})
        self.assertEqual(result["reason"], "reconfigure_successful")

    def test_topic_unsafe_team_ids_are_refused(self):
        for bad in ("", "   ", "a/b", "+", "#", "lo cal", "x" * 65, "t\n1"):
            with self.subTest(team_id=bad):
                result = self.run_flow(_flow(self.hass, self.entry, modern=True),
                                       {"team_id": bad})
                self.assertEqual(result["errors"], {"team_id": "invalid_team_id"})
        self.assertEqual(self.entry.data, {"team_id": "1"})

    def test_user_step_uses_the_same_validation(self):
        flow = CF.DeckhandConfigFlow()
        flow.hass = _Hass([])
        bad = asyncio.run(flow.async_step_user({"team_id": "a/b"}))
        self.assertEqual(bad["errors"], {"team_id": "invalid_team_id"})
        # 1.26.0: a valid team id shows the "what next" page first; submitting
        # it creates the entry.
        finish = asyncio.run(flow.async_step_user({"team_id": " local "}))
        self.assertEqual((finish["type"], finish["step_id"]), ("form", "finish"))
        good = asyncio.run(flow.async_step_finish({}))
        self.assertEqual((good["type"], good["title"], good["data"]),
                         ("create_entry", "Deckhand (Team local)", {"team_id": "local"}))
        self.assertEqual(flow.unique_id, "deckhand_local")

    def test_missing_entry_aborts(self):
        flow = _flow(self.hass, self.entry, modern=False)
        flow.context["entry_id"] = "gone"
        self.assertEqual(self.run_flow(flow)["reason"], "reconfigure_entry_missing")


class CutoverKeepsDevices(unittest.TestCase):
    """End to end: setup on team 1, reconfigure to local, reload."""

    def setUp(self):
        self.entry = _Entry("entry-helm", "1")
        self.hass = _Hass([self.entry])
        self.mqtt = _Mqtt()
        self.registry = _Registry()
        ReconfigureFlow._patch_setup(self, self.mqtt, self.registry)
        asyncio.run(MOD.async_setup_entry(self.hass, self.entry))

    def _publish_fleet(self, team):
        self.mqtt.deliver(f"deckhand/{team}/dial/DECK-AAAA/status",
                          {"device_id": "DECK-AAAA", "online": True})
        self.mqtt.deliver(f"deckhand/{team}/groups/list", {"groups": [
            {"id": 12, "name": "Office", "dial_ids": ["DECK-AAAA"]},
        ] + ([{"id": "6f1c2a9e-0b7d-4c55-9a3e-1d2f3a4b5c6d", "name": "Deck",
               "dial_ids": ["DECK-AAAA"]}] if team == "local" else [])})

    def test_devices_keep_their_ids_and_topics_move(self):
        self._publish_fleet("1")
        dial = self.registry.async_get_device({(DOMAIN, "DECK-AAAA")})
        room = self.registry.async_get_device({(DOMAIN, "group:12")})
        self.assertIsNotNone(dial)
        self.assertIsNotNone(room)
        before = {dial.id, room.id}
        self.assertIn("deckhand/1/dial/+/status", self.mqtt.topics())

        result = ReconfigureFlow.run_flow(
            self, _flow(self.hass, self.entry, modern=True), {"team_id": "local"})
        self.assertEqual(result["reason"], "reconfigure_successful")

        # Every subscription now lives under the new prefix, none under the old.
        topics = self.mqtt.topics()
        self.assertTrue(topics)
        self.assertFalse([t for t in topics if t.startswith("deckhand/1/")], topics)
        self.assertIn("deckhand/local/dial/+/status", topics)
        self.assertIn("deckhand/local/groups/list", topics)
        self.assertEqual(self.hass.data[DOMAIN]["entry-helm"]["team_id"], "local")

        # The old team's presence beacon was cleared (not the new team's),
        # and the new one is announced.
        self.assertIn(("deckhand/1/hacs/presence", "", True), self.mqtt.published)
        self.assertTrue([p for p in self.mqtt.published
                         if p[0] == "deckhand/local/hacs/presence" and p[1]])

        # Old prefix is dead; the new one feeds the same devices.
        self.assertEqual(self.mqtt.deliver("deckhand/1/dial/DECK-AAAA/status", {}), 0)
        self._publish_fleet("local")
        self.assertEqual(self.registry.async_get_device({(DOMAIN, "DECK-AAAA")}).id,
                         dial.id)
        self.assertEqual(self.registry.async_get_device({(DOMAIN, "group:12")}).id,
                         room.id)
        self.assertEqual(self.registry.removed, [])
        self.assertTrue(before <= set(self.registry.devices))
        # The Console-only UUID room becomes a new device alongside.
        uuid_room = self.registry.async_get_device(
            {(DOMAIN, "group:6f1c2a9e-0b7d-4c55-9a3e-1d2f3a4b5c6d")})
        self.assertIsNotNone(uuid_room)

        # Targets resolve through the entry's NEW team.
        self.assertEqual(MOD._resolve_one_target(self.hass, dial.id),
                         [("DECK-AAAA", "local")])
        self.assertEqual(MOD._resolve_one_target(self.hass, room.id),
                         [("DECK-AAAA", "local")])
        self.assertEqual(MOD._resolve_one_target(self.hass, uuid_room.id),
                         [("DECK-AAAA", "local")])


class GroupIdParsing(unittest.TestCase):
    def test_int_ids_are_byte_identical(self):
        self.assertEqual(MOD._normalize_group_id(12), "12")
        self.assertEqual(MOD._normalize_group_id(0), "0")
        groups = MOD._normalize_groups([{"id": 12, "name": "Office"}])
        self.assertEqual(list(groups), ["12"])
        self.assertEqual(groups["12"]["id"], 12)

    def test_str_ids_are_accepted_and_stripped(self):
        u = "6f1c2a9e-0b7d-4c55-9a3e-1d2f3a4b5c6d"
        self.assertEqual(MOD._normalize_group_id(f"  {u} "), u)
        self.assertEqual(MOD._normalize_group_id("room_7"), "room_7")
        self.assertEqual(MOD._normalize_group_id("x" * 64), "x" * 64)
        groups = MOD._normalize_groups([{"id": u, "dial_ids": ["DECK-AAAA"]}])
        self.assertEqual(groups[u]["dial_ids"], ["DECK-AAAA"])
        self.assertEqual(groups[u]["name"], f"Room {u}")

    def test_bad_ids_are_rejected(self):
        for bad in (True, False, None, 1.5, "", "   ", "x" * 65, "a b", "a/b",
                    "a:b", "a\nb", "a\x00b", "ü", ["1"], {"id": 1}):
            with self.subTest(bad=bad):
                self.assertIsNone(MOD._normalize_group_id(bad))
        self.assertEqual(MOD._normalize_groups([{"id": True}, {"id": "a/b"}, "x"]), {})


if __name__ == "__main__":
    unittest.main()
