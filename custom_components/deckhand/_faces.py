"""The face sync plane, Home Assistant side (docs/face_sync_contract.md v1).

Helm and Console publish the team's face library on two retained topics:

    deckhand/{team}/faces/list          the index + the server's capabilities
    deckhand/{team}/faces/item/{uid}    one full record per face ("" = deleted)

and answer ``deckhand/{team}/faces/request`` exactly once on
``deckhand/{team}/faces/result``, matched by ``request_id``.

Everything here is pure (no Home Assistant imports) so it can be tested on
its own: normalising what the broker hands us (a hostile or buggy publisher
must not balloon memory or inject junk), the rev liveness rule, the small
pre-validation mirror of the servers' validators, the request builder, the
result normaliser, turning a server error into one operator-readable
sentence, and the dial's own "which face am I wearing" report.

The servers are authoritative. The mirror only catches what is certainly
wrong, so Home Assistant never blocks something the server would accept.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from ._source import SOURCE_KEY

PLANE_VERSION = 1
ACTIONS = ("create", "update", "delete", "mount", "test", "clear_test")

# Contract caps (§1.1, §2, §1.3).
INDEX_CAP = 1000
KINDS_CAP = 64
NAME_MAX = 128
DESCRIPTION_MAX = 256
UPDATED_BY_MAX = 64
SERVER_MAX = 32
TIMESTAMP_MAX = 64
SLUG_MAX = 128
PK_MAX = 64
DEFAULT_MAX_PAYLOAD_BYTES = 3600
# An index that claims a bigger cap than this is not believed.
MAX_PAYLOAD_BYTES_CEILING = 16384
# A whole retained item message: the payload cap plus the record's own
# fields. Anything bigger is dropped unparsed.
ITEM_MESSAGE_MAX_BYTES = 16384
# The index is ~150 B per face, 1000 faces at most.
INDEX_MESSAGE_MAX_BYTES = 512 * 1024
RESULT_MESSAGE_MAX_BYTES = 64 * 1024
MAX_DIALS = 32
TTL_MIN_S, TTL_MAX_S, TTL_DEFAULT_S = 30, 900, 300
REQUEST_ID_MAX = 64

UID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Retired face ids → their survivor, as the servers and firmware fold them.
KIND_ALIASES = {"perimeter_pulse": "perimeter_ring", "volume": "now_playing"}

DIAL_OUTCOMES = ("sent", "unknown_dial", "refused")

# Payload mirror (Helm apps/faces/payloads.py HeroPayloadBase).
TOP_LINE_TEXT_MAX = 32
MAX_LINES = 3
LINE_TEXT_MAX = 48
LINE_ENTITY_MAX = 63
IDLE_RETURN_MIN_S, IDLE_RETURN_MAX_S = 10, 86400

FACE_REPORT_FEATURE = "face_report"
FACE_REPORT_SLOTS = ("hero", "bridge", "home")


class FaceSyncError(ValueError):
    """Something the operator has to fix; the message says what."""


# ── small helpers ───────────────────────────────────────────────────


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_text(value: Any, limit: int) -> str | None:
    """A string of at most ``limit`` chars with control characters removed,
    or None when it is not a string or too long."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"[\x00-\x1f\x7f]", "", value)
    if len(text) > limit:
        return None
    return text


def normalize_uid(raw: Any) -> str | None:
    """The canonical lowercase form of a face uid, or None."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not UID_RE.match(raw):
        return None
    return raw.lower()


def canonical_kind(kind: Any) -> str:
    kind = str(kind or "")
    return KIND_ALIASES.get(kind, kind)


def compact_size(payload: Any) -> int:
    """Bytes of ``payload`` as compact JSON — what the size cap measures."""
    return len(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def server_label(server: Any) -> str:
    if server == "helm":
        return "Helm"
    if server == "console":
        return "Deckhand Console"
    return "the Deckhand server"


# ── index (faces/list) ──────────────────────────────────────────────


def _normalize_index_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    uid = normalize_uid(raw.get("uid"))
    rev = raw.get("rev")
    name = _clean_text(raw.get("name"), NAME_MAX)
    kind = raw.get("kind")
    if uid is None or not _is_int(rev) or rev < 1:
        return None
    if not name or not name.strip():
        return None
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        return None
    return {
        "uid": uid,
        "rev": rev,
        "name": name.strip(),
        "kind": kind,
        "system": bool(raw.get("system", False)),
        "enabled": bool(raw.get("enabled", True)),
    }


def normalize_index(raw: Any) -> dict[str, Any] | None:
    """The ``faces/list`` message as a bounded, well-formed index, or None
    when it is not an index at all.

    Malformed entries are dropped one by one (a single bad entry must not
    hide the library); duplicates keep the first; at most INDEX_CAP faces.
    Fields a newer server adds are ignored (contract §7).
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > INDEX_MESSAGE_MAX_BYTES:
            return None
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict) or not isinstance(raw.get("faces"), list):
        return None

    faces: list[dict[str, Any]] = []
    by_uid: dict[str, dict[str, Any]] = {}
    for item in raw["faces"]:
        if len(faces) >= INDEX_CAP:
            break
        entry = _normalize_index_entry(item)
        if entry is None or entry["uid"] in by_uid:
            continue
        faces.append(entry)
        by_uid[entry["uid"]] = entry

    kinds: list[str] = []
    for kind in raw.get("kinds") or []:
        if len(kinds) >= KINDS_CAP:
            break
        if isinstance(kind, str) and KIND_RE.match(kind) and kind not in kinds:
            kinds.append(kind)

    max_bytes = raw.get("max_payload_bytes")
    if not _is_int(max_bytes) or not 1 <= max_bytes <= MAX_PAYLOAD_BYTES_CEILING:
        max_bytes = DEFAULT_MAX_PAYLOAD_BYTES

    server = raw.get("server")
    server = server if isinstance(server, str) and 0 < len(server) <= SERVER_MAX else None
    version = raw.get("v")
    updated_at = _clean_text(raw.get("updated_at"), TIMESTAMP_MAX)

    return {
        "v": version if _is_int(version) else PLANE_VERSION,
        "server": server,
        "kinds": kinds,
        "max_payload_bytes": max_bytes,
        "faces": faces,
        "by_uid": by_uid,
        "truncated": bool(raw.get("truncated", False)),
        "updated_at": updated_at,
    }


# ── items (faces/item/{uid}) ────────────────────────────────────────


def item_uid_from_topic(topic: str) -> str | None:
    """The uid a ``deckhand/{team}/faces/item/{uid}`` topic names."""
    parts = (topic or "").split("/")
    if len(parts) != 5 or parts[2] != "faces" or parts[3] != "item":
        return None
    return normalize_uid(parts[4])


def normalize_item(
    raw: Any, topic_uid: str, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
) -> dict[str, Any] | None:
    """One retained face record, bounded and checked, or None to drop it.

    The record's uid must be the one its topic names (trust the topic), the
    payload an object within the size cap, and every text field within the
    contract's limits.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > ITEM_MESSAGE_MAX_BYTES:
            return None
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    uid = normalize_uid(raw.get("uid"))
    if uid is None or uid != topic_uid:
        return None
    rev = raw.get("rev")
    if not _is_int(rev) or rev < 1:
        return None
    name = _clean_text(raw.get("name"), NAME_MAX)
    if not name or not name.strip():
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        return None
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        return None
    if compact_size(payload) > max_payload_bytes:
        return None
    description = _clean_text(raw.get("description") or "", DESCRIPTION_MAX)
    if description is None:
        return None
    updated_by = _clean_text(raw.get("updated_by") or "", UPDATED_BY_MAX)
    if updated_by is None:
        return None
    pk = raw.get("pk")
    slug = raw.get("slug")
    origin = raw.get("origin")
    return {
        "uid": uid,
        "rev": rev,
        "pk": str(pk)[:PK_MAX] if isinstance(pk, (str, int)) and not isinstance(pk, bool) else None,
        "slug": slug[:SLUG_MAX] if isinstance(slug, str) else None,
        "name": name.strip(),
        "description": description,
        "kind": kind,
        "system": bool(raw.get("system", False)),
        "enabled": bool(raw.get("enabled", True)),
        "payload": payload,
        "updated_at": _clean_text(raw.get("updated_at"), TIMESTAMP_MAX),
        "origin": origin[:SERVER_MAX] if isinstance(origin, str) else None,
        "updated_by": updated_by,
    }


def is_live(item: dict[str, Any] | None, index: dict[str, Any] | None) -> bool:
    """An item is live only while the index lists its uid at a rev ≥ the
    item's (contract §1.2). An item the index doesn't name is an orphan (a
    clear that never landed); an item AHEAD of the index waits until the
    index catches up."""
    if not item or not index:
        return False
    entry = index.get("by_uid", {}).get(item.get("uid"))
    return entry is not None and entry["rev"] >= item.get("rev", 0)


def live_items(
    items: dict[str, dict[str, Any]], index: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    return {uid: item for uid, item in items.items() if is_live(item, index)}


# ── picking a face: uid or exact name ───────────────────────────────


def resolve_face(index: dict[str, Any], ref: Any) -> dict[str, Any]:
    """The index entry a service's ``face`` field names: a uid, or a face's
    exact name (case-insensitive when only one face matches that way).

    A well-formed uid the index doesn't list (yet — the index is debounced,
    or truncated) still goes through as ``{"uid": uid, "rev": None}``: the
    server is authoritative and answers ``not_found`` if it really is
    unknown. A name must match exactly one face.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise FaceSyncError("Pick a face: its uid or its exact name in the face library.")
    ref = ref.strip()
    uid = normalize_uid(ref)
    by_uid = index.get("by_uid", {})
    if uid is not None:
        return by_uid.get(uid) or {"uid": uid, "rev": None, "name": None, "kind": None}
    faces = index.get("faces", [])
    matches = [f for f in faces if f["name"] == ref]
    if not matches:
        folded = ref.casefold()
        matches = [f for f in faces if f["name"].casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        known = ", ".join(sorted({f["name"] for f in faces})[:10])
        more = "" if len({f["name"] for f in faces}) <= 10 else ", …"
        hint = f" Faces in the library: {known}{more}." if known else " The face library is empty."
        raise FaceSyncError(f"No face named '{ref}' in the face library.{hint}")
    listed = "; ".join(
        f"{f['name']} ({f['kind']}{', system' if f['system'] else ''}) uid {f['uid']}"
        for f in matches[:10]
    )
    raise FaceSyncError(
        f"{len(matches)} faces are named '{ref}'. Pick one by uid: {listed}."
    )


# ── pre-validation mirror (contract §3) ─────────────────────────────


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _payload_problems(payload: dict[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []

    def bad(path: str, message: str) -> None:
        problems.append({"path": f"payload.{path}", "message": message})

    top = payload.get("top_line")
    if isinstance(top, dict) and isinstance(top.get("text"), str):
        if len(top["text"].strip()) > TOP_LINE_TEXT_MAX:
            bad("top_line.text", f"at most {TOP_LINE_TEXT_MAX} characters")

    lines = payload.get("lines")
    if isinstance(lines, list):
        if len(lines) > MAX_LINES:
            bad("lines", f"at most {MAX_LINES} lines")
        for i, line in enumerate(lines[:MAX_LINES]):
            if not isinstance(line, dict):
                bad(f"lines[{i}]", "must be an object")
                continue
            text = line.get("text")
            if isinstance(text, str) and len(text.strip()) > LINE_TEXT_MAX:
                bad(f"lines[{i}].text", f"at most {LINE_TEXT_MAX} characters")
            entity = line.get("entity_id")
            if isinstance(entity, str) and len(entity.strip()) > LINE_ENTITY_MAX:
                bad(f"lines[{i}].entity_id", f"at most {LINE_ENTITY_MAX} characters")
            when = line.get("when")
            if isinstance(when, dict) and when.get("op") in ("above", "below"):
                if not (isinstance(entity, str) and entity.strip()):
                    bad(f"lines[{i}].entity_id", f"required for an '{when['op']}' rule")
                if _number(when.get("value")) is None:
                    bad(f"lines[{i}].when.value", f"'{when['op']}' needs a number")

    brightness = payload.get("brightness")
    if brightness is not None:
        num = _number(brightness)
        if num is not None and not 0 <= num <= 100:
            bad("brightness", "must be 0–100")

    idle = payload.get("idle_return_s")
    if idle is not None:
        num = _number(idle)
        if num is not None and num != 0 and not IDLE_RETURN_MIN_S <= num <= IDLE_RETURN_MAX_S:
            bad(
                "idle_return_s",
                f"0 (never) or {IDLE_RETURN_MIN_S}–{IDLE_RETURN_MAX_S} seconds",
            )
    return problems


def check_kind(kind: Any, index: dict[str, Any]) -> str:
    """``kind`` if the server accepts it on create/update (its index's
    ``kinds``; a retired alias whose survivor is offered passes too)."""
    kinds = index.get("kinds") or []
    if not isinstance(kind, str) or not kind.strip():
        raise FaceSyncError("A face kind is required.")
    kind = kind.strip()
    if kinds and kind not in kinds and canonical_kind(kind) not in kinds:
        label = server_label(index.get("server"))
        raise FaceSyncError(
            f"{label} does not author '{kind}' faces. It accepts: {', '.join(kinds)}."
        )
    return kind


def prevalidate(face: dict[str, Any], index: dict[str, Any]) -> None:
    """Refuse what the server would certainly refuse, before any round trip.

    ``face`` holds only the keys being sent (a create has them all, an
    update the patch). Raises FaceSyncError naming every problem found.
    """
    problems: list[dict[str, str]] = []
    if "kind" in face:
        check_kind(face["kind"], index)
    if "name" in face:
        name = face["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= NAME_MAX:
            problems.append({"path": "name", "message": f"1–{NAME_MAX} characters"})
    if "description" in face:
        desc = face["description"]
        if not isinstance(desc, str) or len(desc.strip()) > DESCRIPTION_MAX:
            problems.append(
                {"path": "description", "message": f"at most {DESCRIPTION_MAX} characters"}
            )
    if "payload" in face:
        payload = face["payload"]
        if not isinstance(payload, dict):
            problems.append({"path": "payload", "message": "must be an object"})
        else:
            cap = index.get("max_payload_bytes") or DEFAULT_MAX_PAYLOAD_BYTES
            size = compact_size(payload)
            if size > cap:
                problems.append(
                    {"path": "payload", "message": f"{size} bytes; the limit is {cap}"}
                )
            problems.extend(_payload_problems(payload))
    if problems:
        detail = "; ".join(f"{p['path']}: {p['message']}" for p in problems)
        raise FaceSyncError(f"This face would be refused — {detail}.")


def check_ttl(ttl: Any) -> int:
    if ttl is None or ttl == "":
        return TTL_DEFAULT_S
    num = _number(ttl)
    if num is None or num != int(num) or not TTL_MIN_S <= num <= TTL_MAX_S:
        raise FaceSyncError(f"ttl_s must be a whole number of seconds, {TTL_MIN_S}–{TTL_MAX_S}.")
    return int(num)


# ── request / result ────────────────────────────────────────────────


def new_request_id() -> str:
    return str(uuid.uuid4())


def build_request(
    action: str,
    *,
    request_id: str | None = None,
    uid: str | None = None,
    base_rev: int | None = None,
    face: dict[str, Any] | None = None,
    dial_ids: list[str] | None = None,
    overrides: dict[str, Any] | None = None,
    test_id: str | None = None,
    ttl_s: int | None = None,
    src: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One ``faces/request`` message (contract §1.3). Only the keys the
    action uses are present.

    ``_src`` is always stamped: this goes to the server, not to a dial, so
    the dial's 4 KB buffer cap that makes ``_source.with_source`` drop the
    stamp on big payloads does not apply — and an unstamped face write
    would land in the activity record as nobody.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown face action {action!r}")
    request: dict[str, Any] = {
        "v": PLANE_VERSION,
        "request_id": request_id or new_request_id(),
        "action": action,
    }
    if uid is not None:
        request["uid"] = uid
    if base_rev is not None:
        request["base_rev"] = base_rev
    if face is not None:
        request["face"] = face
    if dial_ids is not None:
        request["dial_ids"] = list(dial_ids)
    if overrides is not None:
        request["overrides"] = overrides
    if test_id is not None:
        request["test_id"] = test_id
    if ttl_s is not None:
        request["ttl_s"] = ttl_s
    if src:
        request[SOURCE_KEY] = src
    return request


def normalize_result(raw: Any) -> dict[str, Any] | None:
    """A ``faces/result`` message as a dict with every contract key, or
    None when it is not a result (no request_id)."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        if not raw or len(raw.encode("utf-8")) > RESULT_MESSAGE_MAX_BYTES:
            return None
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > REQUEST_ID_MAX:
        return None
    error = raw.get("error")
    if error is not None and not isinstance(error, dict):
        error = {"code": "server_error", "message": str(error)[:500]}
    dials = raw.get("dials") if isinstance(raw.get("dials"), dict) else {}
    warnings = [str(w)[:500] for w in (raw.get("warnings") or []) if w] if isinstance(
        raw.get("warnings"), list
    ) else []
    out = {
        "v": raw.get("v") if _is_int(raw.get("v")) else PLANE_VERSION,
        "request_id": request_id,
        "action": raw.get("action") if isinstance(raw.get("action"), str) else "",
        "ok": bool(raw.get("ok")) and error is None,
        "uid": raw.get("uid") if isinstance(raw.get("uid"), str) else None,
        "rev": raw.get("rev") if _is_int(raw.get("rev")) else None,
        "dials": {str(k): str(v) for k, v in dials.items()},
        "warnings": warnings,
        "error": error,
    }
    # Helm and Console both add these to test / clear_test results.
    for key in ("test_id", "expires_at"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key]
    return out


# ── operator messages ───────────────────────────────────────────────

_CODE_FALLBACK = {
    "validation": "The face was refused: it does not pass validation.",
    "unknown_kind": "The server does not author that kind of face.",
    "unrenderable": "That kind of face can't be shown on this deployment.",
    "empty_mount": "That face has nothing to show yet.",
    "too_large": "The face payload is over the size limit.",
    "limit": "The face library is full. Delete a face first.",
    "not_found": "No face with that uid in the library.",
    "read_only": "System faces can't be changed. Copy it into a new face instead.",
    "stale": "This face changed since you loaded it.",
    "unknown_dial": "None of those dials belong to this team.",
    "bad_request": "The server could not read the request.",
    "rate_limited": "Too many face requests a second. Wait a moment and retry.",
    "unsupported_version": "The server speaks an older face sync version. Update it.",
    "server_error": "The server could not complete this request.",
}

_CODE_HINT = {
    "rate_limited": "Wait a moment and retry.",
    "unsupported_version": "Update the Deckhand integration or the server so they match.",
}


def error_message(error: dict[str, Any] | None, server: Any = None) -> str:
    """One operator-readable sentence for a ``faces/result`` error.

    The server's own ``message`` leads (it knows the most); ``fields`` add
    the exact path; ``stale`` names the rev now in the library so the
    caller can retry with it as ``base_rev``.
    """
    error = error if isinstance(error, dict) else {}
    code = str(error.get("code") or "server_error")
    message = str(error.get("message") or "").strip() or _CODE_FALLBACK.get(
        code, f"{server_label(server)} refused the request ({code})."
    )
    parts = [message.rstrip(".") + "."]
    fields = error.get("fields")
    if isinstance(fields, list):
        details = [
            f"{f.get('path')}: {f.get('message')}"
            for f in fields[:5]
            if isinstance(f, dict) and f.get("path") and f.get("message")
        ]
        if details and not any(d.split(":")[0] in message for d in details):
            parts.append("(" + "; ".join(details) + ")")
    if code == "stale":
        current = error.get("current") if isinstance(error.get("current"), dict) else {}
        rev = current.get("rev")
        if _is_int(rev):
            parts.append(
                f"The library now has rev {rev}: reload it, or resend with base_rev {rev} to overwrite."
            )
    elif code in _CODE_HINT and _CODE_HINT[code] not in message:
        parts.append(_CODE_HINT[code])
    return " ".join(parts)


def unavailable_message(team_id: Any = None) -> str:
    where = f" on deckhand/{team_id}/faces/list" if team_id else ""
    return (
        "Face sync isn't available: no face library has arrived from Helm or "
        f"Deckhand Console{where}. Update Helm/Console to a version that syncs "
        "faces (and restart its MQTT listener)."
    )


def timeout_message(server: Any, timeout_s: float) -> str:
    label = server_label(server)
    return (
        f"{label} didn't answer the face request within {timeout_s:g} s. Update "
        f"{label}, restart its MQTT listener, or resync the broker ACLs "
        "(manage.py sync_mqtt_acls) so Home Assistant may publish on faces/request."
    )


# ── the dial's own report: which face is it wearing ────────────────

_KIND_NAMES = {
    "clock": "Clock",
    "sensor": "Readings",
    "message": "Message",
    "image": "Image",
    "blank": "Blank",
    "now_playing": "Now Playing",
    "charge": "Charge",
    "climate": "Climate",
    "framecast": "FrameCast",
    "perimeter_ring": "Perimeter Ring",
    "timer": "Timer",
    "invitation": "Invitation",
}


def friendly_kind(kind: Any, slot: Any = None) -> str:
    """'Readings face', 'Climate face' — or, for what the dial shows on its
    own with nothing mounted, 'Home clock'."""
    kind = canonical_kind(kind)
    label = _KIND_NAMES.get(kind) or kind.replace("_", " ").strip().title() or "Unknown"
    if slot == "home":
        return f"Home {label.lower()}"
    return f"{label} face"


def face_report(heartbeat: dict[str, Any] | None) -> dict[str, Any] | None:
    """The heartbeat's ``face`` report (firmware #236), or None when the
    firmware doesn't report faces (no ``face_report`` feature) or the report
    is malformed."""
    if not isinstance(heartbeat, dict):
        return None
    features = heartbeat.get("features")
    if not isinstance(features, list) or FACE_REPORT_FEATURE not in features:
        return None
    face = heartbeat.get("face")
    if not isinstance(face, dict):
        return None
    face_id = face.get("id")
    if not isinstance(face_id, str) or not KIND_RE.match(face_id):
        return None
    slot = face.get("slot") if face.get("slot") in FACE_REPORT_SLOTS else None
    since_s = face.get("since_s") if _is_int(face.get("since_s")) else None
    ref = face.get("ref")
    ref = ref if isinstance(ref, str) and 0 < len(ref) <= 64 and ref.isprintable() else None
    return {"id": face_id, "slot": slot, "since_s": since_s, "ref": ref}


def ring_report(heartbeat: dict[str, Any] | None) -> bool | None:
    """Whether the perimeter ring is mounted, or None when not reported."""
    if not isinstance(heartbeat, dict):
        return None
    features = heartbeat.get("features")
    if not isinstance(features, list) or FACE_REPORT_FEATURE not in features:
        return None
    ring = heartbeat.get("ring")
    return ring if isinstance(ring, bool) else None


def library_face_for(report: dict[str, Any], index: dict[str, Any] | None) -> dict[str, Any] | None:
    """The library entry the report's ``ref`` names, when its kind matches
    what the dial says it is showing (contract §6)."""
    if not report or not index or not report.get("ref") or report.get("slot") == "home":
        return None
    uid = normalize_uid(report["ref"])
    entry = index.get("by_uid", {}).get(uid) if uid else None
    if entry is None or canonical_kind(entry["kind"]) != canonical_kind(report["id"]):
        return None
    return entry


def showing_since(heartbeat: dict[str, Any], report: dict[str, Any]) -> datetime | None:
    """When the reported face went up, as an absolute local time:
    ``_last_seen − (uptime_s − since_s)``."""
    uptime = heartbeat.get("uptime_s")
    since_s = report.get("since_s")
    last_seen = heartbeat.get("_last_seen")
    if not _is_int(uptime) or since_s is None or not isinstance(last_seen, str):
        return None
    try:
        seen = datetime.fromisoformat(last_seen)
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.astimezone()
    age = max(0, uptime - since_s)
    return (seen - timedelta(seconds=age)).replace(microsecond=0)
