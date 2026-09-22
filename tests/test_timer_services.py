"""Timer services (1.18.0): start_timer / cancel_timer / add_timer_time.

The dial's Timer face gains a ``cmd/timer`` topic so an automation or an
Assist sentence can start, cancel or extend the same timer a guest starts on
the glass. These tests run the REAL ``__init__.py`` against the Home
Assistant stand-in from test_entities_follow_heartbeats.py (the loader from
test_emoji_strip_every_service.py): the module is imported for real,
``_register_services`` registers the real handlers, and only the MQTT
client, target resolution and the error class are swapped.

Pinned here:

* the wire contract — ``{"action": "start", "seconds", "label", "from"}``,
  ``{"action": "cancel"}``, ``{"action": "add", "seconds"}`` — one
  non-retained publish per resolved dial on ``.../cmd/timer``;
* seconds wins over minutes, minutes are converted, and both are bounded
  (minutes 1-1440, seconds 10-86400, add 10-3600);
* the label loses its emoji (helm#399) and is capped at 32 characters;
* validation is reported before the device is resolved;
* the services are declared in services.yaml with their selectors;
* the Timer event entity maps ``timer_cancel`` to ``cancelled`` and the raw
  ``deckhand_dial_event`` relay is untouched.

Run from the tests directory:  python3 -m unittest test_timer_services
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import types
import unittest
from pathlib import Path

import yaml

import test_entities_follow_heartbeats as standin  # installs the HA stand-in

PKG = "deckhand_timer_under_test"
TARGETS = [("DECK-AAAA", "team-1"), ("DECK-BBBB", "team-1")]
ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
SERVICES_YAML = COMPONENT / "services.yaml"
EVENT_PY = COMPONENT / "event.py"
INIT_PY = COMPONENT / "__init__.py"
README = ROOT / "README.md"


def _load_component():
    spec = importlib.util.spec_from_file_location(
        PKG, standin.PKG_DIR / "__init__.py",
        submodule_search_locations=[str(standin.PKG_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load_component()


class _SVE(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args or (kwargs.get("translation_key"),))


class _Mqtt:
    def __init__(self):
        self.sent: list[tuple[str, dict | None, bool]] = []

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.sent.append((topic, json.loads(payload) if payload else None, retain))


class _Services:
    def __init__(self):
        self.handlers = {}

    def has_service(self, domain, name):
        return name in self.handlers

    def async_register(self, domain, name, handler, *args, **kwargs):
        self.handlers[name] = handler


class _ServiceTest(unittest.TestCase):
    def setUp(self):
        self.mqtt = _Mqtt()
        self.resolved_with: list = []
        self.hass = types.SimpleNamespace(services=_Services(), data={})

        def _resolve(hass, device_id):
            self.resolved_with.append(device_id)
            return list(TARGETS)

        patches = {
            "mqtt": self.mqtt,
            "ServiceValidationError": _SVE,
            "_resolve_targets": _resolve,
        }
        self._saved = {k: getattr(MOD, k) for k in patches}
        for k, v in patches.items():
            setattr(MOD, k, v)
        MOD._register_services(self.hass, types.SimpleNamespace(entry_id="entry-1"))

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(MOD, k, v)

    def call(self, service, **data):
        data.setdefault("device_id", ["d1"])
        self.mqtt.sent.clear()
        asyncio.run(self.hass.services.handlers[service](types.SimpleNamespace(data=data)))
        return self.mqtt.sent

    def one(self, service, **data):
        """Call and return the single cmd/timer payload — after checking
        every resolved dial got the same one, non-retained."""
        sent = self.call(service, **data)
        self.assertEqual(len(sent), len(TARGETS), f"expected one publish per dial, got {sent}")
        topics = sorted(t for t, _p, _r in sent)
        self.assertEqual(
            topics,
            sorted(f"deckhand/{team}/dial/{dial}/cmd/timer" for dial, team in TARGETS),
        )
        payloads = [p for _t, p, _r in sent]
        self.assertTrue(all(p == payloads[0] for p in payloads), payloads)
        self.assertTrue(all(r is False for _t, _p, r in sent), "cmd/timer must not be retained")
        return payloads[0]

    def refused(self, service, **data):
        with self.assertRaises(_SVE) as ctx:
            self.call(service, **data)
        self.assertEqual(self.mqtt.sent, [], "nothing may reach the dial on a refusal")
        return str(ctx.exception)


# ── start_timer ───────────────────────────────────────────────────────

class StartTimerTests(_ServiceTest):
    def test_registered(self):
        for svc in ("start_timer", "cancel_timer", "add_timer_time"):
            self.assertIn(svc, self.hass.services.handlers, f"{svc} not registered")

    def test_minutes_become_seconds_with_the_full_contract(self):
        p = self.one("start_timer", minutes=10, label="Pasta", from_name="Kitchen")
        self.assertEqual(
            p, {"action": "start", "seconds": 600, "label": "Pasta", "from": "Kitchen"},
        )

    def test_seconds_alone(self):
        p = self.one("start_timer", seconds=90)
        self.assertEqual(p["action"], "start")
        self.assertEqual(p["seconds"], 90)

    def test_seconds_win_over_minutes(self):
        # An automation's default minutes may ride along with the seconds a
        # voice sentence captured; the more precise one is what was meant.
        p = self.one("start_timer", minutes=10, seconds=90)
        self.assertEqual(p["seconds"], 90)

    def test_defaults_when_label_and_from_are_omitted(self):
        p = self.one("start_timer", minutes=1)
        self.assertEqual(p["label"], "")
        self.assertEqual(p["from"], "Home Assistant")
        self.assertEqual(set(p), {"action", "seconds", "label", "from"})

    def test_number_strings_from_yaml_templates_are_accepted(self):
        # A Jinja template renders "{{ trigger.slots.minutes }}" as a string.
        self.assertEqual(self.one("start_timer", minutes="10")["seconds"], 600)
        self.assertEqual(self.one("start_timer", seconds="45.0")["seconds"], 45)
        # Empty string (a blank UI field) means "not given", not zero.
        self.assertEqual(self.one("start_timer", minutes=5, seconds="")["seconds"], 300)

    def test_fractional_minutes_are_rounded_to_seconds(self):
        self.assertEqual(self.one("start_timer", minutes=1.5)["seconds"], 90)

    # ── bounds ──

    def test_minutes_bounds(self):
        self.assertEqual(self.one("start_timer", minutes=1)["seconds"], 60)
        self.assertEqual(self.one("start_timer", minutes=1440)["seconds"], 86400)
        self.assertIn("minutes", self.refused("start_timer", minutes=0))
        self.assertIn("minutes", self.refused("start_timer", minutes=1441))
        self.assertIn("minutes", self.refused("start_timer", minutes=-5))

    def test_seconds_bounds(self):
        self.assertEqual(self.one("start_timer", seconds=10)["seconds"], 10)
        self.assertEqual(self.one("start_timer", seconds=86400)["seconds"], 86400)
        self.assertIn("seconds", self.refused("start_timer", seconds=9))
        self.assertIn("seconds", self.refused("start_timer", seconds=86401))

    def test_neither_minutes_nor_seconds_is_refused(self):
        msg = self.refused("start_timer")
        self.assertIn("minutes", msg)
        self.assertIn("seconds", msg)

    def test_non_numbers_are_refused(self):
        self.assertIn("minutes", self.refused("start_timer", minutes="ten"))
        self.assertIn("seconds", self.refused("start_timer", seconds="soon"))
        # bool is an int subclass — "minutes: true" is a typo, not 1 minute.
        self.assertIn("minutes", self.refused("start_timer", minutes=True))

    def test_duration_is_validated_before_the_device_is_resolved(self):
        # A bad duration must be reported as such even when the device is
        # also wrong — and never resolve (or publish to) anything.
        self.refused("start_timer", minutes=0)
        self.assertEqual(self.resolved_with, [])

    def test_unknown_device_is_refused_with_the_shared_key(self):
        setattr(MOD, "_resolve_targets", lambda hass, device_id: [])
        msg = self.refused("start_timer", minutes=5)
        self.assertIn("unknown_device", msg)

    # ── label ──

    def test_label_loses_its_emoji_and_keeps_renderable_text(self):
        p = self.one("start_timer", minutes=3, label="Eggs 🥚 → soft", from_name="Chef 👨‍🍳")
        self.assertEqual(p["label"], "Eggs → soft")
        self.assertEqual(p["from"], "Chef")

    def test_emoji_only_label_is_sent_blank_not_refused(self):
        # Unlike a menu label, the timer label is optional: an emoji-only
        # label degrades to the unlabeled timer rather than blocking it.
        p = self.one("start_timer", minutes=3, label="🍝")
        self.assertEqual(p["label"], "")

    def test_label_cap_is_32_after_stripping(self):
        self.assertEqual(len(self.one("start_timer", minutes=3, label="x" * 32)["label"]), 32)
        msg = self.refused("start_timer", minutes=3, label="x" * 33)
        self.assertIn("label", msg)
        self.assertIn("32", msg)
        # Emoji do not count: they are gone before the length check.
        p = self.one("start_timer", minutes=3, label="x" * 32 + "🎉🎉🎉")
        self.assertEqual(p["label"], "x" * 32)

    def test_emoji_only_from_name_falls_back_to_the_default(self):
        p = self.one("start_timer", minutes=3, from_name="🏠")
        self.assertEqual(p["from"], "Home Assistant")


# ── cancel_timer / add_timer_time ─────────────────────────────────────

class CancelTimerTests(_ServiceTest):
    def test_payload_is_just_the_action(self):
        self.assertEqual(self.one("cancel_timer"), {"action": "cancel"})

    def test_unknown_device_is_refused(self):
        setattr(MOD, "_resolve_targets", lambda hass, device_id: [])
        self.assertIn("unknown_device", self.refused("cancel_timer"))


class AddTimerTimeTests(_ServiceTest):
    def test_payload_shape(self):
        self.assertEqual(self.one("add_timer_time", seconds=300), {"action": "add", "seconds": 300})

    def test_bounds(self):
        self.assertEqual(self.one("add_timer_time", seconds=10)["seconds"], 10)
        self.assertEqual(self.one("add_timer_time", seconds=3600)["seconds"], 3600)
        self.assertIn("seconds", self.refused("add_timer_time", seconds=9))
        self.assertIn("seconds", self.refused("add_timer_time", seconds=3601))
        self.assertIn("seconds", self.refused("add_timer_time"))
        self.assertIn("seconds", self.refused("add_timer_time", seconds="lots"))

    def test_string_seconds_accepted(self):
        self.assertEqual(self.one("add_timer_time", seconds="120")["seconds"], 120)


# ── services.yaml ─────────────────────────────────────────────────────

class ServicesYamlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICES_YAML, encoding="utf-8") as f:
            cls.services = yaml.safe_load(f)

    def _fields(self, svc):
        self.assertIn(svc, self.services, f"{svc} missing from services.yaml")
        return self.services[svc].get("fields") or {}

    def test_start_timer_fields_and_selectors(self):
        f = self._fields("start_timer")
        self.assertEqual(set(f), {"device_id", "minutes", "seconds", "label", "from_name"})
        self.assertTrue(f["device_id"].get("required"))
        self.assertTrue(f["device_id"]["selector"]["device"].get("multiple"))
        self.assertEqual(f["device_id"]["selector"]["device"].get("integration"), "deckhand")
        # Neither duration field is required on its own — the handler
        # insists on at least one; the UI cannot express "one of".
        self.assertFalse(f["minutes"].get("required", False))
        self.assertFalse(f["seconds"].get("required", False))
        self.assertEqual(f["minutes"]["selector"]["number"]["min"], 1)
        self.assertEqual(f["minutes"]["selector"]["number"]["max"], 1440)
        self.assertEqual(f["seconds"]["selector"]["number"]["min"], 10)
        self.assertEqual(f["seconds"]["selector"]["number"]["max"], 86400)
        self.assertIn("text", f["label"]["selector"])
        self.assertIn("text", f["from_name"]["selector"])
        for name, field in f.items():
            self.assertTrue(field.get("description"), f"start_timer.{name} has no description")

    def test_cancel_timer_fields(self):
        f = self._fields("cancel_timer")
        self.assertEqual(set(f), {"device_id"})
        self.assertTrue(f["device_id"].get("required"))
        self.assertTrue(f["device_id"]["selector"]["device"].get("multiple"))

    def test_add_timer_time_fields(self):
        f = self._fields("add_timer_time")
        self.assertEqual(set(f), {"device_id", "seconds"})
        self.assertTrue(f["seconds"].get("required"))
        self.assertEqual(f["seconds"]["selector"]["number"]["min"], 10)
        self.assertEqual(f["seconds"]["selector"]["number"]["max"], 3600)

    def test_every_timer_service_has_a_description(self):
        for svc in ("start_timer", "cancel_timer", "add_timer_time"):
            self.assertTrue(self.services[svc].get("description"), f"{svc} has no description")
            self.assertTrue(self.services[svc].get("name"), f"{svc} has no name")

    def test_handlers_read_declared_fields(self):
        src = INIT_PY.read_text(encoding="utf-8")
        for field in ("minutes", "seconds", "label", "from_name"):
            self.assertIn(f'call.data.get("{field}")', src, f"start_timer never reads {field}")

    def test_topic_constant_matches_the_firmware_contract(self):
        const = (COMPONENT / "const.py").read_text(encoding="utf-8")
        self.assertIn('TOPIC_CMD_TIMER = "deckhand/{team_id}/dial/{dial_id}/cmd/timer"', const)


# ── Timer event entity: cancelled ─────────────────────────────────────

class TimerEventCancelledTests(unittest.TestCase):
    def test_timer_cancel_maps_to_cancelled(self):
        import ast

        tree = ast.parse(EVENT_PY.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TIMER_EVENT_TYPES" for t in node.targets
            ):
                types_map = ast.literal_eval(node.value)
                break
        else:
            raise AssertionError("TIMER_EVENT_TYPES not found")
        self.assertEqual(types_map.get("timer_cancel"), "cancelled")
        # The two existing types are untouched.
        self.assertEqual(types_map.get("timer_start"), "started")
        self.assertEqual(types_map.get("timer_complete"), "completed")

    def test_entity_offers_cancelled_as_an_event_type(self):
        src = EVENT_PY.read_text(encoding="utf-8")
        # event_types is derived from the map, so adding to the map is
        # what surfaces it in the automation editor.
        self.assertIn("_attr_event_types = list(TIMER_EVENT_TYPES.values())", src)

    def test_raw_bus_relay_is_unchanged(self):
        # The entity listens to deckhand_dial_event and never re-fires or
        # renames it; automations on the raw bus see timer_cancel as-is.
        src = EVENT_PY.read_text(encoding="utf-8")
        self.assertIn('self.hass.bus.async_listen(f"{DOMAIN}_dial_event"', src)
        self.assertNotIn("bus.async_fire", src)
        # Positive control: the relay in __init__.py still fires the raw
        # event under its wire name and passes ``type`` straight through
        # from the payload — no timer-specific renaming or filtering.
        init_src = INIT_PY.read_text(encoding="utf-8")
        relay = re.search(
            r"def _handle_event\(msg.*?\n(.*?)\n        # Media control loopback",
            init_src, re.DOTALL,
        )
        self.assertIsNotNone(relay, "_handle_event relay not found")
        body = relay.group(1)
        self.assertIn('f"{DOMAIN}_dial_event"', body)
        self.assertIn('"type": payload.get("type")', body)
        self.assertNotIn("timer", body)

    def test_readme_documents_cancelled_and_the_three_services(self):
        text = README.read_text(encoding="utf-8")
        for needle in (
            "### `deckhand.start_timer`",
            "### `deckhand.cancel_timer`",
            "### `deckhand.add_timer_time`",
            "`cancelled`",
            "type: timer_cancel",
            "Voice: Assist custom sentence",
            "{{ trigger.slots.minutes }}",
            "event_type: timer.started",
        ):
            self.assertIn(needle, text, f"README is missing {needle!r}")


class ManifestTests(unittest.TestCase):
    def test_version_bumped_to_1_18_0(self):
        manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "1.18.0")


if __name__ == "__main__":
    unittest.main()
