"""Number platform for Deckhand integration (brightness control)."""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, TOPIC_CMD_CONFIG
from .entity import DeckhandEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Deckhand number entities from a config entry."""
    known_dials: set[str] = set()

    @callback
    def _async_discover_dial(dial_id: str, data: dict[str, Any]) -> None:
        """Handle discovery of a new dial."""
        if dial_id in known_dials:
            return
        known_dials.add(dial_id)

        async_add_entities([DeckhandBrightnessNumber(dial_id, data, entry)])
        _LOGGER.debug("Added brightness control for %s", dial_id)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_dial_discovered", _async_discover_dial
        )
    )

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    for dial_id, data in store.get("dials", {}).items():
        _async_discover_dial(dial_id, data)


class DeckhandBrightnessNumber(DeckhandEntity, NumberEntity):
    """Number entity for controlling dial display brightness."""

    _attr_name = "Brightness"
    _attr_icon = "mdi:brightness-6"
    _attr_native_min_value = 0
    _attr_native_max_value = 255
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, dial_id: str, data: dict[str, Any], entry: ConfigEntry
    ) -> None:
        """Initialize the brightness number entity."""
        super().__init__(dial_id, data)
        self._entry = entry
        self._attr_unique_id = f"deckhand_{dial_id}_brightness"
        self._attr_native_value = 180.0  # Reasonable default

    async def async_set_native_value(self, value: float) -> None:
        """Set the brightness value — publishes MQTT config command."""
        store = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        team_id = store.get("team_id", "1")
        topic = TOPIC_CMD_CONFIG.format(team_id=team_id, dial_id=self._dial_id)

        await mqtt.async_publish(
            self.hass, topic, json.dumps({"brightness": int(value)})
        )
        self._attr_native_value = value
        self.async_write_ha_state()
        _LOGGER.info("Set brightness %d on %s", int(value), self._dial_id)
