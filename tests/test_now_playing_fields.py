"""Now-Playing field-routing regression tests.

Pins how a media_player state maps to the dial's four fields — "Artist -
Song" up top, "Channel | Speaker" on the bottom marquee. The routing has
to survive two very different data shapes: well-behaved players (clean
media_title + app_name) and whole-home amps (AmpliPi), which leave
app_name empty and cram a channel/station string like "Pandora <Station>
- pandora" into media_title while carrying the real track in album_*.

The pure mapper loads by path so it's exercised without a HA runtime.

Run with:  python3 -m pytest tests/test_now_playing_fields.py
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "deckhand_now_playing",
    ROOT / "custom_components" / "deckhand" / "_now_playing.py",
)
_np = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_np)
now_playing_fields = _np.now_playing_fields


class NowPlayingFieldsTests(unittest.TestCase):
    def test_amplipi_pandora_moves_channel_junk_to_bottom(self):
        # The real office_2 shape: no app_name, messy media_title, real
        # track only in album_*. Channel belongs on the bottom next to the
        # speaker; artist + album read up top.
        f = now_playing_fields(
            {
                "friendly_name": "Speakers - Office",
                "media_title": "Pandora Parra for Cuva - pandora",
                "media_album_artist": "Eli & Fur",
                "media_album_name": "Dreamscapes",
            },
            "media_player.office_2",
        )
        self.assertEqual(f["artist"], "Eli & Fur")
        self.assertEqual(f["title"], "Dreamscapes")
        self.assertEqual(f["source"], "Pandora | Speakers - Office")

    def test_clean_player_keeps_song_and_app(self):
        f = now_playing_fields(
            {
                "friendly_name": "Kitchen",
                "app_name": "Spotify",
                "media_title": "Nightcall",
                "media_artist": "Kavinsky",
            },
            "media_player.kitchen",
        )
        self.assertEqual(f["artist"], "Kavinsky")
        self.assertEqual(f["title"], "Nightcall")
        self.assertEqual(f["source"], "Spotify | Kitchen")

    def test_capitalized_dash_suffix_is_not_mistaken_for_channel(self):
        # "Everything - Live" is a real song; only a lowercase provider tag
        # (AmpliPi's "- pandora") should trigger channel routing.
        f = now_playing_fields(
            {"friendly_name": "Den", "media_title": "Everything - Live",
             "media_artist": "Fleetwood Mac"},
            "media_player.den",
        )
        self.assertEqual(f["title"], "Everything - Live")
        self.assertEqual(f["artist"], "Fleetwood Mac")
        self.assertEqual(f["source"], "Den")  # no channel → speaker only

    def test_provider_suffix_only_fires_without_app_name(self):
        # If a well-behaved player somehow has a lowercase-suffix title but
        # DOES report app_name, trust app_name and keep the title intact.
        f = now_playing_fields(
            {"friendly_name": "Office", "app_name": "TuneIn",
             "media_title": "Some Show - live"},
            "media_player.office",
        )
        self.assertEqual(f["title"], "Some Show - live")
        self.assertEqual(f["source"], "TuneIn | Office")

    def test_redundant_artist_equal_title_is_dropped(self):
        f = now_playing_fields(
            {"friendly_name": "TV", "app_name": "Netflix",
             "media_title": "Stranger Things",
             "media_series_title": "Stranger Things"},
            "media_player.tv",
        )
        self.assertEqual(f["title"], "Stranger Things")
        self.assertEqual(f["artist"], "")  # not "Stranger Things - Stranger Things"

    def test_channel_only_no_speaker_falls_back(self):
        f = now_playing_fields(
            {"app_name": "Radio", "media_title": "Morning Show"},
            "media_player.x",
        )
        self.assertEqual(f["source"], "Radio")  # channel alone, no " | "

    def test_nothing_useful_falls_back_to_entity_id(self):
        f = now_playing_fields({}, "media_player.mystery")
        self.assertEqual(f["source"], "media_player.mystery")
        self.assertEqual(f["title"], "")

    def test_short_content_id_backfills_empty_title(self):
        f = now_playing_fields(
            {"friendly_name": "Hall", "app_name": "AllThe", "media_content_id": "Track42"},
            "media_player.hall",
        )
        self.assertEqual(f["title"], "Track42")

    def test_url_content_id_does_not_backfill_title(self):
        f = now_playing_fields(
            {"friendly_name": "Hall", "app_name": "X",
             "media_content_id": "http://host/stream/very/long/path"},
            "media_player.hall",
        )
        self.assertEqual(f["title"], "")


if __name__ == "__main__":
    unittest.main()
