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
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_COMPONENT = ROOT / "custom_components" / "deckhand"

# _now_playing imports ._units for ASCII folding, so it needs a parent
# package for that relative import to resolve. We can't import the real
# `custom_components.deckhand` package — its __init__ pulls in
# homeassistant.* and there's no HA runtime here, which is the whole
# reason these modules are kept pure. So stand up a synthetic parent whose
# __path__ points at the component directory: the relative import then
# resolves against the real files without executing __init__.
_pkg = types.ModuleType("deckhand_pure")
_pkg.__path__ = [str(_COMPONENT)]
sys.modules.setdefault("deckhand_pure", _pkg)

_spec = importlib.util.spec_from_file_location(
    "deckhand_pure._now_playing",
    _COMPONENT / "_now_playing.py",
)
_np = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _np
_spec.loader.exec_module(_np)
now_playing_fields = _np.now_playing_fields
now_playing_capabilities = _np.now_playing_capabilities
now_playing_sources = _np.now_playing_sources
now_playing_controls = _np.now_playing_controls
now_playing_is_cold = _np.now_playing_is_cold


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


PAUSE, VOLUME_SET, PREVIOUS_TRACK = 1, 4, 16
NEXT_TRACK, SELECT_SOURCE, PLAY = 32, 2048, 16384


class CapabilitiesTests(unittest.TestCase):
    """These used to assert only can_next / can_prev.

    That narrowness was the bug: Helm pushed no flags at all while HACS
    pushed two, so the same dial drew a different button set depending on
    which system published last. Both now derive the same six keys from the
    same Home Assistant bits, so the tests widened with them.
    """

    def test_both_skip_supported(self):
        self.assertEqual(
            now_playing_capabilities({"supported_features": PREVIOUS_TRACK | NEXT_TRACK | VOLUME_SET}),
            {"can_next": True, "can_prev": True, "can_volume": True},
        )

    def test_office_2_real_value(self):
        # Real AmpliPi office_2 reported 675629. Decoded, that is PAUSE,
        # VOLUME_SET, VOLUME_MUTE, NEXT_TRACK, TURN_OFF, PLAY_MEDIA,
        # VOLUME_STEP, SELECT_SOURCE, PLAY, BROWSE_MEDIA and GROUPING —
        # PREVIOUS_TRACK is genuinely clear, so the dial offers next but
        # not previous. Kept as a literal because it is a real observed
        # value; the decode is asserted in test_the_real_value_decodes_as_claimed.
        self.assertEqual(
            now_playing_capabilities({"supported_features": 675629}),
            {
                "can_pause": True,
                "can_play": True,
                "can_play_pause": True,
                "can_volume": True,
                "can_next": True,
                "can_select_source": True,
            },
        )

    def test_the_real_value_decodes_as_claimed(self):
        """Guards the comment above. A literal nobody can verify is how a
        fabricated 'realistic' mask slips in and pins the wrong behaviour."""
        self.assertTrue(675629 & NEXT_TRACK)
        self.assertFalse(675629 & PREVIOUS_TRACK)
        self.assertTrue(675629 & SELECT_SOURCE)

    def test_only_true_keys_returned(self):
        # A player with NEXT but not PREVIOUS (32, no 16).
        self.assertEqual(
            now_playing_capabilities({"supported_features": NEXT_TRACK | VOLUME_SET}),
            {"can_next": True, "can_volume": True},
        )

    def test_volume_only_player_advertises_volume_and_nothing_else(self):
        # Previously this asserted {} — volume wasn't derived at all, so a
        # player that could only set volume told the dial nothing.
        self.assertEqual(
            now_playing_capabilities({"supported_features": VOLUME_SET}),
            {"can_volume": True},
        )

    def test_either_half_of_play_pause_gives_one_centre_press(self):
        # The dial has a single centre control; HA's media_play_pause
        # resolves direction from state, so either bit is enough.
        for bit in (PLAY, PAUSE):
            with self.subTest(bit=bit):
                self.assertIs(now_playing_capabilities({"supported_features": bit})["can_play_pause"], True)

    def test_missing_or_bad_supported_features(self):
        self.assertEqual(now_playing_capabilities({}), {})
        self.assertEqual(now_playing_capabilities({"supported_features": None}), {})
        self.assertEqual(now_playing_capabilities({"supported_features": "x"}), {})

    def test_a_bool_is_not_a_bitmask(self):
        """bool subclasses int and True & PAUSE == 1, so an integration
        returning a bool here would silently draw a pause button."""
        self.assertEqual(now_playing_capabilities({"supported_features": True}), {})
        self.assertEqual(now_playing_capabilities({"supported_features": False}), {})


class SourcePickerTests(unittest.TestCase):
    """SELECT_SOURCE being set is not enough to draw a picker."""

    def test_flag_and_list_together_produce_a_picker(self):
        out = now_playing_controls(
            {"supported_features": SELECT_SOURCE, "source_list": ["Aux", "Turntable"]}
        )
        self.assertEqual(out["sources"], ["Aux", "Turntable"])
        self.assertEqual(out["source_count"], 2)
        self.assertIs(out["can_select_source"], True)

    def test_flag_without_a_list_drops_the_flag(self):
        # An empty picker is exactly the dead affordance we're removing.
        out = now_playing_controls({"supported_features": SELECT_SOURCE, "source_list": []})
        self.assertNotIn("can_select_source", out)
        self.assertNotIn("sources", out)

    def test_list_without_the_flag_offers_nothing(self):
        out = now_playing_controls({"supported_features": 0, "source_list": ["Aux"]})
        self.assertNotIn("can_select_source", out)
        self.assertNotIn("sources", out)

    def test_source_names_are_folded_not_dropped(self):
        # "Küche" must not arrive as "Kche" — that reads as a typo and gets
        # filed against the wrong component.
        out = now_playing_controls(
            {"supported_features": SELECT_SOURCE, "source_list": ["Küche", "Café", "Ærø"]}
        )
        self.assertEqual(out["sources"], ["Kuche", "Cafe", "AEro"])

    def test_unrenderable_names_fall_out_and_can_take_the_picker_with_them(self):
        out = now_playing_controls(
            {"supported_features": SELECT_SOURCE, "source_list": ["日本語", "中文"]}
        )
        self.assertNotIn("can_select_source", out)

    def test_list_is_bounded(self):
        many = [f"Input {i}" for i in range(40)]
        out = now_playing_controls({"supported_features": SELECT_SOURCE, "source_list": many})
        self.assertEqual(len(out["sources"]), 12)

    def test_junk_entries_are_skipped(self):
        out = now_playing_sources({"source_list": ["Aux", None, 7, "", "Phono"]})
        self.assertEqual(out, ["Aux", "Phono"])

    def test_bad_source_list_shape(self):
        self.assertEqual(now_playing_sources({}), [])
        self.assertEqual(now_playing_sources({"source_list": "Aux"}), [])


class ColdPlayerTests(unittest.TestCase):
    """The "Turn On" affordance for a player with nothing loaded (helm#319).

    Must agree key-for-key with Helm's ``media_face_payload``: both systems
    push cmd/now_playing, so a disagreement means the dial's button set
    changes depending on who published last — the asymmetry the whole
    capability pairing exists to remove.
    """

    PLAYING = {"supported_features": PLAY | PAUSE, "media_title": "Blue in Green"}
    COLD = {"supported_features": PLAY | PAUSE}

    def test_a_cold_player_offers_turn_on(self):
        self.assertTrue(now_playing_controls(self.COLD)["can_turn_on"])

    def test_a_loaded_track_does_not(self):
        """Paused mid-track is NOT cold — its play button works, and a
        second competing "start" control beside it would be noise."""
        self.assertNotIn("can_turn_on", now_playing_controls(self.PLAYING))

    def test_an_off_speaker_still_advertises_play_pause(self):
        """Why title, not supported_features, decides this. An off zone
        keeps PLAY and PAUSE set, so the bits cannot tell "paused on a
        track" from "powered down"."""
        out = now_playing_controls(self.COLD)
        self.assertTrue(out["can_play_pause"], "premise: the bits look identical")
        self.assertTrue(out["can_turn_on"], "so title is what separates them")

    def test_an_empty_string_title_counts_as_cold(self):
        self.assertTrue(now_playing_is_cold({"media_title": ""}))
        self.assertTrue(now_playing_is_cold({}))
        self.assertFalse(now_playing_is_cold({"media_title": "So What"}))

    def test_it_reads_the_raw_title_not_the_resolved_one(self):
        """now_playing_fields can synthesize a display title from a short
        media_content_id (test_short_content_id_backfills_empty_title). If
        coldness read that resolved value, HACS and Helm would answer
        differently for the same entity. Display text and "is anything
        loaded" are different questions, and only the raw field is shared.
        """
        attr = {"media_content_id": "track42", "supported_features": PLAY}
        self.assertTrue(now_playing_is_cold(attr))
        self.assertTrue(now_playing_controls(attr)["can_turn_on"])

    def test_a_cold_player_can_still_offer_its_inputs(self):
        """The founder's decision on helm#319 was BOTH — the wake action and
        the source picker, not one or the other."""
        out = now_playing_controls(
            {"supported_features": SELECT_SOURCE, "source_list": ["Aux", "Phono"]}
        )
        self.assertTrue(out["can_turn_on"])
        self.assertEqual(out["sources"], ["Aux", "Phono"])


if __name__ == "__main__":
    unittest.main()
