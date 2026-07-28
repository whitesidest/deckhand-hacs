"""Pure helpers for the HACS presence heartbeat (R1 executor arbitration).

Deliberately free of Home Assistant imports so the payload builder and the
capability list are unit-testable without a HA runtime — same rationale as
``_media_control.py``. ``__init__.py`` owns the actual MQTT publish + the
``async_track_time_interval`` scheduling; everything that decides *what* the
beacon says lives here.

The beacon is a **retained** message on ``deckhand/<team_id>/hacs/presence``.
Helm and Console consume it to arbitrate who executes a dial action: they
DEFER a domain to HACS iff HACS advertises that domain in ``owns`` AND the
beacon is fresh (``now - ts <= PRESENCE_TTL_S``). Stale or absent → the
server side stays authoritative (fail-toward-Helm), so there is never a
zero-executor window. The embedded wall-clock ``ts`` is what makes a
*retained* beacon safe: a stale retained message re-delivered on reconnect
reads as absent instead of "present forever".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# Domains THIS HACS build actuates locally. THE load-bearing field: Helm and
# Console defer a domain iff it appears here. R1 ships ``media_player`` only —
# the single overlap with the server-side executors (Helm's
# ``_try_execute_ha_action`` / Console's ``try_execute_ha_action``). Widening
# this list (R3, when the SDK mapper handles light/fan/cover/…) is what lets
# the server side defer more domains with NO coordinated release — the beacon
# is the source of truth. Keep it in lockstep with what ``__init__.py``'s
# dispatch (``_dispatch_dial_media_control`` today) actually executes.
PRESENCE_OWNS: list[str] = ["media_player"]

# Refresh cadence (seconds) advertised in the payload and used to schedule the
# re-publish. TTL is 3x the interval so one or two missed beats (jitter) do
# not flip authority.
PRESENCE_INTERVAL_S = 30
PRESENCE_TTL_S = PRESENCE_INTERVAL_S * 3  # 90s


def hacs_version() -> str:
    """Return the integration version from the sibling ``manifest.json``.

    Best-effort: ``presence.version`` is debug / capability-audit metadata,
    never load-bearing for the gate, so a read failure degrades to
    ``"unknown"`` rather than raising.
    """
    try:
        manifest = Path(__file__).with_name("manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return str(data.get("version", "unknown"))
    except Exception:  # noqa: BLE001 - metadata only, never fatal
        return "unknown"


def build_presence_payload(
    entry_id: str,
    *,
    ts: float | None = None,
    version: str | None = None,
    owns: list[str] | None = None,
    online: bool = True,
) -> dict:
    """Build the retained presence beacon payload.

    ``ts`` is epoch seconds (defaults to wall clock now). ``owns`` defaults to
    :data:`PRESENCE_OWNS`. ``version`` defaults to :func:`hacs_version`.
    """
    return {
        "online": online,
        "ts": int(ts if ts is not None else time.time()),
        "interval_s": PRESENCE_INTERVAL_S,
        "version": version if version is not None else hacs_version(),
        "entry_id": entry_id,
        "owns": list(owns) if owns is not None else list(PRESENCE_OWNS),
    }
