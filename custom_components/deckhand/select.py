"""Select platform for Deckhand integration (theme selector)."""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DEFAULT_THEMES, TOPIC_CMD_THEME
from .entity import DeckhandEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Deckhand theme selectors from a config entry."""
    known_dials: set[str] = set()

    @callback
    def _async_discover_dial(dial_id: str, data: dict[str, Any]) -> None:
        """Handle discovery of a new dial."""
        if dial_id in known_dials:
            return
        known_dials.add(dial_id)

        async_add_entities([DeckhandThemeSelect(dial_id, data, entry)])
        _LOGGER.debug("Added theme select for %s", dial_id)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_dial_discovered", _async_discover_dial
        )
    )

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    for dial_id, data in store.get("dials", {}).items():
        _async_discover_dial(dial_id, data)


class DeckhandThemeSelect(DeckhandEntity, SelectEntity):
    """Select entity for choosing a dial's theme."""

    _attr_name = "Theme"
    _attr_icon = "mdi:palette"

    def __init__(
        self, dial_id: str, data: dict[str, Any], entry: ConfigEntry
    ) -> None:
        """Initialize the theme selector."""
        super().__init__(dial_id, data)
        self._entry = entry
        self._attr_unique_id = f"deckhand_{dial_id}_theme_select"
        self._attr_options = list(DEFAULT_THEMES)
        self._attr_current_option = data.get("current_theme", DEFAULT_THEMES[0])

    async def async_select_option(self, option: str) -> None:
        """Change the selected theme — publishes MQTT command."""
        store = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        team_id = store.get("team_id", "1")
        topic = TOPIC_CMD_THEME.format(team_id=team_id, dial_id=self._dial_id)

        await mqtt.async_publish(
            self.hass, topic, json.dumps({"id": option})
        )
        self._attr_current_option = option
        self.async_write_ha_state()
        _LOGGER.info("Set theme '%s' on %s", option, self._dial_id)

    async def async_added_to_hass(self) -> None:
        """Subscribe to status updates when added to hass."""
        await super().async_added_to_hass()

        @callback
        def _handle_update(event) -> None:
            """Handle a status update event."""
            if event.data.get("dial_id") != self._dial_id:
                return
            data = event.data["data"]
            theme = data.get("current_theme")
            if theme and theme in self._attr_options:
                self._attr_current_option = theme
            elif theme:
                # Theme not in list — add it dynamically
                self._attr_options.append(theme)
                self._attr_current_option = theme
            self.update_from_status(data)
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_status_update", _handle_update)
        )
