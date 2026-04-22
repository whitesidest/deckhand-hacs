"""Sensor platform for Deckhand integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTemperature,
    UnitOfIlluminance,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import DeckhandEntity

_LOGGER = logging.getLogger(__name__)


SENSOR_DEFINITIONS: list[dict[str, Any]] = [
    {
        "key": "battery_pct",
        "name": "Battery",
        "device_class": SensorDeviceClass.BATTERY,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": PERCENTAGE,
        "icon": None,
        "entity_category": None,
    },
    {
        "key": "rssi",
        "name": "WiFi Signal",
        "device_class": SensorDeviceClass.SIGNAL_STRENGTH,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        "icon": None,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    {
        "key": "temperature_c",
        "name": "Temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": None,
        "entity_category": None,
        "optional": True,
    },
    {
        "key": "humidity_pct",
        "name": "Humidity",
        "device_class": SensorDeviceClass.HUMIDITY,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": PERCENTAGE,
        "icon": None,
        "entity_category": None,
        "optional": True,
    },
    {
        "key": "ambient_lux",
        "name": "Ambient Light",
        "device_class": SensorDeviceClass.ILLUMINANCE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfIlluminance.LUX,
        "icon": None,
        "entity_category": None,
        "optional": True,
    },
    {
        "key": "current_theme",
        "name": "Theme",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "icon": "mdi:palette",
        "entity_category": None,
    },
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Deckhand sensors from a config entry."""
    known_dials: set[str] = set()

    @callback
    def _async_discover_dial(dial_id: str, data: dict[str, Any]) -> None:
        """Handle discovery of a new dial."""
        if dial_id in known_dials:
            return
        known_dials.add(dial_id)

        entities: list[DeckhandSensor] = []
        for defn in SENSOR_DEFINITIONS:
            # Skip optional sensors that aren't present in the data
            if defn.get("optional") and defn["key"] not in data:
                continue
            entities.append(DeckhandSensor(dial_id, data, defn))

        if entities:
            async_add_entities(entities)
            _LOGGER.debug("Added %d sensors for %s", len(entities), dial_id)

    # Listen for new dial discoveries
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_dial_discovered", _async_discover_dial
        )
    )

    # Also create sensors for already-discovered dials
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    for dial_id, data in store.get("dials", {}).items():
        _async_discover_dial(dial_id, data)


class DeckhandSensor(DeckhandEntity, SensorEntity):
    """Representation of a Deckhand sensor."""

    def __init__(
        self, dial_id: str, data: dict[str, Any], definition: dict[str, Any]
    ) -> None:
        """Initialize the sensor."""
        super().__init__(dial_id, data)
        self._definition = definition
        self._key = definition["key"]

        self._attr_unique_id = f"deckhand_{dial_id}_{self._key}"
        self._attr_name = definition["name"]
        self._attr_device_class = definition["device_class"]
        self._attr_state_class = definition["state_class"]
        self._attr_native_unit_of_measurement = definition["unit"]
        if definition["icon"]:
            self._attr_icon = definition["icon"]
        if definition["entity_category"]:
            self._attr_entity_category = definition["entity_category"]

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        value = self._dial_data.get(self._key)
        # battery_pct can be null (no battery)
        if value is None:
            return None
        return value

    async def async_added_to_hass(self) -> None:
        """Subscribe to status updates when added to hass."""
        await super().async_added_to_hass()

        @callback
        def _handle_update(event) -> None:
            """Handle a status update event."""
            if event.data.get("dial_id") != self._dial_id:
                return
            self.update_from_status(event.data["data"])
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_status_update", _handle_update)
        )
