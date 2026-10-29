"""What the Repairs issues know: firmware gates, the offline window, the
shipped blueprints. Pure (no Home Assistant imports); ``_issues.py`` turns
these into issue-registry entries.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import quote

# Feature key -> (first firmware that honours it, what the person tried to
# use). The versions are the ones the services and entities already cite:
# DND (set_dnd, the DND sensor), the Perimeter Ring's two layer switches
# (mount_perimeter_ring), and cmd/face/climate/mount (helm#302, first in the
# 0.4.98 build).
FIRMWARE_GATES: dict[str, tuple[tuple[int, int, int], str]] = {
    "dnd": ((0, 4, 44), "Do Not Disturb"),
    "climate_face": ((0, 4, 98), "the Climate face"),
    "ring_theme_colors": ((0, 4, 158), "Perimeter Ring theme colours"),
    "ring_hide_when_calm": ((0, 4, 161), "Perimeter Ring hide-when-calm"),
}

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# A dial silent this long gets a Repairs issue; its next heartbeat clears it.
OFFLINE_AFTER_S = 24 * 3600
# How often the integration looks for dials that have gone quiet.
OFFLINE_CHECK_S = 30 * 60


def parse_version(raw: Any) -> tuple[int, int, int] | None:
    """``"0.4.161"`` -> ``(0, 4, 161)``. Anything else (missing, a
    ``0.4.0-lan`` bench build) is unknown, and an unknown version is never
    called too old."""
    if not isinstance(raw, str):
        return None
    m = _VERSION_RE.match(raw.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _fmt(version: tuple[int, int, int]) -> str:
    return ".".join(str(p) for p in version)


def missing_features(fw_ver: Any, features: Iterable[str]) -> list[str]:
    """The gated features this firmware cannot honour, in gate order."""
    current = parse_version(fw_ver)
    if current is None:
        return []
    wanted = set(features)
    return [k for k, (need, _l) in FIRMWARE_GATES.items() if k in wanted and current < need]


def firmware_satisfies(fw_ver: Any, features: Iterable[str] | None) -> bool:
    """True when the firmware honours every feature (all gates when
    ``features`` is None, i.e. after a restart forgot which were asked)."""
    current = parse_version(fw_ver)
    if current is None:
        return False
    keys = FIRMWARE_GATES if features is None else [k for k in features if k in FIRMWARE_GATES]
    return all(current >= FIRMWARE_GATES[k][0] for k in keys)


def required_version(features: Iterable[str]) -> str:
    keys = [k for k in features if k in FIRMWARE_GATES]
    return _fmt(max(FIRMWARE_GATES[k][0] for k in keys)) if keys else ""


def feature_labels(features: Iterable[str]) -> str:
    return ", ".join(
        f"{FIRMWARE_GATES[k][1]} ({_fmt(FIRMWARE_GATES[k][0])}+)"
        for k in FIRMWARE_GATES if k in set(features)
    )


def seconds_silent(last_seen: Any, watching_since: datetime, now: datetime) -> float:
    """How long a dial has been quiet as far as this Home Assistant knows:
    since its last heartbeat, or since the integration started for a dial
    that has not reported since."""
    since = watching_since
    if isinstance(last_seen, str):
        try:
            seen = datetime.fromisoformat(last_seen)
        except ValueError:
            seen = None
        if seen is not None and seen > since:
            since = seen
    return (now - since).total_seconds()


# ── Blueprints ────────────────────────────────────────────────────────
# The repository's blueprints/automation/deckhand/. HACS installs only
# custom_components/deckhand, so they are not on disk next to the
# integration and cannot be copied: the issue links each one to My Home
# Assistant's import instead. tests/test_repairs.py keeps this list equal
# to the folder.
BLUEPRINT_FILES = (
    "dnd_hold_calls.yaml",
    "door_open_ring_flash.yaml",
    "doorbell_snapshot_announcement.yaml",
    "laundry_done_menu_item.yaml",
    "nfc_known_tap_scene.yaml",
    "nfc_revoked_tap_alert.yaml",
    "nfc_unknown_tap_alert.yaml",
)
BLUEPRINT_RAW_BASE = (
    "https://raw.githubusercontent.com/whitesidest/deckhand-hacs/main/blueprints/automation/deckhand/"
)
DOCS_URL = "https://github.com/whitesidest/deckhand-hacs#readme"
BLUEPRINTS_DOCS_URL = "https://github.com/whitesidest/deckhand-hacs#shipped-blueprints"


def blueprint_import_url(filename: str) -> str:
    return "https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=" + quote(
        BLUEPRINT_RAW_BASE + filename, safe=""
    )


def blueprint_links_markdown() -> str:
    """One Markdown import link per blueprint, for an issue or a form."""
    return "\n".join(
        f"- [{name.removesuffix('.yaml').replace('_', ' ').capitalize()}]({blueprint_import_url(name)})"
        for name in BLUEPRINT_FILES
    )


def blueprints_present(found: Iterable[str]) -> bool:
    """Any shipped blueprint among the file names found under
    ``config/blueprints/automation/*/`` (an import from GitHub lands in
    ``whitesidest/``, a hand copy usually in ``deckhand/``)."""
    return bool(set(BLUEPRINT_FILES) & set(found))
