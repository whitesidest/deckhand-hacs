"""update_perimeter_state can drive the perimeter_ring overlay, not just the pulse hero (helm#415).

A dial draws a perimeter ring two ways: the ``perimeter_pulse`` HERO face, or
the ``perimeter_ring`` OVERLAY around a sensor / clock / charge face. Firmware
``face_dispatch_state`` (face.h) sends ``cmd/face/perimeter_ring/state`` to the
overlay slot and every other state topic to the hero, so an update only
reaches a ring overlay if its topic names ``perimeter_ring``. The service used
to hard-code ``perimeter_pulse``, so an automation could never move a ring
authored on another face.

HACS keeps no record of which ring a dial shows, so the caller picks with
``layer``. Omitting it must keep today's behaviour exactly.

Runs the REAL handler through the harness in test_emoji_strip_every_service.py.
"""

from __future__ import annotations

import unittest

import yaml

from test_emoji_strip_every_service import TARGETS, _ServiceTest, _SVE
from test_service_schema import SERVICES_YAML

STATES = [{"id": "front_door", "state": "unlocked"}, {"id": "living_temp", "value": 0.62}]
WIRE = {"bindings": [{"id": "front_door", "state": "unlocked"}, {"id": "living_temp", "value": 0.62}]}
PULSE = "/cmd/face/perimeter_pulse/state"
RING = "/cmd/face/perimeter_ring/state"


class PerimeterStateLayer(_ServiceTest):
    def _sent(self, **data):
        return self.call("update_perimeter_state", states=STATES, **data)

    def _assert_only(self, sent, suffix):
        topics = [t for t, _ in sent]
        self.assertEqual(len(sent), len(TARGETS), f"expected one push per dial, got {topics}")
        for (dial_id, team_id), (topic, payload) in zip(TARGETS, sent):
            self.assertEqual(topic, f"deckhand/{team_id}/dial/{dial_id}{suffix}")
            self.assertEqual(payload, WIRE)

    def test_omitted_layer_still_updates_the_pulse_face(self):
        self._assert_only(self._sent(), PULSE)

    def test_layer_pulse_updates_the_pulse_face(self):
        self._assert_only(self._sent(layer="pulse"), PULSE)

    def test_layer_ring_updates_the_ring_overlay_with_the_same_payload(self):
        self._assert_only(self._sent(layer="ring"), RING)

    def test_an_unknown_layer_is_refused_not_guessed(self):
        # "both" is deliberately not a layer: the dial hands a state that
        # isn't for its overlay to the hero, so the pulse face would get it
        # twice. A typo must not silently fall back to the pulse face either.
        for bad in ("both", "Ring", "perimeter_ring", 1):
            with self.subTest(layer=bad):
                with self.assertRaises(_SVE):
                    self._sent(layer=bad)
                self.assertEqual(self.mqtt.sent, [])


class LayerIsDocumented(unittest.TestCase):
    def test_services_yaml_offers_pulse_and_ring_defaulting_to_pulse(self):
        with open(SERVICES_YAML, encoding="utf-8") as f:
            field = yaml.safe_load(f)["update_perimeter_state"]["fields"]["layer"]
        self.assertEqual(field["selector"]["select"]["options"], ["pulse", "ring"])
        self.assertEqual(field["default"], "pulse")
        self.assertFalse(field.get("required", False))
