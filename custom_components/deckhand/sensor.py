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
    LIGHT_LUX,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTemperature,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _faces
from .const import DOMAIN, SIGNAL_FACES_UPDATED
from .entity import DeckhandEntity

_LOGGER = logging.getLogger(__name__)


SENSOR_DEFINITIONS: list[dict[str, Any]] = [
    {
        "key": "battery_pct",
        "translation_key": "battery",
        "device_class": SensorDeviceClass.BATTERY,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": PERCENTAGE,
        "icon": None,
        "entity_category": None,
    },
    {
        "key": "rssi",
        "translation_key": "wifi_signal",
        "device_class": SensorDeviceClass.SIGNAL_STRENGTH,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        "icon": None,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    {
        "key": "temperature_c",
        "translation_key": "temperature",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfTemperature.CELSIUS,
        "icon": None,
        "entity_category": None,
        "optional": True,
    },
    {
        "key": "humidity_pct",
        "translation_key": "humidity",
        "device_class": SensorDeviceClass.HUMIDITY,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": PERCENTAGE,
        "icon": None,
        "entity_category": None,
        "optional": True,
    },
    {
        "key": "ambient_lux",
        "translation_key": "ambient_light",
        "device_class": SensorDeviceClass.ILLUMINANCE,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": LIGHT_LUX,
        "icon": None,
        "entity_category": None,
        "optional": True,
    },
    {
        "key": "current_theme",
        "translation_key": "theme",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "icon": "mdi:palette",
        # The Theme select shows (and changes) the same value; this read-only
        # copy stays for history and existing automations, out of the way.
        "entity_category": EntityCategory.DIAGNOSTIC,
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

        # Which face the dial is wearing, from its own heartbeat (firmware
        # face_report). Created for every dial: unavailable until the
        # firmware reports, so an OTA brings it to life without a restart.
        entities.append(DeckhandShowingSensor(dial_id, data, entry))

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
        # Named from translations (strings.json "entity"), whose English
        # matches the old fixed names, so names and entity_ids are unchanged.
        self._attr_translation_key = definition["translation_key"]
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


class DeckhandShowingSensor(DeckhandEntity, SensorEntity):
    """What the dial is showing, as the dial itself reports it.

    Firmware with the ``face_report`` feature puts
    ``"face": {"id", "slot", "since_s"[, "ref"]}`` and ``"ring"`` in every
    heartbeat. The state is the library face's name when ``ref`` (the face
    uid the server stamped on the mount) is in the synced face library with
    the kind the dial reports; otherwise a friendly kind ("Readings face",
    "Home clock").

    On firmware without the report the sensor is unavailable, never a
    made-up "none": an automation must be able to tell "showing nothing in
    particular" from "this dial can't say".
    """

    _attr_translation_key = "showing"
    _attr_icon = "mdi:image-frame"

    def __init__(self, dial_id: str, data: dict[str, Any], entry: ConfigEntry) -> None:
        super().__init__(dial_id, data)
        self._entry = entry
        self._attr_unique_id = f"deckhand_{dial_id}_showing"
        self._since_key: tuple | None = None
        self._since: str | None = None
        self._last_uptime: int | None = None
        self._update_since(data)

    def _index(self) -> dict[str, Any] | None:
        hass = getattr(self, "hass", None)
        if hass is None:
            return None
        store = (hass.data.get(DOMAIN) or {}).get(self._entry.entry_id) or {}
        return (store.get("faces") or {}).get("index")

    def _update_since(self, data: dict[str, Any]) -> None:
        """Absolute time the face went up. Recomputed only when the report
        changes or the dial rebooted, so heartbeat jitter doesn't rewrite
        the attribute every 30 s."""
        report = _faces.face_report(data)
        uptime = data.get("uptime_s") if isinstance(data.get("uptime_s"), int) else None
        rebooted = (
            uptime is not None and self._last_uptime is not None and uptime < self._last_uptime
        )
        self._last_uptime = uptime
        if report is None:
            self._since_key, self._since = None, None
            return
        key = (report["id"], report["slot"], report["since_s"], report["ref"])
        if key == self._since_key and not rebooted and self._since is not None:
            return
        since = _faces.showing_since(data, report)
        self._since_key = key
        self._since = since.isoformat() if since else None

    def _on_status(self, data: dict[str, Any]) -> None:
        super()._on_status(data)
        self._update_since(data)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _faces_updated() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_FACES_UPDATED, _faces_updated)
        )

    @property
    def available(self) -> bool:
        return super().available and _faces.face_report(self._dial_data) is not None

    @property
    def native_value(self) -> str | None:
        report = _faces.face_report(self._dial_data)
        if report is None:
            return None
        found = _faces.library_face_for(report, self._index())
        if found is not None:
            return found["name"]
        return _faces.friendly_kind(report["id"], report["slot"])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        report = _faces.face_report(self._dial_data)
        if report is None:
            return None
        found = _faces.library_face_for(report, self._index())
        return {
            "face_id": report["id"],
            "slot": report["slot"],
            "ref": report["ref"],
            "since": self._since,
            "ring": _faces.ring_report(self._dial_data),
            "in_library": found is not None,
        }
