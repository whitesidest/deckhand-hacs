"""Event platform: one ``Timer`` event entity per dial.

The dial already publishes ``timer_start`` / ``timer_complete`` on its event
topic and the integration already re-fires every dial event as
``deckhand_dial_event`` on the bus. Automating off that works, but it is a
raw bus event: nothing to pick in the automation editor, and the author has
to know the wire names. An ``event`` entity gives each dial a "Timer" that
shows up under the device with two event types — ``started`` and
``completed`` — that the editor offers directly, and that carries the
label and length of the timer as attributes.

Both paths fire for the same timer; ``deckhand_dial_event`` stays raw and
unchanged (see tests/test_nfc_event_paths.py for why).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import DeckhandEntity

_LOGGER = logging.getLogger(__name__)

# Wire event type (firmware) → entity event type (what an automation picks).
TIMER_EVENT_TYPES = {
    "timer_start": "started",
    "timer_complete": "completed",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one Timer event entity per discovered dial."""
    known_dials: set[str] = set()

    @callback
    def _async_discover_dial(dial_id: str, data: dict[str, Any]) -> None:
        if dial_id in known_dials:
            return
        known_dials.add(dial_id)
        async_add_entities([DeckhandTimerEvent(dial_id, data)])
        _LOGGER.debug("Added timer event entity for %s", dial_id)

    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{DOMAIN}_dial_discovered", _async_discover_dial)
    )

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    for dial_id, data in store.get("dials", {}).items():
        _async_discover_dial(dial_id, data)


class DeckhandTimerEvent(DeckhandEntity, EventEntity):
    """``started`` when a guest starts the dial's timer, ``completed`` when
    it reaches zero. Attributes: ``label`` (the timer's menu label) and
    ``minutes`` (the length it was set to; absent on firmware before 0.4.125)."""

    _attr_name = "Timer"
    _attr_icon = "mdi:timer-outline"
    _attr_event_types = list(TIMER_EVENT_TYPES.values())

    def __init__(self, dial_id: str, data: dict[str, Any]) -> None:
        super().__init__(dial_id, data)
        self._attr_unique_id = f"{dial_id}_timer"

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

    @callback
    def _handle_dial_event(self, event: Event) -> None:
        data = event.data or {}
        if data.get("dial_id") != self.dial_id:
            return
        kind = TIMER_EVENT_TYPES.get(data.get("type"))
        if kind is None:
            return
        payload = data.get("payload") or {}
        attributes: dict[str, Any] = {"label": payload.get("item_label") or ""}
        minutes = payload.get("minutes")
        if isinstance(minutes, (int, float)):
            attributes["minutes"] = int(minutes)
        self._trigger_event(kind, attributes)
        self.async_write_ha_state()
