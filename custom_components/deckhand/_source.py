"""Say who sent a command: the ``_src`` stamp for Deckhand's activity record.

Helm and Console keep one activity record of everything done to a dial:
who did it, and from which channel (web, mobile, Home Assistant, API, MCP,
Salesforce, ...). Commands this integration publishes straight to the
broker (announcements, timers, overlays, reboots, faces, invitations, ...)
never pass through either server's request handling, so each server
watches its team's ``cmd/#`` topics and files what it sees. The stamp is
how it can tell "Home Assistant, automation Sunset dim" from an unknown
publisher; the ``*_request`` lanes (theme, settings, menu, alarm,
credential, schedule, invitation) carry it too, so the row the server
writes for them names the same person.

Wire shape (identical in Helm, Console and every client)::

    "_src": {"ch": "home_assistant", "by": "<who, at most 64 chars>"}

Short keys keep it cheap on the dial's 4 KB MQTT buffer; firmware ignores
unknown keys. A payload that would grow past ~3.9 KB is sent unstamped
rather than risk the dial dropping it. Telemetry feeds (sensor values, now
playing, perimeter state, album art) are never stamped: they are not
actions, and the servers exclude them from the record anyway.

``by`` is a label, never an authorisation input: the servers trust it
only to name the row.

No Home Assistant imports here, so the module is testable on its own;
``hass`` and ``context`` are used duck-typed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

SOURCE_KEY = "_src"
SOURCE_CHANNEL = "home_assistant"
BY_MAX_LEN = 64
# esp-mqtt's receive buffer on the dial is 4 KB; leave headroom for the
# topic and MQTT framing. Over this, the stamp is dropped, not the command.
MAX_STAMPED_PAYLOAD_BYTES = 3900
FALLBACK_BY = "Home Assistant"
# A call that carries a parent context but whose automation/script could
# not be identified (renamed, removed, or a trigger we cannot see).
AUTOMATION_BY = "automation"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Checked in this order: when an automation runs a script, both entities
# can carry the run's context, and the script is the nearer cause.
_ORIGIN_DOMAINS = ("script", "automation")


def clean_by(value: Any) -> str:
    """Strip control characters, collapse to one line, clamp to 64."""
    if value is None:
        return ""
    text = _CONTROL_CHARS.sub("", str(value)).strip()
    return text[:BY_MAX_LEN]


def make_source(by: Any) -> dict[str, str]:
    """The ``_src`` object for an action taken by ``by``."""
    return {"ch": SOURCE_CHANNEL, "by": clean_by(by) or FALLBACK_BY}


def with_source(payload: dict[str, Any], src: dict[str, str] | None) -> dict[str, Any]:
    """``payload`` plus ``_src`` — a new dict; the input is not mutated.

    Returned unchanged when there is no source, or when stamping would push
    the serialised payload past MAX_STAMPED_PAYLOAD_BYTES.
    """
    if not src:
        return payload
    stamped = {**payload, SOURCE_KEY: src}
    if len(json.dumps(stamped).encode("utf-8")) > MAX_STAMPED_PAYLOAD_BYTES:
        return payload
    return stamped


def dumps_with_source(payload: dict[str, Any], src: dict[str, str] | None) -> str:
    """``json.dumps(with_source(payload, src))``."""
    return json.dumps(with_source(payload, src))


def _origin_entity(hass: Any, context: Any) -> str | None:
    """``automation: <name>`` / ``script: <name>`` for the automation or
    script whose run produced ``context``, if one can be found.

    An automation run writes its state (``last_triggered``) under the run's
    context, and the actions it calls carry that same context (or, through
    a nested call, a child of it whose ``parent_id`` points back). A script
    entity likewise writes its ``on`` state under the context it runs with.
    So an automation/script entity whose current state context id equals
    the call's context id or parent id is the one that ran it.
    """
    ids = {
        cid
        for cid in (getattr(context, "id", None), getattr(context, "parent_id", None))
        if cid
    }
    if not ids:
        return None
    states = getattr(hass, "states", None)
    if states is None or not hasattr(states, "async_all"):
        return None
    for domain in _ORIGIN_DOMAINS:
        try:
            candidates = states.async_all(domain)
        except Exception:  # noqa: BLE001 - attribution must never break a command
            _LOGGER.debug("could not list %s states for attribution", domain, exc_info=True)
            continue
        for state in candidates:
            state_ctx = getattr(state, "context", None)
            if getattr(state_ctx, "id", None) not in ids:
                continue
            attrs = getattr(state, "attributes", None) or {}
            name = attrs.get("friendly_name") or getattr(state, "entity_id", "") or ""
            name = clean_by(name)
            return clean_by(f"{domain}: {name}") if name else domain
    return None


async def async_describe_actor(hass: Any, context: Any) -> str:
    """Who is behind ``context`` (a service call's or an entity's), as a
    short human label:

    * a person (``context.user_id``) → their Home Assistant user name;
    * an automation or script → ``automation: <name>`` / ``script: <name>``;
    * some other parent context → ``automation``;
    * otherwise → ``Home Assistant``.

    Never raises: attribution is a label, and a failure to work it out must
    not stop the command it describes.
    """
    if context is None:
        return FALLBACK_BY
    user_id = getattr(context, "user_id", None)
    if user_id:
        user = None
        auth = getattr(hass, "auth", None)
        if auth is not None:
            try:
                user = await auth.async_get_user(user_id)
            except Exception:  # noqa: BLE001 - attribution must never break a command
                _LOGGER.debug("could not look up HA user %s", user_id, exc_info=True)
                user = None
        name = getattr(user, "name", None) if user is not None else None
        return clean_by(name or user_id) or FALLBACK_BY
    origin = _origin_entity(hass, context)
    if origin:
        return origin
    if getattr(context, "parent_id", None):
        return AUTOMATION_BY
    return FALLBACK_BY


async def async_source_for(hass: Any, context: Any) -> dict[str, str]:
    """The ``_src`` object for whoever is behind ``context``."""
    return make_source(await async_describe_actor(hass, context))
