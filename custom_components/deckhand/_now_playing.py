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
_FEAT_PREVIOUS_TRACK = 16
_FEAT_NEXT_TRACK = 32


def now_playing_capabilities(attr: dict) -> dict:
    """Return the transport capabilities to advertise to the dial.

    Reads ``supported_features`` and returns only the keys that are True
    (``can_next`` / ``can_prev``) so the firmware defaults them off when a
    player can't skip — the dial then shows a plain Now-Playing view with
    no next affordance, which is what makes it a superset of the old media
    face.
    """
    sf = attr.get("supported_features")
    if not isinstance(sf, int):
        return {}
    caps: dict = {}
    if sf & _FEAT_NEXT_TRACK:
        caps["can_next"] = True
    if sf & _FEAT_PREVIOUS_TRACK:
        caps["can_prev"] = True
    return caps


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
