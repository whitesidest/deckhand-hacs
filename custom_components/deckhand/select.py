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
        """Initialize the theme selector.

        Note: ``self.hass`` isn't bound until the entity is added to HA, so
        we can only seed options from constants here. The real catalog is
        loaded from the integration cache in ``async_added_to_hass``.
        """
        super().__init__(dial_id, data)
        self._entry = entry
        self._attr_unique_id = f"deckhand_{dial_id}_theme_select"
        self._attr_options = list(DEFAULT_THEMES)
        current = data.get("current_theme")
        self._attr_current_option = (
            current if current in self._attr_options else self._attr_options[0]
        )

    def _options_from_store(self) -> list[str]:
        """Pull the current theme catalog out of the integration cache.

        Falls back to ``DEFAULT_THEMES`` when the team hasn't published
        a themes/list yet (e.g. fresh broker, no Helm/Console connected).
        Only safe to call once the entity has been added to HA — uses
        ``self.hass``.
        """
        store = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        themes = store.get("themes") or []
        slugs = [t["slug"] for t in themes if isinstance(t, dict) and t.get("slug")]
        return slugs or list(DEFAULT_THEMES)

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
        """Subscribe to status + themes-catalog updates."""
        await super().async_added_to_hass()

        # Now that self.hass is bound, refresh options from whatever the
        # team's themes/list publish has populated. The MQTT subscribe
        # in __init__.py has had the chance to fire by now, so the cache
        # is usually warm.
        cached = self._options_from_store()
        if self._attr_current_option and self._attr_current_option not in cached:
            cached = cached + [self._attr_current_option]
        self._attr_options = cached
        self.async_write_ha_state()

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
                # Theme not in catalog — surface it anyway so the
                # current value renders (HA hides selects whose value
                # isn't in options).
                self._attr_options = self._attr_options + [theme]
                self._attr_current_option = theme
            self.update_from_status(data)
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_status_update", _handle_update)
        )

        @callback
        def _handle_themes_updated() -> None:
            """Refresh the picker when the team's catalog changes."""
            new_options = self._options_from_store()
            # Keep the currently-applied theme visible even if it was
            # removed from the catalog (rare — usually means a custom
            # theme got deleted while still active on a dial).
            if self._attr_current_option and self._attr_current_option not in new_options:
                new_options = new_options + [self._attr_current_option]
            self._attr_options = new_options
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{DOMAIN}_themes_updated", _handle_themes_updated
            )
        )
