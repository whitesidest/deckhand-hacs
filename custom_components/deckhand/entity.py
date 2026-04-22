"""Base entity for Deckhand integration."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN, HEARTBEAT_TIMEOUT, MANUFACTURER, HARDWARE_MODELS


class DeckhandEntity(Entity):
    """Base class for Deckhand entities."""

    _attr_has_entity_name = True

    def __init__(self, dial_id: str, data: dict[str, Any]) -> None:
        """Initialize the entity."""
        self._dial_id = dial_id
        self._dial_data = data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dial_id)},
            manufacturer=MANUFACTURER,
            name=data.get("_label") or f"Deckhand {dial_id}",
            model=HARDWARE_MODELS.get(
                data.get("hardware_type", "unknown"), "Deckhand Dial"
            ),
            sw_version=data.get("fw_ver"),
        )

    @property
    def dial_id(self) -> str:
        """Return the dial ID."""
        return self._dial_id

    @property
    def available(self) -> bool:
        """Return True if the dial has sent a recent heartbeat."""
        last_seen = self._dial_data.get("_last_seen")
        if not last_seen:
            return False
        try:
            ts = datetime.fromisoformat(last_seen)
        except (TypeError, ValueError):
            return False
        return datetime.now() - ts < timedelta(seconds=HEARTBEAT_TIMEOUT)

    def update_from_status(self, data: dict[str, Any]) -> None:
        """Update entity state from a heartbeat payload."""
        self._dial_data = data
        if hasattr(self, "_attr_device_info") and self._attr_device_info:
            # Update sw_version if firmware changed
            fw = data.get("fw_ver")
            if fw:
                self._attr_device_info = DeviceInfo(
                    identifiers={(DOMAIN, self._dial_id)},
                    manufacturer=MANUFACTURER,
                    name=data.get("_label") or f"Deckhand {self._dial_id}",
                    model=HARDWARE_MODELS.get(
                        data.get("hardware_type", "unknown"), "Deckhand Dial"
                    ),
                    sw_version=fw,
                )
