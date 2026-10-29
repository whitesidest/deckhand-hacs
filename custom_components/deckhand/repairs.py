"""Repairs fix flows.

Only the blueprints issue is fixable. The blueprints are not inside the
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
    raise ValueError(f"Deckhand issue {issue_id} has no fix flow")
