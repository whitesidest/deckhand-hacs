"""Now-Playing dial control regression tests.

Two things a dial on the Now-Playing / Volume face must do — and did NOT
before this change — are pinned here:

1. **Turning the dial sets volume; tapping play/pause actually toggles
   the speaker.** The firmware publishes a ``button_press`` whose
   ``item_id`` is the HA media_player entity; the integration must turn
   that into a ``media_player.<action>`` service call. Before, the
   raw event was only re-fired onto the HA bus (for user automations)
   and nothing reached the speaker, so the tap flipped the on-dial icon
   but the music kept playing. The decision logic lives in the
   HA-import-free ``_media_control`` module so it's testable here (the
   package ``__init__`` pulls in ``homeassistant.*`` and can't load in
   this bare env — see the sibling source-level guards).

2. **cmd/now_playing carries the current volume.** ``_extract_now_playing``
   must emit a ``volume`` (0-100) from the player's ``volume_level`` so
   the dial seeds its rotary from the real level instead of a guess.

Run with:  python3 -m pytest tests/test_now_playing_control.py
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_PY = ROOT / "custom_components" / "deckhand" / "__init__.py"

# Load _media_control.py by path so we don't trip the package __init__,
# which imports homeassistant.* (absent in this bare test env).
_spec = importlib.util.spec_from_file_location(
    "deckhand_media_control",
    ROOT / "custom_components" / "deckhand" / "_media_control.py",
)
_mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mc)
DIAL_MEDIA_ACTIONS = _mc.DIAL_MEDIA_ACTIONS
media_service_for_dial_event = _mc.media_service_for_dial_event


def _event(type_="button_press", **payload):
    return {"type": type_, "ts": 0, "payload": payload}


# ── Pure decision logic ─────────────────────────────────────────────

class MediaServiceMappingTests(unittest.TestCase):
    def test_play_pause_maps_to_service_on_the_pushed_entity(self):
        result = media_service_for_dial_event(
            _event(item_id="media_player.office_2", action="media_play_pause")
        )
        self.assertEqual(
            result, ("media_play_pause", {"entity_id": "media_player.office_2"})
        )

    def test_next_and_previous_track(self):
        for action in ("media_next_track", "media_previous_track"):
            result = media_service_for_dial_event(
                _event(item_id="media_player.roku_streambar_9102x", action=action)
            )
            self.assertEqual(
                result,
                (action, {"entity_id": "media_player.roku_streambar_9102x"}),
            )

    def test_volume_set_converts_0_100_label_to_0_1_level(self):
        action, data = media_service_for_dial_event(
            _event(
                item_id="media_player.office_2",
                action="volume_set",
                item_label="45",
            )
        )
        self.assertEqual(action, "volume_set")
        self.assertEqual(data["entity_id"], "media_player.office_2")
        self.assertAlmostEqual(data["volume_level"], 0.45)

    def test_volume_clamps_out_of_range_labels(self):
        _, hi = media_service_for_dial_event(
            _event(item_id="media_player.x", action="volume_set", item_label="150")
        )
        _, lo = media_service_for_dial_event(
            _event(item_id="media_player.x", action="volume_set", item_label="-10")
        )
        self.assertEqual(hi["volume_level"], 1.0)
        self.assertEqual(lo["volume_level"], 0.0)

    def test_volume_with_unparseable_label_is_dropped_not_guessed(self):
        self.assertIsNone(
            media_service_for_dial_event(
                _event(item_id="media_player.x", action="volume_set", item_label="")
            )
        )
        self.assertIsNone(
            media_service_for_dial_event(
                _event(item_id="media_player.x", action="volume_set", item_label=None)
            )
        )

    def test_non_media_player_entity_is_ignored(self):
        # A menu button_press (item_id is a menu slug, not an entity) must
        # not be dispatched to media_player — it's not our concern here.
        self.assertIsNone(
            media_service_for_dial_event(
                _event(item_id="scene_movie_night", action="media_play_pause")
            )
        )
        self.assertIsNone(
            media_service_for_dial_event(
                _event(item_id="light.office", action="media_play_pause")
            )
        )

    def test_unknown_action_is_ignored(self):
        self.assertIsNone(
            media_service_for_dial_event(
                _event(item_id="media_player.office_2", action="set_value")
            )
        )

    def test_non_button_press_events_are_ignored(self):
        # nfc_tap, face_revert_request, rotation telemetry, etc.
        for type_ in ("nfc_tap", "face_revert_request", "rotation"):
            self.assertIsNone(
                media_service_for_dial_event(
                    _event(type_, item_id="media_player.x", action="media_play_pause")
                )
            )

    def test_malformed_envelopes_never_raise(self):
        for bad in (None, {}, {"type": "button_press"},
                    {"type": "button_press", "payload": None},
                    {"type": "button_press", "payload": {}}):
            self.assertIsNone(media_service_for_dial_event(bad))

    def test_action_set_matches_firmware_media_actions(self):
        # The firmware emits exactly these actions from the volume /
        # now-playing faces (main.cpp). If a new one is added there,
        # add it here too or the dial control silently no-ops.
        self.assertEqual(
            DIAL_MEDIA_ACTIONS,
            {
                "media_play_pause",
                "media_play",
                "media_pause",
                "media_next_track",
                "media_previous_track",
                "volume_set",
            },
        )


# ── Source-level wiring guards (no HA runtime in this env) ───────────

class WiringGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INIT_PY.read_text(encoding="utf-8")

    def test_handle_event_dispatches_media_control(self):
        # The raw MQTT event handler must call the media dispatcher, so a
        # tap/turn reaches the speaker (not only the HA event bus).
        block = re.search(
            r"def _handle_event\(.*?(?=\n    entry\.async_on_unload)",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(block, "_handle_event not found")
        self.assertIn("_dispatch_dial_media_control(hass, payload)", block.group(0))

    def test_extract_now_playing_emits_volume_from_volume_level(self):
        block = re.search(
            r"def _extract_now_playing\(.*?\n    return payload",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(block, "_extract_now_playing not found")
        body = block.group(0)
        self.assertIn('attr.get("volume_level")', body)
        self.assertIn('payload["volume"] = volume', body)


if __name__ == "__main__":
    unittest.main()
