"""Repairs issues: firmware too old for a feature, a dial offline for a day,
blueprints not imported.

* **Firmware too old** — one issue per dial, raised when a service asks it
  for something its firmware ignores (``_repairs.FIRMWARE_GATES``). The
  command is still sent; the issue says what to update and why. It clears
  on the first heartbeat whose firmware honours everything it listed (or,
  after a restart forgot the list, every gate).
* **Dial offline** — a dial still on the server (``_repairs.server_presence``)
  with no live heartbeat for 24 h, counted from the last one this Home
  Assistant saw, or from startup for a dial that has not reported since
  (then the notice says so). Cleared by the next live heartbeat or by
  deregistering the dial.
* **Dial not on the server** — a dial in the device registry that the server
  no longer has: no retained cmd/config and no status after the startup
  grace, or its retained cmd/config emptied (deregistered) this session.
  Never also called offline. Fixable: the flow (repairs.py) removes the
  device. Cleared when the server publishes the dial's config or it reports.
* **Blueprints not imported** — none of the shipped blueprints is under
  ``config/blueprints/automation/``. The integration package does not carry
  them (HACS installs ``custom_components/deckhand`` only), so the fix flow
  (repairs.py) lists My Home Assistant import links and re-checks.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from . import _repairs
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BLUEPRINTS_ISSUE_ID = "blueprints_not_imported"


def firmware_issue_id(dial_id: str) -> str:
    return f"firmware_too_old_{dial_id}"


def offline_issue_id(dial_id: str) -> str:
    return f"dial_offline_{dial_id}"


NOT_ON_SERVER_PREFIX = "dial_not_on_server_"


def not_on_server_issue_id(dial_id: str) -> str:
    return f"{NOT_ON_SERVER_PREFIX}{dial_id}"


def dial_name(store: dict[str, Any], dial_id: str) -> str:
    label = (store.get("labels") or {}).get(dial_id)
    return f"{label} ({dial_id})" if label else dial_id


def _store_for(hass: HomeAssistant, dial_id: str, team_id: str) -> dict[str, Any] | None:
    for store in hass.data.get(DOMAIN, {}).values():
        if isinstance(store, dict) and str(store.get("team_id")) == str(team_id) \
                and dial_id in (store.get("dials") or {}):
            return store
    return None


def _has_issue(hass: HomeAssistant, issue_id: str) -> bool:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


@callback
def note_firmware_gaps(
    hass: HomeAssistant, targets: Iterable[tuple[str, str]], features: Iterable[str]
) -> list[str]:
    """Raise (or widen) the firmware issue of every target dial whose
    firmware cannot honour ``features``. Returns the dial ids flagged."""
    features = list(features)
    flagged: list[str] = []
    for dial_id, team_id in targets:
        store = _store_for(hass, dial_id, team_id)
        if store is None:
            continue
        heartbeat = store["dials"][dial_id]
        missing = _repairs.missing_features(heartbeat.get("fw_ver"), features)
        if not missing:
            continue
        gaps = store.setdefault("_fw_gaps", {}).setdefault(dial_id, [])
        for key in missing:
            if key not in gaps:
                gaps.append(key)
        ir.async_create_issue(
            hass,
            DOMAIN,
            firmware_issue_id(dial_id),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="firmware_too_old",
            translation_placeholders={
                "dial": dial_name(store, dial_id),
                "version": str(heartbeat.get("fw_ver")),
                "features": _repairs.feature_labels(gaps),
                "required": _repairs.required_version(gaps),
            },
        )
        flagged.append(dial_id)
    return flagged


@callback
def on_heartbeat(
    hass: HomeAssistant,
    store: dict[str, Any],
    dial_id: str,
    heartbeat: dict[str, Any],
    live: bool = True,
) -> None:
    """A dial reported: it is on the server. A live report clears its offline
    issue (a retained status replayed at subscribe says nothing about now);
    either clears its firmware issue once the firmware honours what the
    issue asked for."""
    _mark_on_server(hass, store, dial_id)
    if live and _has_issue(hass, offline_issue_id(dial_id)):
        ir.async_delete_issue(hass, DOMAIN, offline_issue_id(dial_id))
    issue_id = firmware_issue_id(dial_id)
    if not _has_issue(hass, issue_id):
        return
    gaps = (store.get("_fw_gaps") or {}).get(dial_id)
    if _repairs.firmware_satisfies(heartbeat.get("fw_ver"), gaps):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        (store.get("_fw_gaps") or {}).pop(dial_id, None)


def _mark_on_server(hass: HomeAssistant, store: dict[str, Any], dial_id: str) -> None:
    store.setdefault("_server", {})[dial_id] = _repairs.ON_SERVER
    if _has_issue(hass, not_on_server_issue_id(dial_id)):
        ir.async_delete_issue(hass, DOMAIN, not_on_server_issue_id(dial_id))


def _raise_not_on_server(hass: HomeAssistant, store: dict[str, Any], dial_id: str, entry_id: str | None) -> None:
    if _has_issue(hass, offline_issue_id(dial_id)):
        ir.async_delete_issue(hass, DOMAIN, offline_issue_id(dial_id))
    ir.async_create_issue(
        hass,
        DOMAIN,
        not_on_server_issue_id(dial_id),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="dial_not_on_server",
        translation_placeholders={"dial": dial_name(store, dial_id)},
        data={"dial_id": dial_id, "entry_id": entry_id},
    )


@callback
def on_config(
    hass: HomeAssistant,
    store: dict[str, Any],
    dial_id: str,
    cleared: bool,
    known: bool,
    entry_id: str | None,
) -> None:
    """A retained cmd/config arrived for a dial. Non-empty: the server has
    it. Empty: the server dropped it (deregistered) — it is no longer
    offline, and a dial Home Assistant knows gets the fixable notice."""
    if not cleared:
        _mark_on_server(hass, store, dial_id)
        return
    store.setdefault("_server", {})[dial_id] = _repairs.OFF_SERVER
    if known:
        _raise_not_on_server(hass, store, dial_id, entry_id)
    elif _has_issue(hass, offline_issue_id(dial_id)):
        ir.async_delete_issue(hass, DOMAIN, offline_issue_id(dial_id))


@callback
def forget_dial(hass: HomeAssistant, dial_id: str, store: dict[str, Any] | None = None) -> None:
    """A deregistered or deleted dial's issues go with it, and (given its
    entry's store) everything the entry remembered about it."""
    for issue_id in (
        offline_issue_id(dial_id), firmware_issue_id(dial_id), not_on_server_issue_id(dial_id)
    ):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    if store is None:
        return
    for key in ("dials", "labels", "_fw_gaps", "_live_seen", "_server"):
        bucket = store.get(key)
        if isinstance(bucket, dict):
            bucket.pop(dial_id, None)


def last_live(store: dict[str, Any], dial_id: str) -> Any:
    """ISO time of the dial's last live (not retained) heartbeat, or None."""
    return (store.get("_live_seen") or {}).get(dial_id)


@callback
def remove_dial_device(hass: HomeAssistant, store: dict[str, Any] | None, dial_id: str) -> bool:
    """Delete the dial's device from the registry and forget it. Returns
    False (and does nothing) while the dial is still reporting."""
    if store is not None and not _repairs.can_remove_dial(last_live(store, dial_id), datetime.now()):
        return False
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, dial_id)})
    if device is not None:
        registry.async_remove_device(device.id)
    forget_dial(hass, dial_id, store)
    return True


def entry_dial_ids(hass: HomeAssistant, entry_id: str) -> list[str]:
    """Dial ids of the entry's dial devices (not Rooms) in the registry —
    includes dials that have not reported since startup."""
    registry = dr.async_get(hass)
    out: list[str] = []
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        for domain, ident in device.identifiers:
            if domain == DOMAIN and not str(ident).startswith("group:"):
                out.append(str(ident))
    return out


@callback
def check_offline(
    hass: HomeAssistant,
    store: dict[str, Any],
    dial_ids: Iterable[str],
    watching_since: datetime,
    now: datetime | None = None,
    entry_id: str | None = None,
) -> list[str]:
    """Raise the offline issue for every dial on the server and quiet for
    OFFLINE_AFTER_S, and the not-on-server issue for every dial the server
    no longer has. Returns the dial ids called offline."""
    now = now or datetime.now()
    raised: list[str] = []
    server = store.get("_server") or {}
    for dial_id in dict.fromkeys([*dial_ids, *(store.get("dials") or {})]):
        presence = _repairs.server_presence(server.get(dial_id), watching_since, now)
        if presence == _repairs.PENDING:
            continue
        if presence == _repairs.GONE:
            _raise_not_on_server(hass, store, dial_id, entry_id)
            continue
        silent, key = _repairs.offline_notice(last_live(store, dial_id), watching_since, now)
        if silent < _repairs.OFFLINE_AFTER_S:
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            offline_issue_id(dial_id),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders={
                "dial": dial_name(store, dial_id),
                "hours": str(int(silent // 3600)),
            },
        )
        raised.append(dial_id)
    return raised


def scan_blueprint_files(config_dir: str) -> list[str]:
    """File names one level under ``<config>/blueprints/automation/``.
    Blocking I/O: run in the executor."""
    root = os.path.join(config_dir, "blueprints", "automation")
    found: list[str] = []
    try:
        folders = os.listdir(root)
    except OSError:
        return found
    for folder in folders:
        path = os.path.join(root, folder)
        if not os.path.isdir(path):
            continue
        try:
            found.extend(os.listdir(path))
        except OSError:
            continue
    return found


async def async_blueprints_imported(hass: HomeAssistant) -> bool:
    found = await hass.async_add_executor_job(scan_blueprint_files, hass.config.config_dir)
    return _repairs.blueprints_present(found)


async def async_check_blueprints(hass: HomeAssistant) -> bool:
    """Raise or clear the blueprints issue. Returns True when imported."""
    if await async_blueprints_imported(hass):
        ir.async_delete_issue(hass, DOMAIN, BLUEPRINTS_ISSUE_ID)
        return True
    ir.async_create_issue(
        hass,
        DOMAIN,
        BLUEPRINTS_ISSUE_ID,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        learn_more_url=_repairs.BLUEPRINTS_DOCS_URL,
        translation_key=BLUEPRINTS_ISSUE_ID,
        translation_placeholders={"links": _repairs.blueprint_links_markdown()},
    )
    return False
