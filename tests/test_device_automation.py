"""Device triggers, device actions and the Button / Menu / NFC / Invitation
event entities (1.26.0, HACS Studio phase 2).

Pinned here:

* ``_automation.classify``: which ``deckhand_dial_event`` each trigger
  hears — raw re-fire for presses, menus, timers, DND and invitations, the
  server relay for NFC identity — and that a moment arriving both ways
  fires once (the relay's ``button_press`` and the raw ``nfc_tap`` match
  nothing). Plumbing presses (``state_request``, ``qr_request``, the TV
  remote's ``volume_set`` detents) are not "button pressed".
* ``device_trigger``: the list a dial offers (NFC only once it has an NFC
  entity, nothing for a Room), subtype validation, and a real attach that
  runs the action for its own dial only, honouring the optional label.
* ``device_action``: the list, the fields, and that every action's service
  call is accepted by the REAL service handler and lands on the wire.
* ``event.py``: the four new entities map the right events, carry the right
  attributes, keep their unique ids, and the NFC entity is created on the
  first relayed tap with that tap already on it.
* strings.json translates every trigger, subtype, action, extra field and
  event type.

No Home Assistant runtime here: the modules run against the stand-in from
test_entities_follow_heartbeats.py with small fakes for the registries.

Run with:  python3 -m pytest tests/test_device_automation.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import test_entities_follow_heartbeats as standin  # installs the HA stand-in
import test_emoji_strip_every_service as harness  # the real services

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "deckhand"
PKG = "deckhand_device_automation_under_test"
DIAL = "DECK-AAAA"
OTHER = "DECK-BBBB"


def _voluptuous_stub():
    vol = types.ModuleType("voluptuous")

    class _Marker(str):
        def __new__(cls, key, default=None, **kwargs):
            obj = str.__new__(cls, key)
            obj.required = cls.__name__ == "Required"
            return obj

    class Required(_Marker):
        pass

    class Optional(_Marker):
        pass

    class Schema:
        def __init__(self, schema, **kwargs):
            self.schema = schema

        def __call__(self, value):
            return value

    vol.Schema = Schema
    vol.Required = Required
    vol.Optional = Optional
    vol.In = lambda *a, **k: a
    vol.All = lambda *a, **k: a
    vol.Coerce = lambda t: t
    return vol


class _BaseSchema:
    @staticmethod
    def extend(schema):
        return lambda config: dict(config)


class _InvalidConfig(Exception):
    pass


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(COMPONENT)]
    modules = {PKG: pkg}
    try:
        importlib.import_module("voluptuous")
    except ImportError:
        modules["voluptuous"] = _voluptuous_stub()
    da = importlib.import_module("homeassistant.components.device_automation")
    da.DEVICE_TRIGGER_BASE_SCHEMA = _BaseSchema
    da.DEVICE_ACTION_BASE_SCHEMA = _BaseSchema
    importlib.import_module(
        "homeassistant.components.device_automation.exceptions"
    ).InvalidDeviceAutomationConfig = _InvalidConfig
    importlib.import_module("homeassistant.components.event").EventEntity = _EventEntity
    const = importlib.import_module("homeassistant.const")
    for name in ("CONF_DEVICE_ID", "CONF_DOMAIN", "CONF_PLATFORM", "CONF_TYPE"):
        setattr(const, name, name[len("CONF_"):].lower())
    with mock.patch.dict(sys.modules, modules):
        mods = {
            name: importlib.import_module(f"{PKG}.{name}")
            for name in ("_automation", "_device_lookup", "device_trigger", "device_action", "event")
        }
    return mods


class _EventEntity:
    """Just enough of homeassistant.components.event.EventEntity."""

    def _trigger_event(self, event_type, attributes=None):
        self.fired = getattr(self, "fired", []) + [(event_type, dict(attributes or {}))]


MODS = _load()
AUTO = MODS["_automation"]
TRIGGER = MODS["device_trigger"]
ACTION = MODS["device_action"]
EVENT = MODS["event"]


# ---------------------------------------------------------------- fakes
def raw(wire, **payload):
    return {"dial_id": DIAL, "type": wire, "payload": payload, "ts": 1}


def relay(event_type, dial_id=DIAL, **fields):
    return {"dial_id": dial_id, "team_slug": "home", "event_type": event_type,
            "item_id": "", "item_label": "", "action": "", **fields}


class _Device:
    def __init__(self, ident, entry_id="entry-1"):
        self.identifiers = {("deckhand", ident)}
        self.config_entries = {entry_id}


class _DeviceRegistry:
    def __init__(self, devices):
        self.devices = devices

    def async_get(self, device_id):
        return self.devices.get(device_id)


class _EntityRegistry:
    def __init__(self, unique_ids=()):
        self.unique_ids = set(unique_ids)

    def async_get_entity_id(self, domain, platform, unique_id):
        return f"{domain}.x" if unique_id in self.unique_ids else None


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False, context=None):
        self.calls.append((domain, service, data, blocking, context))


class _Hass(standin._Hass):
    def __init__(self):
        super().__init__()
        self.ran = []
        self.services = _Services()

    def async_run_hass_job(self, job, variables, context=None):
        self.ran.append(variables["trigger"])
        job.target(variables, context)


def _hass_with_devices(nfc=False, store=None):
    hass = _Hass()
    hass.data = {"deckhand": {"entry-1": store or {"team_id": "1", "dials": {DIAL: {}}}}}
    devices = _DeviceRegistry({"dev-dial": _Device(DIAL), "dev-room": _Device("group:12"),
                               "dev-other": _Device(OTHER)})
    ents = _EntityRegistry({TRIGGER.nfc_entity_unique_id(DIAL)} if nfc else ())
    return hass, devices, ents


class _Patched(unittest.TestCase):
    """Swap the registries the modules reach for."""

    nfc = False
    store = None

    def setUp(self):
        self.hass, devices, ents = _hass_with_devices(self.nfc, self.store)
        dr = types.SimpleNamespace(async_get=lambda hass: devices)
        er = types.SimpleNamespace(async_get=lambda hass: ents)
        patches = [
            mock.patch.object(MODS["_device_lookup"], "dr", dr),
            mock.patch.object(TRIGGER, "er", er),
            mock.patch.object(EVENT, "er", er),
            mock.patch.object(TRIGGER, "HassJob",
                              lambda target, name=None: types.SimpleNamespace(target=target)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


# ---------------------------------------------------------------- classify
class Classify(unittest.TestCase):
    def test_a_plain_menu_item_is_a_press_and_a_menu_selection(self):
        self.assertEqual(
            AUTO.classify(raw("button_press", item_id="m7", item_label="Close Garage")),
            [("button_pressed", None), ("menu_item_selected", None)],
        )

    def test_a_press_with_a_builtin_action_is_a_press_only(self):
        self.assertEqual(
            AUTO.classify(raw("button_press", item_id="scene.movie", item_label="Movie",
                              action="scene_activate")),
            [("button_pressed", None)],
        )

    def test_plumbing_presses_fire_nothing(self):
        for action in ("state_request", "qr_request", "volume_set"):
            with self.subTest(action=action):
                self.assertEqual(AUTO.classify(raw("button_press", item_id="x", action=action)), [])

    def test_gestures_timers_dnd_invitations(self):
        cases = {
            ("long_press", ()): [("long_pressed", None)],
            ("double_tap", ()): [("double_tapped", None)],
            ("timer_start", ()): [("timer", "started")],
            ("timer_complete", ()): [("timer", "completed")],
            ("timer_cancel", ()): [("timer", "cancelled")],
        }
        for (wire, _), expected in cases.items():
            with self.subTest(wire=wire):
                self.assertEqual(AUTO.classify(raw(wire)), expected)
        self.assertEqual(AUTO.classify(raw("dnd_changed", action="on")), [("dnd", "turned_on")])
        self.assertEqual(AUTO.classify(raw("dnd_changed", action="off")), [("dnd", "turned_off")])
        self.assertEqual(AUTO.classify(raw("dnd_changed", action="maybe")), [])
        for response in ("accepted", "declined", "expired", "cancelled"):
            self.assertEqual(
                AUTO.classify(raw("invitation_response", invitation_id="i1", response=response)),
                [("invitation_response", response)],
            )

    def test_nfc_identity_comes_from_the_relay_only(self):
        for status in ("known", "unknown", "revoked"):
            self.assertEqual(AUTO.classify(relay("nfc_tap", action=status)), [("nfc_tap", status)])
        # The raw tap has a hash and no identity: it must not fire an NFC trigger.
        self.assertEqual(AUTO.classify(raw("nfc_tap", uid_hash="ab", uid_len=7)), [])

    def test_the_relays_copy_of_a_raw_moment_fires_nothing(self):
        # Helm relays button_press / timers / DND / invitations too, flat. The
        # raw re-fire already fired the trigger: the relay must not again.
        for event_type in ("button_press", "long_press", "timer_start", "dnd_changed",
                           "invitation_response", "menu_opened"):
            with self.subTest(event_type=event_type):
                self.assertEqual(AUTO.classify(relay(event_type, action="on")), [])

    def test_unknown_and_malformed_events(self):
        for data in (None, "x", {}, raw("encoder_rotate"), raw("online"),
                     {"dial_id": DIAL, "type": "button_press", "payload": "junk"}):
            with self.subTest(data=data):
                self.assertIsInstance(AUTO.classify(data), list)
        self.assertEqual(AUTO.classify(raw("encoder_rotate")), [])

    def test_label_filter_is_exact_ignoring_case_and_spaces(self):
        press = raw("button_press", item_label="Close Garage")
        self.assertTrue(AUTO.trigger_matches("menu_item_selected", None, press, " close garage "))
        self.assertFalse(AUTO.trigger_matches("menu_item_selected", None, press, "Close"))
        self.assertTrue(AUTO.trigger_matches("menu_item_selected", None, press, None))
        # Positive control for the type gate: a long press is not a menu item.
        self.assertFalse(AUTO.trigger_matches("menu_item_selected", None, raw("long_press"), None))

    def test_subtype_validation(self):
        self.assertIsNone(AUTO.validate_trigger("timer", "started"))
        self.assertIsNone(AUTO.validate_trigger("button_pressed", None))
        self.assertIn("needs a subtype", AUTO.validate_trigger("timer", None))
        self.assertIn("needs a subtype", AUTO.validate_trigger("nfc_tap", "started"))
        self.assertIn("takes no subtype", AUTO.validate_trigger("long_pressed", "x"))
        self.assertIn("Unknown", AUTO.validate_trigger("rotated", "cw"))

    def test_no_rotation_trigger_is_invented(self):
        # The firmware publishes no rotation event (see _automation's docstring).
        self.assertFalse([t for t in AUTO.TRIGGER_TYPES if "rot" in t])


# ---------------------------------------------------------------- triggers
class DeviceTriggers(_Patched):
    def triggers(self, device_id="dev-dial"):
        return asyncio.run(TRIGGER.async_get_triggers(self.hass, device_id))

    def test_a_dial_offers_every_trigger_but_nfc_before_a_tap(self):
        pairs = {(t["type"], t.get("subtype")) for t in self.triggers()}
        self.assertIn(("button_pressed", None), pairs)
        self.assertIn(("menu_item_selected", None), pairs)
        self.assertIn(("timer", "cancelled"), pairs)
        self.assertIn(("invitation_response", "expired"), pairs)
        self.assertIn(("dnd", "turned_off"), pairs)
        self.assertFalse([p for p in pairs if p[0] == "nfc_tap"])
        for t in self.triggers():
            self.assertEqual((t["platform"], t["domain"], t["device_id"]),
                             ("device", "deckhand", "dev-dial"))

    def test_a_room_and_a_stranger_offer_nothing(self):
        self.assertEqual(self.triggers("dev-room"), [])
        self.assertEqual(self.triggers("nope"), [])

    def test_validation_rejects_a_foreign_subtype(self):
        with self.assertRaises(_InvalidConfig):
            asyncio.run(TRIGGER.async_validate_trigger_config(
                self.hass, {"device_id": "dev-dial", "type": "timer", "subtype": "known"}))
        ok = asyncio.run(TRIGGER.async_validate_trigger_config(
            self.hass, {"device_id": "dev-dial", "type": "timer", "subtype": "started"}))
        self.assertEqual(ok["subtype"], "started")

    def test_label_is_offered_on_press_triggers_only(self):
        caps = asyncio.run(TRIGGER.async_get_trigger_capabilities(
            self.hass, {"type": "menu_item_selected"}))
        self.assertEqual(list(caps["extra_fields"].schema), ["item_label"])
        self.assertEqual(asyncio.run(TRIGGER.async_get_trigger_capabilities(
            self.hass, {"type": "timer", "subtype": "started"})), {})

    def attach(self, **config):
        fired = []
        config = {"device_id": "dev-dial", **config}
        unsub = asyncio.run(TRIGGER.async_attach_trigger(
            self.hass, config, lambda variables, context=None: fired.append(variables),
            {"trigger_data": {"id": "0", "idx": "0"}}))
        return fired, unsub

    def test_attach_runs_the_action_for_its_own_dial_only(self):
        fired, unsub = self.attach(type="menu_item_selected", item_label="Close Garage")
        self.hass.bus.fire("deckhand_dial_event", raw("button_press", item_id="m7",
                                                      item_label="Close Garage"))
        self.hass.bus.fire("deckhand_dial_event", raw("button_press", item_label="Movie"))
        self.hass.bus.fire("deckhand_dial_event", {**raw("button_press", item_label="Close Garage"),
                                                   "dial_id": OTHER})
        self.assertEqual(len(fired), 1)
        trig = fired[0]["trigger"]
        self.assertEqual((trig["platform"], trig["type"], trig["item_label"], trig["item_id"],
                          trig["dial_id"], trig["id"]),
                         ("device", "menu_item_selected", "Close Garage", "m7", DIAL, "0"))
        unsub()
        self.hass.bus.fire("deckhand_dial_event", raw("button_press", item_label="Close Garage"))
        self.assertEqual(len(fired), 1, "still listening after unsubscribe")

    def test_attach_with_a_subtype(self):
        fired, _ = self.attach(type="invitation_response", subtype="declined")
        self.hass.bus.fire("deckhand_dial_event", raw("invitation_response", response="accepted"))
        self.hass.bus.fire("deckhand_dial_event", raw("invitation_response", response="declined",
                                                      invitation_id="inv-1"))
        self.assertEqual([f["trigger"]["invitation_id"] for f in fired], ["inv-1"])

    def test_a_room_device_never_fires(self):
        fired, _ = self.attach(device_id="dev-room", type="button_pressed")
        self.hass.bus.fire("deckhand_dial_event", raw("button_press", item_label="x"))
        self.assertEqual(fired, [])


class DeviceTriggersWithNfc(_Patched):
    nfc = True

    def test_nfc_triggers_appear_once_the_dial_has_an_nfc_entity(self):
        triggers = asyncio.run(TRIGGER.async_get_triggers(self.hass, "dev-dial"))
        subs = {t.get("subtype") for t in triggers if t["type"] == "nfc_tap"}
        self.assertEqual(subs, {"known", "unknown", "revoked"})

    def test_a_known_tap_fires_once_though_it_arrives_twice(self):
        fired = []
        asyncio.run(TRIGGER.async_attach_trigger(
            self.hass, {"device_id": "dev-dial", "type": "nfc_tap", "subtype": "known"},
            lambda v, c=None: fired.append(v), {"trigger_data": {}}))
        self.hass.bus.fire("deckhand_dial_event", raw("nfc_tap", uid_hash="ab"))
        self.hass.bus.fire("deckhand_dial_event", relay("nfc_tap", action="known",
                                                        item_label="Maria", role="Housekeeper"))
        self.assertEqual(len(fired), 1)
        self.assertEqual((fired[0]["trigger"]["item_label"], fired[0]["trigger"]["role"]),
                         ("Maria", "Housekeeper"))


# ---------------------------------------------------------------- actions
_STORE = {
    "team_id": "1",
    "dials": {DIAL: {}},
    "themes": [{"slug": "komorebi", "name": "Komorebi"}, {"slug": "vault", "name": "Vault"}],
    "faces": {"index": {"faces": [
        {"uid": "u1", "rev": 1, "name": "Guest Temp", "kind": "sensor", "enabled": True},
        {"uid": "u2", "rev": 1, "name": "big clock", "kind": "clock", "enabled": True},
        {"uid": "u3", "rev": 1, "name": "Off Face", "kind": "clock", "enabled": False},
    ]}},
}


class DeviceActions(_Patched):
    store = _STORE

    def actions(self, device_id="dev-dial"):
        return [a["type"] for a in asyncio.run(ACTION.async_get_actions(self.hass, device_id))]

    def test_dials_and_rooms_offer_every_action(self):
        expected = ["send_announcement", "start_timer", "push_theme", "dnd_on", "dnd_off",
                    "show_message", "mount_library_face"]
        self.assertEqual(self.actions(), expected)
        self.assertEqual(self.actions("dev-room"), expected)
        self.assertEqual(self.actions("nope"), [])

    def test_library_face_action_hides_without_a_library(self):
        self.hass.data["deckhand"]["entry-1"] = {"team_id": "1", "faces": {"index": None}}
        self.assertNotIn("mount_library_face", self.actions())

    def test_theme_and_face_pickers_offer_the_catalogs(self):
        picked = {}

        class _Sel:
            def __init__(self, name):
                self.name = name

            def __getattr__(self, attr):
                if attr == "SelectSelectorMode":
                    return types.SimpleNamespace(DROPDOWN="dropdown")
                if attr == "NumberSelectorMode":
                    return types.SimpleNamespace(BOX="box")

                def make(*args, **kwargs):
                    if attr == "SelectSelectorConfig":
                        picked.setdefault("options", []).append(kwargs["options"])
                    return (attr, args, kwargs)
                return make

        with mock.patch.object(ACTION, "selector", _Sel("selector")):
            theme = asyncio.run(ACTION.async_get_action_capabilities(
                self.hass, {"device_id": "dev-dial", "type": "push_theme"}))
            face = asyncio.run(ACTION.async_get_action_capabilities(
                self.hass, {"device_id": "dev-dial", "type": "mount_library_face"}))
            timer = asyncio.run(ACTION.async_get_action_capabilities(
                self.hass, {"device_id": "dev-dial", "type": "start_timer"}))
        self.assertEqual(picked["options"][0], ["random", "komorebi", "vault"])
        self.assertEqual(picked["options"][1], ["big clock", "Guest Temp"])
        self.assertEqual(list(theme["extra_fields"].schema), ["theme"])
        self.assertEqual(list(face["extra_fields"].schema), ["face"])
        self.assertEqual(list(timer["extra_fields"].schema), ["minutes", "label", "name", "color"])
        self.assertEqual(asyncio.run(ACTION.async_get_action_capabilities(
            self.hass, {"device_id": "dev-dial", "type": "dnd_on"})), {})

    def test_an_action_calls_its_service_as_the_caller(self):
        ctx = object()
        asyncio.run(ACTION.async_call_action_from_config(
            self.hass, {"device_id": "dev-room", "domain": "deckhand", "type": "dnd_on"}, {}, ctx))
        self.assertEqual(self.hass.services.calls,
                         [("deckhand", "set_dnd", {"device_id": ["dev-room"], "on": True}, True, ctx)])


class ActionsReachTheWire(harness._ServiceTest):
    """Every action's (service, data) is accepted by the REAL handler."""

    def run_action(self, action_type, **fields):
        service, data = AUTO.build_service_call(action_type, {"device_id": "d1", **fields})
        self.mqtt.sent.clear()
        asyncio.run(self.hass.services.handlers[service](types.SimpleNamespace(data=data)))
        return service, self.mqtt.sent

    def test_every_action(self):
        service, sent = self.run_action("send_announcement", message="Dinner", from_name="Chef")
        self.assertEqual(service, "send_announcement")
        self.assertEqual([p["message"] for t, p in sent if t.endswith("/cmd/announce")],
                         ["Dinner", "Dinner"])

        _, sent = self.run_action("start_timer", minutes=10, label="Pasta", color="#C9A84C")
        timers = [p for t, p in sent if t.endswith("/cmd/timer")]
        self.assertEqual(len(timers), 2)
        self.assertEqual(timers[0]["label"], "Pasta")

        _, sent = self.run_action("push_theme", theme="komorebi")
        self.assertEqual([p["slug"] for t, p in sent if t.endswith("/theme_request")],
                         ["komorebi", "komorebi"])

        for action_type, on in (("dnd_on", True), ("dnd_off", False)):
            _, sent = self.run_action(action_type)
            self.assertEqual([p["on"] for t, p in sent if p and t.endswith("/cmd/dnd")], [on, on])

        _, sent = self.run_action("show_message", text="Back soon")
        mounts = [p for t, p in sent if t.endswith("/cmd/face/message/mount")]
        self.assertEqual([m["text"] for m in mounts], ["Back soon", "Back soon"])
        self.assertEqual(mounts[0]["face_id"], "message")

    def test_required_fields_and_blank_optionals(self):
        with self.assertRaises(ValueError):
            AUTO.build_service_call("send_announcement", {"device_id": "d", "message": "  "})
        with self.assertRaises(ValueError):
            AUTO.build_service_call("rotate", {"device_id": "d"})
        service, data = AUTO.build_service_call(
            "start_timer", {"device_id": "d", "minutes": 5, "label": "", "name": None})
        self.assertEqual((service, data), ("start_timer", {"device_id": ["d"], "minutes": 5}))
        service, data = AUTO.build_service_call("mount_library_face", {"device_id": "d", "face": "Guest Temp"})
        self.assertEqual((service, data), ("mount_library_face", {"device_id": ["d"], "face": "Guest Temp"}))


# ---------------------------------------------------------------- event entities
def _add(entity, hass):
    entity.hass = hass
    asyncio.run(entity.async_added_to_hass())
    return entity


class EventEntities(_Patched):
    def entity(self, cls):
        return _add(cls(DIAL, {}), self.hass)

    def fire(self, data):
        self.hass.bus.fire("deckhand_dial_event", data)

    def test_unique_ids_are_new_and_the_timer_keeps_its_own(self):
        self.assertEqual(EVENT.DeckhandTimerEvent(DIAL, {})._attr_unique_id, f"{DIAL}_timer")
        ids = {cls(DIAL, {})._attr_unique_id for cls in (
            EVENT.DeckhandButtonEvent, EVENT.DeckhandMenuEvent,
            EVENT.DeckhandInvitationEvent, EVENT.DeckhandNfcEvent)}
        self.assertEqual(ids, {f"deckhand_{DIAL}_{k}_event"
                               for k in ("button", "menu", "invitation", "nfc")})
        self.assertEqual(EVENT.nfc_unique_id(DIAL), TRIGGER.nfc_entity_unique_id(DIAL))

    def test_button(self):
        button = self.entity(EVENT.DeckhandButtonEvent)
        self.assertEqual(button._attr_event_types, ["press", "long_press", "double_tap"])
        self.fire(raw("button_press", item_id="scene.movie", item_label="Movie", action="scene_activate"))
        self.fire(raw("button_press", item_id="sensor.t", action="state_request"))
        self.fire(raw("long_press", action="scene"))
        self.fire({**raw("double_tap"), "dial_id": OTHER})
        self.assertEqual(button.fired, [
            ("press", {"item_id": "scene.movie", "item_label": "Movie", "action": "scene_activate"}),
            ("long_press", {"item_id": "", "item_label": "", "action": "scene"}),
        ])

    def test_menu(self):
        menu = self.entity(EVENT.DeckhandMenuEvent)
        self.assertEqual(menu._attr_event_types, ["item_selected", "opened", "closed", "timed_out"])
        for wire in ("menu_opened", "menu_timeout"):
            self.fire(raw(wire))
        self.fire(raw("button_press", item_id="m1", item_label="Laundry done"))
        self.fire(raw("button_press", item_id="light.x", item_label="Off", action="turn_off"))
        self.assertEqual([f[0] for f in menu.fired], ["opened", "timed_out", "item_selected"])
        self.assertEqual(menu.fired[-1][1], {"item_id": "m1", "item_label": "Laundry done"})

    def test_invitation(self):
        inv = self.entity(EVENT.DeckhandInvitationEvent)
        self.fire(raw("invitation_response", invitation_id="i9", response="accepted", held_ms=1200))
        self.fire(relay("invitation_response", action="accepted"))  # the relay's copy
        self.assertEqual(inv.fired, [("accepted", {"invitation_id": "i9", "held_ms": 1200})])

    def test_timer_still_maps_raw_only(self):
        timer = self.entity(EVENT.DeckhandTimerEvent)
        self.fire(raw("timer_complete", item_label="Pasta", minutes=10))
        self.fire(relay("timer_complete"))
        self.assertEqual(timer.fired, [("completed", {"label": "Pasta", "minutes": 10})])

    def test_nfc_known_unknown_and_no_hash(self):
        nfc = self.entity(EVENT.DeckhandNfcEvent)
        self.fire(relay("nfc_tap", action="known", item_label="Maria", role="Housekeeper",
                        credential_id="c1"))
        self.fire(relay("nfc_tap", action="unknown", item_label="unknown"))
        self.fire(raw("nfc_tap", uid_hash="ab"))
        self.assertEqual(nfc.fired, [
            ("known", {"identity": "Maria", "role": "Housekeeper", "credential_id": "c1"}),
            ("unknown", {"identity": "", "role": "", "credential_id": ""}),
        ])
        for _kind, attrs in nfc.fired:
            self.assertNotIn("uid_hash", attrs)

    def test_no_restore_and_always_available(self):
        for cls in (EVENT.DeckhandButtonEvent, EVENT.DeckhandNfcEvent, EVENT.DeckhandTimerEvent):
            ent = cls(DIAL, {})
            self.assertIsNone(asyncio.run(ent.async_get_last_state()))
            self.assertTrue(ent.available)


class NfcEntityAppearsOnFirstTap(_Patched):
    store = {"team_id": "1", "dials": {DIAL: {"online": True}}}

    def setup_platform(self):
        added = []
        entry = types.SimpleNamespace(entry_id="entry-1", async_on_unload=lambda f: None)
        asyncio.run(EVENT.async_setup_entry(self.hass, entry, lambda ents: added.extend(ents)))
        return added

    def test_first_relayed_tap_creates_the_entity_with_that_tap(self):
        added = self.setup_platform()
        names = [type(e).__name__ for e in added]
        self.assertEqual(names, ["DeckhandTimerEvent", "DeckhandButtonEvent",
                                 "DeckhandMenuEvent", "DeckhandInvitationEvent"])
        # Raw taps and other dials' relays create nothing.
        self.hass.bus.fire("deckhand_dial_event", raw("nfc_tap", uid_hash="ab"))
        self.hass.bus.fire("deckhand_dial_event", relay("nfc_tap", dial_id="DECK-ZZZZ", action="known"))
        self.assertEqual(len(added), 4)
        self.hass.bus.fire("deckhand_dial_event", relay("nfc_tap", action="revoked", item_label="Old fob"))
        self.assertEqual(len(added), 5)
        nfc = _add(added[-1], self.hass)
        self.assertEqual(nfc.fired, [("revoked", {"identity": "Old fob", "role": "", "credential_id": ""})])
        # A second tap is the entity's own business, not a second entity.
        self.hass.bus.fire("deckhand_dial_event", relay("nfc_tap", action="known"))
        self.assertEqual(len(added), 5)
        self.assertEqual(len(nfc.fired), 2)


class NfcEntityComesBackAfterRestart(_Patched):
    nfc = True
    store = {"team_id": "1", "dials": {DIAL: {}}}

    def test_a_registered_nfc_entity_is_recreated_at_startup(self):
        added = []
        entry = types.SimpleNamespace(entry_id="entry-1", async_on_unload=lambda f: None)
        asyncio.run(EVENT.async_setup_entry(self.hass, entry, lambda ents: added.extend(ents)))
        self.assertIn("DeckhandNfcEvent", [type(e).__name__ for e in added])


# ---------------------------------------------------------------- strings
class Translations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))

    def test_every_trigger_subtype_action_and_field_is_translated(self):
        da = self.strings["device_automation"]
        self.assertEqual(set(da["trigger_type"]), set(AUTO.TRIGGER_TYPES))
        subtypes = {s for subs in AUTO.TRIGGER_TYPES.values() for s in subs}
        self.assertEqual(set(da["trigger_subtype"]), subtypes)
        self.assertEqual(set(da["action_type"]), set(AUTO.ACTION_TYPES))
        self.assertEqual(set(da["extra_fields"]), set(AUTO.ACTION_KEYS) | {"item_label"})
        for key, text in da["trigger_type"].items():
            with self.subTest(trigger=key):
                self.assertEqual("{subtype}" in text, bool(AUTO.TRIGGER_TYPES[key]))

    def test_every_event_type_is_translated(self):
        events = self.strings["entity"]["event"]
        for key, mapping in AUTO.ENTITY_EVENT_TYPES.items():
            with self.subTest(entity=key):
                states = events[key]["state_attributes"]["event_type"]["state"]
                self.assertEqual(set(states), set(mapping.values()))
                self.assertTrue(events[key]["name"])

    def test_menu_key_description_no_longer_promises_an_echo(self):
        import yaml
        services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
        text = services["add_menu_item"]["fields"]["key"]["description"]
        self.assertNotIn("Also echoed", text)
        self.assertIn("item_label", text)


if __name__ == "__main__":
    unittest.main()
