"""Repairs fix flows.

**Dial not on the server** — the dial was deregistered or moved to another
server, so it will never report here again. The flow confirms, then deletes
its device from Home Assistant (``_issues.remove_dial_device``: the same
cleanup as the device page's Delete). It refuses while the dial is still
reporting live.

**Blueprints not imported**. The blueprints are not inside the
integration package (HACS installs ``custom_components/deckhand`` only), so
there is nothing to copy: the flow shows a My Home Assistant import link for
each one and, on Submit, checks ``config/blueprints/automation/`` again. The
issue closes once any of them is there; until then the form says so (or the
person can ignore the issue).
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from . import _issues, _repairs
from .const import DOMAIN


class RemoveDialRepairFlow(RepairsFlow):
    """Delete a dial the server no longer has from Home Assistant."""

    def __init__(self, dial_id: str, entry_id: str | None) -> None:
        super().__init__()
        self.dial_id = dial_id
        self.entry_id = entry_id

    def _store(self) -> dict[str, Any] | None:
        store = self.hass.data.get(DOMAIN, {}).get(self.entry_id) if self.entry_id else None
        return store if isinstance(store, dict) else None

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        store = self._store()
        if user_input is not None:
            if _issues.remove_dial_device(self.hass, store, self.dial_id):
                return self.async_create_entry(data={})
            errors["base"] = "dial_reporting"
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"dial": _issues.dial_name(store or {}, self.dial_id)},
        )


class BlueprintsRepairFlow(RepairsFlow):
    """Import the shipped blueprints, then confirm."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if await _issues.async_blueprints_imported(self.hass):
                return self.async_create_entry(data={})
            errors["base"] = "still_missing"
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"links": _repairs.blueprint_links_markdown()},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """The flow for a fixable Deckhand issue."""
    if issue_id == _issues.BLUEPRINTS_ISSUE_ID:
        return BlueprintsRepairFlow()
    if issue_id.startswith(_issues.NOT_ON_SERVER_PREFIX):
        data = data or {}
        dial_id = data.get("dial_id") or issue_id.removeprefix(_issues.NOT_ON_SERVER_PREFIX)
        return RemoveDialRepairFlow(str(dial_id), data.get("entry_id"))
    raise ValueError(f"Deckhand issue {issue_id} has no fix flow")
