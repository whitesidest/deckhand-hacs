"""Diagnostics for the Deckhand integration.

What the integration knows about the team and each dial, for a bug report:
the last heartbeat (minus fields that locate the network), whether a
Perimeter Ring mount is retained, the sensor-face watches Helm / Console
asked for, and the media_player bindings. Nothing the integration holds is
a secret today, but anything named like one is redacted anyway, in case a
future heartbeat or option carries it.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_BINDING_DIAL, CONF_MEDIA_PLAYER_BINDINGS, DOMAIN

# Credential-like keys, plus the ones that pin down the network: the dial's
# LAN address, MAC, Wi-Fi name and access point.
TO_REDACT = {
    "token",
    "access_token",
    "password",
    "passwd",
    "secret",
    "api_key",
    "credential",
    "credentials",
    "uid_hash",
    "mqtt_user",
    "mqtt_password",
    "ip",
    "mac",
    "ssid",
    "bssid",
}


def _store(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    return hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}


def _dial_diagnostics(store: dict[str, Any], dial_id: str) -> dict[str, Any]:
    """Everything known about one dial."""
    heartbeat = store.get("dials", {}).get(dial_id)
    feed = store.get("_perimeter_feeds", {}).get(dial_id)
    watch = store.get("_sensor_watch_bindings", {}).get(dial_id)
    overlay = store.get("_sensor_bindings", {}).get(dial_id)
    return {
        "dial_id": dial_id,
        "label": store.get("labels", {}).get(dial_id),
        "fw_ver": (heartbeat or {}).get("fw_ver"),
        "hardware_type": (heartbeat or {}).get("hardware_type"),
        "last_seen": (heartbeat or {}).get("_last_seen"),
        "last_heartbeat": dict(heartbeat) if heartbeat else None,
        "perimeter_ring": {
            # None: no retained mount (or clear) seen this session.
            "mount_retained": store.get("_perimeter_mounted", {}).get(dial_id),
            "live_fed_bindings": [b.get("id") for b in feed["bindings"]] if feed else [],
        },
        "sensor_watches": list(watch["entity_ids"]) if watch else [],
        "overlay_sensor_bindings": list(overlay["entity_ids"]) if overlay else [],
    }


def _media_bindings(entry: ConfigEntry) -> list[Any]:
    return list(entry.options.get(CONF_MEDIA_PLAYER_BINDINGS, []) or [])


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Diagnostics for the whole team."""
    store = _store(hass, entry)
    dial_ids = sorted(set(store.get("dials", {})) | set(store.get("labels", {})))
    data = {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "team_id": store.get("team_id"),
        "theme_catalog_size": len(store.get("themes") or []),
        "rooms": [
            {"id": gid, "name": g.get("name"), "dial_ids": list(g.get("dial_ids", []))}
            for gid, g in (store.get("groups") or {}).items()
        ],
        "media_player_bindings": _media_bindings(entry),
        "dials": [_dial_diagnostics(store, dial_id) for dial_id in dial_ids],
    }
    return async_redact_data(data, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Diagnostics for one dial (or Room) device."""
    store = _store(hass, entry)
    ident = next((i for d, i in device.identifiers if d == DOMAIN), None)
    data: dict[str, Any] = {"team_id": store.get("team_id")}
    if ident is None:
        data["device"] = None
    elif ident.startswith("group:"):
        group = (store.get("groups") or {}).get(ident.removeprefix("group:"))
        data["room"] = (
            {"name": group.get("name"), "dial_ids": list(group.get("dial_ids", []))}
            if group
            else None
        )
    else:
        data["dial"] = _dial_diagnostics(store, ident)
        data["media_player_bindings"] = [
            b
            for b in _media_bindings(entry)
            if isinstance(b, dict) and b.get(CONF_BINDING_DIAL) == device.id
        ]
    return async_redact_data(data, TO_REDACT)
