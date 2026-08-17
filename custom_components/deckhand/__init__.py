"""Deckhand Smart Dial integration for Home Assistant."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

try:  # get_url is in core since 2021 but guard for older installs
    from homeassistant.helpers.network import NoURLAvailableError, get_url
except ImportError:  # pragma: no cover - defensive fallback
    get_url = None  # type: ignore[assignment]

    class NoURLAvailableError(Exception):  # type: ignore[no-redef]
        """Fallback when helpers.network is unavailable."""

from .const import (
    CONF_MEDIA_PLAYER_BINDINGS,
    CONF_TEAM_ID,
    DEFAULT_TRANSITION,
    DOMAIN,
    MANUFACTURER,
    HARDWARE_MODELS,
    MEDIA_PLAYER_DEBOUNCE_S,
    PERIMETER_MAX_BINDINGS,
    PERIMETER_TREATMENTS,
    PLATFORMS,
    TOPIC_CMD_ANNOUNCE,
    TOPIC_CMD_FACE_CONFIG,
    TOPIC_CMD_FACE_MOUNT,
    TOPIC_CMD_FACE_UNMOUNT,
    TOPIC_ALARM_REQUEST,
    TOPIC_CREDENTIAL_REQUEST,
    TOPIC_SCHEDULE_REQUEST,
    TOPIC_SETTINGS_REQUEST,
    TOPIC_MENU_REQUEST,
    TOPIC_INVITATION_REQUEST,
    TOPIC_CMD_FACE_STATE,
    TOPIC_CMD_DND,
    TOPIC_CMD_NOW_PLAYING,
    TOPIC_CMD_OVERLAY,
    TOPIC_CMD_REBOOT,
    TOPIC_CMD_SENSOR_VALUE,
    TOPIC_CMD_SUNSET,
    TOPIC_CMD_THEME,
    TOPIC_HACS_PRESENCE,
    TOPIC_SENSOR_WATCHES,
    TOPIC_STATUS,
    TOPIC_THEME_REQUEST,
    TRANSITIONS,
    SERVER_RESOLVED_THEMES,
)
from ._units import (  # vendored copy of deckhand_sdk/deckhand/units.py
    format_sensor_value as _format_sensor_value_tuple,
)
from .image_push import publish_image_to_dial
from ._media_control import media_service_for_dial_event
from ._now_playing import now_playing_controls, now_playing_fields
from ._presence import PRESENCE_INTERVAL_S, build_presence_payload


def _dispatch_dial_media_control(hass: HomeAssistant, envelope: dict) -> None:
    """Drive a media_player from a dial button_press event.

    ``envelope`` is the raw dial event ``{type, ts, payload}``. When the
    dial is on the Now-Playing / Volume face, a tap (play/pause) or a
    turn (volume) arrives as a ``button_press`` whose inner ``item_id``
    is the HA ``media_player.*`` entity cmd/now_playing was sourced from
    (see ``_extract_now_playing`` → ``payload["entity_id"]``). The pure
    mapping lives in ``_media_control``; here we just fire the resulting
    service call. No-op for any other event — purely additive to the raw
    event re-fire in ``_handle_event``.
    """
    result = media_service_for_dial_event(envelope)
    if result is None:
        return
    service, service_data = result
    hass.async_create_task(
        hass.services.async_call("media_player", service, service_data, blocking=False)
    )

# IANA → POSIX TZ map. The firmware hands the value straight to
# setenv("TZ", ...) which only understands POSIX, not IANA, so we have
# to translate before publishing. Mirrors the maps in Helm
# (apps/devices/timezones.py) and the Console
# (services/timezones.py) — keep all three in sync when adding zones.
_IANA_TO_POSIX = {
    "America/Los_Angeles": "PST8PDT,M3.2.0/2,M11.1.0/2",
    "America/Vancouver": "PST8PDT,M3.2.0/2,M11.1.0/2",
    "America/Denver": "MST7MDT,M3.2.0/2,M11.1.0/2",
    "America/Phoenix": "MST7",
    "America/Chicago": "CST6CDT,M3.2.0/2,M11.1.0/2",
    "America/Mexico_City": "CST6CDT,M4.1.0,M10.5.0",
    "America/New_York": "EST5EDT,M3.2.0/2,M11.1.0/2",
    "America/Toronto": "EST5EDT,M3.2.0/2,M11.1.0/2",
    "America/Anchorage": "AKST9AKDT,M3.2.0/2,M11.1.0/2",
    "America/Sao_Paulo": "BRT3",
    "Pacific/Honolulu": "HST10",
    "Europe/London": "GMT0BST,M3.5.0/1,M10.5.0/2",
    "Europe/Paris": "CET-1CEST,M3.5.0/2,M10.5.0/3",
    "Europe/Berlin": "CET-1CEST,M3.5.0/2,M10.5.0/3",
    "Europe/Madrid": "CET-1CEST,M3.5.0/2,M10.5.0/3",
    "Europe/Rome": "CET-1CEST,M3.5.0/2,M10.5.0/3",
    "Europe/Amsterdam": "CET-1CEST,M3.5.0/2,M10.5.0/3",
    "Europe/Stockholm": "CET-1CEST,M3.5.0/2,M10.5.0/3",
    "Europe/Athens": "EET-2EEST,M3.5.0/3,M10.5.0/4",
    "Africa/Cairo": "EET-2",
    "Africa/Johannesburg": "SAST-2",
    "Asia/Tokyo": "JST-9",
    "Asia/Shanghai": "CST-8",
    "Asia/Hong_Kong": "HKT-8",
    "Asia/Singapore": "SGT-8",
    "Asia/Seoul": "KST-9",
    "Asia/Dubai": "GST-4",
    "Asia/Kolkata": "IST-5:30",
    "Australia/Sydney": "AEST-10AEDT,M10.1.0,M4.1.0/3",
    "Australia/Melbourne": "AEST-10AEDT,M10.1.0,M4.1.0/3",
    "Australia/Brisbane": "AEST-10",
    "Australia/Perth": "AWST-8",
    "Pacific/Auckland": "NZST-12NZDT,M9.5.0,M4.1.0/3",
    "UTC": "UTC0",
}

# apply_overlay is a TRANSIENT RUNTIME OVERRIDE — distinct from the
# persistent Theme/Face state. The firmware's apply_display_overlay
# handler (deckhand-firmware/src/main.cpp ~340) merges all of these
# fields onto the live dial state without touching NVS. They survive
# until the next cmd/theme or cmd/face/<kind>/mount push replaces them
# (or until the optional ttl_s timer expires).
#
# Face-shaping fields are kept here because real automations need them
# ("when air quality drops below X, flip Tyler into a 4-quad sensor
# face for an hour"). For persistent face state, use ``mount_face`` /
# ``mount_perimeter_pulse`` — those publish retained
# cmd/face/<kind>/mount messages that survive reboots.
_OVERLAY_STRING_FIELDS = (
    "subtitle_text",
    "home_message",
    "sensor_entity_id",
    "sensor_label",
    "framecast_frame_id",
)
_OVERLAY_SUBTITLE_MODES = {
    "theme", "custom", "date", "date_year", "ical_next_event", "none",
}
_OVERLAY_HOME_FACES = {
    "theme", "clock", "message", "sensor", "image", "blank",
    "volume", "framecast",
}
_OVERLAY_QUAD_SLOTS = (2, 3, 4)

_LOGGER = logging.getLogger(__name__)

type DeckhandConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: DeckhandConfigEntry) -> bool:
    """Set up Deckhand from a config entry."""
    team_id = entry.data[CONF_TEAM_ID]

    # Store discovered dials in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "team_id": team_id,
        "dials": {},  # dial_id -> last heartbeat data
        # dial_id -> friendly label captured from retained cmd/config.
        # Kept separate from `dials` so an early config-only message
        # doesn't masquerade as a discovered dial.
        "labels": {},
        # Team's catalog of [{slug, name, is_system}] from the retained
        # `deckhand/{team_id}/themes/list` topic. Empty until the first
        # message arrives — the select entity falls back to
        # FALLBACK_THEMES so the picker is never blank.
        "themes": [],
        # group_id (str of int) -> {id, slug, name, dial_ids: [...]} from
        # the retained `deckhand/{team_id}/groups/list` topic. Used by
        # the service handlers to fan a single Room device target out to
        # every member dial. Each entry is also registered as an HA
        # device with identifier (DOMAIN, f"group:{id}") so the existing
        # device-selector picks it up alongside individual dials.
        "groups": {},
        # (dial_id, entity_id) -> {"ts": float, "fingerprint": tuple}
        "_media_player_debounce": {},
        # Disposable that unsubs the active state-change listener. Swapped
        # out on options update so the binding list stays in sync.
        "_media_player_unsub": None,
        # dial_id -> {"entity_ids": [...], "unsub": callable}
        #
        # Tracks the sensor-face entities Helm/Console asked us to watch
        # for this dial (via the retained ``sensor_watches`` topic). Kept
        # separate from ``_sensor_bindings`` (which is the apply_overlay
        # transient binding map) so the two paths can coexist — overlay
        # can pin an entity on top of whatever the face is already
        # watching, and an overlay unbind doesn't tear down the
        # persistent face listeners.
        "_sensor_watch_bindings": {},
    }

    # Subscribe to status heartbeats for dial discovery
    status_topic = TOPIC_STATUS.format(team_id=team_id)

    @callback
    def _handle_status(msg: mqtt.ReceiveMessage) -> None:
        """Handle a dial status heartbeat."""
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            _LOGGER.warning("Invalid JSON on %s", msg.topic)
            return

        # Trust the topic, not the payload. The dial_id from the topic
        # was authenticated by the broker ACL; the `device_id` field in
        # the body is attacker-controllable if the broker is shared
        # with other tenants / integrations.
        parts = msg.topic.split("/")
        topic_dial_id = parts[3] if len(parts) >= 4 else ""
        payload_device_id = payload.get("device_id") or ""
        if not topic_dial_id:
            _LOGGER.warning("Status message has malformed topic: %s", msg.topic)
            return
        if payload_device_id and payload_device_id != topic_dial_id:
            _LOGGER.warning(
                "Status device_id=%s does not match topic slot=%s on %s — dropping",
                payload_device_id,
                topic_dial_id,
                msg.topic,
            )
            return
        dial_id = topic_dial_id

        store = hass.data[DOMAIN].get(entry.entry_id)
        if not store:
            return

        is_new = dial_id not in store["dials"]
        enriched = {
            **payload,
            "_last_seen": datetime.now().isoformat(),
        }
        # Layer in any friendly label captured from retained cmd/config.
        label = store.get("labels", {}).get(dial_id)
        if label:
            enriched["_label"] = label
        store["dials"][dial_id] = enriched

        if is_new:
            _LOGGER.info("Discovered new Deckhand dial: %s", dial_id)
            # Register the device
            _register_device(hass, entry, dial_id, payload)
            # Signal platforms to add entities for this dial
            hass.async_create_task(
                _async_add_dial_entities(hass, entry, dial_id, enriched)
            )

        # Fire an event so existing entities can update. Include the
        # _last_seen timestamp so entities can compute availability.
        hass.bus.async_fire(
            f"{DOMAIN}_status_update",
            {"dial_id": dial_id, "data": enriched},
        )

    entry.async_on_unload(
        await mqtt.async_subscribe(hass, status_topic, _handle_status, qos=0)
    )

    # Subscribe to retained cmd/config — Helm/Console publish the dial's
    # friendly label here, so we use it as the device name (rather than
    # the raw dial_id like "deckhand-A1B2C3"). The retained message means
    # we get the label immediately on subscribe even if no fresh config
    # push happens during this HA session.
    config_topic = f"deckhand/{team_id}/dial/+/cmd/config"

    @callback
    def _handle_config(msg: mqtt.ReceiveMessage) -> None:
        """Pick the dial's friendly label out of a cmd/config payload."""
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            return
        label = payload.get("label")
        if not isinstance(label, str) or not label.strip():
            return
        label = label.strip()

        parts = msg.topic.split("/")
        if len(parts) < 5:
            return
        dial_id = parts[3]

        store = hass.data[DOMAIN].get(entry.entry_id)
        if not store:
            return

        prev_label = store["labels"].get(dial_id)
        store["labels"][dial_id] = label

        # If the dial has already heartbeated, mirror the label onto its
        # live record so `update_from_status` picks it up on the next tick.
        if dial_id in store["dials"]:
            store["dials"][dial_id]["_label"] = label

        # Update the device-registry entry so the name shows up in the HA
        # UI immediately (sensor cards, automation pickers, etc.) without
        # waiting for a restart. name_by_user wins in the UI if the user
        # has manually renamed the device — we only set the integration's
        # `name` field.
        if prev_label != label:
            registry = dr.async_get(hass)
            device = registry.async_get_device(identifiers={(DOMAIN, dial_id)})
            if device is not None:
                registry.async_update_device(device.id, name=label)

    entry.async_on_unload(
        await mqtt.async_subscribe(hass, config_topic, _handle_config, qos=0)
    )

    # Subscribe to the team's published themes catalog. Helm + Console
    # republish this retained whenever a theme is created/edited/deleted
    # so HACS can offer a real per-dial picker that reflects the team's
    # *custom* themes. The service-yaml theme field is now a free-text
    # selector — firmware validates the slug. Empty/missing topic falls
    # back to FALLBACK_THEMES so the Select entity is never blank.
    themes_topic = f"deckhand/{team_id}/themes/list"

    @callback
    def _handle_themes_list(msg: mqtt.ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            _LOGGER.warning("Invalid JSON on %s", msg.topic)
            return
        themes = payload.get("themes")
        if not isinstance(themes, list):
            return
        store = hass.data[DOMAIN].get(entry.entry_id)
        if not store:
            return
        # Guardrails against a hostile broker publisher ballooning the
        # picker or injecting display strings. Caps mirror the
        # firmware/UI limits — anything beyond these would be junk.
        import re

        MAX_THEMES = 256
        MAX_SLUG_LEN = 64
        MAX_NAME_LEN = 128
        SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

        # Normalize to a stable {slug, name, is_system} list. Drop
        # entries missing a slug so a malformed payload can't strand
        # the picker on a blank option.
        normalized = []
        for t in themes[:MAX_THEMES]:
            if not isinstance(t, dict):
                continue
            slug = t.get("slug")
            if not isinstance(slug, str) or not SLUG_RE.match(slug):
                continue
            name = str(t.get("name") or slug)[:MAX_NAME_LEN]
            normalized.append({
                "slug": slug[:MAX_SLUG_LEN],
                "name": name,
                "is_system": bool(t.get("is_system", False)),
            })
        store["themes"] = normalized
        # Tell every dial-Theme entity to re-read the cache so users
        # see the new theme appear without a HA restart.
        from homeassistant.helpers.dispatcher import async_dispatcher_send
        async_dispatcher_send(hass, f"{DOMAIN}_themes_updated")
        _LOGGER.info("Themes catalog updated: %d themes for team %s",
                     len(normalized), team_id)

    entry.async_on_unload(
        await mqtt.async_subscribe(hass, themes_topic, _handle_themes_list, qos=0)
    )

    # Subscribe to the team's published rooms catalog. Helm publishes
    # this retained whenever a DialGroup is created/edited/deleted or a
    # dial is added/removed/renamed so HACS can register each room as a
    # Home Assistant device. Service handlers expand a Room device_id
    # to its member dial_ids and fan the publish out across the group.
    groups_topic = f"deckhand/{team_id}/groups/list"

    @callback
    def _handle_groups_list(msg: mqtt.ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            _LOGGER.warning("Invalid JSON on %s", msg.topic)
            return
        groups = payload.get("groups")
        if not isinstance(groups, list):
            return
        store = hass.data[DOMAIN].get(entry.entry_id)
        if not store:
            return

        # Same guardrails as themes — a hostile broker publisher
        # shouldn't be able to balloon the device registry or inject
        # control characters into device names.
        MAX_GROUPS = 256
        MAX_NAME_LEN = 128
        MAX_DIALS_PER_GROUP = 256

        normalized: dict[str, dict[str, Any]] = {}
        for g in groups[:MAX_GROUPS]:
            if not isinstance(g, dict):
                continue
            gid = g.get("id")
            if not isinstance(gid, int):
                continue
            name = str(g.get("name") or f"Room {gid}")[:MAX_NAME_LEN]
            slug = str(g.get("slug") or "")[:MAX_NAME_LEN]
            raw_dials = g.get("dial_ids") or []
            if not isinstance(raw_dials, list):
                raw_dials = []
            dial_ids = [
                str(d)[:64]
                for d in raw_dials[:MAX_DIALS_PER_GROUP]
                if isinstance(d, str) and d.strip()
            ]
            normalized[str(gid)] = {
                "id": gid,
                "slug": slug,
                "name": name,
                "dial_ids": dial_ids,
            }

        prev_ids = set(store["groups"].keys())
        store["groups"] = normalized

        # Register each group as an HA device so it appears alongside
        # individual dials in the device selector. Identifier prefix
        # `group:` namespaces it from raw dial_ids and serves as the
        # disambiguator for _resolve_dial.
        #
        # The device picker dropdown doesn't render per-device icons or
        # the `model` field prominently, so the operator picking a
        # "Push Theme" target sees rooms and dials in one flat list with
        # no visual distinction. We bake the differentiation into the
        # name itself — a leading 🏠 glyph as a pseudo-icon and a
        # trailing "(Room)" suffix so the picker reads e.g.
        # "🏠 Office (Room)" next to "Tyler Dev (DECK-3140)".
        registry = dr.async_get(hass)
        for gid, group in normalized.items():
            registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"group:{gid}")},
                manufacturer=MANUFACTURER,
                model="Room",
                name=f"🏠 {group['name']} (Room)",
            )

        # Deregister rooms that vanished from the published list — the
        # operator deleted them in Helm or moved them to a different
        # team. Leaving them stranded would surface dead targets in the
        # service picker.
        removed = prev_ids - set(normalized.keys())
        for gid in removed:
            device = registry.async_get_device(identifiers={(DOMAIN, f"group:{gid}")})
            if device is not None:
                registry.async_remove_device(device.id)

        _LOGGER.info(
            "Rooms catalog updated: %d groups for team %s (added/refreshed=%d, removed=%d)",
            len(normalized), team_id, len(normalized), len(removed),
        )

    entry.async_on_unload(
        await mqtt.async_subscribe(hass, groups_topic, _handle_groups_list, qos=0)
    )

    # Subscribe to events for automation triggers
    event_topic = f"deckhand/{team_id}/dial/+/event"

    @callback
    def _handle_event(msg: mqtt.ReceiveMessage) -> None:
        """Handle a dial event (button press, rotation, etc.)."""
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            return

        # Extract dial_id from topic: deckhand/{team}/dial/{dial_id}/event
        parts = msg.topic.split("/")
        if len(parts) >= 5:
            dial_id = parts[3]
        else:
            return

        # Fire as HA event for automations
        hass.bus.async_fire(
            f"{DOMAIN}_dial_event",
            {
                "dial_id": dial_id,
                "type": payload.get("type"),
                "payload": payload.get("payload", {}),
                "ts": payload.get("ts"),
            },
        )

        # Media control loopback. When a dial on the Now-Playing / Volume
        # face is tapped (play/pause) or turned (volume), the firmware
        # publishes a button_press whose item_id is the HA media_player
        # entity that cmd/now_playing was sourced from. Dispatch it to the
        # matching media_player service so the dial actually drives the
        # speaker. Handled in a module-level helper (not inline) so this
        # raw re-fire block stays free of the enriched identity keys the
        # NFC contract test pins — see tests/test_nfc_event_paths.py.
        _dispatch_dial_media_control(hass, payload)

    entry.async_on_unload(
        await mqtt.async_subscribe(hass, event_topic, _handle_event, qos=0)
    )

    # Subscribe to the HA event bus for dial lifecycle events fired by
    # Helm (apps.devices.services.lifecycle._fan_out → fire_ha_event_task
    # → HA REST). A `deregister` event must remove the dial from HA's
    # device registry — otherwise dials decommissioned in Helm linger as
    # dead targets in the Push Theme / Send Announcement pickers and the
    # operator can't tell which dials are actually live. A `register`
    # event is a no-op here because the existing status-topic
    # subscription auto-registers the device on the next status frame.
    @callback
    def _handle_lifecycle(event) -> None:
        data = event.data or {}
        dial_id = data.get("dial_id")
        if not dial_id:
            return
        if data.get("event_type") != "deregister":
            return
        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, dial_id)})
        if device is None:
            _LOGGER.info("Lifecycle deregister for %s — already absent from device registry", dial_id)
            return
        _LOGGER.info("Lifecycle deregister for %s — removing from device registry", dial_id)
        registry.async_remove_device(device.id)

    entry.async_on_unload(
        hass.bus.async_listen(f"{DOMAIN}_dial_event", _handle_lifecycle)
    )

    # Subscribe to the retained per-dial sensor_watches list. Helm and
    # Console publish this every time a dial's sensor face mounts or
    # changes its bound entities. We register an
    # ``async_track_state_change_event`` listener per entity so HA state
    # changes get pushed to the dial as cmd/sensor_value within a second
    # — the realtime path that replaces the 60s backend poll for users
    # with HACS installed.
    sensor_watches_topic = TOPIC_SENSOR_WATCHES.format(team_id=team_id)

    @callback
    def _handle_sensor_watches(msg: mqtt.ReceiveMessage) -> None:
        """Refresh the per-dial sensor-face listener set."""
        parts = msg.topic.split("/")
        if len(parts) < 5:
            return
        dial_id = parts[3]

        store = hass.data[DOMAIN].get(entry.entry_id)
        if store is None:
            return

        # Empty retained payload = "no sensor face on this dial right
        # now" — tear down whatever listeners we had for it.
        raw = msg.payload
        if not raw:
            _set_sensor_watch_listeners(hass, entry, dial_id, team_id, [])
            return

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            _LOGGER.warning("Invalid JSON on %s", msg.topic)
            return

        entities = payload.get("entities")
        if not isinstance(entities, list):
            return

        # Normalise + guard against a hostile publisher: cap the list,
        # drop non-string entries, and clip overlong strings to the
        # firmware's field widths. Mirrors the apply_overlay validator.
        watch_list: list[tuple[str, str]] = []
        for item in entities[:64]:
            if not isinstance(item, dict):
                continue
            eid = item.get("entity_id")
            if not isinstance(eid, str) or not eid:
                continue
            lbl = item.get("label") or ""
            if not isinstance(lbl, str):
                lbl = ""
            watch_list.append((eid[:128], lbl[:64]))

        _set_sensor_watch_listeners(hass, entry, dial_id, team_id, watch_list)

    entry.async_on_unload(
        await mqtt.async_subscribe(hass, sensor_watches_topic, _handle_sensor_watches, qos=0)
    )

    # Set up entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    _register_services(hass, entry)

    # Register state-change listeners for any configured media_player
    # bindings, and keep them in sync when the options flow changes.
    _reload_media_player_listeners(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    # ── R1: executor-arbitration presence beacon ─────────────────────
    # Publish a retained per-team heartbeat advertising the domains this
    # HACS build actuates locally (``_presence.PRESENCE_OWNS`` — media_player
    # today). Helm/Console DEFER those domains while the beacon is fresh so
    # a media tap isn't double-executed (server-side AND in-HA). Seed it
    # immediately (a running Helm/Console defers on the next event), then
    # re-stamp every PRESENCE_INTERVAL_S. Retained + embedded wall-clock
    # ``ts`` makes staleness detectable across listener restarts. Graceful
    # ``async_unload_entry`` empty-clears it for instant server resume.
    presence_topic = TOPIC_HACS_PRESENCE.format(team_id=team_id)

    async def _publish_presence(_now: datetime | None = None) -> None:
        payload = build_presence_payload(entry.entry_id)
        await mqtt.async_publish(
            hass, presence_topic, json.dumps(payload), qos=1, retain=True
        )

    await _publish_presence()
    entry.async_on_unload(
        async_track_time_interval(
            hass, _publish_presence, timedelta(seconds=PRESENCE_INTERVAL_S)
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: DeckhandConfigEntry) -> bool:
    """Unload a Deckhand config entry."""
    # R1: graceful stand-down — empty-clear the retained presence beacon so
    # Helm/Console resume authority instantly (no waiting out the 90s TTL)
    # when HACS is removed/restarted cleanly. Same empty-retained convention
    # as sensor_watches. Best-effort: teardown must never raise on this.
    try:
        await mqtt.async_publish(
            hass,
            TOPIC_HACS_PRESENCE.format(team_id=entry.data[CONF_TEAM_ID]),
            "",
            qos=1,
            retain=True,
        )
    except Exception:  # noqa: BLE001 - unload path must not raise
        _LOGGER.debug("presence beacon clear on unload failed", exc_info=True)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        store = hass.data[DOMAIN].pop(entry.entry_id, None)
        if store and store.get("_media_player_unsub"):
            store["_media_player_unsub"]()
        for binding in (store or {}).get("_sensor_bindings", {}).values():
            unsub = binding.get("unsub")
            if unsub:
                unsub()
        for binding in (store or {}).get("_sensor_watch_bindings", {}).values():
            unsub = binding.get("unsub")
            if unsub:
                unsub()
    return unload_ok


async def _async_update_options(
    hass: HomeAssistant, entry: DeckhandConfigEntry
) -> None:
    """Re-register media_player listeners when options change."""
    _reload_media_player_listeners(hass, entry)


@callback
def _register_device(
    hass: HomeAssistant,
    entry: DeckhandConfigEntry,
    dial_id: str,
    data: dict[str, Any],
) -> None:
    """Register or update a Deckhand device in the device registry."""
    hw_type = data.get("hardware_type", "unknown")
    model = HARDWARE_MODELS.get(hw_type, hw_type)

    # Prefer the friendly label captured from cmd/config (see _handle_config).
    # Falls back to the raw dial_id when no label has been seen yet.
    # The dial_id is appended in parentheses so the device picker
    # disambiguates dials that share a label and so the operator can
    # scan a long list by the canonical DECK-xxxx slug.
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    label = store.get("labels", {}).get(dial_id)
    name = f"{label} ({dial_id})" if label else f"Deckhand {dial_id}"

    # Only build a configuration_url when the IP looks like a valid
    # address. A hostile publisher with MQTT access could otherwise
    # set `ip` to an arbitrary string that becomes a clickable link
    # in the HA devices UI.
    import ipaddress

    raw_ip = str(data.get("ip") or "").strip()
    configuration_url = None
    try:
        ipaddress.ip_address(raw_ip)
        configuration_url = f"http://{raw_ip}"
    except ValueError:
        configuration_url = None

    registry = dr.async_get(hass)
    kwargs: dict[str, Any] = {
        "config_entry_id": entry.entry_id,
        "identifiers": {(DOMAIN, dial_id)},
        "manufacturer": MANUFACTURER,
        "name": name,
        "model": model,
        "sw_version": data.get("fw_ver"),
    }
    if configuration_url:
        kwargs["configuration_url"] = configuration_url
    registry.async_get_or_create(**kwargs)


async def _async_add_dial_entities(
    hass: HomeAssistant,
    entry: DeckhandConfigEntry,
    dial_id: str,
    data: dict[str, Any],
) -> None:
    """Signal platforms to add entities for a newly discovered dial."""
    # Dispatch discovery to each platform
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    async_dispatcher_send(hass, f"{DOMAIN}_dial_discovered", dial_id, data)


def _resolve_entity_picture_url(hass: HomeAssistant, entity_picture: str) -> str:
    """Turn an ``entity_picture`` attribute into something the dial can GET.

    HA hands us relative paths like
    ``/api/media_player_proxy/media_player.x?token=...&cache=...``. The
    dial can't resolve those on its own — it needs an absolute URL. We
    prefer the internal URL (LAN-local, no Nabu Casa round-trip) and
    fall back to whatever ``get_url`` returns, then concatenate. Absolute
    URLs are passed through untouched so Jellyfin's own poster URLs keep
    working. The signed token embedded in the URL is short-lived (HA
    rotates it) but stable long enough for the dial to fetch before the
    track changes.
    """
    if not entity_picture:
        return ""
    if entity_picture.startswith(("http://", "https://")):
        return entity_picture
    if get_url is None:
        return entity_picture
    try:
        base = get_url(hass, allow_ip=True, prefer_external=False)
    except NoURLAvailableError:
        try:
            base = get_url(hass, allow_ip=True, prefer_external=True)
        except NoURLAvailableError:
            return entity_picture
    return f"{base.rstrip('/')}{entity_picture}"


def _extract_now_playing(
    hass: HomeAssistant, entity_id: str
) -> dict[str, Any] | None:
    """Pull now-playing fields off a media_player state.

    Returns the payload dict ready to be JSON-encoded and published to
    ``cmd/now_playing``, or ``None`` if the entity doesn't exist. An
    entity that's idle / off / has no media_title yields an empty-title
    payload so the firmware can revert to its theme-default home face.
    """
    state = hass.states.get(entity_id)
    if state is None:
        return None

    attr = state.attributes or {}

    # Split the media_player state into the dial's four Now-Playing fields:
    # "Artist - Song" up top, "Channel | Speaker" on the bottom marquee.
    # The routing (incl. the AmpliPi channel-in-media_title quirk) is pure
    # and lives in _now_playing so it's unit-testable.
    fields = now_playing_fields(attr, entity_id)
    title = fields["title"]
    artist = fields["artist"]
    source = fields["source"]

    album_art_url = _resolve_entity_picture_url(hass, attr.get("entity_picture") or "")

    is_playing = state.state == "playing"

    # Current volume (0-100). The dial seeds its rotary from this so the
    # first encoder tick nudges from the real level instead of a guess.
    # Absent on players that don't expose volume (e.g. some TVs) — the
    # firmware keeps its last-known value in that case.
    volume: int | None = None
    raw_vol = attr.get("volume_level")
    if isinstance(raw_vol, (int, float)):
        volume = max(0, min(100, int(round(float(raw_vol) * 100))))

    payload: dict[str, Any] = {
        # Echo the entity id so the dial can target the right
        # media_player when the user taps play/pause on the now-playing
        # face. Without it the server falls back to the dial's room
        # media_player binding, which is rarely what HA is currently
        # tracking (e.g. tablet Jellyfin vs. Office speakers).
        "entity_id": entity_id,
        "title": str(title)[:96],
        "artist": str(artist)[:96],
        "source": str(source)[:32],
        "is_playing": bool(is_playing),
    }
    if album_art_url:
        payload["album_art_url"] = str(album_art_url)[:256]
    if volume is not None:
        payload["volume"] = volume
    # Transport capabilities + source list, so the dial only offers a
    # control HA actually supports — this is what lets Now-Playing replace
    # the dedicated media-control face. Uses now_playing_controls rather
    # than the raw capability flags because the source picker needs a
    # second gate: SELECT_SOURCE with an empty source_list would otherwise
    # draw an empty picker.
    payload.update(now_playing_controls(attr))
    return payload


async def _publish_now_playing(
    hass: HomeAssistant,
    entry: DeckhandConfigEntry,
    dial_id: str,
    team_id: str,
    payload: dict[str, Any],
) -> None:
    """Publish a now-playing payload to a dial over MQTT."""
    topic = TOPIC_CMD_NOW_PLAYING.format(team_id=team_id, dial_id=dial_id)
    await mqtt.async_publish(hass, topic, json.dumps(payload))


# The previously-inline _format_sensor_value / _safe_unit_for_dial /
# _DIAL_UNIT_GLYPH_MAP helpers now live in ._units (a vendored copy of
# deckhand_sdk/deckhand/units.py). They're imported at the top of this
# module under the same underscore-prefixed names. The canonical
# format_sensor_value returns a ``(value, unit)`` tuple — see the two
# call sites below for the new shape.


def _build_sensor_value_payload(
    hass: HomeAssistant, entity_id: str, label: str
) -> dict[str, Any] | None:
    """Pull a sensor reading + unit off an HA entity for cmd/sensor_value."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if state.state in (None, "", "unknown", "unavailable"):
        return None
    value, unit = _format_sensor_value_tuple(state.state, state.attributes)
    if value == "" and unit == "":
        # Canonical formatter signals "skip this publish" via empty tuple.
        return None
    payload: dict[str, Any] = {
        "entity_id": str(entity_id)[:128],
        "label": str(label or state.attributes.get("friendly_name") or "")[:64],
        "value": value[:48],
        "unit": unit[:8],
    }
    return payload


async def _push_sensor_value_for_entity(
    hass: HomeAssistant, team_id: str, dial_id: str, entity_id: str, label: str
) -> None:
    """One-shot publish of the entity's current state to cmd/sensor_value."""
    payload = _build_sensor_value_payload(hass, entity_id, label)
    if payload is None:
        _LOGGER.warning(
            "apply_overlay: sensor_entity_id %s has no readable state — "
            "dial will show '—' until the entity reports a value",
            entity_id,
        )
        return
    topic = TOPIC_CMD_SENSOR_VALUE.format(team_id=team_id, dial_id=dial_id)
    # Retain so the dial picks up the latest value on reconnect — without
    # this, a reboot or wifi blip would blank the sensor face to "—" until
    # the entity next changes state in HA, which for slow-moving sensors
    # (battery %, freezer temp) could be hours.
    await mqtt.async_publish(hass, topic, json.dumps(payload), retain=True)
    _LOGGER.info(
        "apply_overlay: pushed initial sensor value %s=%s to %s",
        entity_id, payload.get("value"), dial_id,
    )


def _bind_sensors_to_dial(
    hass: HomeAssistant,
    entry: DeckhandConfigEntry,
    dial_id: str,
    team_id: str,
    entities: list[tuple[str, str]],
) -> None:
    """Wire up state-change listeners so every face entity stays live.

    Replaces any previous binding for this dial — last apply_overlay
    wins. The dial only ever renders the entities the most-recent
    overlay told it to show, so old listeners would just publish to
    cmd/sensor_value for entities the firmware no longer cares about.
    """
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if store is None:
        return
    bindings: dict[str, Any] = store.setdefault("_sensor_bindings", {})
    prev = bindings.get(dial_id)
    if prev:
        prev["unsub"]()
        bindings.pop(dial_id, None)

    if not entities:
        return

    label_by_eid = {eid: lbl for eid, lbl in entities}
    watched = list(label_by_eid.keys())

    @callback
    def _on_change(event: Event) -> None:
        eid = event.data.get("entity_id")
        if eid not in label_by_eid:
            return
        payload = _build_sensor_value_payload(hass, eid, label_by_eid[eid])
        if payload is None:
            return
        topic = TOPIC_CMD_SENSOR_VALUE.format(team_id=team_id, dial_id=dial_id)
        # Retain so a dial reboot doesn't lose the cached value — see the
        # rationale in _push_sensor_value_for_entity.
        hass.async_create_task(
            mqtt.async_publish(hass, topic, json.dumps(payload), retain=True)
        )

    unsub = async_track_state_change_event(hass, watched, _on_change)
    bindings[dial_id] = {"entity_ids": watched, "unsub": unsub}


def _set_sensor_watch_listeners(
    hass: HomeAssistant,
    entry: DeckhandConfigEntry,
    dial_id: str,
    team_id: str,
    entities: list[tuple[str, str]],
) -> None:
    """Reconcile per-dial sensor-watch listeners with the latest
    retained ``sensor_watches`` payload from Helm/Console.

    Idempotent: replaces any previous binding for this dial outright.
    Empty ``entities`` tears down all listeners — equivalent to "this
    dial switched away from a sensor face."

    Kept distinct from ``_bind_sensors_to_dial`` (apply_overlay) so the
    two paths layer cleanly: an overlay can pin extra entities on top
    of what the persistent face is already watching, and tearing one
    down doesn't touch the other.
    """
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if store is None:
        return
    bindings: dict[str, Any] = store.setdefault("_sensor_watch_bindings", {})
    prev = bindings.get(dial_id)
    if prev:
        prev["unsub"]()
        bindings.pop(dial_id, None)

    if not entities:
        _LOGGER.info("sensor_watches: cleared listeners for %s", dial_id)
        return

    label_by_eid = {eid: lbl for eid, lbl in entities}
    watched = list(label_by_eid.keys())

    @callback
    def _on_change(event: Event) -> None:
        eid = event.data.get("entity_id")
        if eid not in label_by_eid:
            return
        payload = _build_sensor_value_payload(hass, eid, label_by_eid[eid])
        if payload is None:
            return
        topic = TOPIC_CMD_SENSOR_VALUE.format(team_id=team_id, dial_id=dial_id)
        # Retain — see _bind_sensors_to_dial rationale: a dial reboot
        # would otherwise blank the sensor face to "—" until the entity
        # next fires.
        hass.async_create_task(
            mqtt.async_publish(hass, topic, json.dumps(payload), retain=True)
        )

    unsub = async_track_state_change_event(hass, watched, _on_change)
    bindings[dial_id] = {"entity_ids": watched, "unsub": unsub}

    # Warm the LUT with current values for every newly-bound entity so
    # the dial renders real numbers on first frame instead of waiting
    # for the first state_change event to fire.
    for eid, lbl in entities:
        hass.async_create_task(
            _push_sensor_value_for_entity(hass, team_id, dial_id, eid, lbl)
        )

    _LOGGER.info(
        "sensor_watches: armed %d listener(s) for %s (entities=%s)",
        len(watched), dial_id, ", ".join(watched),
    )


@callback
def _reload_media_player_listeners(
    hass: HomeAssistant, entry: DeckhandConfigEntry
) -> None:
    """(Re)register state-change listeners for the configured bindings.

    Called on setup and whenever the options flow updates the binding
    list. Safe to call repeatedly — drops any previous subscription
    before re-registering.
    """
    store = hass.data[DOMAIN].get(entry.entry_id)
    if store is None:
        return

    # Drop any existing subscription first so we never stack listeners.
    old_unsub = store.get("_media_player_unsub")
    if old_unsub:
        old_unsub()
        store["_media_player_unsub"] = None

    raw_bindings = entry.options.get(CONF_MEDIA_PLAYER_BINDINGS, []) or []
    # Normalize: accept both the dict-list format and any stray strings.
    entity_to_dial: dict[str, str] = {}
    for item in raw_bindings:
        if not isinstance(item, dict):
            continue
        ent = item.get("entity_id")
        dev = item.get("dial_device_id")
        if isinstance(ent, str) and isinstance(dev, str) and ent and dev:
            entity_to_dial[ent] = dev

    if not entity_to_dial:
        return

    # Pre-resolve every dial up front. Any bindings that point at a
    # device that's been removed get dropped with a WARNING — we don't
    # want a stale binding to crash setup.
    valid: dict[str, tuple[str, str]] = {}  # entity_id -> (dial_id, team_id)
    for entity_id, device_id in entity_to_dial.items():
        resolved = _resolve_dial(hass, device_id)
        if resolved is None:
            _LOGGER.warning(
                "Deckhand media_player binding: device_id %s no longer exists, "
                "skipping %s",
                device_id,
                entity_id,
            )
            continue
        valid[entity_id] = resolved

    if not valid:
        return

    debounce = store.setdefault("_media_player_debounce", {})

    @callback
    def _handle_state_change(event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id not in valid:
            return
        dial_id, team_id = valid[entity_id]
        payload = _extract_now_playing(hass, entity_id)
        if payload is None:
            return

        # Debounce: skip if same (title, artist, is_playing) within window.
        fingerprint = (
            payload.get("title", ""),
            payload.get("artist", ""),
            payload.get("is_playing", False),
        )
        key = (dial_id, entity_id)
        now = time.monotonic()
        prev = debounce.get(key)
        if (
            prev is not None
            and prev["fingerprint"] == fingerprint
            and (now - prev["ts"]) < MEDIA_PLAYER_DEBOUNCE_S
        ):
            return
        debounce[key] = {"ts": now, "fingerprint": fingerprint}

        _LOGGER.info(
            "Auto-pushing now_playing %s -> %s (title=%r, playing=%s)",
            entity_id,
            dial_id,
            payload.get("title", ""),
            payload.get("is_playing", False),
        )
        hass.async_create_task(
            _publish_now_playing(hass, entry, dial_id, team_id, payload)
        )

    unsub = async_track_state_change_event(
        hass, list(valid.keys()), _handle_state_change
    )
    store["_media_player_unsub"] = unsub


def _register_services(hass: HomeAssistant, entry: DeckhandConfigEntry) -> None:
    """Register Deckhand services."""

    # Write-side services (push_theme, reboot, apply_overlay, etc.) take
    # real action on physical hardware and broadcast to every dial in
    # the team. Gate them behind HA admin — a guest on the household
    # Lovelace shouldn't be able to reboot the lobby dials or replace
    # an announcement with attacker copy.
    async def _require_admin(call) -> None:
        user_id = getattr(getattr(call, "context", None), "user_id", None)
        if not user_id:
            return  # Internal / automation call — allow.
        user = await hass.auth.async_get_user(user_id)
        if user is None or user.is_admin:
            return
        _LOGGER.warning(
            "Non-admin user %s attempted to call deckhand service %s",
            user.name or user_id,
            getattr(call, "service", "?"),
        )
        raise Unauthorized(
            context=call.context,
            permission="deckhand.admin_service",
        )

    async def _push_theme(call) -> None:
        """Push a theme to a dial or every dial in a room (admin-only)."""
        await _require_admin(call)
        device_id = call.data.get("device_id")
        theme = call.data.get("theme")
        if not isinstance(theme, str) or not theme.strip():
            _LOGGER.warning("push_theme called with empty or invalid theme")
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_theme",
            )
        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "push_theme: could not resolve device_id %s to any dial(s)", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        # Per-theme custom options ("config" in firmware terms — wedding's
        # diamond_count / initials / flower_color, photo themes' overrides,
        # etc.). Optional; only forwarded when the operator supplied non-
        # empty values. The dial firmware accepts unknown keys silently so
        # automations can pre-stage options for themes they don't have yet.
        # NOTE: this path is TRANSIENT — it doesn't update Helm's stored
        # DialThemeConfig. For persistence across reboots, set the options
        # in the Helm UI (Gallery → push modal).
        theme_options = call.data.get("theme_options")

        # Server-resolved selections ("random") can't be resolved by the
        # dial — route via theme_request so Helm picks an activated theme
        # per dial (excluding its current one) and pushes the concrete
        # cmd/theme itself. theme_options don't apply to a random pick.
        if theme.strip() in SERVER_RESOLVED_THEMES:
            body = json.dumps({"slug": theme.strip()})
            for dial_id, team_id in targets:
                topic = TOPIC_THEME_REQUEST.format(team_id=team_id, dial_id=dial_id)
                await mqtt.async_publish(hass, topic, body)
            return

        payload: dict[str, Any] = {"id": theme}
        if isinstance(theme_options, dict) and theme_options:
            payload["config"] = {
                k: v for k, v in theme_options.items()
                if isinstance(k, str) and not k.startswith("_")
            }
        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_THEME.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body)
        _LOGGER.info(
            "Pushed theme '%s' to %d dial(s): %s%s",
            theme, len(targets), ", ".join(d for d, _ in targets),
            f" (options: {sorted(payload.get('config', {}).keys())})"
            if "config" in payload else "",
        )

    async def _send_announcement(call) -> None:
        """Send an announcement to a dial or whole room (admin-only).

        Optionally accompanies the text with an image backdrop pushed
        straight to the broker (cmd/image), sourced from either a camera
        entity snapshot (``snapshot_entity``) or an image URL
        (``image_url``). This is the LAN-direct doorbell path — HACS does
        the JPEG→RGB565 conversion + chunking itself rather than routing
        through Helm's REST API, because the camera lives in HA.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        message = call.data.get("message")
        from_name = call.data.get("from_name", "Home Assistant")
        duration = call.data.get("duration", 30)
        animation = call.data.get("animation", "none")
        transition = _resolve_transition(call.data.get("transition"))
        snapshot_entity = call.data.get("snapshot_entity")
        image_url = call.data.get("image_url")
        if not isinstance(message, str) or not message.strip():
            _LOGGER.warning("send_announcement called with empty message")
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="empty_message",
            )
        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "send_announcement: could not resolve device_id %s to any dial(s)",
                device_id,
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        # Fetch the image bytes up front (once for the whole room). A
        # fetch failure is non-fatal — we still send the text announce so
        # the doorbell chime never silently drops just because the camera
        # hiccuped.
        image_bytes: bytes | None = None
        if snapshot_entity or image_url:
            image_bytes = await _fetch_announcement_image(
                hass, snapshot_entity, image_url
            )

        payload: dict[str, Any] = {
            "message": message,
            "from": from_name,
            "duration_s": duration,
        }
        # Only attach the animation field when the user picked something
        # other than the default — keeps payloads small for the dial's
        # 8KB inbound buffer and lets the firmware fall back to whatever
        # the active theme set as its notification animation.
        if isinstance(animation, str) and animation and animation != "none":
            payload["animation"] = animation
        # Per-send arrival transition (helm#224). Same publisher rule as
        # ``animation`` above: the default stays OFF the wire, so an
        # automation that doesn't opt in produces the payload it always
        # did and the dial keeps using its fade coordinator.
        if transition != DEFAULT_TRANSITION:
            payload["transition"] = transition
        # Tell the dial an image backdrop is streaming so it suppresses
        # theme animation and waits for the cmd/image chunks. Firmware
        # reads this as ``announce_has_pending_image`` (mirrors Helm's
        # push_announcement_to_dial ``image=True``).
        if image_bytes is not None:
            payload["image"] = True
        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_ANNOUNCE.format(team_id=team_id, dial_id=dial_id)
            # cmd/announce with image:true FIRST, then the image stream.
            await mqtt.async_publish(hass, topic, body)
            if image_bytes is not None:
                try:
                    await publish_image_to_dial(hass, team_id, dial_id, image_bytes)
                except Exception:  # noqa: BLE001 - best-effort; text already sent
                    _LOGGER.exception(
                        "send_announcement: image push failed for dial %s "
                        "(text announcement was still sent)",
                        dial_id,
                    )
        _LOGGER.info(
            "Sent announcement to %d dial(s): %s (animation=%s, transition=%s, image=%s)",
            len(targets), message, animation, transition, image_bytes is not None,
        )

    async def _send_countdown(call) -> None:
        """Send a countdown announcement to a dial.

        Mirrors _send_announcement but layers in the countdown_to,
        celebration_message, celebration_animation, and (optionally)
        celebration_theme_id fields the firmware reads to switch into
        countdown mode.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        target_dt = call.data.get("target_datetime")
        message = call.data.get("message", "Almost there...")
        celebration_message = call.data.get("celebration_message")
        celebration_animation = call.data.get("celebration_animation", "fireworks")
        celebration_theme = call.data.get("celebration_theme", "")
        lead_seconds = int(call.data.get("lead_seconds", 60) or 60)
        from_name = call.data.get("from_name", "Home Assistant")

        if not isinstance(celebration_message, str) or not celebration_message.strip():
            _LOGGER.warning("send_countdown called with empty celebration_message")
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="empty_message",
            )

        # The HA datetime selector hands us either a datetime instance or
        # an ISO-8601 string depending on caller — normalize to epoch.
        if isinstance(target_dt, datetime):
            target_epoch = int(target_dt.timestamp())
        elif isinstance(target_dt, str) and target_dt:
            try:
                target_epoch = int(datetime.fromisoformat(target_dt).timestamp())
            except ValueError as exc:
                raise ServiceValidationError(
                    f"Invalid target_datetime '{target_dt}': {exc}"
                ) from exc
        else:
            raise ServiceValidationError("target_datetime is required")

        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "send_countdown: could not resolve device_id %s to any dial(s)",
                device_id,
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        # Total visible duration on the dial = countdown phase + a 30s
        # celebration tail. Plenty of headroom for the fireworks.
        duration_s = max(lead_seconds + 30, 35)

        payload: dict[str, Any] = {
            "message": message,
            "from": from_name,
            "duration_s": duration_s,
            "animation": "ripple",
            "countdown_to": target_epoch,
            "celebration_message": celebration_message,
        }
        if isinstance(celebration_animation, str) and celebration_animation:
            payload["celebration_animation"] = celebration_animation
        if isinstance(celebration_theme, str) and celebration_theme.strip():
            payload["celebration_theme_id"] = celebration_theme.strip()

        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_ANNOUNCE.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body)
        _LOGGER.info(
            "Sent countdown to %d dial(s): target=%s celeb=%r anim=%s theme=%r",
            len(targets),
            target_epoch,
            celebration_message,
            celebration_animation,
            celebration_theme,
        )

    async def _apply_overlay(call) -> None:
        """Apply a transient runtime overlay to a dial or room.

        Merges any subset of {subtitle, home_face, brightness, sensor
        bindings, weather/framecast entity, message text, hide_label}
        on top of the dial's current state. Distinct from
        ``mount_face`` (which writes RETAINED face state that survives
        reboot): this is the "flash a different face for an hour" path.
        Set ``ttl_s`` for an auto-revert timer.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "apply_overlay: could not resolve device_id %s to any dial(s)", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        payload: dict[str, Any] = {}

        subtitle_mode = call.data.get("subtitle_mode")
        subtitle_text = call.data.get("subtitle_text")
        has_text = isinstance(subtitle_text, str) and bool(subtitle_text.strip())
        if subtitle_mode is not None:
            if subtitle_mode not in _OVERLAY_SUBTITLE_MODES:
                raise ServiceValidationError(
                    f"subtitle_mode must be one of {sorted(_OVERLAY_SUBTITLE_MODES)}"
                )
            if has_text and subtitle_mode != "custom":
                # Operator passed BOTH a non-empty text AND a non-custom
                # mode. The firmware would honor the mode and drop the
                # text; auto-flip so "type subtitle, see subtitle" works.
                _LOGGER.info(
                    "apply_overlay: subtitle_text with mode=%r — auto-flipping to 'custom'",
                    subtitle_mode,
                )
                payload["subtitle_mode"] = "custom"
            else:
                payload["subtitle_mode"] = subtitle_mode
        elif has_text:
            payload["subtitle_mode"] = "custom"

        # home_face — selects which face renders on the dial home screen.
        # Legacy alias ``home_mode`` kept for firmware <2.0 compatibility.
        home_face = call.data.get("home_face")
        if home_face is not None:
            if home_face not in _OVERLAY_HOME_FACES:
                raise ServiceValidationError(
                    f"home_face must be one of {sorted(_OVERLAY_HOME_FACES)}"
                )
            payload["home_face"] = home_face
            payload["home_mode"] = home_face

        for key in _OVERLAY_STRING_FIELDS:
            val = call.data.get(key)
            if val is not None:
                if not isinstance(val, str):
                    raise ServiceValidationError(f"{key} must be a string")
                payload[key] = val[:128]

        brightness = call.data.get("brightness")
        if brightness is not None:
            try:
                b = int(brightness)
            except (TypeError, ValueError) as exc:
                raise ServiceValidationError("brightness must be an integer") from exc
            if not 0 <= b <= 100:
                raise ServiceValidationError("brightness must be 0-100")
            payload["brightness"] = b

        hide_label = call.data.get("hide_label")
        if hide_label is not None:
            payload["hide_label"] = bool(hide_label)

        ttl_s = call.data.get("ttl_s")
        if ttl_s is not None:
            try:
                t = int(ttl_s)
            except (TypeError, ValueError) as exc:
                raise ServiceValidationError("ttl_s must be an integer") from exc
            if not 0 <= t <= 86400:
                raise ServiceValidationError("ttl_s must be 0-86400")
            if t > 0:
                payload["ttl_s"] = t

        # Multi-sensor block: quad slots 2-4 + a scrolling marquee. Slot 1
        # is the legacy ``sensor_entity_id`` / ``sensor_label`` pair above
        # — we fold it into the same ``sensors.quad`` list the firmware
        # parser expects so all four slots share one code path on the
        # dial. ``sensor_marquee`` accepts either bare entity ids or
        # per-entry dicts; both shapes normalize to the same list of
        # {entity_id, label, unit} objects.
        quad_entries: list[dict[str, Any]] = []
        slot1_eid = call.data.get("sensor_entity_id")
        slot1_lbl = call.data.get("sensor_label") or ""
        if isinstance(slot1_eid, str) and slot1_eid.strip():
            quad_entries.append({
                "slot": 1,
                "entity_id": slot1_eid.strip(),
                "label": str(slot1_lbl)[:32],
            })
        for slot in _OVERLAY_QUAD_SLOTS:
            eid = call.data.get(f"sensor_quad_{slot}_entity_id")
            if not isinstance(eid, str) or not eid.strip():
                continue
            lbl = call.data.get(f"sensor_quad_{slot}_label") or ""
            quad_entries.append({
                "slot": slot,
                "entity_id": eid.strip(),
                "label": str(lbl)[:32],
            })

        marquee_entries: list[dict[str, Any]] = []
        marquee_raw = call.data.get("sensor_marquee")
        if isinstance(marquee_raw, list):
            for item in marquee_raw[:12]:
                if isinstance(item, str) and item.strip():
                    marquee_entries.append({"entity_id": item.strip(), "label": "", "unit": ""})
                elif isinstance(item, dict):
                    eid = (item.get("entity_id") or "").strip()
                    if not eid:
                        continue
                    marquee_entries.append({
                        "entity_id": eid,
                        "label": str(item.get("label") or "")[:24],
                        "unit": str(item.get("unit") or "")[:8],
                    })

        # marquee_position was removed as a service field: "subtitle" was
        # its only legal value once ring mode went away (2026-07-22), so it
        # asked the operator a question with one answer. It is not sent on
        # the wire either — the firmware's parser never reads it. See
        # face_sensor.h, which unconditionally assigns
        # SENSOR_MARQUEE_SUBTITLE and ignores any value in the payload, so
        # sending it was pure noise. If ring mode ever returns at LVGL 9,
        # the firmware has to start reading the field again first.
        #
        # Accepted-and-ignored rather than rejected: an existing automation
        # still passing marquee_position keeps working instead of breaking
        # on upgrade with a validation error.
        if quad_entries or marquee_entries:
            sensors_block: dict[str, Any] = {}
            if quad_entries:
                sensors_block["quad"] = quad_entries
            if marquee_entries:
                sensors_block["marquee"] = marquee_entries
            payload["sensors"] = sensors_block
            # Slot 1 was packed into ``sensors.quad`` above; drop the
            # top-level legacy fields so the firmware reads only one
            # source-of-truth.
            payload.pop("sensor_entity_id", None)
            payload.pop("sensor_label", None)

        if not payload:
            raise ServiceValidationError(
                "apply_overlay requires at least one field to be set"
            )

        # Pre-populate the dial's sensor LUT BEFORE the face swap so the
        # first render of the new face shows real numbers. If cmd/overlay
        # arrives first, the firmware runs build_clock() while the LUT
        # still has no values for the new entities — quadrants render
        # "—" and the per-slot rebuild is debounced to 1.5s, so the user
        # sees a blank face (or the previous face's last values, if the
        # quad slot entity ids happen to overlap) until the next HA state
        # change. Publishing cmd/sensor_value first guarantees the LUT is
        # warm by the time the overlay lands.
        bound_entities: list[tuple[str, str]] = []
        for q in quad_entries:
            bound_entities.append((q["entity_id"], q.get("label") or ""))
        for m in marquee_entries:
            bound_entities.append((m["entity_id"], m.get("label") or ""))

        if bound_entities:
            for dial_id, team_id in targets:
                for eid, lbl in bound_entities:
                    await _push_sensor_value_for_entity(hass, team_id, dial_id, eid, lbl)

        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_OVERLAY.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body)
        _LOGGER.info(
            "Applied overlay to %d dial(s): %s",
            len(targets), sorted(payload.keys()),
        )

        # Register state-change listeners for ongoing updates after the
        # face has switched.
        if bound_entities:
            for dial_id, team_id in targets:
                _bind_sensors_to_dial(
                    hass, entry, dial_id, team_id, bound_entities,
                )

    async def _update_now_playing(call) -> None:
        """Stream a now-playing update to a dial (cmd/now_playing).

        High-frequency ephemeral update. Empty title reverts to the theme-
        default home face. All fields other than title/is_playing are
        optional. Users typically wire this up via template sensors that
        watch a media_player entity and fire an automation on attribute
        change.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "update_now_playing: could not resolve device_id %s", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        title = call.data.get("title", "")
        if title is None:
            title = ""
        payload: dict[str, Any] = {
            "title": str(title)[:96],
            "artist": str(call.data.get("artist") or "")[:96],
            "source": str(call.data.get("source") or "")[:32],
            "is_playing": bool(call.data.get("is_playing", True)),
        }
        # Optional entity_id — when provided, the dial echoes it back on
        # play/pause click so the server can target the actual playing
        # media_player rather than the room's default binding.
        entity = call.data.get("entity_id")
        if isinstance(entity, str) and entity.strip():
            payload["entity_id"] = entity.strip()
        art = call.data.get("album_art_url")
        if art:
            payload["album_art_url"] = str(art)[:256]

        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_NOW_PLAYING.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body)
        _LOGGER.debug(
            "Now-playing -> %d dial(s): %s",
            len(targets), payload.get("title"),
        )

    async def _update_from_media_player(call) -> None:
        """Extract now-playing fields from a media_player entity and publish.

        Thin wrapper that reads the entity's current state + attributes,
        normalizes them via ``_extract_now_playing``, and publishes to
        ``cmd/now_playing``. Designed as the manual counterpart to the
        auto-push bindings configured in the options flow — same payload
        shape, no debounce.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        entity_id = call.data.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise ServiceValidationError("entity_id is required")
        entity_id = entity_id.strip()
        if not entity_id.startswith("media_player."):
            raise ServiceValidationError(
                f"entity_id must be a media_player (got {entity_id})"
            )

        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "update_from_media_player: could not resolve device_id %s",
                device_id,
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        payload = _extract_now_playing(hass, entity_id)
        if payload is None:
            raise ServiceValidationError(
                f"Unknown entity_id '{entity_id}' — is it loaded?"
            )

        for dial_id, team_id in targets:
            await _publish_now_playing(hass, entry, dial_id, team_id, payload)
        _LOGGER.info(
            "update_from_media_player: %s -> %d dial(s) (title=%r, playing=%s)",
            entity_id,
            len(targets),
            payload.get("title", ""),
            payload.get("is_playing", False),
        )

    async def _update_sensor_value(call) -> None:
        """Stream a sensor-value update to a dial (cmd/sensor_value).

        High-frequency ephemeral update. If the dial is currently on the
        sensor home face it refreshes in place; otherwise the value is
        buffered until the sensor face next activates.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "update_sensor_value: could not resolve device_id %s", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        entity_id = call.data.get("entity_id", "")
        if not entity_id:
            raise ServiceValidationError("entity_id is required")

        value = call.data.get("value", "")
        # Accept numbers or strings; publish as string so the dial's parser
        # can render verbatim without locale issues. Float values are
        # rounded to 1 decimal (canonical dial-display precision) so
        # template sensors passing raw HA readings ("21.3333333333333")
        # render cleanly. See deckhand_sdk.units.format_sensor_value.
        raw_unit = str(call.data.get("unit") or "")
        fmt_value, fmt_unit = _format_sensor_value_tuple(
            value, {"unit_of_measurement": raw_unit}
        )
        payload: dict[str, Any] = {
            "entity_id": str(entity_id)[:128],
            "label": str(call.data.get("label") or "")[:64],
            "value": fmt_value[:48],
            "unit": fmt_unit[:8],
        }
        icon = call.data.get("icon")
        if icon:
            payload["icon"] = str(icon)[:16]
        color = call.data.get("color")
        if color:
            payload["color"] = str(color)[:7]

        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_SENSOR_VALUE.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body)
        _LOGGER.debug(
            "Sensor-value -> %d dial(s): %s=%s",
            len(targets), entity_id, value,
        )

    async def _reboot(call) -> None:
        """Reboot a dial or every dial in a room (admin-only)."""
        await _require_admin(call)
        device_id = call.data.get("device_id")
        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "reboot: could not resolve device_id %s to any dial(s)", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_REBOOT.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, "{}")
        _LOGGER.info(
            "Sent reboot command to %d dial(s): %s",
            len(targets), ", ".join(d for d, _ in targets),
        )

    async def _set_dnd(call) -> None:
        """Set/clear quiet-mode Do Not Disturb on dial(s) (helm#179).

        Wire contract mirrors Helm's ``apps/mqtt/tasks.py::push_dnd``
        exactly (see TOPIC_CMD_DND in const.py): the on-payload is
        retained so an offline dial converges on reconnect; the
        off-payload is published (retained, matching Helm's
        ``_RETAINED_SUBTOPICS`` behavior byte-for-byte) and then the
        retained slot is immediately CLEARED with an empty payload —
        the invitation-terminal-state hygiene rule — so the broker
        never replays a stale mode at a reconnecting dial. Publish
        order inside the loop guarantees the clear lands after the
        set. Dial-local state always wins conflicts: the dial's NVS is
        the on-device truth and its status/``dnd_changed`` echo is what
        the DND binary_sensor reports.

        R1 executor-arbitration note: DND is idempotent *state*, not a
        media action — the dial is the single executor and every remote
        setter (Helm, HACS, SF, guest tap) converges through the same
        retained topic + status echo. No presence/``owns`` change here;
        double-publish from two setters is harmless by design.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        on = call.data.get("on")
        if isinstance(on, str):  # YAML mode can hand over "true"/"false"
            low = on.strip().lower()
            if low in ("true", "on", "yes"):
                on = True
            elif low in ("false", "off", "no"):
                on = False
        if not isinstance(on, bool):
            raise ServiceValidationError("on must be true or false")

        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "set_dnd: could not resolve device_id %s to any dial(s)", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        body = json.dumps({"on": on, "source": "ha"})
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_DND.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body, qos=1, retain=True)
            if not on:
                # Empty-clear the retained slot — DND has ended; never
                # leave the broker holding {"on": false} to replay.
                await mqtt.async_publish(hass, topic, "", qos=1, retain=True)
        _LOGGER.info(
            "set_dnd %s → %d dial(s): %s",
            "on" if on else "off",
            len(targets), ", ".join(d for d, _ in targets),
        )

    async def _publish_settings_request(call, payload: dict) -> int:
        """Shared fan-out for the per-dial settings services.

        Routed through Helm (settings_request), never straight to
        cmd/config. Helm persists to the Dial row and THEN republishes the
        dial's complete cmd/config — which is what makes the change stick
        and what keeps the retained payload whole. Publishing the patch
        ourselves would set the dial on the wire while Helm's row still
        held the old value, so the next full config push (boot re-push,
        "Push to dial", any settings save) would quietly revert it.

        retain=False: this is a request, not state. The retained slot
        belongs to cmd/config, and Helm owns it.
        """
        targets = _resolve_targets(hass, call.data.get("device_id"))
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_SETTINGS_REQUEST.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body, qos=1, retain=False)
        return len(targets)

    async def _set_timezone(call) -> None:
        """Set a per-dial timezone.

        Takes an IANA name and hands it to Helm as ``timezone``. Helm
        writes ``Dial.timezone`` and does the IANA → POSIX translation
        itself when it builds cmd/config (the firmware does
        ``setenv("TZ", value, 1); tzset()`` and cannot parse IANA).

        This used to publish ``{"tz": posix}`` directly to cmd/config,
        which never reached Helm's database: the Dial row kept the old
        zone and the next full config push silently put it back. The
        service name and schema are unchanged so existing automations
        keep working — only the destination moved.

        The zone is still validated here against the local map so the user
        gets a real error in the HA UI. The settings plane has no ack, so
        a zone Helm rejects would otherwise fail in silence.
        """
        await _require_admin(call)
        iana = call.data.get("timezone")
        if not isinstance(iana, str) or not iana.strip():
            raise ServiceValidationError("timezone is required")
        iana = iana.strip()
        if iana not in _IANA_TO_POSIX:
            raise ServiceValidationError(
                f"Unsupported timezone '{iana}'. Pick one from the dropdown."
            )

        n = await _publish_settings_request(call, {"timezone": iana})
        _LOGGER.info("set_timezone: %s → %d dial(s)", iana, n)

    async def _set_dial_settings(call) -> None:
        """Set any subset of a dial's sticky settings in one call.

        SF SetDialSettings / REST ``/dials/{id}/settings/`` parity. Every
        field is optional; only the ones supplied are sent, so an
        automation can nudge one setting without disturbing the rest.

        Validation happens twice on purpose. Helm validates atomically and
        is the authority, but this plane carries no ack — a rejected
        request is a line in Helm's log the HA user will never see. So the
        obvious mistakes are caught here where HA can show a real error.
        """
        await _require_admin(call)

        payload: dict[str, Any] = {}

        for key, allowed in (
            ("clock_face_pref", ("auto", "digital", "analog", "both")),
            ("clock_format", ("12h", "24h")),
        ):
            raw = call.data.get(key)
            if raw is None:
                continue
            value = str(raw).strip().lower()
            if value not in allowed:
                raise ServiceValidationError(
                    f"{key} must be one of {', '.join(allowed)} (got '{value}')."
                )
            payload[key] = value

        if call.data.get("timezone") is not None:
            iana = str(call.data["timezone"]).strip()
            # "" is legal — it means "inherit the team default".
            if iana and iana not in _IANA_TO_POSIX:
                raise ServiceValidationError(
                    f"Unsupported timezone '{iana}'. Pick one from the dropdown."
                )
            payload["timezone"] = iana

        if call.data.get("label") is not None:
            label = str(call.data["label"]).strip()
            if len(label) > 64:
                raise ServiceValidationError(
                    f"label must be at most 64 characters (got {len(label)})."
                )
            payload["label"] = label

        if call.data.get("haptic_intensity") is not None:
            try:
                intensity = int(call.data["haptic_intensity"])
            except (TypeError, ValueError):
                raise ServiceValidationError(
                    "haptic_intensity must be an integer 0-3."
                ) from None
            if not 0 <= intensity <= 3:
                raise ServiceValidationError(
                    f"haptic_intensity must be between 0 and 3 (got {intensity})."
                )
            payload["haptic_intensity"] = intensity

        for key in ("hide_label", "haptic_encoder", "haptic_notify", "haptic_confirm"):
            if call.data.get(key) is not None:
                payload[key] = bool(call.data[key])

        if not payload:
            raise ServiceValidationError(
                "set_dial_settings needs at least one setting to change."
            )

        n = await _publish_settings_request(call, payload)
        _LOGGER.info(
            "set_dial_settings: %s → %d dial(s)", ", ".join(sorted(payload)), n
        )

    async def _set_sunset(call) -> None:
        """Push a real sunset anchor to the Sundowner theme (cmd/sunset).

        Sundowner's clock mode sinks the on-dial sun to a *real* sunset
        instead of free-running. The anchor is a Unix epoch; we accept it
        two ways:

        * ``sunset_at`` — an explicit epoch int, ``datetime``, or ISO-8601
          string (what the operator typed / an automation computed).
        * ``entity_id`` — an HA entity to read it off, pulling
          ``attribute`` (default ``next_setting``, sun.sun's standard
          next-sunset timestamp). The founder's call: HA can hand us a
          timestamp far more easily than a lat/long.

        Explicit ``sunset_at`` wins if both are supplied. Publishes
        ``{"epoch": N}`` (the firmware also accepts ``sunset_at`` but
        ``epoch`` is canonical). Pushing ``0`` clears the dial back to the
        theme's manual "HH:MM" fallback.

        One-shot, like every other write service. sun.sun's
        ``next_setting`` advances to tomorrow's sunset once today's has
        passed, so wire a daily HA automation (e.g. a Sun "sunrise"
        trigger, or a time_pattern) that calls this to keep the anchor
        fresh — the integration deliberately does not run its own
        scheduler.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        sunset_at = call.data.get("sunset_at")
        entity_id = call.data.get("entity_id")
        attribute = call.data.get("attribute") or "next_setting"

        has_explicit = sunset_at not in (None, "")
        if not has_explicit and not entity_id:
            raise ServiceValidationError(
                "Provide either sunset_at (an epoch / datetime / ISO "
                "timestamp) or entity_id (e.g. sun.sun) to read the "
                "sunset from."
            )

        if has_explicit:
            epoch = _coerce_epoch(sunset_at)
            if epoch is None:
                raise ServiceValidationError(
                    f"Could not parse sunset_at {sunset_at!r} as an epoch, "
                    "datetime, or ISO-8601 timestamp."
                )
        else:
            epoch = _sunset_epoch_from_entity(hass, entity_id, attribute)
            if epoch is None:
                raise ServiceValidationError(
                    f"Entity {entity_id!r} has no readable '{attribute}' "
                    "timestamp (expected an ISO-8601 / datetime value — "
                    "sun.sun exposes next_setting)."
                )

        targets = _resolve_targets(hass, device_id)
        if not targets:
            _LOGGER.warning(
                "set_sunset: could not resolve device_id %s to any dial(s)", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        body = json.dumps({"epoch": epoch})
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_SUNSET.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body)
        try:
            pretty = datetime.fromtimestamp(epoch).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            pretty = "?"
        _LOGGER.info(
            "set_sunset: anchored %d dial(s) to epoch %s (%s)%s",
            len(targets), epoch, pretty,
            f" from {entity_id}.{attribute}" if not has_explicit else "",
        )

    # ── Phase 6 dial-platform — face dispatch ────────────────────────
    # Faces are functional UI layers. Themes paint atmosphere, faces
    # paint the room's actual signal layer (perimeter ring, ritual
    # walkthrough, future room-aware widgets). Mount-side services
    # publish retained so the dial restores the face on reboot;
    # state-side services publish ephemeral (event-style traffic).

    def _normalise_color(value: Any) -> str | None:
        """Pass through hex color strings like '#RRGGBB' or 'RRGGBB'."""
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            return None
        v = value.strip().lstrip("#")
        if len(v) not in (6, 8) or not all(c in "0123456789abcdefABCDEF" for c in v):
            return None
        return "#" + v

    def _build_perimeter_binding(raw: dict[str, Any]) -> dict[str, Any] | None:
        """Validate and shape one binding dict for the firmware schema."""
        if not isinstance(raw, dict):
            return None
        bid = raw.get("id")
        if not isinstance(bid, str) or not bid.strip():
            return None
        out: dict[str, Any] = {
            "id": bid.strip(),
            "friendly_name": str(raw.get("friendly_name", bid)),
            "angular_center": float(raw.get("angular_center", 0.0)),
        }
        if "angular_width" in raw:
            out["angular_width"] = float(raw["angular_width"])
        treatment = raw.get("treatment", "state_color")
        if treatment not in PERIMETER_TREATMENTS:
            _LOGGER.warning(
                "perimeter binding %s: unknown treatment '%s', using state_color",
                bid, treatment,
            )
            treatment = "state_color"
        out["treatment"] = treatment
        if "state" in raw:
            out["state"] = str(raw["state"])
        if "active_state" in raw:
            out["active_state"] = str(raw["active_state"])
        bc = _normalise_color(raw.get("base_color"))
        ac = _normalise_color(raw.get("active_color"))
        if bc:
            out["base_color"] = bc
        if ac:
            out["active_color"] = ac
        if "value" in raw and raw["value"] is not None:
            try:
                # Gradient position — firmware treats it as 0-1.
                out["value"] = min(max(float(raw["value"]), 0.0), 1.0)
            except (TypeError, ValueError):
                pass
        if "opacity" in raw and raw["opacity"] is not None:
            try:
                op = float(raw["opacity"])
                if 0.0 <= op <= 1.0:
                    out["opacity"] = op
            except (TypeError, ValueError):
                pass
        # Numeric-threshold keys (grid energy, tank level, …). The
        # firmware ignores these at mount — they are honored by Helm's
        # ``poll_perimeter_faces`` feeder ONLY (i.e. faces published
        # through Helm, where float(entity state) > numeric_threshold
        # pushes above_state / below_state). They pass through here so
        # a ring config round-trips intact between the HACS and Helm
        # paths; under the self-driven HACS path, compute the state
        # string with an HA template and send it via
        # ``update_perimeter_state`` instead.
        if raw.get("numeric_threshold") is not None:
            try:
                out["numeric_threshold"] = float(raw["numeric_threshold"])
            except (TypeError, ValueError):
                pass
        for key in ("above_state", "below_state"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                out[key] = val.strip()
        return out

    async def _publish_face_mount(
        face_id: str, payload: dict[str, Any], targets: list, retained: bool = True
    ) -> None:
        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_FACE_MOUNT.format(
                team_id=team_id, dial_id=dial_id, face_id=face_id,
            )
            await mqtt.async_publish(hass, topic, body, retain=retained)

    async def _mount_perimeter_pulse(call) -> None:
        """Mount Perimeter Pulse with structured bindings + appearance.

        Two ways to run a perimeter ring — pick one:

        * **HACS-mounted (this service): self-driven.** The ring is
          static until YOUR automations push updates via
          ``update_perimeter_state`` — event-driven, LAN-local, no Helm
          dependency. For numeric sensors, an HA template can compute
          the state string directly (e.g. ``{{ 'exporting' if
          states('sensor.grid_power')|float > 0 else 'importing' }}``);
          the ``numeric_threshold`` / ``above_state`` / ``below_state``
          binding keys are honored by Helm's feeder only and merely
          pass through here.
        * **Helm-published (Face publish endpoint / Faces editor):
          persistent + fed.** Helm persists the Face, points
          ``Dial.current_face`` at it, and ``poll_perimeter_faces``
          polls the bound HA entities every ~10s — including the
          numeric-threshold comparison. Use that path when you want the
          ring to live without any HA automations.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        targets = _resolve_targets(hass, device_id)
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        raw_bindings = call.data.get("bindings") or []
        if not isinstance(raw_bindings, list) or not raw_bindings:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_bindings",
            )
        bindings = []
        for entry in raw_bindings:
            shaped = _build_perimeter_binding(entry)
            if shaped:
                bindings.append(shaped)
        if not bindings:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_bindings",
            )
        if len(bindings) > PERIMETER_MAX_BINDINGS:
            _LOGGER.warning(
                "mount_perimeter_pulse: %d bindings supplied, firmware "
                "renders at most %d — truncating",
                len(bindings), PERIMETER_MAX_BINDINGS,
            )
            bindings = bindings[:PERIMETER_MAX_BINDINGS]

        # subtitle_text is DEAD as of firmware 0.4.21 — the narrative line
        # renders the dial's normal subtitle (cmd/overlay machinery) so the
        # face never contradicts the operator's subtitle. Warn instead of
        # silently swallowing it so old automations get a breadcrumb.
        if call.data.get("subtitle_text"):
            _LOGGER.warning(
                "mount_perimeter_pulse: subtitle_text is ignored by "
                "firmware >= 0.4.21 — use apply_overlay subtitle_mode/"
                "subtitle_text instead",
            )

        payload: dict[str, Any] = {
            "face_id": "perimeter_pulse",
            "schema_version": 1,
            "bindings": bindings,
        }
        if call.data.get("contiguous") is not None:
            payload["contiguous"] = bool(call.data["contiguous"])
        if call.data.get("bar_thickness") not in (None, ""):
            try:
                payload["bar_thickness"] = min(
                    max(int(call.data["bar_thickness"]), 1), 40
                )
            except (TypeError, ValueError):
                pass
        if call.data.get("bar_opacity") not in (None, ""):
            try:
                payload["bar_opacity"] = min(
                    max(float(call.data["bar_opacity"]), 0.0), 1.0
                )
            except (TypeError, ValueError):
                pass

        await _publish_face_mount("perimeter_pulse", payload, targets, retained=True)
        _LOGGER.info(
            "mount_perimeter_pulse: %d binding(s) → %d dial(s)",
            len(bindings), len(targets),
        )

    async def _update_perimeter_state(call) -> None:
        """Push state updates for one or more Perimeter Pulse bindings.

        The live lane for HACS-mounted (self-driven) rings. Wire
        contract (fw pp_on_state, canonical since 0.4.21):
        ``{"bindings": [{id, state | value | event}]}`` — ``state`` is
        string-matched against the binding's ``active_state``,
        ``value`` (0-1) drives the gradient treatment, ``event: true``
        fires a ripple/flash pulse. Helm-published faces don't need
        this — Helm's feeder pushes the same topic itself.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        targets = _resolve_targets(hass, device_id)
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        raw_states = call.data.get("states") or []
        if not isinstance(raw_states, list) or not raw_states:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_states",
            )

        clean_states = []
        for entry in raw_states:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("id")
            if not isinstance(sid, str) or not sid.strip():
                continue
            row: dict[str, Any] = {"id": sid.strip()}
            if "state" in entry and entry["state"] is not None:
                row["state"] = str(entry["state"])
            if "value" in entry and entry["value"] is not None:
                try:
                    # Gradient position — firmware treats it as 0-1 and
                    # lerps without clamping (pp_on_state stores the raw
                    # float), so keep the 0-1 contract here exactly like
                    # the mount-side shaper does.
                    row["value"] = min(max(float(entry["value"]), 0.0), 1.0)
                except (TypeError, ValueError):
                    pass
            if entry.get("event"):
                row["event"] = True
            if len(row) > 1:  # has more than just "id"
                clean_states.append(row)
        if not clean_states:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_states",
            )

        # Canonical fw 0.4.21 state schema keys the array as "bindings"
        # ("states"/"entities" survive firmware-side as legacy aliases).
        body = json.dumps({"bindings": clean_states})
        for dial_id, team_id in targets:
            topic = TOPIC_CMD_FACE_STATE.format(
                team_id=team_id, dial_id=dial_id, face_id="perimeter_pulse",
            )
            await mqtt.async_publish(hass, topic, body, retain=False)
        _LOGGER.info(
            "update_perimeter_state: %d update(s) → %d dial(s)",
            len(clean_states), len(targets),
        )

    async def _mount_face(call) -> None:
        """Generic mount — escape hatch for forward-compat with new faces."""
        await _require_admin(call)
        device_id = call.data.get("device_id")
        face_id = call.data.get("face_id")
        if not isinstance(face_id, str) or not face_id.strip():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_face_id",
            )
        face_id = face_id.strip()
        payload = call.data.get("payload") or {}
        if not isinstance(payload, dict):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        targets = _resolve_targets(hass, device_id)
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        # Stamp face_id and schema_version into the payload if the caller
        # didn't supply them — the firmware tolerates either shape.
        payload.setdefault("face_id", face_id)
        payload.setdefault("schema_version", 1)
        retained = bool(call.data.get("retained", True))
        await _publish_face_mount(face_id, payload, targets, retained=retained)
        _LOGGER.info(
            "mount_face: %s → %d dial(s) (retained=%s)",
            face_id, len(targets), retained,
        )

    async def _unmount_face(call) -> None:
        """Dismiss the active face and restore the theme's clock.

        Completes the reactive-face lifecycle (mount on event, dismiss
        when done — e.g. the Charge face when the EV session ends).
        Publishes ``cmd/face/<id>/unmount`` AND clears the retained
        mount message: without the clear, the next broker (re)connect
        would replay the stale retained mount and resurrect the face
        the automation just dismissed.
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        face_id = (call.data.get("face_id") or "").strip()
        if not face_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_face_id",
            )
        targets = _resolve_targets(hass, device_id)
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        for dial_id, team_id in targets:
            unmount_topic = TOPIC_CMD_FACE_UNMOUNT.format(
                team_id=team_id, dial_id=dial_id, face_id=face_id,
            )
            mount_topic = TOPIC_CMD_FACE_MOUNT.format(
                team_id=team_id, dial_id=dial_id, face_id=face_id,
            )
            await mqtt.async_publish(hass, unmount_topic, "{}", retain=False)
            # Empty retained publish clears the stale mount (retained-
            # lifecycle contract: state-driving cmd/* gets an empty-clear
            # at lifecycle end).
            await mqtt.async_publish(hass, mount_topic, "", retain=True)
        _LOGGER.info("unmount_face: %s → %d dial(s)", face_id, len(targets))

    async def _add_menu_item(call) -> None:
        """Add (or update) a temporary menu item on the targeted dials.

        SF-parity path: Helm's listener creates a REAL MenuItem on each
        dial's resolved menu profile (idempotent on ``key``), so the item
        shows in every layout, appears in the operator UI, syncs onward
        to Salesforce, and — with ``ttl`` — hides then deletes itself
        via the shared availability machinery. Selecting the item fires
        a ``deckhand_dial_event`` on HA's bus like any menu press, so
        the item's ACTION is an HA automation listening for it (or set
        item_type/action_data for a directly-bound HA action).
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        key = (call.data.get("key") or "").strip()
        label = (call.data.get("label") or "").strip()
        if not key or not label:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        targets = _resolve_targets(hass, device_id)
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        payload: dict[str, Any] = {
            "action": "add",
            "key": key,
            "label": label,
            "icon": (call.data.get("icon") or "").strip(),
        }
        ttl = call.data.get("ttl")
        if ttl:
            payload["ttl_s"] = int(ttl)
        if call.data.get("item_type"):
            payload["item_type"] = str(call.data["item_type"]).strip()
        if isinstance(call.data.get("action_data"), dict):
            payload["action_data"] = call.data["action_data"]
        # Climate comfort range. Promoted to first-class fields because
        # burying them in the action_data dict makes the commonest climate
        # tweak an advanced operation. Explicit fields win over the same keys
        # passed inside action_data. Helm clamps anything wider than the
        # thermostat reports it supports.
        for field, ad_key in (("climate_min_temp", "min_temp"), ("climate_max_temp", "max_temp")):
            value = call.data.get(field)
            if value is not None:
                payload.setdefault("action_data", {})[ad_key] = int(value)
        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_MENU_REQUEST.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body, retain=False)
        _LOGGER.info(
            "add_menu_item: key=%s label=%r ttl=%s → %d dial(s)",
            key, label, ttl, len(targets),
        )

    async def _remove_menu_item(call) -> None:
        """Remove a temporary menu item (by key) from the targeted dials."""
        await _require_admin(call)
        device_id = call.data.get("device_id")
        key = (call.data.get("key") or "").strip()
        if not key:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        targets = _resolve_targets(hass, device_id)
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        body = json.dumps({"action": "remove", "key": key})
        for dial_id, team_id in targets:
            topic = TOPIC_MENU_REQUEST.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body, retain=False)
        _LOGGER.info("remove_menu_item: key=%s → %d dial(s)", key, len(targets))

    async def _publish_alarm_request(call, payload: dict) -> int:
        """Shared fan-out for the alarm services."""
        targets = _resolve_targets(hass, call.data.get("device_id"))
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_ALARM_REQUEST.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body, retain=False)
        return len(targets)

    async def _create_alarm(call) -> None:
        """Create/update a wake-up alarm on the targeted dials (by name).

        SF CreateAlarm parity. The Helm scheduler beat fires it (sunrise
        ramp -> ring); snooze/dismiss gestures on the dial work as
        always and relay back to the HA event bus.
        """
        await _require_admin(call)
        name = (call.data.get("name") or "").strip()
        fire_time = (call.data.get("time") or "").strip()
        if not name or not fire_time:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        payload: dict[str, Any] = {"action": "create", "name": name, "time": fire_time}
        if isinstance(call.data.get("days"), list):
            payload["days"] = call.data["days"]
        for k in ("one_time_date", "label", "sunrise_color"):
            if call.data.get(k):
                payload[k] = str(call.data[k])
        for k in ("snooze_minutes", "sunrise_duration_s"):
            if call.data.get(k) is not None:
                payload[k] = int(call.data[k])
        if call.data.get("enabled") is not None:
            payload["enabled"] = bool(call.data["enabled"])
        n = await _publish_alarm_request(call, payload)
        _LOGGER.info("create_alarm: %r %s → %d dial(s)", name, fire_time, n)

    async def _set_alarm_enabled(call, enabled: bool) -> None:
        await _require_admin(call)
        name = (call.data.get("name") or "").strip()
        if not name:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        n = await _publish_alarm_request(
            call, {"action": "enable" if enabled else "disable", "name": name}
        )
        _LOGGER.info("%s_alarm: %r → %d dial(s)", "enable" if enabled else "disable", name, n)

    async def _enable_alarm(call) -> None:
        """Re-enable a named alarm (SF parity: reverse of DisableAlarm)."""
        await _set_alarm_enabled(call, True)

    async def _disable_alarm(call) -> None:
        """Disable a named alarm without deleting it (SF DisableAlarm parity)."""
        await _set_alarm_enabled(call, False)

    async def _snooze_alarm(call) -> None:
        """Snooze the actively ringing/sunrising alarm (SF SnoozeAlarm parity)."""
        await _require_admin(call)
        n = await _publish_alarm_request(call, {"action": "snooze"})
        _LOGGER.info("snooze_alarm → %d dial(s)", n)

    async def _dismiss_alarm(call) -> None:
        """Dismiss the active alarm (SF DismissAlarm parity)."""
        await _require_admin(call)
        n = await _publish_alarm_request(call, {"action": "dismiss"})
        _LOGGER.info("dismiss_alarm → %d dial(s)", n)

    async def _publish_credential_request(call, payload: dict) -> int:
        targets = _resolve_targets(hass, call.data.get("device_id"))
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_CREDENTIAL_REQUEST.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body, retain=False)
        return len(targets)

    async def _enroll_credential(call) -> None:
        """Enroll an NFC credential (tap-first, assign-later).

        Tap the new card/fob on the dial, then call this within 15
        minutes — the most recent unknown tap on that dial is promoted.
        Or pass uid_hash explicitly. Identity + Role are created by
        name if new (SF EnrollCredential parity).
        """
        await _require_admin(call)
        identity_name = (call.data.get("identity_name") or "").strip()
        role = (call.data.get("role") or "").strip()
        if not identity_name or not role:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        payload: dict[str, Any] = {
            "action": "enroll",
            "identity_name": identity_name,
            "role": role,
        }
        for k in ("uid_hash", "label"):
            if call.data.get(k):
                payload[k] = str(call.data[k]).strip()
        n = await _publish_credential_request(call, payload)
        _LOGGER.info("enroll_credential: %r as %r → %d dial(s)", identity_name, role, n)

    async def _revoke_credential(call) -> None:
        """Revoke every active credential for an identity (checkout flow)."""
        await _require_admin(call)
        identity_name = (call.data.get("identity_name") or "").strip()
        if not identity_name:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        n = await _publish_credential_request(
            call, {"action": "revoke", "identity_name": identity_name}
        )
        _LOGGER.info("revoke_credential: %r → %d dial(s)", identity_name, n)

    async def _restore_credential(call) -> None:
        """Restore previously revoked credentials for an identity."""
        await _require_admin(call)
        identity_name = (call.data.get("identity_name") or "").strip()
        if not identity_name:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        n = await _publish_credential_request(
            call, {"action": "restore", "identity_name": identity_name}
        )
        _LOGGER.info("restore_credential: %r → %d dial(s)", identity_name, n)

    async def _publish_schedule_request(call, payload: dict) -> int:
        targets = _resolve_targets(hass, call.data.get("device_id"))
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        body = json.dumps(payload)
        for dial_id, team_id in targets:
            topic = TOPIC_SCHEDULE_REQUEST.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body, retain=False)
        return len(targets)

    async def _create_schedule(call) -> None:
        """Create/update a Helm schedule by name (SF CreateSchedule parity).

        The schedule's payload can push a theme, a menu profile, and/or an
        announcement when it fires. Targets default to the addressed dial;
        pass target_scope: "all" for team-wide.
        """
        await _require_admin(call)
        name = (call.data.get("name") or "").strip()
        if not name:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        payload: dict[str, Any] = {"action": "create", "name": name}
        for k in ("trigger_type", "time", "recurrence", "one_time_date", "webhook_slug",
                  "theme", "menu_profile", "announcement_message", "announcement_from",
                  "announcement_animation", "target_scope"):
            if call.data.get(k):
                payload[k] = str(call.data[k]).strip()
        if isinstance(call.data.get("days"), list):
            payload["days"] = call.data["days"]
        for k in ("minute_offset", "interval_hours", "hour_start", "hour_end",
                  "announcement_duration_s"):
            if call.data.get(k) is not None:
                payload[k] = int(call.data[k])
        if call.data.get("enabled") is not None:
            payload["enabled"] = bool(call.data["enabled"])
        n = await _publish_schedule_request(call, payload)
        _LOGGER.info("create_schedule: %r → %d dial(s)", name, n)

    async def _schedule_by_name(call, action: str) -> None:
        await _require_admin(call)
        name = (call.data.get("name") or "").strip()
        if not name:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        n = await _publish_schedule_request(call, {"action": action, "name": name})
        _LOGGER.info("%s_schedule: %r → %d dial(s)", action, name, n)

    async def _enable_schedule(call) -> None:
        await _schedule_by_name(call, "enable")

    async def _disable_schedule(call) -> None:
        """SF DisableSchedule parity — pause without deleting."""
        await _schedule_by_name(call, "disable")

    async def _fire_schedule(call) -> None:
        """Execute the named schedule right now (test-fire semantics)."""
        await _schedule_by_name(call, "fire")

    async def _push_menu(call) -> None:
        """Switch the dial(s) to a different menu profile by name.

        SF PushMenu parity. Transient by design — the role-resolved menu
        returns on the next auto-flow push (fresh boot / role change).
        """
        await _require_admin(call)
        profile = (call.data.get("profile") or "").strip()
        if not profile:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_payload",
            )
        targets = _resolve_targets(hass, call.data.get("device_id"))
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        body = json.dumps({"action": "push", "profile": profile})
        for dial_id, team_id in targets:
            topic = TOPIC_MENU_REQUEST.format(team_id=team_id, dial_id=dial_id)
            await mqtt.async_publish(hass, topic, body, retain=False)
        _LOGGER.info("push_menu: %r → %d dial(s)", profile, len(targets))

    async def _send_invitation(call) -> None:
        """Send a touch-and-hold consent prompt to a dial.

        Publishes ``cmd/face/invitation/mount`` directly (no Helm row).
        The dial queues internally (FIFO, cap 5) and shows when the
        clock face is idle. The guest's touch-and-hold accepts; encoder
        spin declines. Listen for ``deckhand_dial_event`` with
        ``type: "invitation_response"`` and ``payload.invitation_id``
        matching what you sent to wire automations on accept/decline.

        Multi-targeting: the device selector accepts multiple dials
        (and Room/group devices fan out via _resolve_targets), and
        ``all_dials: true`` targets every discovered dial across all
        Deckhand entries. When ``invitation_id`` is omitted, each dial
        gets its OWN generated id — invitations are per-dial consent
        prompts, so one guest accepting must not alias another dial's
        pending prompt. An explicitly supplied ``invitation_id`` is
        used verbatim on every target (the caller owns the lifecycle
        and can retract everywhere with a single cancel_invitation).

        presentation="menu" (helm#165) is the quiet path: instead of a
        prompt, the invitation appears as a menu item and selecting it
        accepts. That transport goes via the Helm invitation_request
        plane (see the branch below), not the direct face mount.
        """
        import secrets

        await _require_admin(call)
        device_id = call.data.get("device_id")
        targets = _resolve_targets(hass, device_id)
        if call.data.get("all_dials"):
            # Union, not replace — a group-resolved dial that hasn't
            # heartbeated yet may be missing from the discovery cache.
            seen = set(targets)
            for t in _resolve_all_dials(hass):
                if t not in seen:
                    seen.add(t)
                    targets.append(t)
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        text = (call.data.get("text") or "").strip()
        if not text:
            raise ServiceValidationError("text is required")

        # Explicit id → verbatim on every target. Omitted → generated
        # per dial inside the publish loop below.
        explicit_id = (call.data.get("invitation_id") or "").strip()

        presentation = (call.data.get("presentation") or "prompt").strip().lower()
        if presentation not in ("prompt", "menu"):
            raise ServiceValidationError(
                'presentation must be "prompt" or "menu"'
            )

        # Per-send arrival transition (helm#224). ARRIVAL ONLY —
        # cancel_invitation deliberately never carries this; a cancel is
        # usually a correction or a timeout and making it theatrical
        # draws attention to a mistake.
        transition = _resolve_transition(call.data.get("transition"))

        if presentation == "menu":
            # Quiet invitation (helm#165): a menu item instead of a
            # screen-taking prompt. The direct face-mount transport
            # cannot do this — Helm injects the MenuItem server-side
            # into each dial's resolved menu profile — so this routes
            # through the invitation_request plane (non-retained, one
            # publish per dial; field names match Helm's invitation
            # API body, parsed by handle_invitation_request). Requires
            # the Helm MQTT listener on the broker; pure-offline
            # brokers silently drop it. Dials sharing a menu profile
            # all see the item; selecting it accepts.
            menu_position = None
            raw_pos = call.data.get("menu_position")
            if raw_pos is not None and str(raw_pos).strip() != "":
                try:
                    menu_position = int(raw_pos)
                except (TypeError, ValueError) as exc:
                    raise ServiceValidationError(
                        "menu_position must be a positive integer"
                    ) from exc
                if menu_position < 1:
                    raise ServiceValidationError(
                        "menu_position must be a positive integer"
                    )

            try:
                menu_ttl_s = int(call.data.get("ttl_s") or 600)
            except (TypeError, ValueError) as exc:
                raise ServiceValidationError("ttl_s must be an integer") from exc
            menu_ttl_s = max(10, min(86400, menu_ttl_s))

            request: dict[str, Any] = {
                "action": "send",
                "presentation": "menu",
                "text": text[:240],
                "ttl_s": menu_ttl_s,
            }
            if menu_position is not None:
                request["menu_position"] = menu_position
            menu_subtitle = str(call.data.get("subtitle") or "").strip()
            if menu_subtitle:
                request["subtitle"] = menu_subtitle[:120]
            menu_from = (call.data.get("from_name") or "").strip()
            if menu_from:
                request["from_name"] = menu_from[:64]
            menu_on_accept = call.data.get("on_accept")
            if isinstance(menu_on_accept, dict) and menu_on_accept:
                request["on_accept"] = menu_on_accept
            # A quiet invitation still mounts a prompt when the guest
            # selects the menu item, so the choice is meaningful here
            # too — Helm bakes it into the item's stored payload.
            if transition != DEFAULT_TRANSITION:
                request["transition"] = transition

            sent_ids = []
            for dial_id, team_id in targets:
                invitation_id = explicit_id or secrets.token_urlsafe(12)
                request["invitation_id"] = invitation_id
                sent_ids.append(f"{dial_id}={invitation_id}")
                topic = TOPIC_INVITATION_REQUEST.format(
                    team_id=team_id, dial_id=dial_id,
                )
                await mqtt.async_publish(
                    hass, topic, json.dumps(request), retain=False
                )
            _LOGGER.info(
                "send_invitation (menu): %s → %d dial(s) (%s)",
                text[:40], len(targets), ", ".join(sent_ids),
            )
            return

        try:
            hold_seconds = int(call.data.get("hold_seconds") or 5)
        except (TypeError, ValueError) as exc:
            raise ServiceValidationError("hold_seconds must be an integer") from exc
        hold_seconds = max(2, min(15, hold_seconds))

        try:
            ttl_s = int(call.data.get("ttl_s") or 600)
        except (TypeError, ValueError) as exc:
            raise ServiceValidationError("ttl_s must be an integer") from exc
        ttl_s = max(10, min(86400, ttl_s))

        priority = (call.data.get("priority") or "normal").strip()
        if priority not in ("normal", "urgent"):
            priority = "normal"

        payload: dict[str, Any] = {
            "text": text[:240],
            "subtitle": str(call.data.get("subtitle") or "")[:120],
            "accept_label": str(call.data.get("accept_label") or "Hold to accept")[:64],
            "decline_label": str(call.data.get("decline_label") or "Spin to decline")[:64],
            "hold_seconds": hold_seconds,
            "ttl_s": ttl_s,
            "priority": priority,
        }
        theme_override = (call.data.get("theme_override") or "").strip()
        if theme_override:
            payload["theme_override"] = theme_override[:40]
        solid_color = (call.data.get("solid_color") or "").strip()
        if solid_color:
            payload["solid_color"] = solid_color[:16]
        from_name = (call.data.get("from_name") or "").strip()
        if from_name:
            payload["from_name"] = from_name[:64]

        # on_accept is OPAQUE JSON. The firmware echoes it verbatim in
        # the response event so HA automations (or Helm, if the dial's
        # team has the Helm row) can route on accept. For pure-HA flows
        # this is usually omitted — HA listens for deckhand_dial_event
        # with type=invitation_response and branches in the automation.
        on_accept = call.data.get("on_accept")
        if isinstance(on_accept, dict) and on_accept:
            payload["on_accept"] = on_accept

        if transition != DEFAULT_TRANSITION:
            payload["transition"] = transition

        sent_ids: list[str] = []
        for dial_id, team_id in targets:
            invitation_id = explicit_id or secrets.token_urlsafe(12)
            payload["invitation_id"] = invitation_id
            sent_ids.append(f"{dial_id}={invitation_id}")
            topic = TOPIC_CMD_FACE_MOUNT.format(
                team_id=team_id, dial_id=dial_id, face_id="invitation",
            )
            await mqtt.async_publish(hass, topic, json.dumps(payload))
        _LOGGER.info(
            "send_invitation: %s → %d dial(s) (%s)",
            text[:40], len(targets), ", ".join(sent_ids),
        )

    async def _cancel_invitation(call) -> None:
        """Retract an invitation by id.

        Publishes ``cmd/face/invitation/cancel``. If the invitation was
        still pending in the queue or actively displayed, the dial
        emits a response event with ``response: "cancelled"``.

        Also publishes ``{"action": "cancel", ...}`` to the Helm
        ``invitation_request`` plane so quiet (menu-presentation)
        invitations get retracted server-side — Helm removes the
        MenuItem and republishes the menu. Both publishes are harmless
        where they don't apply: dials ignore cancels for ids they
        don't hold, and Helm ignores unknown ids (on brokers without
        the Helm listener the request-plane publish is simply
        dropped).
        """
        await _require_admin(call)
        device_id = call.data.get("device_id")
        targets = _resolve_targets(hass, device_id)
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )

        invitation_id = (call.data.get("invitation_id") or "").strip()
        if not invitation_id:
            raise ServiceValidationError("invitation_id is required")

        body = json.dumps({"invitation_id": invitation_id})
        request_body = json.dumps(
            {"action": "cancel", "invitation_id": invitation_id}
        )
        for dial_id, team_id in targets:
            topic = (
                f"deckhand/{team_id}/dial/{dial_id}/cmd/face/invitation/cancel"
            )
            await mqtt.async_publish(hass, topic, body)
            await mqtt.async_publish(
                hass,
                TOPIC_INVITATION_REQUEST.format(team_id=team_id, dial_id=dial_id),
                request_body,
                retain=False,
            )
        _LOGGER.info(
            "cancel_invitation: %s → %d dial(s)",
            invitation_id, len(targets),
        )

    # Only register once
    if not hass.services.has_service(DOMAIN, "push_theme"):
        hass.services.async_register(DOMAIN, "push_theme", _push_theme)
    if not hass.services.has_service(DOMAIN, "send_announcement"):
        hass.services.async_register(DOMAIN, "send_announcement", _send_announcement)
    if not hass.services.has_service(DOMAIN, "send_countdown"):
        hass.services.async_register(DOMAIN, "send_countdown", _send_countdown)
    if not hass.services.has_service(DOMAIN, "reboot"):
        hass.services.async_register(DOMAIN, "reboot", _reboot)
    if not hass.services.has_service(DOMAIN, "set_dnd"):
        hass.services.async_register(DOMAIN, "set_dnd", _set_dnd)
    if not hass.services.has_service(DOMAIN, "apply_overlay"):
        hass.services.async_register(DOMAIN, "apply_overlay", _apply_overlay)
    if not hass.services.has_service(DOMAIN, "update_now_playing"):
        hass.services.async_register(DOMAIN, "update_now_playing", _update_now_playing)
    if not hass.services.has_service(DOMAIN, "update_from_media_player"):
        hass.services.async_register(
            DOMAIN, "update_from_media_player", _update_from_media_player
        )
    if not hass.services.has_service(DOMAIN, "update_sensor_value"):
        hass.services.async_register(DOMAIN, "update_sensor_value", _update_sensor_value)
    if not hass.services.has_service(DOMAIN, "set_timezone"):
        hass.services.async_register(DOMAIN, "set_timezone", _set_timezone)
    if not hass.services.has_service(DOMAIN, "set_dial_settings"):
        hass.services.async_register(DOMAIN, "set_dial_settings", _set_dial_settings)
    if not hass.services.has_service(DOMAIN, "set_sunset"):
        hass.services.async_register(DOMAIN, "set_sunset", _set_sunset)
    if not hass.services.has_service(DOMAIN, "mount_perimeter_pulse"):
        hass.services.async_register(
            DOMAIN, "mount_perimeter_pulse", _mount_perimeter_pulse
        )
    if not hass.services.has_service(DOMAIN, "update_perimeter_state"):
        hass.services.async_register(
            DOMAIN, "update_perimeter_state", _update_perimeter_state
        )
    if not hass.services.has_service(DOMAIN, "mount_face"):
        hass.services.async_register(DOMAIN, "mount_face", _mount_face)
    if not hass.services.has_service(DOMAIN, "unmount_face"):
        hass.services.async_register(DOMAIN, "unmount_face", _unmount_face)
    if not hass.services.has_service(DOMAIN, "add_menu_item"):
        hass.services.async_register(DOMAIN, "add_menu_item", _add_menu_item)
    if not hass.services.has_service(DOMAIN, "remove_menu_item"):
        hass.services.async_register(DOMAIN, "remove_menu_item", _remove_menu_item)
    for _svc, _fn in (
        ("create_alarm", _create_alarm),
        ("enable_alarm", _enable_alarm),
        ("disable_alarm", _disable_alarm),
        ("snooze_alarm", _snooze_alarm),
        ("dismiss_alarm", _dismiss_alarm),
        ("enroll_credential", _enroll_credential),
        ("revoke_credential", _revoke_credential),
        ("restore_credential", _restore_credential),
        ("create_schedule", _create_schedule),
        ("enable_schedule", _enable_schedule),
        ("disable_schedule", _disable_schedule),
        ("fire_schedule", _fire_schedule),
        ("push_menu", _push_menu),
    ):
        if not hass.services.has_service(DOMAIN, _svc):
            hass.services.async_register(DOMAIN, _svc, _fn)
    if not hass.services.has_service(DOMAIN, "send_invitation"):
        hass.services.async_register(DOMAIN, "send_invitation", _send_invitation)
    if not hass.services.has_service(DOMAIN, "cancel_invitation"):
        hass.services.async_register(DOMAIN, "cancel_invitation", _cancel_invitation)


def _coerce_epoch(value: Any) -> int | None:
    """Best-effort convert to Unix seconds (int).

    Accepts a bare epoch (int/float or all-digit string), a ``datetime``
    instance, or an ISO-8601 string. Naive datetimes/strings are
    interpreted in local time (what an operator typing a wall-clock time
    means); tz-aware values (e.g. sun.sun's UTC ``next_setting``) convert
    correctly via ``timestamp()``. Returns ``None`` on anything
    unparseable so callers can raise a clean validation error.
    """
    if isinstance(value, bool):  # bool is an int subclass — reject it
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        try:
            # Python <3.11 fromisoformat chokes on a trailing 'Z'.
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(dt.timestamp())
    return None


def _sunset_epoch_from_entity(
    hass: HomeAssistant, entity_id: str, attribute: str
) -> int | None:
    """Read a sunset timestamp off an HA entity and return a Unix epoch.

    Prefers the named ``attribute`` (sun.sun exposes ``next_setting`` — a
    tz-aware UTC datetime that is always the *upcoming* sunset). Falls
    back to the entity's own state so a ``timestamp`` sensor or an
    ``input_datetime`` whose value IS the sunset works too. Returns
    ``None`` if nothing readable/parseable is present.
    """
    state = hass.states.get(entity_id)
    if state is None:
        return None
    raw = state.attributes.get(attribute)
    if raw in (None, ""):
        raw = state.state
    if raw in (None, "", "unknown", "unavailable"):
        return None
    return _coerce_epoch(raw)


def _resolve_dial(
    hass: HomeAssistant, device_id: str | None
) -> tuple[str, str] | None:
    """Resolve an HA device ID to (dial_id, team_id).

    Looks up the device in the registry, finds its owning config entry,
    and reads the team_id from that entry so services work correctly across
    multiple Deckhand config entries (multi-team setups).

    Group devices (identifier prefix ``group:``) are NOT handled here —
    callers that should fan out across a room use :func:`_resolve_targets`
    instead. This function only returns a single (dial_id, team_id) for
    bare-dial identifiers and is kept for the few code paths
    (media_player binding resolution) that genuinely point at one dial.
    """
    if not device_id:
        return None
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if not device:
        return None

    dial_id: str | None = None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            # Skip group identifiers — they're a fan-out scope, not a
            # single dial. The caller should be using _resolve_targets.
            if identifier.startswith("group:"):
                continue
            dial_id = identifier
            break
    if not dial_id:
        return None

    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        team_id = entry.data.get(CONF_TEAM_ID)
        if team_id:
            return dial_id, str(team_id)
    return None


async def _fetch_announcement_image(
    hass: HomeAssistant,
    snapshot_entity: str | None,
    image_url: str | None,
) -> bytes | None:
    """Fetch image bytes for an announcement backdrop.

    ``snapshot_entity`` (a camera entity) takes precedence over
    ``image_url``. Returns the raw encoded bytes (JPEG/PNG/…) or ``None``
    on any failure — the caller treats ``None`` as "send the text
    announcement without an image" so a camera hiccup never drops the
    whole notification.
    """
    if snapshot_entity:
        try:
            from homeassistant.components.camera import async_get_image

            image = await async_get_image(hass, snapshot_entity)
            return image.content
        except Exception:  # noqa: BLE001 - best-effort; log + fall through
            _LOGGER.warning(
                "send_announcement: failed to fetch camera snapshot from %s "
                "— sending text announcement without image",
                snapshot_entity,
                exc_info=True,
            )
            return None

    if image_url:
        try:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(hass)
            async with session.get(image_url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "").lower()
                # Require image/* explicitly — guards against fetching a
                # non-image (SSRF-ish) URL and feeding it to PIL.
                if not content_type.startswith("image/"):
                    _LOGGER.warning(
                        "send_announcement: image_url %s returned non-image "
                        "content-type %r — skipping image",
                        image_url,
                        content_type,
                    )
                    return None
                data = await resp.read()
                if not data:
                    _LOGGER.warning(
                        "send_announcement: image_url %s returned empty body",
                        image_url,
                    )
                    return None
                return data
        except Exception:  # noqa: BLE001 - best-effort; log + fall through
            _LOGGER.warning(
                "send_announcement: failed to fetch image_url %s "
                "— sending text announcement without image",
                image_url,
                exc_info=True,
            )
            return None

    return None


def _resolve_all_dials(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Every discovered dial across every Deckhand config entry.

    Drawn from the per-entry ``dials`` heartbeat cache, so it reflects
    dials that have actually announced themselves — a decommissioned
    dial ages out of the cache and stops receiving fleet-wide pushes.
    """
    out: list[tuple[str, str]] = []
    for store in hass.data.get(DOMAIN, {}).values():
        if not isinstance(store, dict):
            continue
        team_id = store.get("team_id")
        if not team_id:
            continue
        for dial_id in (store.get("dials") or {}):
            if dial_id:
                out.append((dial_id, str(team_id)))
    return out


def _resolve_transition(raw: Any) -> str:
    """Validate a service call's ``transition`` field (helm#224).

    Raises on an unknown value rather than silently degrading. HA
    service calls come from an automation an operator wrote and is
    watching; a curtain that quietly didn't happen reads as a broken
    dial, whereas a validation error names the four valid options in the
    HA log where the author will find it. (Helm's webhook ingress and
    the firmware still degrade to fade — that is the right posture for
    traffic nobody is watching.)
    """
    if raw is None:
        return DEFAULT_TRANSITION
    value = str(raw).strip().lower()
    if not value:
        return DEFAULT_TRANSITION
    if value not in TRANSITIONS:
        raise ServiceValidationError(
            f"transition must be one of {', '.join(TRANSITIONS)} (got {raw!r})"
        )
    return value


def _resolve_targets(
    hass: HomeAssistant, device_id: str | list | None
) -> list[tuple[str, str]]:
    """Resolve service targeting input to (dial_id, team_id) tuples.

    Accepted shapes (every service handler passes ``device_id`` through
    here, so all of them gain these automatically):

    * Bare dial device → single tuple.
    * Room device (``group:{id}`` identifier) → every member dial from
      the cached groups dict the MQTT subscriber populates.
    * A LIST of devices (the selectors are ``multiple: true``) → the
      deduplicated union of each entry's resolution.
    * The literal string ``"all"`` (alone or in the list) → every
      discovered dial across all Deckhand entries. Deliberately an
      explicit sentinel, never the omitted-field default — forgetting
      the field must not broadcast to the fleet.

    Empty list if nothing resolves (unknown device, empty room, no
    dials discovered yet). Handlers branch on that to raise
    ServiceValidationError so HA shows a meaningful error rather than
    silently no-op'ing.
    """
    if not device_id:
        return []
    ids = device_id if isinstance(device_id, list) else [device_id]
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(t: tuple[str, str]) -> None:
        if t not in seen:
            seen.add(t)
            out.append(t)

    for did in ids:
        if isinstance(did, str) and did.strip().lower() == "all":
            for t in _resolve_all_dials(hass):
                _add(t)
            continue
        for t in _resolve_one_target(hass, did):
            _add(t)
    return out


def _resolve_one_target(
    hass: HomeAssistant, device_id: str | None
) -> list[tuple[str, str]]:
    """Resolve a single HA device ID (dial or Room) — see _resolve_targets."""
    if not device_id:
        return []
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if not device:
        return []

    # Walk the identifiers — a single device should only have one
    # Deckhand identifier in practice, but be defensive.
    identifier: str | None = None
    for domain, ident in device.identifiers:
        if domain == DOMAIN:
            identifier = ident
            break
    if not identifier:
        return []

    # Resolve owning team from the device's config entry. Both bare
    # dials and rooms are owned by exactly one Deckhand entry.
    team_id: str | None = None
    entry_id_owner: str | None = None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        team_id = entry.data.get(CONF_TEAM_ID)
        if team_id:
            entry_id_owner = entry_id
            break
    if not team_id or not entry_id_owner:
        return []
    team_id = str(team_id)

    if identifier.startswith("group:"):
        gid = identifier[len("group:"):]
        store = hass.data.get(DOMAIN, {}).get(entry_id_owner, {})
        group = (store.get("groups") or {}).get(gid)
        if not group:
            _LOGGER.warning(
                "Group %s not in cached groups list for entry %s — "
                "Helm hasn't published the catalog yet, or the group "
                "was deleted server-side", gid, entry_id_owner,
            )
            return []
        dial_ids = group.get("dial_ids") or []
        return [(d, team_id) for d in dial_ids if d]

    # Bare dial identifier — single-target fan-out.
    return [(identifier, team_id)]
