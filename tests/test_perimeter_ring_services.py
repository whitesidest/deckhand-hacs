"""The perimeter services target perimeter_ring; the perimeter_pulse face is retired (1.18.1).

``perimeter_pulse`` was the full-screen Perimeter Pulse hero face.
``perimeter_ring`` is the ring-only overlay that composites over whatever
face the dial is showing, with the same payload (bindings, bar_thickness,
bar_opacity, contiguous, treatments). The founder retired the pulse face,
so every service that used to publish to ``cmd/face/perimeter_pulse/*``
now publishes to ``cmd/face/perimeter_ring/*``:

* ``mount_perimeter_ring`` is the new name; ``mount_perimeter_pulse`` is the
  SAME handler under the old name, kept because existing automations call
  it. Both must land on ``cmd/face/perimeter_ring/mount``.
* ``mount_face`` / ``unmount_face`` with ``face_id: perimeter_pulse`` are
  mapped to ``perimeter_ring``.
* Mounting the ring also unmounts ``perimeter_pulse`` and clears its
  retained mount — the way ``unmount_face`` does — so a retained old mount
  cannot resurrect on the dial's next reconnect and cover the ring.

Runs the REAL handlers through the harness in test_emoji_strip_every_service.py
(``_Mqtt`` records ``(topic, payload)``; an empty retained clear records
``None``).
"""

from __future__ import annotations

import json
import unittest

import yaml

from test_emoji_strip_every_service import TARGETS, _ServiceTest
from test_service_schema import SERVICES_YAML

BINDINGS = [{"id": "front_door", "friendly_name": "Front Door", "state": "open",
             "active_state": "open"}]

RING_MOUNT = "/cmd/face/perimeter_ring/mount"
RING_UNMOUNT = "/cmd/face/perimeter_ring/unmount"
PULSE_MOUNT = "/cmd/face/perimeter_pulse/mount"
PULSE_UNMOUNT = "/cmd/face/perimeter_pulse/unmount"


def _with_suffix(sent, suffix):
    return [(t, p) for t, p in sent if t.endswith(suffix)]


class MountTargetsTheRing(_ServiceTest):
    def _assert_ring_mounted(self, sent, *, contiguous):
        mounts = _with_suffix(sent, RING_MOUNT)
        self.assertEqual(len(mounts), len(TARGETS), f"one ring mount per dial, got {mounts}")
        for (dial_id, team_id), (topic, payload) in zip(TARGETS, mounts):
            self.assertEqual(topic, f"deckhand/{team_id}/dial/{dial_id}{RING_MOUNT}")
            self.assertEqual(payload["face_id"], "perimeter_ring")
            # The handler only stamps contiguous when the caller gave it
            # (the firmware default is False), so absent reads as False.
            self.assertEqual(payload.get("contiguous", False), contiguous)
            self.assertEqual(payload["bindings"][0]["id"], "front_door")

    def _assert_pulse_retired(self, sent):
        """The companion publishes: cmd/face/perimeter_pulse/unmount ("{}")
        and an EMPTY retained clear on cmd/face/perimeter_pulse/mount — the
        same pair unmount_face sends — and never a pulse mount body."""
        unmounts = _with_suffix(sent, PULSE_UNMOUNT)
        clears = _with_suffix(sent, PULSE_MOUNT)
        self.assertEqual(len(unmounts), len(TARGETS), f"one pulse unmount per dial, got {unmounts}")
        self.assertEqual(len(clears), len(TARGETS), f"one pulse clear per dial, got {clears}")
        for (dial_id, team_id), (utopic, upayload), (ctopic, cpayload) in zip(TARGETS, unmounts, clears):
            self.assertEqual(utopic, f"deckhand/{team_id}/dial/{dial_id}{PULSE_UNMOUNT}")
            self.assertEqual(upayload, {})
            self.assertEqual(ctopic, f"deckhand/{team_id}/dial/{dial_id}{PULSE_MOUNT}")
            self.assertIsNone(cpayload, "the pulse mount topic gets an empty retained clear, not a mount")

    def _assert_pulse_cleared_before_ring_mounted(self, sent):
        topics = [t for t, _ in sent]
        last_clear = max(i for i, t in enumerate(topics) if t.endswith(PULSE_MOUNT))
        first_ring = min(i for i, t in enumerate(topics) if t.endswith(RING_MOUNT))
        self.assertLess(last_clear, first_ring, "retire the pulse face before the ring goes up")

    def test_mount_perimeter_ring_publishes_the_ring_mount(self):
        sent = self.call("mount_perimeter_ring", bindings=BINDINGS, contiguous=True)
        self._assert_ring_mounted(sent, contiguous=True)
        self._assert_pulse_retired(sent)
        self._assert_pulse_cleared_before_ring_mounted(sent)

    def test_mount_perimeter_pulse_is_the_same_handler_under_the_old_name(self):
        ring = self.call("mount_perimeter_ring", bindings=BINDINGS, bar_thickness=9)
        pulse = self.call("mount_perimeter_pulse", bindings=BINDINGS, bar_thickness=9)
        self.assertEqual(pulse, ring)
        self._assert_ring_mounted(pulse, contiguous=False)
        self.assertEqual(_with_suffix(pulse, RING_MOUNT)[0][1]["bar_thickness"], 9)
        self._assert_pulse_retired(pulse)

    def test_nothing_is_ever_mounted_on_the_retired_pulse_topic(self):
        for svc in ("mount_perimeter_ring", "mount_perimeter_pulse"):
            with self.subTest(service=svc):
                sent = self.call(svc, bindings=BINDINGS)
                bodies = [p for t, p in sent if t.endswith(PULSE_MOUNT) and p is not None]
                self.assertEqual(bodies, [])

    def test_mount_face_with_pulse_id_lands_on_the_ring(self):
        sent = self.call("mount_face", face_id="perimeter_pulse",
                         payload={"face_id": "perimeter_pulse", "contiguous": True,
                                  "bindings": BINDINGS})
        self._assert_ring_mounted(sent, contiguous=True)
        self._assert_pulse_retired(sent)

    def test_mount_face_with_ring_id_also_retires_the_pulse(self):
        sent = self.call("mount_face", face_id="perimeter_ring",
                         payload={"contiguous": False, "bindings": BINDINGS})
        self._assert_ring_mounted(sent, contiguous=False)
        self._assert_pulse_retired(sent)

    def test_mount_face_for_another_face_does_not_touch_the_pulse_topics(self):
        # Positive control for the companion: it is a perimeter thing, not
        # something every face mount pays for.
        sent = self.call("mount_face", face_id="charge",
                         payload={"mode": "ev", "battery_pct": 50})
        self.assertEqual(len(_with_suffix(sent, "/cmd/face/charge/mount")), len(TARGETS))
        self.assertEqual(_with_suffix(sent, PULSE_UNMOUNT), [])
        self.assertEqual(_with_suffix(sent, PULSE_MOUNT), [])


class UnmountTargetsTheRing(_ServiceTest):
    def _assert_face_taken_down(self, sent, unmount_suffix, mount_suffix):
        unmounts = _with_suffix(sent, unmount_suffix)
        clears = _with_suffix(sent, mount_suffix)
        self.assertEqual(len(unmounts), len(TARGETS))
        self.assertEqual(len(clears), len(TARGETS))
        self.assertTrue(all(p == {} for _, p in unmounts))
        self.assertTrue(all(p is None for _, p in clears))

    def test_unmount_face_pulse_id_takes_down_the_ring_and_the_retired_face(self):
        sent = self.call("unmount_face", face_id="perimeter_pulse")
        self._assert_face_taken_down(sent, RING_UNMOUNT, RING_MOUNT)
        self._assert_face_taken_down(sent, PULSE_UNMOUNT, PULSE_MOUNT)

    def test_unmount_face_ring_id_sweeps_the_retired_pulse_topics_too(self):
        sent = self.call("unmount_face", face_id="perimeter_ring")
        self._assert_face_taken_down(sent, RING_UNMOUNT, RING_MOUNT)
        self._assert_face_taken_down(sent, PULSE_UNMOUNT, PULSE_MOUNT)

    def test_unmount_face_for_another_face_is_unchanged(self):
        sent = self.call("unmount_face", face_id="charge")
        self._assert_face_taken_down(sent, "/cmd/face/charge/unmount", "/cmd/face/charge/mount")
        self.assertEqual(len(sent), 2 * len(TARGETS))


class ServicesYamlNamesTheRing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICES_YAML, encoding="utf-8") as f:
            cls.services = yaml.safe_load(f)

    def test_both_names_are_declared_and_read_as_the_ring(self):
        ring = self.services["mount_perimeter_ring"]
        pulse = self.services["mount_perimeter_pulse"]
        self.assertEqual(ring["name"], "Mount Perimeter Ring")
        self.assertIn("Mount Perimeter Ring", pulse["name"])
        self.assertIn("mount_perimeter_ring", pulse["description"])
        self.assertIn("retired", pulse["description"])

    def test_ring_description_says_it_composites_over_the_current_face(self):
        desc = self.services["mount_perimeter_ring"]["description"]
        self.assertIn("composites over", desc)
        self.assertIn("never takes over the screen", desc)

    def test_mount_face_example_no_longer_advertises_the_retired_face(self):
        self.assertEqual(
            self.services["mount_face"]["fields"]["face_id"]["example"], "perimeter_ring",
        )


class ManifestBumped(unittest.TestCase):
    def test_version_is_1_18_1_or_later(self):
        from pathlib import Path
        manifest = Path(SERVICES_YAML).with_name("manifest.json")
        version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        self.assertGreaterEqual(tuple(int(p) for p in version.split(".")), (1, 18, 1))
