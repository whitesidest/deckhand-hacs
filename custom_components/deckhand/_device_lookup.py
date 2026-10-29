"""From a Home Assistant device id to the Deckhand dial (or Room) behind it.

Shared by the device triggers and device actions. A dial device carries the
identifier ``(deckhand, <dial_id>)``; a Room carries ``(deckhand,
"group:<id>")``. Both belong to exactly one Deckhand config entry.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

GROUP_PREFIX = "group:"


def deckhand_identifier(hass: HomeAssistant, device_id: str) -> str | None:
    """The device's Deckhand identifier (a dial id or ``group:<id>``)."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for domain, ident in device.identifiers:
        if domain == DOMAIN and isinstance(ident, str) and ident:
            return ident
    return None


def dial_id_for_device(hass: HomeAssistant, device_id: str) -> str | None:
    """The dial id of a dial device; None for a Room or a foreign device."""
    ident = deckhand_identifier(hass, device_id)
    if ident is None or ident.startswith(GROUP_PREFIX):
        return None
    return ident


def store_for_device(hass: HomeAssistant, device_id: str) -> dict[str, Any] | None:
    """The owning entry's runtime store (``hass.data[DOMAIN][entry_id]``)."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    stores = hass.data.get(DOMAIN, {})
    for entry_id in device.config_entries:
        store = stores.get(entry_id)
        if isinstance(store, dict):
            return store
    return None
