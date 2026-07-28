"""Pure translation of dial events into media_player service calls.

Deliberately free of Home Assistant imports so the decision logic is
unit-testable without a HA runtime — the rest of the integration pulls in
``homeassistant.*`` at import time and can't load in a bare test env.
``__init__.py`` wraps the returned (service, data) in the actual
``media_player`` service call; everything that decides *whether* and
*what* to call lives here.
"""

from __future__ import annotations

# media_player services a dial may drive from the Now-Playing / Volume
# face. A dial button_press whose action is in this set (and whose
# item_id is a media_player entity) maps straight onto media_player.<action>.
DIAL_MEDIA_ACTIONS = frozenset(
    {
        "media_play_pause",
        "media_play",
        "media_pause",
        "media_next_track",
        "media_previous_track",
        "volume_set",
    }
)


def media_service_for_dial_event(envelope: dict) -> tuple[str, dict] | None:
    """Map a raw dial event to a ``(service, service_data)`` pair.

    ``envelope`` is the dial event ``{type, ts, payload}`` published by
    the firmware. Only a ``button_press`` whose inner ``item_id`` is a
    ``media_player.*`` entity and whose ``action`` is in
    :data:`DIAL_MEDIA_ACTIONS` yields a call; everything else returns
    ``None`` (no-op). ``volume_set`` carries its target level in
    ``item_label`` as a ``"0".."100"`` string, converted to a clamped
    ``volume_level`` in ``0.0..1.0``; an unparseable level is dropped
    rather than guessed.
    """
    if not isinstance(envelope, dict) or envelope.get("type") != "button_press":
        return None

    inner = envelope.get("payload") or {}
    target = inner.get("item_id")
    action = inner.get("action")
    if (
        not isinstance(target, str)
        or not target.startswith("media_player.")
        or action not in DIAL_MEDIA_ACTIONS
    ):
        return None

    data: dict = {"entity_id": target}
    if action == "volume_set":
        try:
            level = float(inner.get("item_label"))
        except (TypeError, ValueError):
            return None
        data["volume_level"] = max(0.0, min(1.0, level / 100.0))

    return action, data
