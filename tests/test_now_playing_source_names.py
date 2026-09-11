"""Media source names reach the dial exactly as Home Assistant spells them (helm#410).

The dial's source picker is a round trip: HACS publishes the media_player's
``source_list`` as ``sources`` on ``cmd/now_playing``, the dial copies each
name into a 32-byte buffer, and when one is picked it publishes that copy
back as the source to select. Helm or Console hands it to
``media_player.select_source``, which is an exact match. So what HACS puts on
the wire has to be HA's own name, byte for byte.

It used to be ASCII-folded: "Télé" went out as "Tele" (the pick did nothing)
and "Радио" / "テレビ" folded to nothing and never reached the picker.

Runs the REAL ``update_from_media_player`` handler through the harness in
test_emoji_strip_every_service.py, so the emoji strip ``_publish_now_playing``
applies before MQTT is in the path too.
"""

from __future__ import annotations

from test_emoji_strip_every_service import TARGETS, _ServiceTest, _state

SELECT_SOURCE = 2048
NAMES = ["Télé", "Café Lounge", "Радио", "テレビ", "🎧 Spotify", "HDMI 1"]


class SourceNamesOnTheWire(_ServiceTest):
    def _publish_all(self, source_list):
        self.hass.states._states["media_player.lounge"] = _state(
            "playing", media_title="Song 🎵", friendly_name="Lounge",
            source_list=source_list, supported_features=SELECT_SOURCE,
        )
        sent = [p for t, p in self.call("update_from_media_player",
                                        entity_id="media_player.lounge")
                if t.endswith("/cmd/now_playing")]
        self.assertEqual(len(sent), len(TARGETS), f"expected one push per dial, got {sent}")
        return sent

    def _publish(self, source_list):
        return self._publish_all(source_list)[0]

    def test_every_source_name_goes_out_byte_exact(self):
        p = self._publish(NAMES)
        self.assertEqual(p["sources"], NAMES)
        self.assertEqual(p["source_count"], len(NAMES))
        self.assertIs(p["can_select_source"], True)
        # Positive control: the display text on the same payload IS stripped.
        self.assertEqual(p["title"], "Song")

    def test_every_dial_gets_the_same_exact_list(self):
        for p in self._publish_all(NAMES):
            self.assertEqual(p["sources"], NAMES)

    def test_a_name_the_dial_cannot_hold_is_left_out_not_cut(self):
        too_long = "Living Room Speakers (Kitchen Zone)"  # 35 bytes
        p = self._publish(["Aux", too_long, "テ" * 11, "Phono"])
        self.assertEqual(p["sources"], ["Aux", "Phono"])
