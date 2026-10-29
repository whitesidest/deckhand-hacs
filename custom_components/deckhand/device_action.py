"""Device actions for a Deckhand dial or Room.

The automation editor's "Device" action: announce, start a timer, push a
theme, Do Not Disturb on / off, show a message, mount a library face. Each
one calls the matching ``deckhand.*`` service with the device as its target
(``_automation.build_service_call``), so validation, the admin gate, the
``_src`` stamp and the wire format are exactly the service's.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_ACTION_BASE_SCHEMA
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.helpers.typing import ConfigType, TemplateVarsType

from . import _automation as auto
from ._device_lookup import deckhand_identifier, store_for_device
from .const import DOMAIN

ACTION_SCHEMA = DEVICE_ACTION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(list(auto.ACTION_TYPES)),
        vol.Optional("message"): cv.string,
        vol.Optional("from_name"): cv.string,
        vol.Optional("duration"): vol.Coerce(int),
        vol.Optional("snapshot_entity"): cv.entity_id,
        vol.Optional("image_url"): cv.string,
        vol.Optional("minutes"): vol.Coerce(float),
        vol.Optional("label"): cv.string,
        vol.Optional("name"): cv.string,
        vol.Optional("color"): cv.string,
        vol.Optional("theme"): cv.string,
        vol.Optional("text"): cv.string,
        vol.Optional("face"): cv.string,
    }
)


async def async_get_actions(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Every action a dial or Room offers.

    "Mount a library face" only once the team's face library has synced at
    least one face; there is nothing to pick before that.
    """
    if deckhand_identifier(hass, device_id) is None:
        return []
    faces = auto.library_face_names(store_for_device(hass, device_id))
    base = {CONF_DEVICE_ID: device_id, CONF_DOMAIN: DOMAIN}
    return [
        {**base, CONF_TYPE: action_type}
        for action_type in auto.ACTION_TYPES
        if action_type != auto.ACTION_LIBRARY_FACE or faces
    ]


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: TemplateVarsType,
    context: Context | None,
) -> None:
    """Run the action through its service, as the caller."""
    service, data = auto.build_service_call(config[CONF_TYPE], config)
    await hass.services.async_call(DOMAIN, service, data, blocking=True, context=context)


def _text(multiline: bool = False) -> Any:
    return selector.TextSelector(selector.TextSelectorConfig(multiline=multiline))


def _picker(options: list[str]) -> Any:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


async def async_get_action_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """The fields each action asks for."""
    action_type = config.get(CONF_TYPE)
    if action_type == auto.ACTION_ANNOUNCE:
        fields = {
            vol.Required("message"): _text(),
            vol.Optional("from_name"): _text(),
            vol.Optional("duration"): selector.NumberSelector(
                selector.NumberSelectorConfig(min=5, max=300, unit_of_measurement="s")
            ),
            vol.Optional("snapshot_entity"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="camera")
            ),
            vol.Optional("image_url"): _text(),
        }
    elif action_type == auto.ACTION_TIMER:
        fields = {
            vol.Required("minutes"): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=1440, step=1, unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional("label"): _text(),
            vol.Optional("name"): _text(),
            vol.Optional("color"): _text(),
        }
    elif action_type == auto.ACTION_THEME:
        store = store_for_device(hass, config[CONF_DEVICE_ID])
        fields = {vol.Required("theme"): _picker(auto.theme_options(store))}
    elif action_type == auto.ACTION_MESSAGE:
        fields = {vol.Required("text"): _text(multiline=True)}
    elif action_type == auto.ACTION_LIBRARY_FACE:
        store = store_for_device(hass, config[CONF_DEVICE_ID])
        fields = {vol.Required("face"): _picker(auto.library_face_names(store))}
    else:
        return {}
    return {"extra_fields": vol.Schema(fields)}
