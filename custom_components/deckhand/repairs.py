"""Repairs fix flows.

Only the blueprints issue is fixable. The integration package carries copies
of the shipped blueprints (HACS installs ``custom_components/deckhand``
only), so Submit installs the missing ones into
``config/blueprints/automation/deckhand/`` — never overwriting a file that
is already there — and closes the issue. The form keeps a My Home Assistant
import link for each one as the alternative. If a copy fails the form says
so and the issue stays (or the person can ignore it).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from . import _issues, _repairs

_LOGGER = logging.getLogger(__name__)


class BlueprintsRepairFlow(RepairsFlow):
    """Install the shipped blueprints on Submit."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _issues.async_install_blueprints(self.hass)
            except OSError as err:
                _LOGGER.warning("Could not install the Deckhand blueprints: %s", err)
                errors["base"] = "install_failed"
            else:
                # Re-scans and deletes the issue once one is on disk.
                if await _issues.async_check_blueprints(self.hass):
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
