"""What a dial event means to an automation, and what a device action sends.

Pure (no Home Assistant imports) so the mapping can be tested on its own and
shared by ``device_trigger.py``, ``device_action.py`` and the event entities
in ``event.py``.

Two shapes of ``deckhand_dial_event`` reach the bus:

* **Raw** — this integration re-fires every dial MQTT event as
  ``{"dial_id", "type", "payload", "ts"}`` (``__init__._handle_event``). The
  wire names are the firmware's: ``button_press``, ``long_press``,
  ``double_tap``, ``menu_opened`` / ``menu_closed`` / ``menu_timeout``,
  ``timer_start`` / ``timer_complete`` / ``timer_cancel``, ``dnd_changed``
  (``payload.action`` = ``on`` / ``off``) and ``invitation_response``
  (``payload.response`` = ``accepted`` / ``declined`` / ``expired`` /
  ``cancelled``).
* **Server relay** — Helm and Deckhand Console relay the same moments flat,
  ``{"dial_id", "event_type", "item_id", "item_label", "action", ...}``,
  when the server has a Home Assistant integration configured. Only the
  relay knows WHO tapped an NFC card (``action`` = ``known`` / ``unknown`` /
  ``revoked``); the raw ``nfc_tap`` carries a hash and nothing else.

Every trigger listens to exactly one of the two, so a moment that arrives
both ways fires an automation once: NFC identity from the relay, everything
else from the raw re-fire, which exists whether or not the server talks to
Home Assistant.

There is no rotation trigger. The firmware publishes no event for turning
the knob; the only rotation that leaves the dial is the TV-remote face's
volume, sent per detent as a ``button_press`` with ``action: volume_set``,
which is left out of "button pressed" so it does not fire on every click.
"""
from __future__ import annotations

from typing import Any

from .const import FALLBACK_THEMES, SERVER_RESOLVED_THEMES

# ``button_press`` actions that are not a person pressing something: a
# sensor / now-playing face asking for a fresh value, a Wi-Fi QR fetching
# its code (_press_guard.QR_REQUEST_ACTION), and the TV remote's volume,
# sent once per detent while the knob turns.
INTERNAL_PRESS_ACTIONS = frozenset({"state_request", "qr_request", "volume_set"})

TRIGGER_BUTTON = "button_pressed"
TRIGGER_LONG_PRESS = "long_pressed"
TRIGGER_DOUBLE_TAP = "double_tapped"
TRIGGER_MENU_ITEM = "menu_item_selected"
TRIGGER_NFC = "nfc_tap"
TRIGGER_INVITATION = "invitation_response"
TRIGGER_TIMER = "timer"
TRIGGER_DND = "dnd"

# trigger type -> its subtypes (empty tuple = the type takes no subtype).
TRIGGER_TYPES: dict[str, tuple[str, ...]] = {
    TRIGGER_BUTTON: (),
    TRIGGER_LONG_PRESS: (),
    TRIGGER_DOUBLE_TAP: (),
    TRIGGER_MENU_ITEM: (),
    TRIGGER_NFC: ("known", "unknown", "revoked"),
    TRIGGER_INVITATION: ("accepted", "declined", "expired", "cancelled"),
    TRIGGER_TIMER: ("started", "completed", "cancelled"),
    TRIGGER_DND: ("turned_on", "turned_off"),
}
# Triggers that take an optional item label to match (exact, ignoring case
# and surrounding spaces).
LABEL_TRIGGERS = frozenset({TRIGGER_BUTTON, TRIGGER_MENU_ITEM})
CONF_ITEM_LABEL = "item_label"

_TIMER_SUBTYPES = {
    "timer_start": "started",
    "timer_complete": "completed",
    "timer_cancel": "cancelled",
}
_DND_SUBTYPES = {"on": "turned_on", "off": "turned_off"}
_MENU_EVENTS = {"menu_opened": "opened", "menu_closed": "closed", "menu_timeout": "timed_out"}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def is_raw(data: dict[str, Any]) -> bool:
    """The integration's own re-fire (``type`` + nested ``payload``)."""
    return "type" in data and "event_type" not in data


def _payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else {}


def classify(data: Any) -> list[tuple[str, str | None]]:
    """Every (trigger type, subtype) one ``deckhand_dial_event`` stands for.

    A plain menu item's press is both a button press and a menu item
    selection, so it returns two pairs; anything an automation can't use
    (plumbing, the other path's copy of a moment) returns none.
    """
    if not isinstance(data, dict):
        return []
    if not is_raw(data):
        if data.get("event_type") == "nfc_tap":
            status = _text(data.get("action"))
            if status in TRIGGER_TYPES[TRIGGER_NFC]:
                return [(TRIGGER_NFC, status)]
        return []

    wire = data.get("type")
    payload = _payload(data)
    if wire == "button_press":
        action = _text(payload.get("action"))
        if action in INTERNAL_PRESS_ACTIONS:
            return []
        out: list[tuple[str, str | None]] = [(TRIGGER_BUTTON, None)]
        if not action:
            # A plain menu item: no built-in action, the press IS the event
            # (what deckhand.add_menu_item adds).
            out.append((TRIGGER_MENU_ITEM, None))
        return out
    if wire == "long_press":
        return [(TRIGGER_LONG_PRESS, None)]
    if wire == "double_tap":
        return [(TRIGGER_DOUBLE_TAP, None)]
    if wire in _TIMER_SUBTYPES:
        return [(TRIGGER_TIMER, _TIMER_SUBTYPES[wire])]
    if wire == "dnd_changed":
        sub = _DND_SUBTYPES.get(_text(payload.get("action")))
        return [(TRIGGER_DND, sub)] if sub else []
    if wire == "invitation_response":
        response = _text(payload.get("response"))
        if response in TRIGGER_TYPES[TRIGGER_INVITATION]:
            return [(TRIGGER_INVITATION, response)]
    return []


def item_label(data: dict[str, Any]) -> str:
    """The pressed item's label, from either shape."""
    if is_raw(data):
        return _text(_payload(data).get("item_label"))
    return _text(data.get("item_label"))


def _same_label(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def trigger_matches(
    trigger_type: str,
    subtype: str | None,
    data: Any,
    label: str | None = None,
) -> bool:
    """Does this bus event fire a device trigger configured this way?"""
    if (trigger_type, subtype or None) not in classify(data):
        return False
    if label and trigger_type in LABEL_TRIGGERS:
        return _same_label(item_label(data), label)
    return True


def validate_trigger(trigger_type: Any, subtype: Any) -> str | None:
    """An error sentence for a bad type/subtype pair, else None."""
    if trigger_type not in TRIGGER_TYPES:
        return f"Unknown Deckhand trigger type {trigger_type!r}."
    subtypes = TRIGGER_TYPES[trigger_type]
    if subtypes and subtype not in subtypes:
        return f"Trigger {trigger_type!r} needs a subtype: one of {', '.join(subtypes)}."
    if not subtypes and subtype:
        return f"Trigger {trigger_type!r} takes no subtype."
    return None


def trigger_variables(data: dict[str, Any]) -> dict[str, Any]:
    """Convenience values for ``trigger.*`` in the automation's actions."""
    if is_raw(data):
        payload = _payload(data)
        out = {
            "item_id": _text(payload.get("item_id")),
            "item_label": _text(payload.get("item_label")),
            "action": _text(payload.get("action")),
        }
        if data.get("type") == "invitation_response":
            out["invitation_id"] = _text(payload.get("invitation_id"))
            out["response"] = _text(payload.get("response"))
        if isinstance(payload.get("minutes"), (int, float)) and not isinstance(payload.get("minutes"), bool):
            out["minutes"] = int(payload["minutes"])
        return out
    return {
        "item_id": _text(data.get("item_id")),
        "item_label": _text(data.get("item_label")),
        "action": _text(data.get("action")),
        "role": _text(data.get("role")),
        "credential_id": _text(data.get("credential_id")),
    }


# ── Event entities ────────────────────────────────────────────────────
# entity key -> (trigger type, subtype) -> entity event type.
ENTITY_EVENT_TYPES: dict[str, dict[tuple[str, str | None], str]] = {
    "button": {
        (TRIGGER_BUTTON, None): "press",
        (TRIGGER_LONG_PRESS, None): "long_press",
        (TRIGGER_DOUBLE_TAP, None): "double_tap",
    },
    "menu": {
        (TRIGGER_MENU_ITEM, None): "item_selected",
        ("menu", "opened"): "opened",
        ("menu", "closed"): "closed",
        ("menu", "timed_out"): "timed_out",
    },
    "nfc": {(TRIGGER_NFC, s): s for s in TRIGGER_TYPES[TRIGGER_NFC]},
    "invitation": {(TRIGGER_INVITATION, s): s for s in TRIGGER_TYPES[TRIGGER_INVITATION]},
}


def entity_event(key: str, data: Any) -> tuple[str, dict[str, Any]] | None:
    """(event type, attributes) for the ``key`` event entity, or None."""
    if not isinstance(data, dict):
        return None
    pairs = classify(data)
    if key == "menu" and is_raw(data) and data.get("type") in _MENU_EVENTS:
        pairs = [("menu", _MENU_EVENTS[data["type"]])]
    mapping = ENTITY_EVENT_TYPES.get(key) or {}
    for pair in pairs:
        if pair in mapping:
            return mapping[pair], _entity_attributes(key, data)
    return None


def _entity_attributes(key: str, data: dict[str, Any]) -> dict[str, Any]:
    v = trigger_variables(data)
    if key == "button":
        return {"item_id": v["item_id"], "item_label": v["item_label"], "action": v["action"]}
    if key == "menu":
        return {"item_id": v["item_id"], "item_label": v["item_label"]}
    if key == "nfc":
        known = v["action"] in ("known", "revoked")
        return {
            "identity": v["item_label"] if known else "",
            "role": v["role"],
            "credential_id": v["credential_id"],
        }
    if key == "invitation":
        held = _payload(data).get("held_ms")
        out = {"invitation_id": v.get("invitation_id", "")}
        if isinstance(held, (int, float)) and not isinstance(held, bool):
            out["held_ms"] = int(held)
        return out
    return {}


# ── Device actions ────────────────────────────────────────────────────
ACTION_ANNOUNCE = "send_announcement"
ACTION_TIMER = "start_timer"
ACTION_THEME = "push_theme"
ACTION_DND_ON = "dnd_on"
ACTION_DND_OFF = "dnd_off"
ACTION_MESSAGE = "show_message"
ACTION_LIBRARY_FACE = "mount_library_face"

# action type -> (service, {field: required}) — every field is passed to
# the service under the same name, except ``text`` (show_message), which
# becomes the message face's payload.
ACTION_FIELDS: dict[str, tuple[str, dict[str, bool]]] = {
    ACTION_ANNOUNCE: ("send_announcement", {
        "message": True, "from_name": False, "duration": False,
        "snapshot_entity": False, "image_url": False,
    }),
    ACTION_TIMER: ("start_timer", {
        "minutes": True, "label": False, "name": False, "color": False,
    }),
    ACTION_THEME: ("push_theme", {"theme": True}),
    ACTION_DND_ON: ("set_dnd", {}),
    ACTION_DND_OFF: ("set_dnd", {}),
    ACTION_MESSAGE: ("mount_face", {"text": True}),
    ACTION_LIBRARY_FACE: ("mount_library_face", {"face": True}),
}
ACTION_TYPES = tuple(ACTION_FIELDS)
# Every key an action config may carry besides device_id/domain/type.
ACTION_KEYS = frozenset(k for _svc, fields in ACTION_FIELDS.values() for k in fields)


def build_service_call(action_type: str, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(service, data) for one device action, ``device_id`` included."""
    if action_type not in ACTION_FIELDS:
        raise ValueError(f"Unknown Deckhand action {action_type!r}")
    service, fields = ACTION_FIELDS[action_type]
    data: dict[str, Any] = {"device_id": [config["device_id"]]}
    for name, required in fields.items():
        value = config.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise ValueError(f"{name} is required")
            continue
        data[name] = value
    if action_type == ACTION_DND_ON:
        data["on"] = True
    elif action_type == ACTION_DND_OFF:
        data["on"] = False
    elif action_type == ACTION_MESSAGE:
        data = {
            "device_id": data["device_id"],
            "face_id": "message",
            "payload": {"text": data.pop("text")},
        }
    return service, data


def theme_options(store: dict[str, Any] | None) -> list[str]:
    """The Theme picker's options for one entry: the team's catalog from
    ``themes/list`` (FALLBACK_THEMES until it arrives), ``random`` first."""
    themes = (store or {}).get("themes") or []
    slugs = [t["slug"] for t in themes if isinstance(t, dict) and t.get("slug")]
    base = slugs or list(FALLBACK_THEMES)
    return [t for t in SERVER_RESOLVED_THEMES if t not in base] + base


def library_face_names(store: dict[str, Any] | None) -> list[str]:
    """Names of the enabled faces in the entry's synced library, sorted,
    each once (``mount_library_face`` takes an exact name or a uid)."""
    index = ((store or {}).get("faces") or {}).get("index") or {}
    names: list[str] = []
    for face in index.get("faces") or []:
        if not isinstance(face, dict) or face.get("enabled") is False:
            continue
        name = face.get("name")
        if isinstance(name, str) and name.strip() and name not in names:
            names.append(name)
    return sorted(names, key=str.casefold)
