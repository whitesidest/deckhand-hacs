"""Button platform for Deckhand integration (reboot)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, TOPIC_CMD_REBOOT
from .entity import DeckhandEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Deckhand buttons from a config entry."""
    known_dials: set[str] = set()

    @callback
    def _async_discover_dial(dial_id: str, data: dict[str, Any]) -> None:
        """Handle discovery of a new dial."""
        if dial_id in known_dials:
            return
        known_dials.add(dial_id)

        async_add_entities([DeckhandRebootButton(dial_id, data, entry)])
        _LOGGER.debug("Added reboot button for %s", dial_id)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_dial_discovered", _async_discover_dial
        )
    )

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    for dial_id, data in store.get("dials", {}).items():
        _async_discover_dial(dial_id, data)


class DeckhandRebootButton(DeckhandEntity, ButtonEntity):
    """Button to reboot a Deckhand dial."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_name = "Reboot"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, dial_id: str, data: dict[str, Any], entry: ConfigEntry
    ) -> None:
        """Initialize the reboot button."""
        super().__init__(dial_id, data)
        self._entry = entry
        self._attr_unique_id = f"deckhand_{dial_id}_reboot"

    async def async_press(self) -> None:
        """Handle the button press — send reboot command via MQTT."""
        store = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        team_id = store.get("team_id", "1")
        topic = TOPIC_CMD_REBOOT.format(team_id=team_id, dial_id=self._dial_id)

        await mqtt.async_publish(self.hass, topic, "{}")
        _LOGGER.info("Sent reboot command to %s", self._dial_id)
