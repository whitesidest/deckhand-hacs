"""create_schedule's structured Days of Week selector (2026-09-23 UX audit).

Same fix as test_alarm_services.py, applied to the other hand-typed
``days`` field: ``create_schedule.days`` is now a ``select`` with
``multiple: true`` (values "0".."6", labeled Monday..Sunday) instead
of an ``object`` selector operators had to fill with typed YAML like
``[4, 5]``. The wire convention (0=Mon..6=Sun) is unchanged.

HA delivers the multi-select's choices as a list of value STRINGS, so
the handler coerces to ints before publishing on ``schedule_request``
— Helm/Console/firmware all expect a list of ints here, same as
alarms. A malformed value raises ``ServiceValidationError`` instead of
crashing the handler.

Run from the tests directory:  python3 -m unittest test_schedule_services
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

import yaml

import test_entities_follow_heartbeats as standin  # installs the HA stand-in

PKG = "deckhand_schedule_under_test"
TARGETS = [("DECK-AAAA", "team-1"), ("DECK-BBBB", "team-1")]
ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
SERVICES_YAML = COMPONENT / "services.yaml"


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
        """Call and return the single schedule_request payload — after
        checking every resolved dial got the same one."""
        sent = self.call(service, **data)
        self.assertEqual(len(sent), len(TARGETS), f"expected one publish per dial, got {sent}")
        topics = sorted(t for t, _p, _r in sent)
        self.assertEqual(
            topics,
            sorted(f"deckhand/{team}/dial/{dial}/schedule_request" for dial, team in TARGETS),
        )
        payloads = [p for _t, p, _r in sent]
        self.assertTrue(all(p == payloads[0] for p in payloads), payloads)
        return payloads[0]

    def refused(self, service, **data):
        with self.assertRaises(_SVE) as ctx:
            self.call(service, **data)
        self.assertEqual(self.mqtt.sent, [], "nothing may reach the dial on a refusal")
        return str(ctx.exception)


class CreateScheduleDaysSelectorTests(_ServiceTest):
    def test_multi_select_strings_become_an_int_list(self):
        p = self.one("create_schedule", name="Evening Scene", days=["1", "3"])
        self.assertEqual(p["days"], [1, 3])
        self.assertTrue(all(type(d) is int for d in p["days"]), p["days"])

    def test_empty_selection_is_sent_as_an_empty_list(self):
        p = self.one("create_schedule", name="Evening Scene", days=[])
        self.assertEqual(p["days"], [])

    def test_omitted_days_are_absent_from_the_payload(self):
        p = self.one("create_schedule", name="Evening Scene")
        self.assertNotIn("days", p)

    def test_bad_day_value_is_refused_not_uncaught(self):
        msg = self.refused("create_schedule", name="Evening Scene", days=["oops"])
        self.assertIn("days", msg)
        self.assertEqual(self.resolved_with, [], "a bad day must be reported before targeting")


class ServicesYamlDaysSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICES_YAML, encoding="utf-8") as f:
            cls.services = yaml.safe_load(f)

    def test_create_schedule_days_is_a_multi_select_of_weekdays(self):
        field = self.services["create_schedule"]["fields"]["days"]
        sel = field["selector"]["select"]
        self.assertTrue(sel.get("multiple"), "days selector must be multiple: true")
        values = [opt["value"] for opt in sel["options"]]
        self.assertEqual(values, ["0", "1", "2", "3", "4", "5", "6"])
        labels = [opt["label"] for opt in sel["options"]]
        self.assertEqual(
            labels,
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        )
        self.assertNotIn("object", field["selector"])


if __name__ == "__main__":
    unittest.main()
