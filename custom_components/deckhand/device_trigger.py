"""Device triggers for a Deckhand dial.

"When the Kitchen dial's button is pressed" without knowing the
``deckhand_dial_event`` wire names: the automation editor lists these under
the dial device. Each one listens to the same bus event every existing
automation does; the mapping from event to trigger lives in
``_automation.py`` (which also says why there is no rotation trigger and
why NFC identity comes only from the server relay).

Dial devices only. A Room has no events of its own: trigger on its dials.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.device_automation.exceptions import (
    InvalidDeviceAutomationConfig,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, Event, HassJob, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from . import _automation as auto
from ._device_lookup import dial_id_for_device
from .const import DOMAIN, EVENT_DIAL_EVENT

CONF_SUBTYPE = "subtype"
CONF_ITEM_LABEL = auto.CONF_ITEM_LABEL

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(list(auto.TRIGGER_TYPES)),
        vol.Optional(CONF_SUBTYPE): cv.string,
        vol.Optional(CONF_ITEM_LABEL): cv.string,
    }
)


def nfc_entity_unique_id(dial_id: str) -> str:
    """Unique id of the dial's NFC event entity (event.py)."""
    return f"deckhand_{dial_id}_nfc_event"


def _has_nfc(hass: HomeAssistant, dial_id: str) -> bool:
    """NFC triggers are offered once the dial has reported a tap.

    Most dials have no reader; the NFC event entity is created on a dial's
    first relayed tap, and its registry entry is the record that it has one.
    """
    registry = er.async_get(hass)
    return registry.async_get_entity_id("event", DOMAIN, nfc_entity_unique_id(dial_id)) is not None


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Schema, then a subtype that belongs to the type."""
    config = TRIGGER_SCHEMA(config)
    error = auto.validate_trigger(config[CONF_TYPE], config.get(CONF_SUBTYPE))
    if error:
        raise InvalidDeviceAutomationConfig(error)
    return config


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Every trigger a dial device offers."""
    dial_id = dial_id_for_device(hass, device_id)
    if dial_id is None:
        return []
    base = {CONF_PLATFORM: "device", CONF_DOMAIN: DOMAIN, CONF_DEVICE_ID: device_id}
    nfc = _has_nfc(hass, dial_id)
    triggers: list[dict[str, Any]] = []
    for trigger_type, subtypes in auto.TRIGGER_TYPES.items():
        if trigger_type == auto.TRIGGER_NFC and not nfc:
            continue
        if not subtypes:
            triggers.append({**base, CONF_TYPE: trigger_type})
            continue
        for subtype in subtypes:
            triggers.append({**base, CONF_TYPE: trigger_type, CONF_SUBTYPE: subtype})
    return triggers


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """An optional item label for the press and menu triggers."""
    if config.get(CONF_TYPE) in auto.LABEL_TRIGGERS:
        return {"extra_fields": vol.Schema({vol.Optional(CONF_ITEM_LABEL): cv.string})}
    return {}


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: Any,
    trigger_info: Any,
) -> CALLBACK_TYPE:
    """Listen for the dial's events and run the automation on a match."""
    device_id = config[CONF_DEVICE_ID]
    dial_id = dial_id_for_device(hass, device_id)
    trigger_type = config[CONF_TYPE]
    subtype = config.get(CONF_SUBTYPE)
    label = config.get(CONF_ITEM_LABEL)
    trigger_data = (trigger_info or {}).get("trigger_data") or {}
    job = HassJob(action, f"Deckhand {trigger_type} trigger")

    @callback
    def _handle(event: Event) -> None:
        data = event.data or {}
        if dial_id is None or data.get("dial_id") != dial_id:
            return
        if not auto.trigger_matches(trigger_type, subtype, data, label):
            return
        variables = {
            **trigger_data,
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
            CONF_SUBTYPE: subtype,
            "dial_id": dial_id,
            "event": event,
            "description": f"Deckhand {trigger_type.replace('_', ' ')}"
            + (f" ({subtype.replace('_', ' ')})" if subtype else ""),
            **auto.trigger_variables(data),
        }
        hass.async_run_hass_job(job, {"trigger": variables}, getattr(event, "context", None))

    return hass.bus.async_listen(EVENT_DIAL_EVENT, _handle)
