"""The Climate face payload, validated the way Helm's ``ClimatePayload``
(apps/faces/payloads.py) and Console's ``face_payloads.ClimatePayload``
validate it, so a face mounted from Home Assistant is the face their editors
would write.

Keys: ``title`` (<= 22), ``entity_id`` (the thermostat; a setpoint or mode
change on the dial is filed as a request for it), ``min`` / ``max`` (0-200,
min below max — the firmware falls back to 60-85 on an inverted band),
``step`` (1-10), ``hvac_modes`` (<= 6). ``unit`` is never sent: the firmware
draws °F whatever it says. Pure: no Home Assistant imports.
"""
from __future__ import annotations

from typing import Any

FACE_ID = "climate"
TITLE_MAX = 22
ENTITY_ID_MAX = 128
TEMP_MIN, TEMP_MAX = 0, 200
STEP_MIN, STEP_MAX = 1, 10
MAX_MODES = 6
HVAC_MODE_MAX = 12
DEFAULT_MIN, DEFAULT_MAX, DEFAULT_STEP = 60, 85, 1
DEFAULT_TITLE = "Climate"
# Home Assistant's HVACMode values (homeassistant.components.climate).
HVAC_MODES = ("off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only")


class ClimateError(ValueError):
    """A refused field, with an operator-readable sentence."""


def _int(value: Any, field: str, lo: int, hi: int, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ClimateError(f"{field} must be a whole number from {lo} to {hi}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ClimateError(f"{field} must be a whole number from {lo} to {hi}.") from None
    if number != int(number) or not lo <= number <= hi:
        raise ClimateError(f"{field} must be a whole number from {lo} to {hi} (got {value!r}).")
    return int(number)


def _modes(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = [m for m in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        raise ClimateError("hvac_modes must be a list of modes.")
    out: list[str] = []
    for mode in raw:
        if not isinstance(mode, str) or not mode.strip():
            continue
        mode = mode.strip().lower()
        if mode not in HVAC_MODES:
            raise ClimateError(
                f"{mode!r} is not a Home Assistant HVAC mode (use {', '.join(HVAC_MODES)})."
            )
        if mode not in out:
            out.append(mode)
    if len(out) > MAX_MODES:
        raise ClimateError(f"Pick at most {MAX_MODES} HVAC modes; the dial's picker holds {MAX_MODES}.")
    return out


def build_payload(data: dict[str, Any], state: Any = None) -> dict[str, Any]:
    """The ``cmd/face/climate/mount`` payload, or ClimateError.

    ``state`` is the thermostat's Home Assistant state, when known: its
    target temperature, current temperature and mode seed the face so it
    opens on the real numbers (keys ``value``, ``current_temp``,
    ``hvac_mode`` — Helm declares them so they are not stripped).
    """
    entity_id = data.get("entity_id")
    if isinstance(entity_id, list) and len(entity_id) == 1:
        entity_id = entity_id[0]
    if not isinstance(entity_id, str) or not entity_id.strip().startswith("climate."):
        raise ClimateError("entity_id must be a climate entity (climate.*).")
    entity_id = entity_id.strip()
    if len(entity_id) > ENTITY_ID_MAX:
        raise ClimateError(f"entity_id is longer than {ENTITY_ID_MAX} characters.")

    title = data.get("title")
    if title is None or (isinstance(title, str) and not title.strip()):
        title = DEFAULT_TITLE
    if not isinstance(title, str):
        raise ClimateError("title must be text.")
    title = title.strip()
    if len(title) > TITLE_MAX:
        raise ClimateError(f"title is {len(title)} characters; the dial fits {TITLE_MAX}.")

    low = _int(data.get("min"), "min", TEMP_MIN, TEMP_MAX, DEFAULT_MIN)
    high = _int(data.get("max"), "max", TEMP_MIN, TEMP_MAX, DEFAULT_MAX)
    if low >= high:
        raise ClimateError(f"min ({low}) must be below max ({high}).")
    step = _int(data.get("step"), "step", STEP_MIN, STEP_MAX, DEFAULT_STEP)

    payload: dict[str, Any] = {
        "face_id": FACE_ID,
        "schema_version": 1,
        "title": title,
        "entity_id": entity_id,
        "min": low,
        "max": high,
        "step": step,
    }
    modes = _modes(data.get("hvac_modes"))
    if modes:
        payload["hvac_modes"] = modes
    payload.update(live_state(state))
    return payload


def _whole(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(number)) if number == number else None


def live_state(state: Any) -> dict[str, Any]:
    """``value`` / ``current_temp`` / ``hvac_mode`` from a climate state."""
    if state is None:
        return {}
    attrs = getattr(state, "attributes", None) or {}
    out: dict[str, Any] = {}
    target = _whole(attrs.get("temperature"))
    if target is not None:
        out["value"] = target
    current = _whole(attrs.get("current_temperature"))
    if current is not None:
        out["current_temp"] = current
    mode = getattr(state, "state", None)
    if isinstance(mode, str) and mode in HVAC_MODES:
        out["hvac_mode"] = mode
    return out
