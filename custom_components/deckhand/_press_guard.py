"""De-duplicate and expire dial presses before Home Assistant acts on them.

Firmware with the press ledger (deckhand-firmware, DECK-F0A4 2026-10-14)
holds a menu press for up to a minute while its MQTT session is down and
re-sends it on reconnect. Every such press carries ``payload.press_id``
(unique per press per dial) and, once it has waited, ``payload.queued_s``.
The same press can therefore arrive twice — a QoS 1 redelivery, or a re-send
after a PUBACK lost with a dying session — and one could in principle arrive
late. Helm and Console apply the same rule; this is the copy for the events
the integration re-fires onto the HA bus (``deckhand_dial_event``) and turns
into media_player calls, so a user automation keyed on a press never runs
twice and never runs a minute late.

Rules, for a ``button_press`` that carries ``press_id`` only (older firmware
is untouched):
  - a repeat of (dial_id, press_id) within :data:`DEDUPE_WINDOW_S` is dropped;
  - a press with ``queued_s`` > :data:`MAX_QUEUED_S`, or a non-zero ``ts``
    more than :data:`MAX_AGE_S` behind now, is refused.

HA-import-free so it is testable without Home Assistant.
"""

from __future__ import annotations

import time
from collections import OrderedDict

DEDUPE_WINDOW_S = 300
MAX_QUEUED_S = 60
MAX_AGE_S = 65  # the dial's 60 s plus clock grace
MAX_TRACKED = 2048

ACCEPT = "accept"
DUPLICATE = "duplicate"
STALE = "stale"


class PressGuard:
    """Remembers recent press ids; one instance per config entry."""

    def __init__(self) -> None:
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()

    def check(self, dial_id: str, envelope: dict, now: float | None = None) -> str:
        """Return ACCEPT, DUPLICATE or STALE for a raw dial event envelope."""
        if not isinstance(envelope, dict) or envelope.get("type") != "button_press":
            return ACCEPT
        inner = envelope.get("payload")
        if not isinstance(inner, dict):
            return ACCEPT
        press_id = inner.get("press_id")
        if not isinstance(press_id, str) or not press_id:
            return ACCEPT
        now = time.time() if now is None else now

        while self._seen:
            key, seen_at = next(iter(self._seen.items()))
            if now - seen_at < DEDUPE_WINDOW_S and len(self._seen) <= MAX_TRACKED:
                break
            self._seen.popitem(last=False)

        key = (dial_id, press_id)
        if key in self._seen:
            return DUPLICATE
        # Recorded before the age check: a refused press's own re-send is a
        # duplicate, not a second refusal.
        self._seen[key] = now

        queued = inner.get("queued_s")
        if isinstance(queued, (int, float)) and not isinstance(queued, bool) and queued > MAX_QUEUED_S:
            return STALE
        ts = envelope.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0 and now - ts > MAX_AGE_S:
            return STALE
        return ACCEPT
