"""Number platform for Deckhand integration (brightness control)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from ._source import dumps_with_source
from .const import DOMAIN, TOPIC_CMD_OVERLAY
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


def brightness_from_status(data: dict[str, Any]) -> float | None:
    """What the slider should show for one heartbeat, or None if unknown.

    The slider sets the dial's night-dim cap (see async_set_native_value),
    and the heartbeat reports that cap as ``brightness_cap`` (firmware newer
    than 0.4.169): 0-99 is the cap, ``null`` means none, which is the
    slider at 100. A heartbeat without the key (older firmware, or a dial
    that has never recorded a cap) says nothing about the slider.

    ``brightness_pct`` is NOT this value: it is the backlight right now,
    which drops whenever the dial idles into its dim level.
    """
    if "brightness_cap" not in data:
        return None
    cap = data["brightness_cap"]
    if cap is None:
        return 100.0
    if isinstance(cap, bool) or not isinstance(cap, (int, float)):
        return None
    return float(max(0, min(100, cap)))


class DeckhandBrightnessNumber(DeckhandEntity, NumberEntity, RestoreEntity):
    """Number entity for controlling dial display brightness."""

    _attr_translation_key = "brightness"
    _attr_icon = "mdi:brightness-6"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, dial_id: str, data: dict[str, Any], entry: ConfigEntry
    ) -> None:
        """Initialize the brightness number entity."""
        super().__init__(dial_id, data)
        self._entry = entry
        self._attr_unique_id = f"deckhand_{dial_id}_brightness"
        # What the dial reports, else unknown until async_added_to_hass
        # restores the last value. It used to start at a made-up 80.
        self._attr_native_value = brightness_from_status(data)

    async def async_added_to_hass(self) -> None:
        """Restore the last value when the dial doesn't report its cap."""
        await super().async_added_to_hass()
        if self._attr_native_value is not None:
            return
        last = await self.async_get_last_state()
        if last is None:
            return
        try:
            self._attr_native_value = float(last.state)
        except (TypeError, ValueError):
            # "unknown" / "unavailable": stay unknown.
            pass

    def _on_status(self, data: dict[str, Any]) -> None:
        """Follow the cap the dial reports (the base class does the listening)."""
        value = brightness_from_status(data)
        if value is not None:
            self._attr_native_value = value
        super()._on_status(data)

    async def async_set_native_value(self, value: float) -> None:
        """Set the brightness value — publishes a theme-brightness overlay.

        This published ``{"brightness": N}`` to ``cmd/config`` until
        2026-08. The firmware's cmd/config handler does not read
        ``brightness`` at ALL — it reads clock_format, clock_face_pref,
        label, hide_label, tz, the haptics and the NFC fields, and
        nothing else — so the slider moved in Home Assistant and the dial
        never changed. ``brightness`` is handled by
        apply_display_overlay(), which is reached from cmd/theme and
        cmd/overlay only.

        cmd/overlay is the right destination of the two: cmd/theme would
        also repaint the palette. A bare brightness overlay carries no
        ``home_face``/``home_mode`` and no ``ttl_s``, so the firmware
        classifies it as neither a face launch nor a home-face change and
        leaves that state alone (see the overlay handler's comment: "a
        subtitle flash or brightness bump is neither"). The firmware
        persists the value to NVS itself, so it survives a reboot.

        Since the dial's night-dim cap (firmware newer than 0.4.169) a
        ttl-less overlay brightness is a persistent CAP rather than a theme
        brightness: the slider below the theme's level caps the dial there
        through theme pushes, reconnects and reboots; at or above it (100
        always) it lifts the cap. Older firmware keeps the behaviour below.

        Note this is THEME brightness — a 0-100% scale on the panel's
        normal level — not the operator's power profile
        (dim_brightness / full_brightness), which lives on Helm's Dial row
        and travels on cmd/power_config. Helm has no field for this one,
        so there is no row to persist to and nothing that reverts it.
        """
        store = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        team_id = store.get("team_id", "1")
        topic = TOPIC_CMD_OVERLAY.format(team_id=team_id, dial_id=self._dial_id)

        body = dumps_with_source({"brightness": int(value)}, await self._async_source())
        await mqtt.async_publish(self.hass, topic, body, qos=1, retain=False)
        self._attr_native_value = value
        self.async_write_ha_state()
        _LOGGER.info("Set brightness %d on %s", int(value), self._dial_id)
