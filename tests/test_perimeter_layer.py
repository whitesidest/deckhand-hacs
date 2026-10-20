"""The Perimeter Ring is a LAYER, and HACS drives it end to end (1.21.0).

The perimeter stopped being a face (the full-screen ``perimeter_pulse``) and
became the ``perimeter_ring`` overlay that rides on top of any hero. Helm,
Console, the MCP tools and the firmware all moved; this integration still
published the old contract in four places:

1. **Treatments.** ``PERIMETER_TREATMENTS`` held the legacy five
   (state_color, ripple, gradient, flash, sweep), so ``bounce`` was
   downgraded to state_color and the retired names went out on the wire.
   Canonical is {state_color, flash, bounce}; retired names fold the way
   the backends' validators fold them.
2. **Layout.** Every binding without an ``angular_center`` got 0, so all
   arcs stacked at 12 o'clock and only one showed. Missing centres are
   spread evenly and overlapping arcs re-spaced, as the backends do.
3. **Layer options + the state the dial matches.** No ``theme_colors`` /
   ``hide_when_calm``; ``active_state`` was left out, and the firmware never
   activates an empty one. The id cap (64, the firmware's state-match key
   buffer) was not enforced.
4. **Lifecycle.** No live feed (a HACS-mounted ring stayed frozen unless the
   user wrote an automation per binding) and no clear service.

Runs the REAL handlers through the harness in
test_emoji_strip_every_service.py, and the pure helpers in ``_perimeter.py``
directly.
"""

from __future__ import annotations

import asyncio
import json
import types
import unittest

import yaml

from perimeter_shaper_ns import const, perimeter
from test_emoji_strip_every_service import MOD, TARGETS, _SVE, _ServiceTest, _state
from test_service_schema import SERVICES_YAML

RING_MOUNT = "/cmd/face/perimeter_ring/mount"
RING_UNMOUNT = "/cmd/face/perimeter_ring/unmount"
RING_STATE = "/cmd/face/perimeter_ring/state"
PULSE_MOUNT = "/cmd/face/perimeter_pulse/mount"
PULSE_UNMOUNT = "/cmd/face/perimeter_pulse/unmount"

DOORS = [
    {"id": "binary_sensor.front_door"},
    {"id": "binary_sensor.back_door"},
    {"id": "binary_sensor.garage_door"},
    {"id": "binary_sensor.guest_suite_sliding_door_contact_1"},
]


def _ring(sent):
    mounts = [p for t, p in sent if t.endswith(RING_MOUNT) and p is not None]
    assert len(mounts) == len(TARGETS), mounts
    return mounts[0]


# ── Pure helpers ────────────────────────────────────────────────────


class TreatmentVocabulary(unittest.TestCase):
    def test_canonical_set_matches_the_backends(self):
        self.assertEqual(set(const.PERIMETER_TREATMENTS), {"state_color", "flash", "bounce"})

    def test_retired_names_fold_to_their_survivor(self):
        norm = lambda v: perimeter.normalize_treatment(  # noqa: E731
            v, const.PERIMETER_TREATMENTS, const.PERIMETER_TREATMENT_ALIASES
        )
        self.assertEqual(norm("bounce"), "bounce")
        self.assertEqual(norm("ripple"), "flash")
        self.assertEqual(norm("sweep"), "flash")
        self.assertEqual(norm("blink"), "flash")
        self.assertEqual(norm("gradient"), "state_color")
        self.assertEqual(norm(None), "state_color")
        self.assertIsNone(norm("disco"))


class Layout(unittest.TestCase):
    def test_missing_centres_are_spread_evenly(self):
        b = perimeter.layout_bindings([{"id": f"x.{i}"} for i in range(4)], contiguous=False)
        self.assertEqual([x["angular_center"] for x in b], [0.0, 90.0, 180.0, 270.0])

    def test_colliding_arcs_are_respaced(self):
        b = perimeter.layout_bindings(
            [{"id": "a", "angular_center": 0}, {"id": "b", "angular_center": 10}, {"id": "c", "angular_center": 200}],
            contiguous=False,
        )
        self.assertEqual([x["angular_center"] for x in b], [0.0, 120.0, 240.0])

    def test_a_clean_manual_layout_is_kept(self):
        b = perimeter.layout_bindings(
            [{"id": "a", "angular_center": 45}, {"id": "b", "angular_center": 200, "angular_width": 40}],
            contiguous=False,
        )
        self.assertEqual([x["angular_center"] for x in b], [45, 200])

    def test_contiguous_only_fills_missing_centres(self):
        # Contiguous ignores positions on the dial (even split by order), so
        # equal centres are not a collision there.
        b = perimeter.layout_bindings(
            [{"id": "a", "angular_center": 0}, {"id": "b", "angular_center": 0}], contiguous=True,
        )
        self.assertEqual([x["angular_center"] for x in b], [0, 0])


class LiveStateResolution(unittest.TestCase):
    """A copy of Helm's _perimeter_state_updates: the ring must read the
    same whichever side feeds it."""

    def test_ha_on_is_sent_as_the_bindings_own_active_state(self):
        self.assertEqual(perimeter.binding_state({"active_state": "open"}, "on"), "open")
        self.assertEqual(perimeter.binding_state({"active_state": "unlocked"}, "unlocked"), "unlocked")

    def test_a_calm_reading_passes_through(self):
        self.assertEqual(perimeter.binding_state({"active_state": "open"}, "off"), "off")

    def test_numeric_threshold(self):
        b = {"numeric_threshold": 0.1, "above_state": "exporting", "below_state": "importing"}
        self.assertEqual(perimeter.binding_state(b, "0.4"), "exporting")
        self.assertEqual(perimeter.binding_state(b, "0.05"), "importing")
        self.assertEqual(perimeter.binding_state(b, "unavailable"), "unavailable")
        self.assertEqual(perimeter.binding_state({"numeric_threshold": 0}, "3"), "on")

    def test_an_entity_ha_does_not_know_is_left_out(self):
        states = {"binary_sensor.a": "on"}
        out = perimeter.state_updates(
            [{"id": "binary_sensor.a", "active_state": "on"}, {"id": "binary_sensor.gone"}], states.get,
        )
        self.assertEqual(out, [{"id": "binary_sensor.a", "state": "on"}])

    def test_live_state_off_and_junk_mounts_feed_nothing(self):
        key = const.PERIMETER_LIVE_STATE_KEY
        self.assertEqual(perimeter.fed_bindings({"bindings": DOORS, key: False}, key), [])
        self.assertEqual(perimeter.fed_bindings("nope", key), [])
        self.assertEqual(len(perimeter.fed_bindings({"bindings": DOORS}, key)), 4)


# ── Services ────────────────────────────────────────────────────────


class MountTheLayer(_ServiceTest):
    def test_bindings_without_centres_get_distinct_ones(self):
        ring = _ring(self.call("mount_perimeter_ring", bindings=DOORS))
        centres = [b["angular_center"] for b in ring["bindings"]]
        self.assertEqual(centres, [0.0, 90.0, 180.0, 270.0])

    def test_bounce_survives_and_retired_treatments_fold(self):
        ring = _ring(self.call("mount_perimeter_ring", bindings=[
            {"id": "binary_sensor.a", "treatment": "bounce"},
            {"id": "binary_sensor.b", "treatment": "ripple"},
            {"id": "binary_sensor.c", "treatment": "gradient"},
        ]))
        self.assertEqual([b["treatment"] for b in ring["bindings"]], ["bounce", "flash", "state_color"])

    def test_active_state_defaults_to_on(self):
        ring = _ring(self.call("mount_perimeter_ring", bindings=[
            {"id": "binary_sensor.a"}, {"id": "lock.front", "active_state": "unlocked"},
        ]))
        self.assertEqual([b["active_state"] for b in ring["bindings"]], ["on", "unlocked"])

    def test_layer_options_reach_the_wire(self):
        ring = _ring(self.call(
            "mount_perimeter_ring", bindings=DOORS, theme_colors=True, hide_when_calm=True, contiguous=True,
        ))
        self.assertIs(ring["theme_colors"], True)
        self.assertIs(ring["hide_when_calm"], True)
        self.assertIs(ring["contiguous"], True)
        self.assertNotIn(const.PERIMETER_LIVE_STATE_KEY, ring)

    def test_live_state_off_is_written_only_when_off(self):
        ring = _ring(self.call("mount_perimeter_ring", bindings=DOORS, live_state=False))
        self.assertIs(ring[const.PERIMETER_LIVE_STATE_KEY], False)

    def test_a_64_char_id_is_kept_whole(self):
        eid = "binary_sensor." + "x" * 50
        self.assertEqual(len(eid), 64)
        ring = _ring(self.call("mount_perimeter_ring", bindings=[{"id": eid}]))
        self.assertEqual(ring["bindings"][0]["id"], eid)

    def test_a_longer_id_is_refused_not_truncated(self):
        with self.assertRaises(_SVE):
            self.call("mount_perimeter_ring", bindings=[{"id": "binary_sensor." + "x" * 51}])
        self.assertEqual(
            [t for t, _ in self.mqtt.sent if t.endswith(RING_MOUNT)], [],
            "nothing may be mounted when a binding id would be cut",
        )


class ClearTheLayer(_ServiceTest):
    def test_clear_unmounts_and_empties_the_retained_ring(self):
        sent = self.call("clear_perimeter_ring")
        for suffix in (RING_UNMOUNT, PULSE_UNMOUNT):
            hits = [p for t, p in sent if t.endswith(suffix)]
            self.assertEqual(hits, [{}] * len(TARGETS), suffix)
        for suffix in (RING_MOUNT, PULSE_MOUNT):
            hits = [p for t, p in sent if t.endswith(suffix)]
            self.assertEqual(hits, [None] * len(TARGETS), f"{suffix} gets an empty retained clear")

    def test_clear_never_touches_a_hero(self):
        sent = self.call("clear_perimeter_ring")
        faces = {t.split("/cmd/face/")[1].split("/")[0] for t, _ in sent}
        self.assertEqual(faces, {"perimeter_ring", "perimeter_pulse"})


# ── The live feed ───────────────────────────────────────────────────


class LiveFeed(_ServiceTest):
    def setUp(self):
        super().setUp()
        self.listeners: list = []
        self.unsubbed = 0
        self.tasks: list = []

        def _track(hass, entity_ids, handler):
            rec = {"ids": list(entity_ids), "handler": handler}
            self.listeners.append(rec)

            def _unsub():
                self.unsubbed += 1
                self.listeners.remove(rec)

            return _unsub

        self._saved["async_track_state_change_event"] = MOD.async_track_state_change_event
        MOD.async_track_state_change_event = _track
        self.hass.async_create_task = self.tasks.append
        self.hass.data[MOD.DOMAIN] = {"entry-1": {"_perimeter_feeds": {}}}
        self.hass.states._states.update({
            "binary_sensor.front_door": _state("on"),
            "binary_sensor.back_door": _state("off"),
            "sensor.grid_power": _state("0.4"),
        })
        self.entry = types.SimpleNamespace(entry_id="entry-1")

    def tearDown(self):
        for task in self.tasks:
            task.close()
        super().tearDown()

    def _drain(self):
        self.mqtt.sent.clear()
        while self.tasks:
            asyncio.run(self.tasks.pop(0))
        return [(t, p) for t, p in self.mqtt.sent if t.endswith(RING_STATE)]

    def _arm(self, bindings):
        MOD._set_perimeter_feed(self.hass, self.entry, "DECK-AAAA", "team-1", bindings)

    def test_arming_pushes_every_known_binding_folded_to_its_active_state(self):
        self._arm([
            {"id": "binary_sensor.front_door", "active_state": "open"},
            {"id": "binary_sensor.back_door", "active_state": "open"},
            {"id": "sensor.grid_power", "numeric_threshold": 0.1, "above_state": "exporting"},
            {"id": "binary_sensor.not_loaded_yet", "active_state": "on"},
        ])
        sent = self._drain()
        self.assertEqual(len(sent), 1)
        topic, body = sent[0]
        self.assertEqual(topic, "deckhand/team-1/dial/DECK-AAAA" + RING_STATE)
        self.assertEqual(body, {"bindings": [
            {"id": "binary_sensor.front_door", "state": "open"},
            {"id": "binary_sensor.back_door", "state": "off"},
            {"id": "sensor.grid_power", "state": "exporting"},
        ]})
        # The entity HA has not loaded yet is still watched.
        self.assertIn("binary_sensor.not_loaded_yet", self.listeners[0]["ids"])

    def test_a_change_pushes_only_that_binding(self):
        self._arm([{"id": "binary_sensor.front_door", "active_state": "open"},
                   {"id": "binary_sensor.back_door", "active_state": "open"}])
        self._drain()
        self.hass.states._states["binary_sensor.back_door"] = _state("on")
        self.listeners[0]["handler"](types.SimpleNamespace(data={"entity_id": "binary_sensor.back_door"}))
        self.assertEqual(self._drain()[0][1], {"bindings": [{"id": "binary_sensor.back_door", "state": "open"}]})

    def test_rearming_replaces_and_clearing_stands_down(self):
        self._arm([{"id": "binary_sensor.front_door"}])
        self._arm([{"id": "binary_sensor.back_door"}])
        self.assertEqual(self.unsubbed, 1)
        self.assertEqual(len(self.listeners), 1)
        self._arm([])  # the empty retained clear
        self.assertEqual(self.listeners, [])
        self.assertEqual(self.hass.data[MOD.DOMAIN]["entry-1"]["_perimeter_feeds"], {})

    def test_ids_that_are_not_entities_are_not_watched(self):
        self._arm([{"id": "front_door"}])
        self.assertEqual(self.listeners, [])


class Wiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICES_YAML, encoding="utf-8") as f:
            cls.services = yaml.safe_load(f)
        cls.init_src = (SERVICES_YAML.parent / "__init__.py").read_text(encoding="utf-8")

    def test_feed_follows_every_retained_ring_mount_on_the_team(self):
        self.assertEqual(
            const.TOPIC_PERIMETER_MOUNTS, "deckhand/{team_id}/dial/+/cmd/face/perimeter_ring/mount",
        )
        self.assertIn("mqtt.async_subscribe(hass, perimeter_topic, _handle_perimeter_mount", self.init_src)
        self.assertIn("timedelta(seconds=PERIMETER_REFRESH_S)", self.init_src)

    def test_layer_fields_declared_on_both_mount_names(self):
        for name in ("mount_perimeter_ring", "mount_perimeter_pulse"):
            fields = self.services[name]["fields"]
            for key in ("theme_colors", "hide_when_calm", "live_state", "contiguous"):
                self.assertIn(key, fields, f"{name} missing {key}")
            self.assertNotIn("ripple", fields["bindings"]["example"])

    def test_clear_service_declared_and_registered(self):
        self.assertIn("clear_perimeter_ring", self.services)
        self.assertEqual(list(self.services["clear_perimeter_ring"]["fields"]), ["device_id"])
        self.assertIn('"clear_perimeter_ring", _clear_perimeter_ring', self.init_src)

    def test_id_too_long_has_a_translation(self):
        for rel in ("strings.json", "translations/en.json"):
            data = json.loads((SERVICES_YAML.parent / rel).read_text(encoding="utf-8"))
            self.assertIn("binding_id_too_long", data["exceptions"])
