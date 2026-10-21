"""Every command and request this integration publishes says who sent it (1.22.0).

Helm and Console keep one activity record of everything done to a dial:
who, and from which channel. Commands HA publishes straight to the broker
never pass through either server's request handling, so each server
watches ``cmd/#`` and attributes what it sees by the payload's

    "_src": {"ch": "home_assistant", "by": "<who>"}

stamp; the ``*_request`` lanes carry the same stamp so the server's row
for them names the same person. These tests pin:

* the ``by`` resolver: HA user → their name; automation / script → its
  friendly name; another parent context → "automation"; else
  "Home Assistant"; clamped to 64 chars, control characters stripped;
* the ~3.9 KB size guard (drop the stamp, never the command);
* every action service and entity carries ``_src`` (run through the REAL
  ``__init__.py`` against the HA stand-in), and telemetry feeds
  (now playing, sensor values, perimeter live state) stay unstamped.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import types
import unittest

import test_entities_follow_heartbeats as standin
from test_emoji_strip_every_service import TARGETS, _ServiceTest

_spec = importlib.util.spec_from_file_location("_source_under_test", standin.PKG_DIR / "_source.py")
source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(source)

UNATTRIBUTED = {"ch": "home_assistant", "by": "Home Assistant"}


def _ctx(id="ctx-1", user_id=None, parent_id=None):
    return types.SimpleNamespace(id=id, user_id=user_id, parent_id=parent_id)


def _entity_state(entity_id, context_id, friendly_name=None):
    attrs = {"friendly_name": friendly_name} if friendly_name else {}
    return types.SimpleNamespace(
        entity_id=entity_id,
        context=types.SimpleNamespace(id=context_id),
        attributes=attrs,
    )


class _States:
    def __init__(self, *states):
        self._states = states

    def async_all(self, domain=None):
        return [s for s in self._states if domain is None or s.entity_id.startswith(f"{domain}.")]


def _hass(users=None, states=()):
    users = users or {}

    async def _get_user(user_id):
        return users.get(user_id)

    return types.SimpleNamespace(
        auth=types.SimpleNamespace(async_get_user=_get_user),
        states=_States(*states),
    )


def _describe(hass, context):
    return asyncio.run(source.async_describe_actor(hass, context))


# ── who: the resolver ───────────────────────────────────────────────────


class DescribeActorTests(unittest.TestCase):
    def test_a_person_is_named(self):
        hass = _hass(users={"u1": types.SimpleNamespace(name="Tyler")})
        self.assertEqual(_describe(hass, _ctx(user_id="u1")), "Tyler")

    def test_a_person_wins_over_an_automation_context(self):
        # A dashboard button running a script: the person pressed it.
        hass = _hass(
            users={"u1": types.SimpleNamespace(name="Tyler")},
            states=[_entity_state("script.goodnight", "ctx-1", "Goodnight")],
        )
        self.assertEqual(_describe(hass, _ctx(user_id="u1")), "Tyler")

    def test_an_unknown_user_falls_back_to_the_id(self):
        self.assertEqual(_describe(_hass(), _ctx(user_id="abc123")), "abc123")

    def test_an_automation_is_found_by_its_run_context(self):
        # An automation's actions run under the context its state was
        # written with (last_triggered) — a time trigger has no parent.
        hass = _hass(states=[
            _entity_state("automation.other", "ctx-x", "Other"),
            _entity_state("automation.sunset_dim", "run-7", "Sunset dim"),
        ])
        self.assertEqual(_describe(hass, _ctx(id="run-7")), "automation: Sunset dim")

    def test_an_automation_is_found_through_the_parent_context(self):
        hass = _hass(states=[_entity_state("automation.door", "run-1", "Front door chime")])
        self.assertEqual(
            _describe(hass, _ctx(id="child-2", parent_id="run-1")),
            "automation: Front door chime",
        )

    def test_a_script_is_named(self):
        hass = _hass(states=[_entity_state("script.movie_mode", "run-3", "Movie mode")])
        self.assertEqual(_describe(hass, _ctx(id="run-3")), "script: Movie mode")

    def test_a_script_run_by_an_automation_names_the_script(self):
        # Both carry the run's context; the script is the nearer cause.
        hass = _hass(states=[
            _entity_state("automation.evening", "run-5", "Evening"),
            _entity_state("script.dim_dials", "run-5", "Dim dials"),
        ])
        self.assertEqual(_describe(hass, _ctx(id="run-5")), "script: Dim dials")

    def test_without_a_friendly_name_the_entity_id_names_it(self):
        hass = _hass(states=[_entity_state("automation.no_name", "run-9")])
        self.assertEqual(_describe(hass, _ctx(id="run-9")), "automation: automation.no_name")

    def test_an_unidentified_parent_reads_as_automation(self):
        self.assertEqual(_describe(_hass(), _ctx(parent_id="evt-1")), "automation")

    def test_nothing_known_reads_as_home_assistant(self):
        self.assertEqual(_describe(_hass(), _ctx()), "Home Assistant")
        self.assertEqual(_describe(_hass(), None), "Home Assistant")
        # A hass without auth/states (or one that raises) never breaks a command.
        self.assertEqual(_describe(object(), _ctx(user_id="u1")), "u1")
        self.assertEqual(_describe(object(), _ctx(parent_id="p")), "automation")

    def test_lookup_failures_are_swallowed(self):
        async def _boom(_uid):
            raise RuntimeError("auth store unavailable")

        def _boom_states(_domain=None):
            raise RuntimeError("no states")

        hass = types.SimpleNamespace(
            auth=types.SimpleNamespace(async_get_user=_boom),
            states=types.SimpleNamespace(async_all=_boom_states),
        )
        self.assertEqual(_describe(hass, _ctx(user_id="u1")), "u1")
        self.assertEqual(_describe(hass, _ctx(id="run-1", parent_id="p")), "automation")

    def test_by_is_clamped_and_cleaned(self):
        hass = _hass(users={"u1": types.SimpleNamespace(name="Evil\r\nName\x00" + "x" * 100)})
        by = _describe(hass, _ctx(user_id="u1"))
        self.assertEqual(len(by), 64)
        self.assertTrue(by.startswith("EvilName"))
        self.assertNotIn("\n", by)
        long_name = _entity_state("automation.long", "run-1", "A" * 200)
        self.assertEqual(len(_describe(_hass(states=[long_name]), _ctx(id="run-1"))), 64)


# ── the stamp ───────────────────────────────────────────────────────────


class StampTests(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(source.make_source("Tyler"), {"ch": "home_assistant", "by": "Tyler"})
        self.assertEqual(source.make_source(""), UNATTRIBUTED)

    def test_with_source_adds_a_top_level_key_and_does_not_mutate(self):
        payload = {"message": "hi"}
        out = source.with_source(payload, source.make_source("Tyler"))
        self.assertEqual(out, {"message": "hi", "_src": {"ch": "home_assistant", "by": "Tyler"}})
        self.assertEqual(payload, {"message": "hi"})

    def test_no_source_is_a_no_op(self):
        self.assertEqual(source.dumps_with_source({"a": 1}, None), '{"a": 1}')

    def test_an_oversize_payload_is_sent_unstamped(self):
        src = source.make_source("Tyler")
        near = {"d": "x" * (source.MAX_STAMPED_PAYLOAD_BYTES - 30)}
        self.assertNotIn("_src", json.loads(source.dumps_with_source(near, src)))
        small = {"d": "x" * 100}
        self.assertIn("_src", json.loads(source.dumps_with_source(small, src)))


# ── every action carries it; telemetry does not ─────────────────────────


class _Recorder(_ServiceTest):
    """The real handlers, with a person behind every call."""

    def setUp(self):
        super().setUp()
        self.hass.auth = types.SimpleNamespace(async_get_user=self._get_user)

    @staticmethod
    async def _get_user(user_id):
        return types.SimpleNamespace(name="Tyler", is_admin=True) if user_id == "u1" else None

    def run_service(self, service, **data):
        data.setdefault("device_id", ["d1"])
        self.mqtt.sent.clear()
        call = types.SimpleNamespace(data=data, context=_ctx(user_id="u1"))
        asyncio.run(self.hass.services.handlers[service](call))
        return list(self.mqtt.sent)

    def assert_stamped(self, service, suffix, **data):
        sent = self.run_service(service, **data)
        hits = [(t, p) for t, p in sent if t.endswith(suffix)]
        self.assertEqual(len(hits), len(TARGETS), f"{service}: expected one {suffix} per dial, got {sent}")
        for topic, payload in hits:
            self.assertIsInstance(payload, dict, topic)
            self.assertEqual(
                payload.get("_src"), {"ch": "home_assistant", "by": "Tyler"},
                f"{service} → {topic} carries no _src",
            )
        return sent


class DirectCommandsAreStamped(_Recorder):
    def test_announcement(self):
        self.assert_stamped("send_announcement", "/cmd/announce", message="Dinner")

    def test_countdown(self):
        self.assert_stamped(
            "send_countdown", "/cmd/announce",
            target_datetime="2027-01-01T00:00:00+00:00", message="Soon",
            celebration_message="Now",
        )

    def test_timers(self):
        self.assert_stamped("start_timer", "/cmd/timer", minutes=5)
        self.assert_stamped("cancel_timer", "/cmd/timer")
        self.assert_stamped("add_timer_time", "/cmd/timer", seconds=60)

    def test_overlay(self):
        self.assert_stamped("apply_overlay", "/cmd/overlay", subtitle_text="Hi", brightness=50)

    def test_reboot(self):
        self.assert_stamped("reboot", "/cmd/reboot")

    def test_dnd_on_and_the_off_clear_stays_bare(self):
        self.assert_stamped("set_dnd", "/cmd/dnd", on=True)
        sent = self.run_service("set_dnd", on=False)
        dnd = [p for t, p in sent if t.endswith("/cmd/dnd")]
        # off = the stamped {"on": false} and then the empty retained clear.
        self.assertEqual(dnd.count(None), len(TARGETS))
        self.assertTrue(all(p["_src"]["by"] == "Tyler" for p in dnd if p is not None))

    def test_sunset(self):
        self.assert_stamped("set_sunset", "/cmd/sunset", sunset_at=1893456000)

    def test_face_mount_and_unmount(self):
        self.assert_stamped(
            "mount_face", "/cmd/face/message/mount", face_id="message",
            payload={"text": "Welcome"},
        )
        sent = self.assert_stamped("unmount_face", "/cmd/face/message/unmount", face_id="message")
        clears = [p for t, p in sent if t.endswith("/cmd/face/message/mount")]
        self.assertEqual(clears, [None] * len(TARGETS), "the retained clear stays an empty payload")

    def test_perimeter_ring_mount_and_clear(self):
        sent = self.assert_stamped(
            "mount_perimeter_ring", "/cmd/face/perimeter_ring/mount",
            bindings=[{"id": "binary_sensor.front_door"}], live_state=False,
        )
        for t, p in sent:
            if t.endswith("/cmd/face/perimeter_pulse/unmount"):
                self.assertEqual(p["_src"]["by"], "Tyler")
        self.assert_stamped("clear_perimeter_ring", "/cmd/face/perimeter_ring/unmount")

    def test_invitations(self):
        self.assert_stamped("send_invitation", "/cmd/face/invitation/mount", text="Open the bar?")
        self.assert_stamped(
            "send_invitation", "/invitation_request", text="Cookies", presentation="menu",
        )
        sent = self.assert_stamped("cancel_invitation", "/cmd/face/invitation/cancel", invitation_id="inv-1")
        requests = [p for t, p in sent if t.endswith("/invitation_request")]
        self.assertTrue(requests and all(p["_src"]["by"] == "Tyler" for p in requests))


class RequestLanesAreStamped(_Recorder):
    def test_theme(self):
        sent = self.assert_stamped("push_theme", "/theme_request", theme="arcanum")
        # requested_by stays for servers that predate _src (helm#521).
        self.assertTrue(all(p["requested_by"] == "Tyler" for _t, p in sent))

    def test_settings(self):
        self.assert_stamped("set_dial_settings", "/settings_request", label="Salon")
        self.assert_stamped("set_timezone", "/settings_request", timezone="Europe/Paris")

    def test_menu(self):
        self.assert_stamped("add_menu_item", "/menu_request", key="towels", label="Towels")
        self.assert_stamped("remove_menu_item", "/menu_request", key="towels")
        self.assert_stamped("push_menu", "/menu_request", profile="Guest")

    def test_alarm(self):
        self.assert_stamped("create_alarm", "/alarm_request", name="Wake", time="06:45")
        self.assert_stamped("dismiss_alarm", "/alarm_request")

    def test_credential(self):
        self.assert_stamped("revoke_credential", "/credential_request", identity_name="Guest 1")

    def test_schedule(self):
        self.assert_stamped("disable_schedule", "/schedule_request", name="Evening")


class TelemetryStaysUnstamped(_Recorder):
    def _assert_bare(self, service, suffix, **data):
        sent = self.run_service(service, **data)
        hits = [p for t, p in sent if t.endswith(suffix)]
        self.assertTrue(hits, f"{service} published nothing on {suffix}: {sent}")
        for p in hits:
            self.assertNotIn("_src", p, f"{service} → {suffix} is telemetry and must stay unstamped")

    def test_now_playing(self):
        self._assert_bare("update_now_playing", "/cmd/now_playing", title="Song")

    def test_sensor_value(self):
        self._assert_bare(
            "update_sensor_value", "/cmd/sensor_value", entity_id="sensor.pool", value="27.5",
        )

    def test_perimeter_state(self):
        self._assert_bare(
            "update_perimeter_state", "/cmd/face/perimeter_ring/state",
            states=[{"id": "binary_sensor.front_door", "state": "on"}],
        )

    def test_overlay_sensor_seed_is_unstamped_but_the_overlay_is(self):
        sent = self.run_service(
            "apply_overlay", home_face="sensor",
            sensors={"quad": [{"entity_id": "sensor.pool", "label": "Pool"}]},
        )
        for t, p in sent:
            if t.endswith("/cmd/sensor_value"):
                self.assertNotIn("_src", p)
            if t.endswith("/cmd/overlay"):
                self.assertEqual(p["_src"]["by"], "Tyler")


# ── entities: the entity's own context names the actor ──────────────────


class _EntityMqtt:
    def __init__(self):
        self.sent = []

    async def async_publish(self, hass, topic, payload, *args, **kwargs):
        self.sent.append((topic, json.loads(payload)))


class EntitiesAreStamped(unittest.TestCase):
    def setUp(self):
        self.mqtt = _EntityMqtt()
        self._saved = {}
        for name in ("button", "number", "select"):
            mod = standin.MODS[name]
            self._saved[name] = mod.mqtt
            mod.mqtt = self.mqtt
        hass = standin._Hass()
        hass.data = {"deckhand": {"entry-1": {"team_id": "team-1"}}}
        hass.auth = types.SimpleNamespace(async_get_user=self._get_user)
        hass.states = _States(_entity_state("automation.bedtime", "run-4", "Bedtime"))
        self.hass = hass
        self.entities = standin._all_entities(standin._data(1))
        for e in self.entities.values():
            e.hass = hass
            e.async_write_ha_state = lambda: None

    def tearDown(self):
        for name, mqtt in self._saved.items():
            standin.MODS[name].mqtt = mqtt

    @staticmethod
    async def _get_user(user_id):
        return types.SimpleNamespace(name="Tyler", is_admin=True) if user_id == "u1" else None

    def test_reboot_button(self):
        button = self.entities["button"]
        button._context = _ctx(user_id="u1")
        asyncio.run(button.async_press())
        (topic, payload), = self.mqtt.sent
        self.assertTrue(topic.endswith("/cmd/reboot"))
        self.assertEqual(payload, {"_src": {"ch": "home_assistant", "by": "Tyler"}})

    def test_brightness_slider_from_an_automation(self):
        number = self.entities["number"]
        number._context = _ctx(id="run-4")
        asyncio.run(number.async_set_native_value(40))
        (topic, payload), = self.mqtt.sent
        self.assertTrue(topic.endswith("/cmd/overlay"))
        self.assertEqual(payload["brightness"], 40)
        self.assertEqual(payload["_src"], {"ch": "home_assistant", "by": "automation: Bedtime"})

    def test_theme_select(self):
        select = self.entities["select"]
        select._context = _ctx(user_id="u1")
        asyncio.run(select.async_select_option("arcanum"))
        (topic, payload), = self.mqtt.sent
        self.assertTrue(topic.endswith("/theme_request"))
        self.assertEqual(payload["slug"], "arcanum")
        self.assertEqual(payload["requested_by"], "Tyler")
        self.assertEqual(payload["_src"], {"ch": "home_assistant", "by": "Tyler"})

    def test_no_context_is_home_assistant(self):
        button = self.entities["button"]
        asyncio.run(button.async_press())
        self.assertEqual(self.mqtt.sent[0][1], {"_src": UNATTRIBUTED})


if __name__ == "__main__":
    unittest.main()
