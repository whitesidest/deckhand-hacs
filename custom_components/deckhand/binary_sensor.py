"""Binary sensor platform for Deckhand integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _faces
from .const import DOMAIN, HEARTBEAT_TIMEOUT
from .entity import DeckhandEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Deckhand binary sensors from a config entry."""
    known_dials: set[str] = set()

    @callback
    def _async_discover_dial(dial_id: str, data: dict[str, Any]) -> None:
        """Handle discovery of a new dial."""
        if dial_id in known_dials:
            return
        known_dials.add(dial_id)

        async_add_entities(
            [
                DeckhandConnectivitySensor(dial_id, data),
                DeckhandDndSensor(dial_id, data),
                DeckhandRingSensor(dial_id, data),
            ]
        )
        _LOGGER.debug("Added connectivity + DND sensors for %s", dial_id)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_dial_discovered", _async_discover_dial
        )
    )

    # Also create for already-discovered dials
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    for dial_id, data in store.get("dials", {}).items():
        _async_discover_dial(dial_id, data)


class DeckhandConnectivitySensor(DeckhandEntity, BinarySensorEntity):
    """Binary sensor representing dial online/offline status."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connectivity"

    def __init__(self, dial_id: str, data: dict[str, Any]) -> None:
        """Initialize the connectivity sensor."""
        super().__init__(dial_id, data)
        self._attr_unique_id = f"deckhand_{dial_id}_connectivity"

    @property
    def is_on(self) -> bool:
        """Return True if the dial is online."""
        online = self._dial_data.get("online", False)
        if not online:
            return False
        # Also check heartbeat freshness — read the authoritative timestamp
        # stamped by the status subscriber into _dial_data.
        last_seen = self._dial_data.get("_last_seen")
        if not last_seen:
            return False
        try:
            ts = datetime.fromisoformat(last_seen)
        except (TypeError, ValueError):
            return False
        return datetime.now() - ts < timedelta(seconds=HEARTBEAT_TIMEOUT)

    @property
    def available(self) -> bool:
        """Connectivity sensor is always available so it can report offline."""
        return True


class DeckhandDndSensor(DeckhandEntity, BinarySensorEntity):
    """Binary sensor for the dial's quiet-mode Do Not Disturb (helm#179).

    Driven by the ``dnd`` field on the retained status topic the
    integration already subscribes (``_handle_status`` in __init__.py)
    — zero new subscriptions, and state survives HA restarts via broker
    retention.

    No device_class: HA has no class for "the room asks for quiet"
    (``occupancy``/``sound`` would lie to voice assistants and
    dashboards). Plain sensor with an ``mdi:minus-circle`` icon, per
    the DND plan.

    OLD-FIRMWARE CHOICE (documented decision): dials on firmware
    < 0.4.44 never include the ``dnd`` field in their status payload.
    This sensor reports **unavailable** in that case, NOT ``off`` —
    an automation must be able to distinguish "not in DND" from "this
    dial cannot report DND", otherwise a hold-calls automation would
    confidently release the hold on a dial that simply hasn't been
    updated yet. The field appearing after an OTA flips the sensor
    available automatically on the next heartbeat.
    """

    _attr_translation_key = "do_not_disturb"
    _attr_icon = "mdi:minus-circle"

    def __init__(self, dial_id: str, data: dict[str, Any]) -> None:
        """Initialize the DND sensor."""
        super().__init__(dial_id, data)
        self._attr_unique_id = f"deckhand_{dial_id}_dnd"

    @property
    def is_on(self) -> bool:
        """Return True if the dial reports quiet-mode DND active."""
        return bool(self._dial_data.get("dnd"))

    @property
    def available(self) -> bool:
        """Available only when the firmware actually reports DND.

        Combines the base heartbeat-freshness gate with the presence of
        the ``dnd`` field (absent on firmware < 0.4.44 — see class
        docstring).
        """
        if "dnd" not in self._dial_data:
            return False
        return super().available


class DeckhandRingSensor(DeckhandEntity, BinarySensorEntity):
    """Whether the Perimeter Ring overlay is mounted, as the dial reports it.

    From the heartbeat's ``ring`` flag (firmware ``face_report``). Like
    Do Not Disturb, unavailable on firmware that doesn't report it rather
    than a confident ``off``.
    """

    _attr_translation_key = "ring"
    _attr_icon = "mdi:circle-double"

    def __init__(self, dial_id: str, data: dict[str, Any]) -> None:
        super().__init__(dial_id, data)
        self._attr_unique_id = f"deckhand_{dial_id}_ring"

    @property
    def is_on(self) -> bool | None:
        return _faces.ring_report(self._dial_data)

    @property
    def available(self) -> bool:
        if _faces.ring_report(self._dial_data) is None:
            return False
        return super().available
