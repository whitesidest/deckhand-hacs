"""The second source tier: a zone's stream, and the inputs that stream offers.

A matrix amp (AmpliPi is the one on the bench) exposes TWO tiers of
``media_player``. The ZONE a dial is bound to has a ``source_list`` of
upstream feeds — literally ``["Source 1", ..., "Source 4"]`` — and its
``source`` says which one it is listening to. Each feed is ITSELF a
media_player whose own ``source_list`` is the list a person actually wants:
internet radio stations, Pandora channels, the physical inputs.

Firmware 0.4.158 put that second tier on swipe-DOWN. Helm and Console both
publish it; HACS did not (helm#613), so a HACS-driven dial got swipe-up and
nothing else — invisible exactly on the dials a HA-centric user is most
likely to own. Worse, a dial driven by HACS *sometimes* and Helm's poller
other times had the picker appear and disappear.

There are three publishers of ``cmd/now_playing`` and they cannot import
each other, so this mirrors Helm's ``test_media_input_tier.py`` and
Console's deliberately.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Load _now_playing.py BY PATH so we don't trip the package __init__, which
# imports homeassistant.* (absent in this bare test env). Same pattern as
# tests/test_now_playing_control.py.
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "deckhand_now_playing",
    ROOT / "custom_components" / "deckhand" / "_now_playing.py",
)
_np = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_np)
now_playing_input_tier = _np.now_playing_input_tier
upstream_source_entity = _np.upstream_source_entity

# The real shapes, read off the amp on the bench (2026-09-29).
ZONE = {
    "friendly_name": "Speakers - Office",
    "source": "Source 1",
    "source_list": ["Source 1", "Source 2", "Source 3", "Source 4"],
    "supported_features": 659212,
}
STREAM = {
    "friendly_name": "Stream 1",
    "source": "Internet Radio Groove Salad",
    "source_list": [
        "None",
        "Master TV",
        "Internet Radio Groove Salad",
        "Pandora Stick Figure",
        "Internet Radio Beach",
        "Internet Radio House",
        "Pandora Appalachian",
        "AmpliPi",
        "Pandora Parra For Cuva",
    ],
    "supported_features": 679692,
}


def _lookup(mapping):
    return lambda eid: mapping.get(eid)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("Source 1", "media_player.source_1"),
        ("Living Room Feed", "media_player.living_room_feed"),
        # "None" is a REAL selectable value on a stream, meaning "disconnect".
        # It must never be chased as an entity id.
        ("None", ""),
        ("unknown", ""),
        ("unavailable", ""),
        ("", ""),
        ("   ", ""),
        (None, ""),
    ],
)
def test_upstream_source_entity(source, expected):
    assert upstream_source_entity({"source": source}) == expected


def test_upstream_source_entity_tolerates_no_attrs():
    assert upstream_source_entity({}) == ""


def test_it_offers_the_streams_inputs_targeted_at_the_stream():
    out = now_playing_input_tier(ZONE, _lookup({"media_player.source_1": STREAM}))
    assert out["can_select_input"] is True
    assert out["input_count"] == 9
    # The target is the STREAM, not the zone the face is bound to — telling a
    # zone to select "Pandora Appalachian" is a call the amp refuses.
    assert out["input_entity_id"] == "media_player.source_1"
    assert "Internet Radio Groove Salad" in out["input_sources"]
    assert "Pandora Stick Figure" in out["input_sources"]


def test_every_real_station_name_survives_the_dials_buffer():
    """A name over 31 UTF-8 bytes is DROPPED, not truncated — truncated it
    could never round-trip through select_source."""
    out = now_playing_input_tier(ZONE, _lookup({"media_player.source_1": STREAM}))
    assert len(out["input_sources"]) == len(STREAM["source_list"])
    for name in out["input_sources"]:
        assert len(name.encode("utf-8")) <= 31


def test_an_ordinary_speaker_gets_no_second_picker():
    out = now_playing_input_tier({"source": "Spotify", "supported_features": 2048}, _lookup({}))
    assert out == {}


def test_a_stream_with_nothing_to_pick_gets_no_flag():
    """The verb alone is not enough — the dial would open an empty list."""
    out = now_playing_input_tier(
        ZONE,
        _lookup({"media_player.source_1": {"source_list": [], "supported_features": 2048}}),
    )
    assert out == {}


def test_a_stream_that_cannot_select_gets_no_flag():
    out = now_playing_input_tier(
        ZONE,
        _lookup({"media_player.source_1": {"source_list": ["Radio"], "supported_features": 0}}),
    )
    assert out == {}


def test_a_failing_state_lookup_costs_the_picker_never_the_push():
    def boom(_eid):
        raise RuntimeError("hass went away")

    assert now_playing_input_tier(ZONE, boom) == {}
    assert now_playing_input_tier(ZONE, _lookup({})) == {}


def test_only_true_keys_so_older_firmware_degrades_cleanly():
    """Absent means no. A payload that carried can_select_input: False would
    force the firmware to distinguish absent from false."""
    out = now_playing_input_tier({"source": "Spotify"}, _lookup({}))
    assert "can_select_input" not in out
