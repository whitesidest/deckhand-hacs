"""Pure field-routing for the dial's Now-Playing face.

Splits a media_player state's attributes into the four fields the dial
renders — ``title`` (song) and ``artist`` on the top marquee, ``source``
("Channel | Speaker") on the bottom. Kept free of Home Assistant imports
so it's unit-testable without a HA runtime (the package ``__init__`` pulls
in ``homeassistant.*``). ``_extract_now_playing`` calls this, then layers
on album art, volume, and is_playing.
"""

from __future__ import annotations

import re

from ._units import safe_text_for_dial

# AmpliPi and other whole-home amps append a lowercase provider tag to
# media_title ("Pandora <Station> - pandora") instead of exposing a clean
# song + app_name. This matches that trailing " - <provider>" tag; the
# lowercase requirement keeps a real capitalized song like "Everything -
# Live" from being mistaken for a channel string.
_PROVIDER_SUFFIX = re.compile(r"^(?P<body>.+?)\s*-\s*(?P<prov>[a-z][a-z0-9]{1,20})$")

# HA MediaPlayerEntityFeature bits — the transport verbs the dial can
# conditionally expose on the Now-Playing face (so it subsumes the old
# dedicated media-control face). Only advertise a control the entity
# actually supports.
#
# Must stay identical to helm/apps/integrations/media_capabilities.py.
# Both systems push cmd/now_playing, so if the two derivations disagree a
# dial's button set changes depending on who pushed last — which is the
# bug this pairing exists to remove (HACS used to send only can_next /
# can_prev while Helm sent nothing at all). Neither repo can import the
# other, so both are anchored to Home Assistant's published values.
_FEAT_PAUSE = 1
_FEAT_VOLUME_SET = 4
_FEAT_PREVIOUS_TRACK = 16
_FEAT_NEXT_TRACK = 32
_FEAT_SELECT_SOURCE = 2048
_FEAT_PLAY = 16384

# Wire key -> the bit that has to be set for the dial to draw it.
_CAPABILITY_BITS: dict[str, int] = {
    "can_pause": _FEAT_PAUSE,
    "can_play": _FEAT_PLAY,
    "can_volume": _FEAT_VOLUME_SET,
    "can_prev": _FEAT_PREVIOUS_TRACK,
    "can_next": _FEAT_NEXT_TRACK,
    "can_select_source": _FEAT_SELECT_SOURCE,
}

# The centre press is one control on the dial, so it needs one flag. A
# player that implements either half can be toggled: HA's media_play_pause
# service resolves the direction from current state.
_PLAY_PAUSE_BITS = _FEAT_PLAY | _FEAT_PAUSE

# A long source list does not fit a 360px round screen and costs PSRAM in
# the cached menu item. Twelve is more than any real amp exposes as useful
# inputs; beyond that the picker stops being a calm affordance anyway.
_MAX_SOURCES = 12
_MAX_SOURCE_LEN = 24


def now_playing_capabilities(attr: dict) -> dict:
    """Return the transport capabilities to advertise to the dial.

    Reads ``supported_features`` and returns **only the keys that are
    True**, so the firmware defaults every control off. A player that
    can't skip yields no next affordance and the dial shows a plain
    Now-Playing view, which is what makes it a superset of the old media
    face. Absent has to mean "no": if this emitted ``can_next: False`` the
    firmware would need to distinguish absent from false, and every older
    publisher would be ambiguous.
    """
    sf = attr.get("supported_features")
    if not isinstance(sf, int):
        return {}
    caps = {key: True for key, bit in _CAPABILITY_BITS.items() if sf & bit}
    if sf & _PLAY_PAUSE_BITS:
        caps["can_play_pause"] = True
    return caps


def now_playing_sources(attr: dict) -> list[str]:
    """Selectable inputs for the source picker, ASCII-safe and bounded.

    Separate from the capability flags because ``SELECT_SOURCE`` being set
    is not sufficient: an entity can advertise the verb and expose an empty
    ``source_list``, and a picker with nothing in it is exactly the dead
    affordance this module exists to prevent.

    Names are ASCII-folded at the publisher because the dial fonts cannot
    render anything else — an amp with a source called "Küche" would
    otherwise reach the glass as tofu. Folding (not dropping) matters here:
    ``safe_unit_for_dial`` would turn it into "Kche", which reads as a typo.
    """
    raw = attr.get("source_list")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        name = safe_text_for_dial(entry).strip()[:_MAX_SOURCE_LEN]
        if name:
            out.append(name)
        if len(out) >= _MAX_SOURCES:
            break
    return out


def now_playing_controls(attr: dict) -> dict:
    """Capability half of a ``cmd/now_playing`` push: flags plus sources.

    Mirrors ``media_face_payload`` in Helm. Callers should ``update()`` a
    payload with this rather than calling the two halves separately, so the
    source-picker gate below can't be skipped by accident.
    """
    payload = now_playing_capabilities(attr)
    sources = now_playing_sources(attr)
    if sources and payload.get("can_select_source"):
        payload["sources"] = sources
        payload["source_count"] = len(sources)
    else:
        # Advertising the verb without anything to pick would draw an empty
        # picker. Drop the flag so the dial cannot offer it.
        payload.pop("can_select_source", None)
    return payload


def now_playing_fields(attr: dict, entity_id: str) -> dict:
    """Return ``{"title", "artist", "source"}`` for a media_player state.

    ``attr`` is the entity's attribute dict. Well-behaved players expose a
    clean ``media_title`` (song) + ``app_name`` (channel); whole-home amps
    put a channel/station string in ``media_title``, leave ``app_name``
    empty, and carry the real track in the ``album_*`` fields — handled by
    the provider-suffix detection below.
    """
    friendly = attr.get("friendly_name") or ""
    app = attr.get("app_name") or ""
    raw_title = (attr.get("media_title") or "").strip()

    # Artist: album_artist covers compilations / whole-home audio; series
    # title makes "The Bear - S2E3" read right for video.
    artist = (
        attr.get("media_artist")
        or attr.get("media_album_artist")
        or attr.get("media_series_title")
        or ""
    )

    channel = app
    title = raw_title
    m = _PROVIDER_SUFFIX.match(raw_title)
    if raw_title and not app and m:
        # media_title is a channel/station string, not a song.
        channel = m.group("prov").capitalize()  # "pandora" -> "Pandora"
        title = attr.get("media_album_name") or m.group("body").strip()

    if not title:
        # content_id is often a URL/path; only use it if it's short.
        cid = attr.get("media_content_id") or ""
        if cid and len(cid) < 96 and "/" not in cid:
            title = cid

    # Avoid a redundant "X - X" top line (e.g. a series whose title equals
    # its series title) — show it once.
    if artist and artist == title:
        artist = ""

    # Bottom marquee: "Channel | Speaker" (either half may be empty). The
    # firmware uppercases + marquees this, so a long combo scrolls rather
    # than clipping.
    if channel and friendly:
        source = f"{channel} | {friendly}"
    else:
        source = channel or friendly or entity_id

    return {"title": title, "artist": artist, "source": source}
