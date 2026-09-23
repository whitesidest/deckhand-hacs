"""update_perimeter_state publishes to the perimeter_ring overlay whatever ``layer`` says.

``layer`` (helm#415) used to pick between the ``perimeter_pulse`` HERO face
and the ``perimeter_ring`` OVERLAY around a sensor / clock / charge face —
firmware ``face_dispatch_state`` (face.h) routes a state topic to the mounted
face whose id it names, so the topic had to say which. The pulse face is
retired (1.18.1): there is one ring, and every layer value — ``ring``,
``pulse`` and omitted — lands on ``cmd/face/perimeter_ring/state`` with the
same payload. The field stays so automations written for it keep validating;
an unknown value is still refused rather than guessed.

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

    def test_omitted_layer_updates_the_ring_overlay(self):
        self._assert_only(self._sent(), RING)

    def test_layer_ring_updates_the_ring_overlay(self):
        self._assert_only(self._sent(layer="ring"), RING)

    def test_layer_pulse_is_a_retired_alias_for_the_ring(self):
        # An automation written for the pulse face keeps moving the ring
        # it now sees; nothing goes to the retired topic.
        sent = self._sent(layer="pulse")
        self._assert_only(sent, RING)
        self.assertFalse([t for t, _ in sent if t.endswith(PULSE)])

    def test_an_unknown_layer_is_refused_not_guessed(self):
        for bad in ("both", "Ring", "perimeter_ring", 1):
            with self.subTest(layer=bad):
                with self.assertRaises(_SVE):
                    self._sent(layer=bad)
                self.assertEqual(self.mqtt.sent, [])


class LayerIsDocumented(unittest.TestCase):
    def test_services_yaml_offers_ring_and_pulse_defaulting_to_ring(self):
        with open(SERVICES_YAML, encoding="utf-8") as f:
            field = yaml.safe_load(f)["update_perimeter_state"]["fields"]["layer"]
        self.assertEqual(field["selector"]["select"]["options"], ["pulse", "ring"])
        self.assertEqual(field["default"], "ring")
        self.assertFalse(field.get("required", False))
