"""Pure helpers for the Perimeter Ring LAYER (``perimeter_ring`` overlay).

Deliberately free of Home Assistant imports, like ``_presence.py``, so the
binding layout and the live-state resolution are unit-testable without a HA
runtime. ``__init__.py`` owns the MQTT publishes and the state listeners.

The perimeter is a layer, not a face: ``cmd/face/perimeter_ring/mount``
(retained) composites a ring over whatever hero face is up, and
``cmd/face/perimeter_ring/state`` (not retained) moves its segments. The
contract is Helm's ``apps/faces/payloads.py`` PerimeterRingPayload and
Console's ``services/face_payloads.py``; both re-validate what they publish,
but this integration publishes straight to the broker, so the two rules the
backends enforce on the way in have to hold here too:

* **Distinct angular_center.** In gap mode two arcs with the same centre
  draw on top of each other (the second binding "never appears"). A caller
  who leaves the centres out gets them spread evenly by order, and a layout
  whose arcs collide is re-spaced evenly, exactly as the backends do.
* **The live state the dial can match.** The firmware activates a segment on
  an EXACT ``state == active_state``. Home Assistant says ``on`` where an
  operator writes ``open``; the backends' pollers fold those vocabularies
  together and send the binding's own ``active_state`` when the reading is
  synonymous. :func:`binding_state` is a copy of Helm's
  ``apps.devices.tasks._perimeter_state_updates`` so a ring reads the same
  whichever side feeds it.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

# Mirror of Helm apps/devices/tasks.py _STATE_SYNONYMS (and Console's
# live_faces). Keep the three in lockstep.
STATE_SYNONYMS = {
    "on": "on",
    "open": "on",
    "opened": "on",
    "true": "on",
    "yes": "on",
    "active": "on",
    "detected": "on",
    "motion": "on",
    "unlocked": "on",
    "occupied": "on",
    "wet": "on",
    "home": "on",
    "off": "off",
    "closed": "off",
    "false": "off",
    "no": "off",
    "inactive": "off",
    "clear": "off",
    "locked": "off",
    "away": "off",
    "dry": "off",
}


def canon_state(value: Any) -> str:
    s = str(value if value is not None else "").strip().lower()
    return STATE_SYNONYMS.get(s, s)


def normalize_treatment(
    value: Any, treatments: Iterable[str], aliases: dict[str, str]
) -> str | None:
    """The canonical treatment for ``value``, or None when it is not one.

    ``None``/empty means the default, ``state_color``. Retired names fold to
    their survivor (ripple/sweep/blink → flash, gradient → state_color).
    """
    if value is None or value == "":
        return "state_color"
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    v = aliases.get(v, v)
    return v if v in treatments else None


def _collides(bindings: list[dict[str, Any]]) -> bool:
    segs = [
        (float(b.get("angular_center", 0.0)) % 360.0, max(0.0, float(b.get("angular_width", 24.0))))
        for b in bindings
    ]
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            d = abs(segs[i][0] - segs[j][0]) % 360.0
            d = min(d, 360.0 - d)
            if d < (segs[i][1] + segs[j][1]) / 2.0 - 1e-6:
                return True
    return False


def layout_bindings(bindings: list[dict[str, Any]], contiguous: bool) -> list[dict[str, Any]]:
    """Give every binding a usable ``angular_center``, in place.

    * A binding without one gets ``round(i * 360 / n, 1)`` — the even spread
      the editors and the MCP tool use.
    * Gap mode only: when any two arcs overlap (circular distance between
      centres < the sum of half-widths) ALL bindings are re-spaced evenly by
      order, the guard Helm's and Console's payload models apply. A clean
      manual layout is left alone. Contiguous ignores positions (the
      firmware splits the rim evenly by array order), so it is not touched
      beyond filling in missing centres.
    """
    n = len(bindings)
    if not n:
        return bindings
    for i, b in enumerate(bindings):
        if b.get("angular_center") is None:
            b["angular_center"] = round(i * 360.0 / n, 1)
    if not contiguous and n > 1 and _collides(bindings):
        for i, b in enumerate(bindings):
            b["angular_center"] = float(round(i * 360.0 / n))
    return bindings


def binding_state(binding: dict[str, Any], raw: Any) -> str | None:
    """The state string to push for ``binding`` given the entity's raw HA
    state, or None when there is nothing to send (no reading).

    * ``numeric_threshold`` set: ``above_state`` (default ``on``) when
      float(raw) > threshold, else ``below_state`` (default ``off``); a
      reading that is not a number is passed through raw.
    * otherwise, when the binding's ``active_state`` is synonymous with the
      reading (on≡open, off≡closed, …) the binding's own ``active_state`` is
      sent so the firmware's exact match fires; any other reading is passed
      through as-is.
    """
    if raw is None:
        return None
    s = str(raw)
    thr = binding.get("numeric_threshold")
    if thr is not None:
        try:
            val = float(raw)
            limit = float(thr)
        except (TypeError, ValueError):
            return s
        return str(binding.get("above_state") or "on") if val > limit else str(binding.get("below_state") or "off")
    active = binding.get("active_state")
    if active and canon_state(s) == canon_state(active):
        return str(active)
    return s


def state_updates(
    bindings: Iterable[Any], state_of: Callable[[str], Any]
) -> list[dict[str, str]]:
    """``[{"id", "state"}]`` for every binding whose entity has a reading.

    ``state_of(entity_id)`` returns the raw HA state string or None (entity
    unknown to this Home Assistant, e.g. a binding id that is not an
    entity). Bindings without a reading are left out rather than sent as
    "", so a ring that hides when calm keeps showing a segment it has not
    heard about (firmware 0.4.161).
    """
    out: list[dict[str, str]] = []
    for b in bindings or []:
        if not isinstance(b, dict):
            continue
        eid = b.get("id")
        if not isinstance(eid, str) or not eid:
            continue
        s = binding_state(b, state_of(eid))
        if s is None:
            continue
        out.append({"id": eid, "state": s})
    return out


def fed_bindings(payload: Any, live_state_key: str) -> list[dict[str, Any]]:
    """The bindings of a retained ring mount this integration should feed.

    Empty when the payload is not a ring, has no bindings, or was mounted
    with the live feed switched off (``<live_state_key>: false``).
    """
    if not isinstance(payload, dict):
        return []
    if payload.get(live_state_key) is False:
        return []
    raw = payload.get("bindings")
    if not isinstance(raw, list):
        return []
    out = []
    for b in raw[:16]:
        if isinstance(b, dict) and isinstance(b.get("id"), str) and b["id"].strip():
            out.append(b)
    return out
