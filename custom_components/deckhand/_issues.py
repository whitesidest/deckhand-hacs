"""Repairs issues: firmware too old for a feature, a dial offline for a day,
blueprints not imported.

* **Firmware too old** — one issue per dial, raised when a service asks it
  for something its firmware ignores (``_repairs.FIRMWARE_GATES``). The
  command is still sent; the issue says what to update and why. It clears
  on the first heartbeat whose firmware honours everything it listed (or,
  after a restart forgot the list, every gate).
* **Dial offline** — no heartbeat for 24 h, counted from the last one this
  Home Assistant saw, or from startup for a dial that has not reported
  since. Cleared by the next heartbeat or by deregistering the dial.
* **Blueprints not imported** — none of the shipped blueprints is under
  ``config/blueprints/automation/``. The package carries copies (HACS
  installs ``custom_components/deckhand`` only, not the repository's
  ``blueprints/``), so the fix flow (repairs.py) installs them into
  ``config/blueprints/automation/deckhand/``; the My Home Assistant import
  links stay as the alternative.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from typing import Any, Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from . import _repairs
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BLUEPRINTS_ISSUE_ID = "blueprints_not_imported"
# The packaged copies of the repository's blueprints/automation/deckhand/.
PACKAGED_BLUEPRINTS_DIR = os.path.join(os.path.dirname(__file__), "blueprints", "automation")


def firmware_issue_id(dial_id: str) -> str:
    return f"firmware_too_old_{dial_id}"


def offline_issue_id(dial_id: str) -> str:
    return f"dial_offline_{dial_id}"


def _dial_name(store: dict[str, Any], dial_id: str) -> str:
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
                "dial": _dial_name(store, dial_id),
                "version": str(heartbeat.get("fw_ver")),
                "features": _repairs.feature_labels(gaps),
                "required": _repairs.required_version(gaps),
            },
        )
        flagged.append(dial_id)
    return flagged


@callback
def on_heartbeat(hass: HomeAssistant, store: dict[str, Any], dial_id: str, heartbeat: dict[str, Any]) -> None:
    """A dial reported: clear its offline issue, and its firmware issue once
    the firmware honours what the issue asked for."""
    if _has_issue(hass, offline_issue_id(dial_id)):
        ir.async_delete_issue(hass, DOMAIN, offline_issue_id(dial_id))
    issue_id = firmware_issue_id(dial_id)
    if not _has_issue(hass, issue_id):
        return
    gaps = (store.get("_fw_gaps") or {}).get(dial_id)
    if _repairs.firmware_satisfies(heartbeat.get("fw_ver"), gaps):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        (store.get("_fw_gaps") or {}).pop(dial_id, None)


@callback
def forget_dial(hass: HomeAssistant, dial_id: str) -> None:
    """A deregistered dial's issues go with it."""
    for issue_id in (offline_issue_id(dial_id), firmware_issue_id(dial_id)):
        ir.async_delete_issue(hass, DOMAIN, issue_id)


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
) -> list[str]:
    """Raise the offline issue for every dial quiet for OFFLINE_AFTER_S."""
    now = now or datetime.now()
    raised: list[str] = []
    for dial_id in dict.fromkeys([*dial_ids, *(store.get("dials") or {})]):
        last = ((store.get("dials") or {}).get(dial_id) or {}).get("_last_seen")
        silent = _repairs.seconds_silent(last, watching_since, now)
        if silent < _repairs.OFFLINE_AFTER_S:
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            offline_issue_id(dial_id),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="dial_offline",
            translation_placeholders={
                "dial": _dial_name(store, dial_id),
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


def install_blueprint_files(config_dir: str, source_dir: str = PACKAGED_BLUEPRINTS_DIR) -> list[str]:
    """Copy every shipped blueprint not already under
    ``<config>/blueprints/automation/*/`` into
    ``<config>/blueprints/automation/deckhand/``. Never overwrites: a file of
    that name anywhere there (possibly edited, or a GitHub import in
    ``whitesidest/``) is left alone. Returns the names copied. Raises
    OSError when a copy fails. Blocking I/O: run in the executor."""
    present = set(scan_blueprint_files(config_dir))
    dest = os.path.join(config_dir, "blueprints", "automation", _repairs.BLUEPRINT_INSTALL_FOLDER)
    os.makedirs(dest, exist_ok=True)
    copied: list[str] = []
    for name in _repairs.BLUEPRINT_FILES:
        if name in present:
            continue
        target = os.path.join(dest, name)
        with open(os.path.join(source_dir, name), "rb") as src:
            try:
                out = open(target, "xb")  # "x": fails rather than overwrite
            except FileExistsError:
                continue
            try:
                with out:
                    shutil.copyfileobj(src, out)
            except OSError:
                os.remove(target)  # no half-written blueprint left behind
                raise
        copied.append(name)
    return copied


async def async_install_blueprints(hass: HomeAssistant) -> list[str]:
    """Install the missing shipped blueprints, then have the automation
    editor read the folder afresh. Raises OSError when a copy fails."""
    copied = await hass.async_add_executor_job(install_blueprint_files, hass.config.config_dir)
    # The blueprint integration keeps one DomainBlueprints per domain in
    # hass.data["blueprint"]; drop the automation one's cache so the new
    # files are read from disk (absent when blueprint has not set up yet,
    # and then there is nothing cached).
    domain_blueprints = (hass.data.get("blueprint") or {}).get("automation")
    reset = getattr(domain_blueprints, "async_reset_cache", None)
    if reset is not None:
        await reset()
    if copied:
        _LOGGER.info("Installed Deckhand blueprints: %s", ", ".join(copied))
    return copied


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
