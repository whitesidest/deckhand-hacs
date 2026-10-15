"""Config flow for Deckhand integration."""
from __future__ import annotations

import logging
import re
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


# A team id is ONE MQTT topic level: ``deckhand/<team_id>/dial/...``. A
# ``/`` would shift every level the handlers index by position, and ``+`` /
# ``#`` are wildcards that would subscribe this entry to other teams.
# Helm uses integer ids ("1"), Console the synthetic id "local".
_TEAM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _normalize_team_id(raw: Any) -> str | None:
    """Return the stripped team id, or None when it can't be a topic level."""
    if not isinstance(raw, str):
        return None
    team_id = raw.strip()
    return team_id if _TEAM_ID_RE.match(team_id) else None


def _entry_title(team_id: str) -> str:
    """The config entry title convention: ``Deckhand (Team <id>)``."""
    return f"Deckhand (Team {team_id})"


def _unique_id(team_id: str) -> str:
    """The config entry unique_id convention: one entry per team."""
    return f"deckhand_{team_id}"


class DeckhandConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Deckhand."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            team_id = _normalize_team_id(user_input.get(CONF_TEAM_ID))

            # Check that MQTT is configured
            if not self.hass.config_entries.async_entries("mqtt"):
                return self.async_abort(reason="mqtt_not_configured")

            if team_id is None:
                errors[CONF_TEAM_ID] = "invalid_team_id"
            else:
                # Check if already configured for this team
                await self.async_set_unique_id(_unique_id(team_id))
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=_entry_title(team_id),
                    data={CONF_TEAM_ID: team_id},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_mqtt(self, discovery_info: Any) -> ConfigFlowResult:
        """Handle MQTT discovery.

        When HA's MQTT integration receives a message on deckhand/+/dial/+/status,
        it triggers this flow automatically (via the 'mqtt' key in manifest.json).

        ``discovery_info`` is HA's ``MqttServiceInfo`` DATACLASS on current
        cores. It used to be a plain dict, and this handler called ``.get()``
        on it — which raises AttributeError against a dataclass.

        That is expensive here in a way it would not be elsewhere. This
        integration subscribes to ``deckhand/+/dial/+/status``, so EVERY status
        message from EVERY dial enters this flow, and HA builds a full
        traceback on MainThread for each one. A normal fleet produced ~18
        tracebacks per minute — 16,325 in 16 hours, 65% of every line in the
        core log (reported 2026-09-04).

        Read defensively rather than switching to a bare ``.topic``: the
        manifest still declares support back to HA 2024.1.0, where this really
        was a dict, and getattr costs nothing. Deliberately NOT importing
        MqttServiceInfo for this — it has moved between
        ``homeassistant.components.mqtt`` and
        ``homeassistant.helpers.service_info.mqtt`` across the supported
        range, and a bad import here fails the whole config flow at load,
        which is strictly worse than the bug it would be fixing.
        """
        # Extract team_id from the topic
        topic = getattr(discovery_info, "topic", None)
        if topic is None and isinstance(discovery_info, dict):
            # Pre-dataclass cores (and the unit tests' fixtures).
            topic = discovery_info.get("topic", "")
        topic = topic or ""
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change an existing entry's team id IN PLACE.

        The Helm -> Console cutover moves a home from ``deckhand/1/...`` to
        ``deckhand/local/...``. Deleting the entry and adding a new one would
        recreate every dial and Room device with NEW HA device ids, breaking
        every automation that targets them. Reconfigure keeps the entry_id,
        so the devices (keyed by dial_id and ``group:<id>``, neither of which
        contains the team id) and their entities survive untouched; the
        reload re-subscribes everything under the new prefix.

        Offered by HA 2024.4+ (three-dot menu on the entry -> Reconfigure).
        Written against both the 2024.4 API and the 2024.11 helpers.
        """
        entry = self._deckhand_reconfigure_entry()
        if entry is None:
            return self.async_abort(reason="reconfigure_entry_missing")

        current = str(entry.data.get(CONF_TEAM_ID, ""))
        errors: dict[str, str] = {}

        if user_input is not None:
            team_id = _normalize_team_id(user_input.get(CONF_TEAM_ID))
            if team_id is None:
                errors[CONF_TEAM_ID] = "invalid_team_id"
            elif self._team_id_in_use(team_id, entry.entry_id):
                errors[CONF_TEAM_ID] = "team_id_in_use"
            else:
                return self._apply_reconfigure(entry, team_id)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_TEAM_ID, default=current): str}
            ),
            errors=errors,
            description_placeholders={"current_team_id": current},
        )

    def _deckhand_reconfigure_entry(self) -> ConfigEntry | None:
        """The entry being reconfigured, on any HA that offers the step."""
        getter = getattr(self, "_get_reconfigure_entry", None)  # HA 2024.11+
        if getter is not None:
            try:
                return getter()
            except Exception:  # noqa: BLE001 - fall back to the context
                _LOGGER.debug("_get_reconfigure_entry failed", exc_info=True)
        entry_id = self.context.get("entry_id")
        if not entry_id:
            return None
        return self.hass.config_entries.async_get_entry(entry_id)

    def _team_id_in_use(self, team_id: str, own_entry_id: str) -> bool:
        """True if ANOTHER Deckhand entry already serves this team."""
        for other in self.hass.config_entries.async_entries(DOMAIN):
            if other.entry_id == own_entry_id:
                continue
            if (
                str(other.data.get(CONF_TEAM_ID, "")) == team_id
                or other.unique_id == _unique_id(team_id)
            ):
                return True
        return False

    def _apply_reconfigure(
        self, entry: ConfigEntry, team_id: str
    ) -> ConfigFlowResult:
        """Rewrite team_id, title and unique_id in place, then reload.

        ``async_update_entry`` with ``unique_id=`` is the one call that is
        stable across every HA version that offers reconfigure, so the entry
        is rewritten here and the reload/abort helper is called with no
        changes of its own (its keyword set differs between 2024.4 and
        2024.11; ``reason`` and the entry are common to both).
        """
        new_unique_id = _unique_id(team_id)
        self.hass.config_entries.async_update_entry(
            entry,
            title=_entry_title(team_id),
            data={**entry.data, CONF_TEAM_ID: team_id},
            unique_id=new_unique_id,
        )
        self._abort_pending_discovery(new_unique_id)

        reload_and_abort = getattr(self, "async_update_reload_and_abort", None)
        if reload_and_abort is not None:
            return reload_and_abort(entry, reason="reconfigure_successful")
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(entry.entry_id)
        )
        return self.async_abort(reason="reconfigure_successful")

    def _abort_pending_discovery(self, unique_id: str) -> None:
        """Drop a "Discovered" card for the team this entry now serves.

        The manifest's MQTT discovery sees the new prefix's dials before the
        cutover and offers them as a new integration. Once this entry owns
        that team the card is a trap: accepting it would fail, and it would
        sit in the UI until restart. Best effort only.
        """
        try:
            flow_manager = self.hass.config_entries.flow
            for flow in flow_manager.async_progress_by_handler(DOMAIN):
                if flow.get("flow_id") == self.flow_id:
                    continue
                if (flow.get("context") or {}).get("unique_id") == unique_id:
                    flow_manager.async_abort(flow["flow_id"])
        except Exception:  # noqa: BLE001 - never fail the reconfigure on this
            _LOGGER.debug("could not abort pending discovery flows", exc_info=True)

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
