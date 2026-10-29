"""Event platform: per dial, Timer, Button, Menu, Invitation and NFC.

Each is an ``event`` entity under the dial device whose event types the
automation editor offers directly, so nobody has to know the
``deckhand_dial_event`` wire names. The entities only listen; the raw bus
event stays exactly as it was (see tests/test_nfc_event_paths.py for why),
and which bus events map to which event type is ``_automation.py``.

Timer (1.17.0): the dial already publishes ``timer_start`` /
``timer_complete`` / ``timer_cancel`` on its event topic, carried with the
label and length of the timer as attributes. ``cancelled`` (1.18.0) fires
whether the guest cancelled on the glass or an automation called
``deckhand.cancel_timer``: the dial reports what happened to its timer, not
who asked. An automation that armed something on ``started`` (lights, a hood
fan) needs it to stand down on cancel just as it does on complete.

Button, Menu, Invitation (1.26.0) come from the integration's raw re-fire.
NFC (1.26.0) comes from the Helm / Console relay, the only path that knows
whose card it was, and is created on a dial's first relayed tap: most dials
have no reader, and an NFC entity on them would be noise.

None of them restores a past event across a restart or flaps with the
dial's heartbeat (see ``_DialEventEntity``).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _automation as auto
from .const import DOMAIN, EVENT_DIAL_EVENT
from .entity import DeckhandEntity

_LOGGER = logging.getLogger(__name__)

# Wire event type (firmware) → entity event type (what an automation picks).
TIMER_EVENT_TYPES = {
    "timer_start": "started",
    "timer_complete": "completed",
    "timer_cancel": "cancelled",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Timer, Button, Menu and Invitation per discovered dial; NFC per dial
    that has reported a tap (now, or before a restart)."""
    known_dials: set[str] = set()
    nfc_dials: set[str] = set()
    registry = er.async_get(hass)

    @callback
    def _add_nfc(dial_id: str, data: dict[str, Any], first: dict | None = None) -> None:
        if dial_id in nfc_dials:
            return
        nfc_dials.add(dial_id)
        async_add_entities([DeckhandNfcEvent(dial_id, data, first)])
        _LOGGER.debug("Added NFC event entity for %s", dial_id)

    @callback
    def _async_discover_dial(dial_id: str, data: dict[str, Any]) -> None:
        if dial_id in known_dials:
            return
        known_dials.add(dial_id)
        async_add_entities([
            DeckhandTimerEvent(dial_id, data),
            DeckhandButtonEvent(dial_id, data),
            DeckhandMenuEvent(dial_id, data),
            DeckhandInvitationEvent(dial_id, data),
        ])
        _LOGGER.debug("Added event entities for %s", dial_id)
        if registry.async_get_entity_id("event", DOMAIN, nfc_unique_id(dial_id)):
            _add_nfc(dial_id, data)

    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{DOMAIN}_dial_discovered", _async_discover_dial)
    )

    @callback
    def _first_tap(event: Event) -> None:
        """Create a dial's NFC entity on its first relayed tap, and hand it
        that tap so the first one is not lost."""
        data = event.data or {}
        dial_id = data.get("dial_id")
        if not isinstance(dial_id, str) or dial_id in nfc_dials:
            return
        if auto.entity_event("nfc", data) is None:
            return
        store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        dial = (store.get("dials") or {}).get(dial_id)
        if dial is None:
            return  # not one of this entry's dials
        _add_nfc(dial_id, dial, data)

    entry.async_on_unload(hass.bus.async_listen(EVENT_DIAL_EVENT, _first_tap))

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    for dial_id, data in store.get("dials", {}).items():
        _async_discover_dial(dial_id, data)


def nfc_unique_id(dial_id: str) -> str:
    """Kept equal to device_trigger.nfc_entity_unique_id."""
    return f"deckhand_{dial_id}_nfc_event"


class _DialEventEntity(DeckhandEntity, EventEntity):
    """One dial's moments of one kind. Subclasses map a bus event to
    ``(event type, attributes)`` in ``_map``."""

    async def async_get_last_state(self):
        """Never restore a past event across a restart.

        EventEntity restores its last event on startup, which writes the old
        timestamp as a fresh state change (``unavailable`` → yesterday's
        ``completed``). A state trigger on the entity fired on exactly that
        at 03:51 one morning and chimed for a timer that had finished twelve
        hours earlier. A timer that ended before the restart is not news:
        start ``unknown`` and let the next real timer be the first event.
        """
        return None

    @property
    def available(self) -> bool:
        """Always available.

        The base class follows the dial's heartbeat, and every lapse-and-
        return would replay the last timestamp as ``unavailable`` → state —
        another false ``completed`` for any state trigger. Reachability is
        the connectivity binary_sensor's job; the last event stays true
        whether or not the dial is online right now.
        """
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_dial_event", self._handle_dial_event)
        )

    def _map(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        raise NotImplementedError

    @callback
    def _handle_dial_event(self, event: Event) -> None:
        data = event.data or {}
        if data.get("dial_id") != self.dial_id:
            return
        mapped = self._map(data)
        if mapped is None:
            return
        kind, attributes = mapped
        self._trigger_event(kind, attributes)
        self.async_write_ha_state()


class DeckhandTimerEvent(_DialEventEntity):
    """``started`` when the dial's timer starts (a guest on the glass or
    ``deckhand.start_timer``), ``completed`` when it reaches zero,
    ``cancelled`` when it is stopped early (on the glass or by
    ``deckhand.cancel_timer``). Attributes: ``label`` (the timer's label)
    and ``minutes`` (the length it was set to; absent on firmware before
    0.4.125)."""

    _attr_translation_key = "timer"
    _attr_icon = "mdi:timer-outline"
    _attr_event_types = list(TIMER_EVENT_TYPES.values())

    def __init__(self, dial_id: str, data: dict[str, Any]) -> None:
        super().__init__(dial_id, data)
        self._attr_unique_id = f"{dial_id}_timer"

    def _map(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        kind = TIMER_EVENT_TYPES.get(data.get("type"))
        if kind is None:
            return None
        payload = data.get("payload") or {}
        attributes: dict[str, Any] = {"label": payload.get("item_label") or ""}
        minutes = payload.get("minutes")
        if isinstance(minutes, (int, float)):
            attributes["minutes"] = int(minutes)
        return kind, attributes


class _MappedEvent(_DialEventEntity):
    """An entity whose event types are ``_automation.ENTITY_EVENT_TYPES[key]``."""

    _key = ""

    def __init__(self, dial_id: str, data: dict[str, Any]) -> None:
        super().__init__(dial_id, data)
        self._attr_unique_id = f"deckhand_{dial_id}_{self._key}_event"
        self._attr_translation_key = self._key
        self._attr_event_types = list(dict.fromkeys(auto.ENTITY_EVENT_TYPES[self._key].values()))

    def _map(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        return auto.entity_event(self._key, data)


class DeckhandButtonEvent(_MappedEvent):
    """``press`` for any press on the dial (a menu item, a scene, a media
    control — not the sensor faces' value requests or the TV remote's
    volume detents), ``long_press`` and ``double_tap`` when the dial's hold
    or double tap is set to run something. Attributes: ``item_id``,
    ``item_label``, ``action``."""

    _key = "button"
    _attr_icon = "mdi:gesture-tap-button"


class DeckhandMenuEvent(_MappedEvent):
    """``item_selected`` for a plain menu item (no built-in action — what
    ``deckhand.add_menu_item`` adds), ``opened`` / ``closed`` / ``timed_out``
    for the menu itself. Attributes: ``item_id``, ``item_label``."""

    _key = "menu"
    _attr_icon = "mdi:menu"


class DeckhandInvitationEvent(_MappedEvent):
    """The guest's answer to ``deckhand.send_invitation``: ``accepted``,
    ``declined``, ``expired`` or ``cancelled``. Attributes:
    ``invitation_id``, ``held_ms``."""

    _key = "invitation"
    _attr_icon = "mdi:email-open-outline"


class DeckhandNfcEvent(_MappedEvent):
    """A card tapped on the dial's reader, as Helm / Deckhand Console
    resolved it: ``known``, ``unknown`` or ``revoked``. Attributes:
    ``identity`` (empty for an unknown card), ``role``, ``credential_id``.
    The card's hash never reaches Home Assistant."""

    _key = "nfc"
    _attr_icon = "mdi:nfc-variant"

    def __init__(
        self, dial_id: str, data: dict[str, Any], first: dict[str, Any] | None = None
    ) -> None:
        super().__init__(dial_id, data)
        self._first = first

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._first is not None:
            first, self._first = self._first, None
            mapped = self._map(first)
            if mapped is not None:
                self._trigger_event(*mapped)
                self.async_write_ha_state()
