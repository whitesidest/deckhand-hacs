"""Deckhand Smart Dial integration for Home Assistant."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event

try:  # get_url is in core since 2021 but guard for older installs
    from homeassistant.helpers.network import NoURLAvailableError, get_url
except ImportError:  # pragma: no cover - defensive fallback
    get_url = None  # type: ignore[assignment]

    class NoURLAvailableError(Exception):  # type: ignore[no-redef]
        """Fallback when helpers.network is unavailable."""

from .const import (
    CONF_MEDIA_PLAYER_BINDINGS,
    CONF_TEAM_ID,
    DOMAIN,
    MANUFACTURER,
    HARDWARE_MODELS,
    MEDIA_PLAYER_DEBOUNCE_S,
    PLATFORMS,
    TOPIC_CMD_ANNOUNCE,
    TOPIC_CMD_CONFIG,
    TOPIC_CMD_NOW_PLAYING,
    TOPIC_CMD_OVERLAY,
    TOPIC_CMD_REBOOT,
    TOPIC_CMD_SENSOR_VALUE,
    TOPIC_CMD_THEME,
    TOPIC_STATUS,
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

# Overlay field validation — mirrors
# helm/apps/themes/services/overlay.py. Keep in sync. We fail fast in
# HA land rather than rely on the firmware to silently ignore bad input
# so automation authors see meaningful errors.
_OVERLAY_SUBTITLE_MODES = {"theme", "custom", "date", "date_year", "none"}
_OVERLAY_HOME_FACES = {"theme", "clock", "message", "sensor", "image", "blank"}
_OVERLAY_STRING_FIELDS = (
    "subtitle_text",
    "home_message",
    "sensor_entity_id",
    "sensor_label",
)
_OVERLAY_QUAD_SLOTS = (2, 3, 4)  # slot 1 is the legacy sensor_entity_id pair
_OVERLAY_MARQUEE_POSITIONS = {"subtitle", "ring"}

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
        # DEFAULT_THEMES so the picker is never blank.
        "themes": [],
        # (dial_id, entity_id) -> {"ts": float, "fingerprint": tuple}
        "_media_player_debounce": {},
        # Disposable that unsubs the active state-change listener. Swapped
        # out on options update so the binding list stays in sync.
        "_media_player_unsub": None,
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

        dial_id = payload.get("device_id")
        if not dial_id:
            _LOGGER.warning("Status message missing device_id on %s", msg.topic)
            return

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
    # *custom* themes — not just the hardcoded system list shipped in
    # services.yaml. Empty/missing topic falls back to DEFAULT_THEMES.
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
        # Normalize to a stable {slug, name, is_system} list. Drop
        # entries missing a slug so a malformed payload can't strand
        # the picker on a blank option.
        normalized = []
        for t in themes:
            if not isinstance(t, dict):
                continue
            slug = t.get("slug")
            if not isinstance(slug, str) or not slug:
                continue
            normalized.append({
                "slug": slug,
                "name": str(t.get("name") or slug),
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

    entry.async_on_unload(
        await mqtt.async_subscribe(hass, event_topic, _handle_event, qos=0)
    )

    # Set up entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    _register_services(hass, entry)

    # Register state-change listeners for any configured media_player
    # bindings, and keep them in sync when the options flow changes.
    _reload_media_player_listeners(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: DeckhandConfigEntry) -> bool:
    """Unload a Deckhand config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        store = hass.data[DOMAIN].pop(entry.entry_id, None)
        if store and store.get("_media_player_unsub"):
            store["_media_player_unsub"]()
        for binding in (store or {}).get("_sensor_bindings", {}).values():
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
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    label = store.get("labels", {}).get(dial_id)
    name = label or f"Deckhand {dial_id}"

    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, dial_id)},
        manufacturer=MANUFACTURER,
        name=name,
        model=model,
        sw_version=data.get("fw_ver"),
        configuration_url=f"http://{data.get('ip', '0.0.0.0')}",
    )


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

    # Title: media_title is the canonical field. Jellyfin/Plex sometimes
    # leave it empty on the transition into a new item — fall back to
    # media_content_id so we emit something rather than a blank.
    title = attr.get("media_title") or ""
    if not title:
        # content_id is often a URL/path; only use it if it's short.
        cid = attr.get("media_content_id") or ""
        if cid and len(cid) < 96 and "/" not in cid:
            title = cid

    # Artist: album_artist is the fallback for compilations; for video
    # we prefer the series title so "The Bear - S2E3" reads right.
    artist = attr.get("media_artist") or attr.get("media_album_artist") or ""
    if not artist:
        artist = attr.get("media_series_title") or ""

    # Source: app_name is set by Jellyfin/Plex/Spotify; otherwise use the
    # friendly name so the dial can show which device is playing.
    source = attr.get("app_name") or attr.get("friendly_name") or entity_id

    album_art_url = _resolve_entity_picture_url(hass, attr.get("entity_picture") or "")

    is_playing = state.state == "playing"

    payload: dict[str, Any] = {
        "title": str(title)[:96],
        "artist": str(artist)[:96],
        "source": str(source)[:32],
        "is_playing": bool(is_playing),
    }
    if album_art_url:
        payload["album_art_url"] = str(album_art_url)[:256]
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


def _format_sensor_value(raw: Any) -> str:
    """Trim sensor readings to something a 240x240 dial can display cleanly.

    Home Assistant hands us string states like "21.3333333333334"; that's
    unreadable on a 2.8cm glass circle. Round floats to 2 decimals, leave
    integers alone (no trailing ".00" on a count), and pass non-numeric
    strings through so statuses like "heating" or "home" render verbatim.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s[:48]
    if f.is_integer() and "." not in s and "e" not in s.lower():
        return str(int(f))[:48]
    return f"{f:.2f}"[:48]


def _build_sensor_value_payload(
    hass: HomeAssistant, entity_id: str, label: str
) -> dict[str, Any] | None:
    """Pull a sensor reading + unit off an HA entity for cmd/sensor_value."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if state.state in (None, "", "unknown", "unavailable"):
        return None
    unit = state.attributes.get("unit_of_measurement") or ""
    payload: dict[str, Any] = {
        "entity_id": str(entity_id)[:128],
        "label": str(label or state.attributes.get("friendly_name") or "")[:64],
        "value": _format_sensor_value(state.state),
        "unit": str(unit)[:8],
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
    await mqtt.async_publish(hass, topic, json.dumps(payload))
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
        hass.async_create_task(mqtt.async_publish(hass, topic, json.dumps(payload)))

    unsub = async_track_state_change_event(hass, watched, _on_change)
    bindings[dial_id] = {"entity_ids": watched, "unsub": unsub}


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

    async def _push_theme(call) -> None:
        """Push a theme to a dial."""
        device_id = call.data.get("device_id")
        theme = call.data.get("theme")
        if not isinstance(theme, str) or not theme.strip():
            _LOGGER.warning("push_theme called with empty or invalid theme")
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_theme",
            )
        resolved = _resolve_dial(hass, device_id)
        if not resolved:
            _LOGGER.warning(
                "push_theme: could not resolve device_id %s to a dial", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        dial_id, team_id = resolved
        topic = TOPIC_CMD_THEME.format(team_id=team_id, dial_id=dial_id)
        await mqtt.async_publish(hass, topic, json.dumps({"id": theme}))
        _LOGGER.info("Pushed theme '%s' to %s", theme, dial_id)

    async def _send_announcement(call) -> None:
        """Send an announcement to a dial."""
        device_id = call.data.get("device_id")
        message = call.data.get("message")
        from_name = call.data.get("from_name", "Home Assistant")
        duration = call.data.get("duration", 30)
        animation = call.data.get("animation", "none")
        if not isinstance(message, str) or not message.strip():
            _LOGGER.warning("send_announcement called with empty message")
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="empty_message",
            )
        resolved = _resolve_dial(hass, device_id)
        if not resolved:
            _LOGGER.warning(
                "send_announcement: could not resolve device_id %s to a dial",
                device_id,
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        dial_id, team_id = resolved
        topic = TOPIC_CMD_ANNOUNCE.format(team_id=team_id, dial_id=dial_id)
        payload = {
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
        await mqtt.async_publish(hass, topic, json.dumps(payload))
        _LOGGER.info(
            "Sent announcement to %s: %s (animation=%s)",
            dial_id, message, animation,
        )

    async def _send_countdown(call) -> None:
        """Send a countdown announcement to a dial.

        Mirrors _send_announcement but layers in the countdown_to,
        celebration_message, celebration_animation, and (optionally)
        celebration_theme_id fields the firmware reads to switch into
        countdown mode.
        """
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

        resolved = _resolve_dial(hass, device_id)
        if not resolved:
            _LOGGER.warning(
                "send_countdown: could not resolve device_id %s to a dial",
                device_id,
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        dial_id, team_id = resolved
        topic = TOPIC_CMD_ANNOUNCE.format(team_id=team_id, dial_id=dial_id)

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

        await mqtt.async_publish(hass, topic, json.dumps(payload))
        _LOGGER.info(
            "Sent countdown to %s: target=%s celeb=%r anim=%s theme=%r",
            dial_id,
            target_epoch,
            celebration_message,
            celebration_animation,
            celebration_theme,
        )

    async def _apply_overlay(call) -> None:
        """Apply a partial-theme overlay to a dial.

        Unlike push_theme, this does NOT replace the dial's current
        theme — it merges a handful of fields (subtitle, home face,
        brightness, etc.) on top of whatever theme is running. Useful
        for short-lived weather / sensor / doorbell state flashes.
        """
        device_id = call.data.get("device_id")
        resolved = _resolve_dial(hass, device_id)
        if not resolved:
            _LOGGER.warning(
                "apply_overlay: could not resolve device_id %s to a dial", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        dial_id, team_id = resolved

        payload: dict[str, Any] = {}

        subtitle_mode = call.data.get("subtitle_mode")
        subtitle_text = call.data.get("subtitle_text")
        if subtitle_mode is not None:
            if subtitle_mode not in _OVERLAY_SUBTITLE_MODES:
                raise ServiceValidationError(
                    f"subtitle_mode must be one of {sorted(_OVERLAY_SUBTITLE_MODES)}"
                )
            payload["subtitle_mode"] = subtitle_mode
        elif isinstance(subtitle_text, str) and subtitle_text.strip():
            # User supplied subtitle_text but didn't pick a mode. Without
            # subtitle_mode=custom the firmware happily stores the text
            # but never displays it (theme default mode wins) — auto-flip
            # it so "type subtitle, see subtitle" is the obvious UX.
            payload["subtitle_mode"] = "custom"

        home_face = call.data.get("home_face")
        if home_face is not None:
            if home_face not in _OVERLAY_HOME_FACES:
                raise ServiceValidationError(
                    f"home_face must be one of {sorted(_OVERLAY_HOME_FACES)}"
                )
            payload["home_face"] = home_face
            # Legacy alias for older firmware that still reads home_mode.
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

        if not payload:
            raise ServiceValidationError(
                "apply_overlay requires at least one field to be set"
            )

        # Multi-sensor block: quadrant slots 2-4 + marquee. Slot 1 is the
        # legacy sensor_entity_id/sensor_label pair handled above; we fold
        # it into the quad list here so the firmware's `sensors` parser
        # owns all four slots in one place.
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

        marquee_position = call.data.get("marquee_position")
        if marquee_position is not None and marquee_position not in _OVERLAY_MARQUEE_POSITIONS:
            raise ServiceValidationError(
                f"marquee_position must be one of {sorted(_OVERLAY_MARQUEE_POSITIONS)}"
            )

        if quad_entries or marquee_entries or marquee_position:
            sensors_block: dict[str, Any] = {}
            if quad_entries:
                sensors_block["quad"] = quad_entries
            if marquee_entries:
                sensors_block["marquee"] = marquee_entries
            if marquee_position:
                sensors_block["marquee_position"] = marquee_position
            payload["sensors"] = sensors_block
            # Slot 1 was originally pushed via sensor_entity_id/sensor_label
            # at the top level; the firmware's sensors-block path is a
            # superset, so drop the legacy fields if they're present (the
            # multi-block parser will set them via slot 1).
            payload.pop("sensor_entity_id", None)
            payload.pop("sensor_label", None)

        topic = TOPIC_CMD_OVERLAY.format(team_id=team_id, dial_id=dial_id)
        await mqtt.async_publish(hass, topic, json.dumps(payload))
        _LOGGER.info("Applied overlay to %s: %s", dial_id, sorted(payload.keys()))

        # Push live values + register listeners for every entity the dial
        # will display (legacy slot 1, quad 2-4, marquee). cmd/overlay only
        # carries the entity_id; the firmware never reaches into HA on its
        # own. Same auto-push + state-change-listener pattern that already
        # makes slot 1 work for the sensor face.
        bound_entities: list[tuple[str, str]] = []
        for q in quad_entries:
            bound_entities.append((q["entity_id"], q.get("label") or ""))
        for m in marquee_entries:
            bound_entities.append((m["entity_id"], m.get("label") or ""))

        for eid, lbl in bound_entities:
            await _push_sensor_value_for_entity(hass, team_id, dial_id, eid, lbl)
        if bound_entities:
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
        device_id = call.data.get("device_id")
        resolved = _resolve_dial(hass, device_id)
        if not resolved:
            _LOGGER.warning(
                "update_now_playing: could not resolve device_id %s", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        dial_id, team_id = resolved

        title = call.data.get("title", "")
        if title is None:
            title = ""
        payload: dict[str, Any] = {
            "title": str(title)[:96],
            "artist": str(call.data.get("artist") or "")[:96],
            "source": str(call.data.get("source") or "")[:32],
            "is_playing": bool(call.data.get("is_playing", True)),
        }
        art = call.data.get("album_art_url")
        if art:
            payload["album_art_url"] = str(art)[:256]

        topic = TOPIC_CMD_NOW_PLAYING.format(team_id=team_id, dial_id=dial_id)
        await mqtt.async_publish(hass, topic, json.dumps(payload))
        _LOGGER.debug("Now-playing -> %s: %s", dial_id, payload.get("title"))

    async def _update_from_media_player(call) -> None:
        """Extract now-playing fields from a media_player entity and publish.

        Thin wrapper that reads the entity's current state + attributes,
        normalizes them via ``_extract_now_playing``, and publishes to
        ``cmd/now_playing``. Designed as the manual counterpart to the
        auto-push bindings configured in the options flow — same payload
        shape, no debounce.
        """
        device_id = call.data.get("device_id")
        entity_id = call.data.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise ServiceValidationError("entity_id is required")
        entity_id = entity_id.strip()
        if not entity_id.startswith("media_player."):
            raise ServiceValidationError(
                f"entity_id must be a media_player (got {entity_id})"
            )

        resolved = _resolve_dial(hass, device_id)
        if not resolved:
            _LOGGER.warning(
                "update_from_media_player: could not resolve device_id %s",
                device_id,
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        dial_id, team_id = resolved

        payload = _extract_now_playing(hass, entity_id)
        if payload is None:
            raise ServiceValidationError(
                f"Unknown entity_id '{entity_id}' — is it loaded?"
            )

        _LOGGER.info(
            "update_from_media_player: %s -> %s (title=%r, playing=%s)",
            entity_id,
            dial_id,
            payload.get("title", ""),
            payload.get("is_playing", False),
        )
        await _publish_now_playing(hass, entry, dial_id, team_id, payload)

    async def _update_sensor_value(call) -> None:
        """Stream a sensor-value update to a dial (cmd/sensor_value).

        High-frequency ephemeral update. If the dial is currently on the
        sensor home face it refreshes in place; otherwise the value is
        buffered until the sensor face next activates.
        """
        device_id = call.data.get("device_id")
        resolved = _resolve_dial(hass, device_id)
        if not resolved:
            _LOGGER.warning(
                "update_sensor_value: could not resolve device_id %s", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        dial_id, team_id = resolved

        entity_id = call.data.get("entity_id", "")
        if not entity_id:
            raise ServiceValidationError("entity_id is required")

        value = call.data.get("value", "")
        # Accept numbers or strings; publish as string so the dial's parser
        # can render verbatim without locale issues. Float values get
        # rounded to two decimals so template sensors that pass raw HA
        # readings ("21.3333333333333") render cleanly on the dial.
        payload: dict[str, Any] = {
            "entity_id": str(entity_id)[:128],
            "label": str(call.data.get("label") or "")[:64],
            "value": _format_sensor_value(value),
            "unit": str(call.data.get("unit") or "")[:8],
        }
        icon = call.data.get("icon")
        if icon:
            payload["icon"] = str(icon)[:16]
        color = call.data.get("color")
        if color:
            payload["color"] = str(color)[:7]

        topic = TOPIC_CMD_SENSOR_VALUE.format(team_id=team_id, dial_id=dial_id)
        await mqtt.async_publish(hass, topic, json.dumps(payload))
        _LOGGER.debug("Sensor-value -> %s: %s=%s", dial_id, entity_id, value)

    async def _reboot(call) -> None:
        """Reboot a dial."""
        device_id = call.data.get("device_id")
        resolved = _resolve_dial(hass, device_id)
        if not resolved:
            _LOGGER.warning(
                "reboot: could not resolve device_id %s to a dial", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        dial_id, team_id = resolved
        topic = TOPIC_CMD_REBOOT.format(team_id=team_id, dial_id=dial_id)
        await mqtt.async_publish(hass, topic, "{}")
        _LOGGER.info("Sent reboot command to %s", dial_id)

    async def _set_timezone(call) -> None:
        """Push a per-dial timezone via cmd/config.

        The firmware does ``setenv("TZ", value, 1); tzset()`` with whatever
        we send, so we pre-translate the IANA name to a POSIX TZ string
        from our static map. Unknown zones are rejected loudly rather than
        silently shipping ``UTC0`` and confusing the user.
        """
        device_id = call.data.get("device_id")
        iana = call.data.get("timezone")
        if not isinstance(iana, str) or not iana.strip():
            raise ServiceValidationError("timezone is required")
        iana = iana.strip()
        posix = _IANA_TO_POSIX.get(iana)
        if posix is None:
            raise ServiceValidationError(
                f"Unsupported timezone '{iana}'. Pick one from the dropdown."
            )

        resolved = _resolve_dial(hass, device_id)
        if not resolved:
            _LOGGER.warning(
                "set_timezone: could not resolve device_id %s to a dial", device_id
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
            )
        dial_id, team_id = resolved
        topic = TOPIC_CMD_CONFIG.format(team_id=team_id, dial_id=dial_id)
        await mqtt.async_publish(hass, topic, json.dumps({"tz": posix}))
        _LOGGER.info("Set timezone for %s: %s (%s)", dial_id, iana, posix)

    # Only register once
    if not hass.services.has_service(DOMAIN, "push_theme"):
        hass.services.async_register(DOMAIN, "push_theme", _push_theme)
    if not hass.services.has_service(DOMAIN, "send_announcement"):
        hass.services.async_register(DOMAIN, "send_announcement", _send_announcement)
    if not hass.services.has_service(DOMAIN, "send_countdown"):
        hass.services.async_register(DOMAIN, "send_countdown", _send_countdown)
    if not hass.services.has_service(DOMAIN, "reboot"):
        hass.services.async_register(DOMAIN, "reboot", _reboot)
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


def _resolve_dial(
    hass: HomeAssistant, device_id: str | None
) -> tuple[str, str] | None:
    """Resolve an HA device ID to (dial_id, team_id).

    Looks up the device in the registry, finds its owning config entry,
    and reads the team_id from that entry so services work correctly across
    multiple Deckhand config entries (multi-team setups).
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
