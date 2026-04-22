"""Config flow for Deckhand integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
)

from .const import (
    CONF_BINDING_DIAL,
    CONF_BINDING_ENTITY,
    CONF_MEDIA_PLAYER_BINDINGS,
    CONF_TEAM_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TEAM_ID, default="1"): str,
    }
)


class DeckhandConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Deckhand."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            team_id = user_input[CONF_TEAM_ID].strip()

            # Check that MQTT is configured
            if not self.hass.config_entries.async_entries("mqtt"):
                return self.async_abort(reason="mqtt_not_configured")

            # Check if already configured for this team
            await self.async_set_unique_id(f"deckhand_{team_id}")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Deckhand (Team {team_id})",
                data={CONF_TEAM_ID: team_id},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_mqtt(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle MQTT discovery.

        When HA's MQTT integration receives a message on deckhand/+/dial/+/status,
        it triggers this flow automatically (via the 'mqtt' key in manifest.json).
        """
        # Extract team_id from the topic
        topic = discovery_info.get("topic", "")
        parts = topic.split("/")
        if len(parts) >= 2:
            team_id = parts[1]
        else:
            team_id = "1"

        await self.async_set_unique_id(f"deckhand_{team_id}")
        self._abort_if_unique_id_configured()

        # Pre-fill the team_id for user confirmation
        self.context["team_id"] = team_id

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TEAM_ID, default=team_id): str,
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler for media_player auto-push bindings."""
        return DeckhandOptionsFlow(config_entry)


class DeckhandOptionsFlow(OptionsFlow):
    """Options flow: manage dial <-> media_player auto-push bindings.

    UI is a single-page menu with three actions:
      - "add": add a new (dial, media_player) pairing
      - "remove": drop one or more existing pairings
      - "done": save and exit

    Bindings are stored in ``entry.options["media_player_bindings"]`` as
    a list of dicts ``{"dial_device_id": str, "entity_id": str}``.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        # Intentionally do NOT assign self.config_entry — HA core provides
        # that property on the base class and setting it triggers a
        # deprecation warning on 2024.12+.
        self._entry_id = config_entry.entry_id
        existing = config_entry.options.get(CONF_MEDIA_PLAYER_BINDINGS, []) or []
        # Deep copy-ish: we mutate this as the user walks the flow.
        self._bindings: list[dict[str, str]] = [
            {
                CONF_BINDING_DIAL: b.get(CONF_BINDING_DIAL, ""),
                CONF_BINDING_ENTITY: b.get(CONF_BINDING_ENTITY, ""),
            }
            for b in existing
            if isinstance(b, dict)
            and b.get(CONF_BINDING_DIAL)
            and b.get(CONF_BINDING_ENTITY)
        ]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the menu of options actions."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["add", "remove", "done"],
        )

    async def async_step_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a new (dial, media_player) binding."""
        errors: dict[str, str] = {}
        if user_input is not None:
            dial_device_id = user_input.get(CONF_BINDING_DIAL, "").strip()
            entity_id = user_input.get(CONF_BINDING_ENTITY, "").strip()
            if not dial_device_id or not entity_id:
                errors["base"] = "missing_fields"
            elif not entity_id.startswith("media_player."):
                errors["base"] = "not_media_player"
            else:
                # Validate the dial belongs to THIS config entry so users
                # can't accidentally bind across Deckhand teams.
                registry = dr.async_get(self.hass)
                device = registry.async_get(dial_device_id)
                if device is None or self._entry_id not in device.config_entries:
                    errors["base"] = "unknown_dial"
                else:
                    # Replace any existing binding for the same entity —
                    # point-to-point semantics, last write wins.
                    self._bindings = [
                        b
                        for b in self._bindings
                        if b[CONF_BINDING_ENTITY] != entity_id
                    ]
                    self._bindings.append(
                        {
                            CONF_BINDING_DIAL: dial_device_id,
                            CONF_BINDING_ENTITY: entity_id,
                        }
                    )
                    return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required(CONF_BINDING_DIAL): DeviceSelector(
                    DeviceSelectorConfig(integration=DOMAIN)
                ),
                vol.Required(CONF_BINDING_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="media_player")
                ),
            }
        )
        return self.async_show_form(
            step_id="add",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "count": str(len(self._bindings)),
            },
        )

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove one or more existing bindings."""
        if not self._bindings:
            return await self.async_step_init()

        # Label each binding as "entity_id -> device_name" so the user
        # can tell them apart when several media_players are wired up.
        registry = dr.async_get(self.hass)
        labels: dict[str, str] = {}
        for idx, b in enumerate(self._bindings):
            key = str(idx)
            dev = registry.async_get(b[CONF_BINDING_DIAL])
            dev_label = (
                dev.name_by_user or dev.name or b[CONF_BINDING_DIAL]
                if dev
                else b[CONF_BINDING_DIAL]
            )
            labels[key] = f"{b[CONF_BINDING_ENTITY]} -> {dev_label}"

        if user_input is not None:
            to_remove = set(user_input.get("remove", []) or [])
            self._bindings = [
                b for idx, b in enumerate(self._bindings) if str(idx) not in to_remove
            ]
            return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Optional("remove", default=[]): vol.All(
                    [vol.In(list(labels.keys()))],
                )
            }
        )
        return self.async_show_form(
            step_id="remove",
            data_schema=schema,
            description_placeholders={
                "bindings": "\n".join(
                    f"- {idx}: {label}" for idx, label in labels.items()
                )
            },
        )

    async def async_step_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Persist the current bindings back to entry.options."""
        return self.async_create_entry(
            title="",
            data={CONF_MEDIA_PLAYER_BINDINGS: self._bindings},
        )
