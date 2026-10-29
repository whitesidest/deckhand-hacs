"""The face sync plane, Home Assistant side (1.25.0, docs/face_sync_contract.md).

Pins the contract's HACS test list plus what this integration adds:

* ``_faces`` normalisation drops malformed, oversize and orphaned data; an
  item whose rev is ahead of the index is not live until the index catches
  up;
* the ``faces/request`` shape (``v``, ``request_id``, ``action``, only the
  keys the action uses, ``_src``);
* the face services: admin gate, result correlation by ``request_id``, the
  10 s timeout wording, ``stale`` surfaced with the current rev, the
  feature-unavailable error before any index, and picking a face by uid or
  exact name;
* the dial's own report: the Showing sensor and the Ring binary sensor.

The services run the REAL ``__init__.py`` against the Home Assistant
stand-in from test_entities_follow_heartbeats.py; only the MQTT client,
target resolution and the error classes are swapped. A fake server answers
each request by feeding a result to the real ``_handle_face_result``.

Run with:  python3 -m pytest tests/test_face_sync.py
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import types
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import test_entities_follow_heartbeats as standin  # installs the HA stand-in

FACES = importlib.import_module(f"{standin.PKG}._faces")
MODS = standin.MODS
DIAL = standin.DIAL
COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "deckhand"

PKG = "deckhand_face_sync_under_test"
TEAM = "team-1"
TARGETS = [("DECK-AAAA", TEAM), ("DECK-BBBB", TEAM)]
STAMP = {"ch": "home_assistant", "by": "Home Assistant"}

UID_TEMP = "7c0e9a52-0000-4000-8000-000000000001"
UID_CLOCK = "7c0e9a52-0000-4000-8000-000000000002"
UID_DUP_A = "7c0e9a52-0000-4000-8000-00000000000a"
UID_DUP_B = "7c0e9a52-0000-4000-8000-00000000000b"
UID_ORPHAN = "7c0e9a52-0000-4000-8000-0000000000ff"


def _load_component():
    spec = importlib.util.spec_from_file_location(
        PKG, standin.PKG_DIR / "__init__.py",
        submodule_search_locations=[str(standin.PKG_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load_component()


def _raw_index(**extra):
    index = {
        "v": 1,
        "server": "helm",
        "kinds": ["blank", "clock", "message", "sensor", "perimeter_ring"],
        "max_payload_bytes": 3600,
        "faces": [
            {"uid": UID_CLOCK, "rev": 1, "name": "Big Clock", "kind": "clock", "system": True, "enabled": True},
            {"uid": UID_TEMP, "rev": 4, "name": "Guest Temp", "kind": "sensor", "system": False, "enabled": True},
            {"uid": UID_DUP_A, "rev": 2, "name": "Lobby", "kind": "message", "system": False, "enabled": True},
            {"uid": UID_DUP_B, "rev": 3, "name": "Lobby", "kind": "clock", "system": False, "enabled": False},
        ],
        "updated_at": "2026-10-28T19:30:00+00:00",
    }
    index.update(extra)
    return index


def _index(**extra):
    return FACES.normalize_index(json.dumps(_raw_index(**extra)))


def _record(uid=UID_TEMP, rev=4, **extra):
    record = {
        "v": 1, "uid": uid, "pk": "42", "slug": "guest-temp", "name": "Guest Temp",
        "description": "", "kind": "sensor", "system": False, "enabled": True,
        "payload": {"entity_id": "sensor.guest"}, "rev": rev,
        "updated_at": "2026-10-28T19:30:00+00:00", "origin": "helm", "updated_by": "Tyler",
    }
    record.update(extra)
    return record


# ── pure: normalisation, liveness, mirror, messages ─────────────────


class IndexNormalisation(unittest.TestCase):
    def test_well_formed_index(self):
        index = _index()
        self.assertEqual(index["server"], "helm")
        self.assertEqual(len(index["faces"]), 4)
        self.assertEqual(index["by_uid"][UID_TEMP]["rev"], 4)
        self.assertEqual(index["max_payload_bytes"], 3600)

    def test_malformed_entries_are_dropped_one_by_one(self):
        good = {"uid": UID_TEMP, "rev": 1, "name": "Ok", "kind": "sensor"}
        index = FACES.normalize_index({"faces": [
            good,
            {"uid": "not-a-uid", "rev": 1, "name": "x", "kind": "sensor"},
            {"uid": UID_CLOCK, "rev": True, "name": "x", "kind": "sensor"},
            {"uid": UID_CLOCK, "rev": 0, "name": "x", "kind": "sensor"},
            {"uid": UID_CLOCK, "rev": 1, "name": "", "kind": "sensor"},
            {"uid": UID_CLOCK, "rev": 1, "name": "x" * 129, "kind": "sensor"},
            {"uid": UID_CLOCK, "rev": 1, "name": "x", "kind": "Bad Kind!"},
            {"uid": UID_TEMP, "rev": 9, "name": "Duplicate", "kind": "sensor"},
            "junk",
        ], "kinds": ["sensor", "BAD", 3, "sensor"]})
        self.assertEqual([f["name"] for f in index["faces"]], ["Ok"])
        self.assertEqual(index["kinds"], ["sensor"])

    def test_caps(self):
        faces = [
            {"uid": str(uuid.UUID(int=i + 1)), "rev": 1, "name": f"F{i}", "kind": "clock"}
            for i in range(FACES.INDEX_CAP + 5)
        ]
        index = FACES.normalize_index({"faces": faces, "max_payload_bytes": 10**9})
        self.assertEqual(len(index["faces"]), FACES.INDEX_CAP)
        self.assertEqual(index["max_payload_bytes"], FACES.DEFAULT_MAX_PAYLOAD_BYTES)

    def test_not_an_index(self):
        for raw in ("", "not json", "[]", json.dumps({"faces": "nope"}), json.dumps({})):
            with self.subTest(raw=raw):
                self.assertIsNone(FACES.normalize_index(raw))

    def test_newer_version_fields_are_ignored(self):
        index = FACES.normalize_index(_raw_index(v=2, shiny_new_thing={"a": 1}))
        self.assertEqual(index["v"], 2)
        self.assertNotIn("shiny_new_thing", index)
        self.assertEqual(len(index["faces"]), 4)


class ItemNormalisation(unittest.TestCase):
    def test_well_formed_record(self):
        item = FACES.normalize_item(json.dumps(_record()), UID_TEMP)
        self.assertEqual(item["name"], "Guest Temp")
        self.assertEqual(item["payload"], {"entity_id": "sensor.guest"})

    def test_uid_must_match_the_topic(self):
        self.assertIsNone(FACES.normalize_item(json.dumps(_record()), UID_CLOCK))

    def test_oversize_payload_is_dropped(self):
        big = _record(payload={"text": "x" * 3700})
        self.assertIsNone(FACES.normalize_item(json.dumps(big), UID_TEMP))
        # ...against the cap the index advertises.
        self.assertIsNotNone(FACES.normalize_item(json.dumps(big), UID_TEMP, 8000))

    def test_oversize_message_is_dropped_unparsed(self):
        raw = json.dumps(_record(junk="y" * (FACES.ITEM_MESSAGE_MAX_BYTES + 1)))
        self.assertIsNone(FACES.normalize_item(raw, UID_TEMP, 10**6))

    def test_malformed_records(self):
        for bad in (
            _record(payload=[1, 2]),
            _record(rev="4"),
            _record(name=""),
            _record(kind="no spaces"),
            _record(description="d" * 300),
            _record(updated_by="u" * 65),
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(FACES.normalize_item(json.dumps(bad), UID_TEMP))
        self.assertIsNone(FACES.normalize_item("{not json", UID_TEMP))

    def test_topic_uid(self):
        self.assertEqual(
            FACES.item_uid_from_topic(f"deckhand/1/faces/item/{UID_TEMP.upper()}"), UID_TEMP
        )
        self.assertIsNone(FACES.item_uid_from_topic("deckhand/1/faces/item/nope"))
        self.assertIsNone(FACES.item_uid_from_topic(f"deckhand/1/faces/list/{UID_TEMP}"))


class RevLiveness(unittest.TestCase):
    def test_listed_at_the_same_or_higher_rev_is_live(self):
        index = _index()
        self.assertTrue(FACES.is_live({"uid": UID_TEMP, "rev": 4}, index))
        self.assertTrue(FACES.is_live({"uid": UID_TEMP, "rev": 3}, index))

    def test_item_ahead_of_the_index_waits_for_it(self):
        index = _index()
        item = {"uid": UID_TEMP, "rev": 5}
        self.assertFalse(FACES.is_live(item, index))
        caught_up = _raw_index()
        caught_up["faces"][1]["rev"] = 5
        self.assertTrue(FACES.is_live(item, FACES.normalize_index(caught_up)))

    def test_orphan_is_not_live(self):
        self.assertFalse(FACES.is_live({"uid": UID_ORPHAN, "rev": 1}, _index()))
        self.assertFalse(FACES.is_live({"uid": UID_TEMP, "rev": 1}, None))

    def test_live_items_filters(self):
        items = {UID_TEMP: {"uid": UID_TEMP, "rev": 4}, UID_ORPHAN: {"uid": UID_ORPHAN, "rev": 1}}
        self.assertEqual(list(FACES.live_items(items, _index())), [UID_TEMP])


class PreValidationMirror(unittest.TestCase):
    def refused(self, face):
        with self.assertRaises(FACES.FaceSyncError) as ctx:
            FACES.prevalidate(face, _index())
        return str(ctx.exception)

    def test_a_good_face_passes_with_unknown_keys(self):
        FACES.prevalidate({
            "name": "Ok", "kind": "sensor",
            "payload": {
                "entity_id": "sensor.x", "something_new": True,
                "top_line": {"mode": "custom", "text": "Hi"},
                "lines": [{"text": "Hot", "entity_id": "sensor.x", "when": {"op": "above", "value": "30"}}],
                "brightness": 50, "idle_return_s": 0,
            },
        }, _index())

    def test_kind_must_be_offered(self):
        self.assertIn("does not author 'laser'", self.refused({"kind": "laser"}))

    def test_retired_alias_of_an_offered_kind_passes(self):
        FACES.prevalidate({"kind": "perimeter_pulse"}, _index())

    def test_each_rule(self):
        cases = {
            "name": {"name": "n" * 129},
            "description": {"description": "d" * 257},
            "payload": {"payload": {"text": "x" * 3700}},
            "top_line.text": {"payload": {"top_line": {"text": "t" * 33}}},
            "payload.lines": {"payload": {"lines": [{"text": "a"}] * 4}},
            "lines[0].text": {"payload": {"lines": [{"text": "a" * 49}]}},
            "lines[0].entity_id": {"payload": {"lines": [{"text": "a", "entity_id": "s." + "e" * 62}]}},
            "lines[0].when.value": {"payload": {"lines": [{"text": "a", "entity_id": "sensor.x", "when": {"op": "below", "value": "cold"}}]}},
            "brightness": {"payload": {"brightness": 101}},
            "idle_return_s": {"payload": {"idle_return_s": 5}},
        }
        for path, face in cases.items():
            with self.subTest(path=path):
                self.assertIn(path, self.refused(face))

    def test_ttl(self):
        self.assertEqual(FACES.check_ttl(None), 300)
        self.assertEqual(FACES.check_ttl(30), 30)
        for bad in (29, 901, "soon", 45.5):
            with self.subTest(bad=bad), self.assertRaises(FACES.FaceSyncError):
                FACES.check_ttl(bad)


class RequestShape(unittest.TestCase):
    def test_only_the_keys_the_action_uses(self):
        req = FACES.build_request("update", uid=UID_TEMP, base_rev=4, face={"name": "N"}, src=STAMP)
        self.assertEqual(set(req), {"v", "request_id", "action", "uid", "base_rev", "face", "_src"})
        self.assertEqual(req["v"], 1)
        self.assertEqual(str(uuid.UUID(req["request_id"])), req["request_id"])
        self.assertEqual(req["_src"], STAMP)

    def test_src_is_stamped_even_on_a_large_payload(self):
        # _source.with_source drops the stamp past 3.9 KB (the DIAL's buffer);
        # a face request goes to the server, so it always carries who did it.
        req = FACES.build_request("create", face={"payload": {"t": "x" * 3590}}, src=STAMP)
        self.assertEqual(req["_src"], STAMP)

    def test_unknown_action(self):
        with self.assertRaises(ValueError):
            FACES.build_request("explode")


class ResultAndMessages(unittest.TestCase):
    def test_result_keeps_test_fields(self):
        r = FACES.normalize_result(json.dumps({
            "v": 1, "request_id": "r1", "action": "test", "ok": True, "uid": None, "rev": None,
            "dials": {"D": "sent"}, "warnings": [], "error": None,
            "test_id": "t1", "expires_at": "2026-10-28T20:00:00+00:00",
        }))
        self.assertEqual((r["test_id"], r["expires_at"]), ("t1", "2026-10-28T20:00:00+00:00"))

    def test_not_a_result(self):
        for raw in ("", "x", json.dumps({"ok": True}), json.dumps({"request_id": ""})):
            with self.subTest(raw=raw):
                self.assertIsNone(FACES.normalize_result(raw))

    def test_error_never_counts_as_ok(self):
        r = FACES.normalize_result({"request_id": "r", "ok": True, "error": {"code": "validation"}})
        self.assertFalse(r["ok"])

    def test_stale_names_the_current_rev(self):
        msg = FACES.error_message({
            "code": "stale", "message": "This face changed since you loaded it (rev 7, you edited rev 4).",
            "current": {"rev": 7},
        })
        self.assertIn("rev 7", msg)
        self.assertIn("base_rev 7", msg)

    def test_fields_are_listed(self):
        msg = FACES.error_message({
            "code": "validation", "message": "The sensor face payload is invalid.",
            "fields": [{"path": "payload.lines[0].entity_id", "message": "required for an 'above' rule"}],
        })
        self.assertIn("payload.lines[0].entity_id", msg)

    def test_code_without_message_has_a_sentence(self):
        self.assertIn("system", FACES.error_message({"code": "read_only"}).lower())
        self.assertTrue(FACES.error_message(None))


class NameOrUid(unittest.TestCase):
    def test_uid(self):
        self.assertEqual(FACES.resolve_face(_index(), UID_TEMP.upper())["name"], "Guest Temp")

    def test_unknown_uid_still_goes_to_the_server(self):
        found = FACES.resolve_face(_index(), UID_ORPHAN)
        self.assertEqual((found["uid"], found["rev"]), (UID_ORPHAN, None))

    def test_exact_name(self):
        self.assertEqual(FACES.resolve_face(_index(), "Guest Temp")["uid"], UID_TEMP)

    def test_case_insensitive_when_unique(self):
        self.assertEqual(FACES.resolve_face(_index(), "guest temp")["uid"], UID_TEMP)

    def test_ambiguous_name_lists_the_matches(self):
        with self.assertRaises(FACES.FaceSyncError) as ctx:
            FACES.resolve_face(_index(), "Lobby")
        self.assertIn(UID_DUP_A, str(ctx.exception))
        self.assertIn(UID_DUP_B, str(ctx.exception))

    def test_unknown_name_lists_the_library(self):
        with self.assertRaises(FACES.FaceSyncError) as ctx:
            FACES.resolve_face(_index(), "Nope")
        self.assertIn("Guest Temp", str(ctx.exception))


# ── the services, against the real __init__.py ──────────────────────


class _SVE(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args or (kwargs.get("translation_key"),))


class _Unauthorized(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__("unauthorized")


class _Services:
    def __init__(self):
        self.handlers = {}
        self.kwargs = {}

    def has_service(self, domain, name):
        return name in self.handlers

    def async_register(self, domain, name, handler, *args, **kwargs):
        self.handlers[name] = handler
        self.kwargs[name] = kwargs


class _Server:
    """Stands in for the broker + Helm: records each publish and answers a
    faces/request through the real result handler."""

    def __init__(self, test):
        self.test = test
        self.sent: list[tuple[str, dict, bool]] = []
        self.answer = self.ok

    @staticmethod
    def ok(request):
        return {
            "v": 1, "request_id": request["request_id"], "action": request["action"],
            "ok": True, "uid": request.get("uid") or UID_ORPHAN, "rev": 5,
            "dials": {d: "sent" for d in request.get("dial_ids") or []},
            "warnings": [], "error": None,
        }

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        request = json.loads(payload)
        self.sent.append((topic, request, retain))
        answer = self.answer(request)
        for result in answer if isinstance(answer, list) else [answer]:
            if result is not None:
                MOD._handle_face_result(self.test.store, json.dumps(result))


class _FaceServiceTest(unittest.TestCase):
    def setUp(self):
        self.server = _Server(self)
        self.store = {"team_id": TEAM, "faces": MOD._new_faces_store()}
        self.store["faces"]["index"] = _index()
        self.hass = types.SimpleNamespace(
            services=_Services(), data={"deckhand": {"entry-1": self.store}},
        )
        patches = {
            "mqtt": self.server,
            "ServiceValidationError": _SVE,
            "Unauthorized": _Unauthorized,
            "_resolve_targets": lambda hass, device_id: list(TARGETS),
        }
        self._saved = {k: getattr(MOD, k) for k in patches}
        for k, v in patches.items():
            setattr(MOD, k, v)
        MOD._register_services(self.hass, types.SimpleNamespace(entry_id="entry-1"))

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(MOD, k, v)

    def call(self, service, context=None, **data):
        self.server.sent.clear()
        call = types.SimpleNamespace(data=data, service=service)
        if context is not None:
            call.context = context
        return asyncio.run(self.hass.services.handlers[service](call))

    def refused(self, service, **data):
        with self.assertRaises(_SVE) as ctx:
            self.call(service, **data)
        return str(ctx.exception)

    def requests(self):
        return [r for t, r, _ in self.server.sent]


class Registration(_FaceServiceTest):
    def test_six_services_with_optional_response(self):
        for name in ("create_face", "update_face", "delete_face",
                     "mount_library_face", "test_face", "clear_face_test"):
            with self.subTest(name=name):
                self.assertIn(name, self.hass.services.handlers)
                self.assertIn("supports_response", self.hass.services.kwargs[name])


class CreateUpdateDelete(_FaceServiceTest):
    def test_create_request_shape_and_response(self):
        result = self.call("create_face", name="Pool", kind="sensor", payload={"entity_id": "sensor.pool"})
        ((topic, req, retain),) = self.server.sent
        self.assertEqual(topic, f"deckhand/{TEAM}/faces/request")
        self.assertFalse(retain)
        self.assertEqual(req["action"], "create")
        self.assertEqual(req["face"], {"name": "Pool", "kind": "sensor", "payload": {"entity_id": "sensor.pool"}})
        self.assertEqual(req["_src"], STAMP)
        self.assertNotIn("uid", req)
        self.assertNotIn("base_rev", req)
        self.assertTrue(result["ok"])
        self.assertEqual(result["request_id"], req["request_id"])

    def test_create_prevalidates_before_any_round_trip(self):
        msg = self.refused("create_face", name="Pool", kind="laser")
        self.assertIn("laser", msg)
        self.assertEqual(self.server.sent, [])

    def test_update_by_name_uses_the_library_rev(self):
        self.call("update_face", face="Guest Temp", name="Guest Temperature")
        (req,) = self.requests()
        self.assertEqual(
            {k: req[k] for k in ("action", "uid", "base_rev", "face")},
            {"action": "update", "uid": UID_TEMP, "base_rev": 4, "face": {"name": "Guest Temperature"}},
        )

    def test_update_with_explicit_base_rev(self):
        self.call("update_face", face=UID_TEMP, base_rev=7, enabled=False)
        self.assertEqual(self.requests()[0]["base_rev"], 7)
        self.assertEqual(self.requests()[0]["face"], {"enabled": False})

    def test_update_with_nothing_to_change(self):
        self.assertIn("Nothing to change", self.refused("update_face", face="Guest Temp"))

    def test_stale_is_surfaced_with_the_current_rev(self):
        def stale(request):
            return {
                "v": 1, "request_id": request["request_id"], "action": "update", "ok": False,
                "uid": UID_TEMP, "rev": None, "dials": {}, "warnings": [],
                "error": {
                    "code": "stale",
                    "message": "This face changed since you loaded it (rev 9, you edited rev 4).",
                    "fields": [], "current": _record(rev=9),
                },
            }

        self.server.answer = stale
        msg = self.refused("update_face", face="Guest Temp", name="X")
        self.assertIn("rev 9", msg)
        self.assertIn("base_rev 9", msg)

    def test_server_validation_error_is_the_servers_sentence(self):
        self.server.answer = lambda r: {
            "request_id": r["request_id"], "ok": False,
            "error": {"code": "validation", "message": "This face is still in use: Schedule Evening."},
        }
        self.assertIn("still in use: Schedule Evening", self.refused("delete_face", face="Guest Temp"))

    def test_system_face_is_read_only_locally(self):
        self.assertIn("system face", self.refused("delete_face", face="Big Clock"))
        self.assertEqual(self.server.sent, [])

    def test_delete(self):
        self.call("delete_face", face="guest temp")
        (req,) = self.requests()
        self.assertEqual((req["action"], req["uid"], req["base_rev"]), ("delete", UID_TEMP, 4))
        self.assertNotIn("face", req)

    def test_ambiguous_name_is_refused_with_the_matches(self):
        msg = self.refused("mount_library_face", device_id=["d1"], face="Lobby")
        self.assertIn(UID_DUP_A, msg)
        self.assertEqual(self.server.sent, [])


class AdminGate(_FaceServiceTest):
    def setUp(self):
        super().setUp()

        async def _get_user(user_id):
            return types.SimpleNamespace(is_admin=user_id == "admin", name=user_id)

        self.hass.auth = types.SimpleNamespace(async_get_user=_get_user)

    def test_non_admin_is_unauthorized_for_every_face_service(self):
        guest = types.SimpleNamespace(user_id="guest", id="c1", parent_id=None)
        for name in ("create_face", "update_face", "delete_face",
                     "mount_library_face", "test_face", "clear_face_test"):
            with self.subTest(name=name), self.assertRaises(_Unauthorized):
                self.call(name, context=guest, face="Guest Temp", name="X", kind="sensor",
                          device_id=["d1"], test_id="t")
        self.assertEqual(self.server.sent, [])

    def test_admin_is_stamped_by_name(self):
        admin = types.SimpleNamespace(user_id="admin", id="c2", parent_id=None)
        self.call("create_face", context=admin, name="Pool", kind="sensor")
        self.assertEqual(self.requests()[0]["_src"], {"ch": "home_assistant", "by": "admin"})


class Correlation(_FaceServiceTest):
    def test_results_for_other_requests_are_ignored(self):
        def noisy(request):
            other = _Server.ok({"request_id": "someone-else", "action": "create"})
            other["uid"] = "not-ours"
            mine = _Server.ok(request)
            return [other, mine]

        self.server.answer = noisy
        result = self.call("create_face", name="Pool", kind="sensor")
        self.assertEqual(result["uid"], UID_ORPHAN)
        self.assertEqual(self.store["faces"]["pending"], {}, "the future is always released")

    def test_a_stray_result_is_harmless(self):
        MOD._handle_face_result(self.store, json.dumps({"request_id": "nobody", "ok": True}))
        MOD._handle_face_result(self.store, "not json")

    def test_timeout_message(self):
        self.server.answer = lambda r: None
        saved = MOD.FACE_REQUEST_TIMEOUT_S
        MOD.FACE_REQUEST_TIMEOUT_S = 0.02
        try:
            msg = self.refused("create_face", name="Pool", kind="sensor")
        finally:
            MOD.FACE_REQUEST_TIMEOUT_S = saved
        self.assertIn("Helm didn't answer", msg)
        self.assertIn("MQTT listener", msg)
        self.assertIn("sync_mqtt_acls", msg)
        self.assertEqual(self.store["faces"]["pending"], {})


class Unavailable(_FaceServiceTest):
    def test_before_any_index_every_service_says_update_the_server(self):
        self.store["faces"]["index"] = None
        for name, data in (
            ("create_face", {"name": "Pool", "kind": "sensor"}),
            ("update_face", {"face": "Guest Temp", "name": "X"}),
            ("delete_face", {"face": "Guest Temp"}),
            ("mount_library_face", {"device_id": ["d1"], "face": "Guest Temp"}),
            ("test_face", {"device_id": ["d1"], "kind": "message"}),
            ("clear_face_test", {"device_id": ["d1"], "test_id": "t"}),
        ):
            with self.subTest(name=name):
                msg = self.refused(name, **data)
                self.assertIn("Face sync isn't available", msg)
                self.assertIn("Update Helm/Console", msg)
        self.assertEqual(self.server.sent, [])

    def test_an_empty_retained_index_makes_it_unavailable_again(self):
        MOD._handle_faces_list(self.hass, self.store, "")
        self.assertIsNone(self.store["faces"]["index"])
        self.assertIn("isn't available", self.refused("create_face", name="P", kind="sensor"))


class DialActions(_FaceServiceTest):
    def test_mount_by_name(self):
        result = self.call("mount_library_face", device_id=["d1"], face="Guest Temp",
                           overrides={"label": "Pool"})
        (req,) = self.requests()
        self.assertEqual(req["action"], "mount")
        self.assertEqual(req["uid"], UID_TEMP)
        self.assertEqual(req["dial_ids"], ["DECK-AAAA", "DECK-BBBB"])
        self.assertEqual(req["overrides"], {"label": "Pool"})
        self.assertEqual(req["_src"], STAMP)
        self.assertEqual(result["dials"], {"DECK-AAAA": "sent", "DECK-BBBB": "sent"})

    def test_test_an_unsaved_face_then_clear_it(self):
        result = self.call("test_face", device_id=["d1"], kind="message", payload={"text": "Hi"})
        (req,) = self.requests()
        self.assertEqual(req["action"], "test")
        self.assertEqual(req["face"], {"kind": "message", "payload": {"text": "Hi"}})
        self.assertEqual(req["ttl_s"], 300)
        self.assertNotIn("uid", req)
        test_id = req["test_id"]
        self.assertTrue(test_id)

        self.call("clear_face_test", device_id=["d1"], test_id=test_id)
        (clear,) = self.requests()
        self.assertEqual(clear["action"], "clear_test")
        self.assertEqual(clear["test_id"], test_id)
        self.assertEqual(clear["face"], {"kind": "message"}, "the remembered kind goes along")
        self.assertEqual(clear["dial_ids"], ["DECK-AAAA", "DECK-BBBB"])
        self.assertTrue(result["ok"])

    def test_test_a_library_face(self):
        self.call("test_face", device_id=["d1"], face="Guest Temp", ttl_s=60, test_id="mine")
        (req,) = self.requests()
        self.assertEqual((req["uid"], req["ttl_s"], req["test_id"]), (UID_TEMP, 60, "mine"))
        self.assertNotIn("face", req)

    def test_test_needs_one_source(self):
        self.assertIn("not both", self.refused("test_face", device_id=["d1"], face="Guest Temp", kind="clock"))
        self.assertIn("Pick a library face", self.refused("test_face", device_id=["d1"]))

    def test_clear_needs_a_test_id_or_kind(self):
        self.assertIn("test_id", self.refused("clear_face_test", device_id=["d1"]))

    def test_more_than_32_dials_go_in_batches(self):
        many = [(f"DECK-{i:04d}", TEAM) for i in range(40)]
        MOD._resolve_targets = lambda hass, device_id: many
        result = self.call("mount_library_face", device_id=["room"], face=UID_TEMP)
        self.assertEqual([len(r["dial_ids"]) for r in self.requests()], [32, 8])
        self.assertEqual(len(result["dials"]), 40)


class CatalogHandlers(_FaceServiceTest):
    def test_items_cache_and_clear(self):
        topic = f"deckhand/{TEAM}/faces/item/{UID_TEMP}"
        MOD._handle_face_item(self.hass, self.store, topic, json.dumps(_record()))
        self.assertIn(UID_TEMP, self.store["faces"]["items"])
        MOD._handle_face_item(self.hass, self.store, topic, "")
        self.assertNotIn(UID_TEMP, self.store["faces"]["items"])

    def test_malformed_item_is_ignored(self):
        topic = f"deckhand/{TEAM}/faces/item/{UID_TEMP}"
        MOD._handle_face_item(self.hass, self.store, topic, json.dumps(_record(uid=UID_CLOCK)))
        self.assertEqual(self.store["faces"]["items"], {})

    def test_new_index_drops_orphaned_records(self):
        orphan = f"deckhand/{TEAM}/faces/item/{UID_ORPHAN}"
        MOD._handle_face_item(self.hass, self.store, orphan, json.dumps(
            _record(uid=UID_ORPHAN, rev=1)))
        self.assertIn(UID_ORPHAN, self.store["faces"]["items"])
        MOD._handle_faces_list(self.hass, self.store, json.dumps(_raw_index()))
        self.assertNotIn(UID_ORPHAN, self.store["faces"]["items"])

    def test_malformed_index_keeps_the_last_good_one(self):
        before = self.store["faces"]["index"]
        MOD._handle_faces_list(self.hass, self.store, "{oops")
        self.assertIs(self.store["faces"]["index"], before)


# ── the dial's own report: Showing + Ring ───────────────────────────


def _report(face_id="sensor", slot="hero", since_s=400, ref=UID_TEMP, ring=True, uptime_s=1000, **extra):
    face = {"id": face_id, "slot": slot, "since_s": since_s}
    if ref is not None:
        face["ref"] = ref
    return standin._data(1, features=["img_jpeg", "face_report"], face=face, ring=ring,
                         uptime_s=uptime_s, **extra)


def _showing(data):
    hass = standin._Hass()
    hass.data["deckhand"] = {"entry-1": {"faces": {"index": _index()}}}
    sensor = MODS["sensor"].DeckhandShowingSensor(DIAL, data, types.SimpleNamespace(entry_id="entry-1"))
    return standin._add(sensor, hass), hass


class ShowingSensor(unittest.TestCase):
    def test_identity(self):
        sensor, _ = _showing(_report())
        self.assertEqual(sensor._attr_unique_id, f"deckhand_{DIAL}_showing")
        self.assertEqual(sensor._attr_translation_key, "showing")
        self.assertTrue(sensor._attr_has_entity_name)

    def test_library_name_when_ref_resolves_with_matching_kind(self):
        sensor, _ = _showing(_report())
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, "Guest Temp")
        self.assertTrue(sensor.extra_state_attributes["in_library"])

    def test_kind_mismatch_falls_back_to_the_kind(self):
        sensor, _ = _showing(_report(face_id="message"))
        self.assertEqual(sensor.native_value, "Message face")

    def test_no_ref_is_a_friendly_kind(self):
        sensor, _ = _showing(_report(ref=None))
        self.assertEqual(sensor.native_value, "Readings face")
        self.assertFalse(sensor.extra_state_attributes["in_library"])

    def test_home_slot(self):
        sensor, _ = _showing(_report(face_id="clock", slot="home", ref=None))
        self.assertEqual(sensor.native_value, "Home clock")

    def test_attributes(self):
        sensor, _ = _showing(_report())
        attrs = sensor.extra_state_attributes
        self.assertEqual(
            {k: attrs[k] for k in ("face_id", "slot", "ref", "ring")},
            {"face_id": "sensor", "slot": "hero", "ref": UID_TEMP, "ring": True},
        )
        seen = datetime.fromisoformat(sensor._dial_data["_last_seen"]).astimezone()
        since = datetime.fromisoformat(attrs["since"])
        self.assertLess(abs((seen - timedelta(seconds=600) - since).total_seconds()), 1.5)

    def test_since_is_stable_across_heartbeat_jitter(self):
        sensor, hass = _showing(_report())
        first = sensor.extra_state_attributes["since"]
        later = _report(uptime_s=1030)
        later["_last_seen"] = (datetime.now() + timedelta(seconds=31)).isoformat()
        hass.bus.fire(standin.EVENT, {"dial_id": DIAL, "data": later})
        self.assertEqual(sensor.extra_state_attributes["since"], first)

    def test_unavailable_without_face_report(self):
        sensor, _ = _showing(standin._data(1, features=["img_jpeg"], face={"id": "clock"}))
        self.assertFalse(sensor.available)
        self.assertIsNone(sensor.native_value)
        self.assertIsNone(sensor.extra_state_attributes)

    def test_old_firmware_without_features(self):
        sensor, _ = _showing(standin._data(1))
        self.assertFalse(sensor.available)
        self.assertNotEqual(sensor.native_value, "none")

    def test_follows_heartbeats_and_library_changes(self):
        sensor, hass = _showing(standin._data(1))
        hass.bus.fire(standin.EVENT, {"dial_id": DIAL, "data": _report()})
        self.assertEqual(sensor.native_value, "Guest Temp")
        hass.data["deckhand"]["entry-1"]["faces"]["index"] = None
        self.assertEqual(sensor.native_value, "Readings face")


class RingSensor(unittest.TestCase):
    def _ring(self, data):
        return MODS["binary_sensor"].DeckhandRingSensor(DIAL, data)

    def test_identity(self):
        ring = self._ring(_report())
        self.assertEqual(ring._attr_unique_id, f"deckhand_{DIAL}_ring")
        self.assertEqual(ring._attr_translation_key, "ring")

    def test_reported(self):
        self.assertTrue(self._ring(_report(ring=True)).is_on)
        off = self._ring(_report(ring=False))
        self.assertTrue(off.available)
        self.assertFalse(off.is_on)

    def test_unavailable_when_not_reported(self):
        self.assertFalse(self._ring(standin._data(1)).available)
        self.assertFalse(self._ring(standin._data(1, features=["face_report"])).available)


class TranslationsAndDiagnostics(unittest.TestCase):
    def test_entity_names(self):
        strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
        self.assertEqual(strings["entity"]["sensor"]["showing"]["name"], "Showing")
        self.assertEqual(strings["entity"]["binary_sensor"]["ring"]["name"], "Ring")

    def test_diagnostics_face_library(self):
        import test_diagnostics_and_entities as diag_tests

        hass, entry = diag_tests._hass_and_entry()
        store = hass.data["deckhand"]["entry-1"]
        store["faces"] = {"index": _index(), "items": {UID_TEMP: {"uid": UID_TEMP, "rev": 4}},
                          "pending": {}, "tests": {}}
        out = asyncio.run(diag_tests.DIAG.async_get_config_entry_diagnostics(hass, entry))
        lib = out["face_library"]
        self.assertEqual(
            {k: lib[k] for k in ("available", "server", "faces", "system_faces", "index_updated_at")},
            {"available": True, "server": "helm", "faces": 4, "system_faces": 1,
             "index_updated_at": "2026-10-28T19:30:00+00:00"},
        )
        self.assertEqual(lib["live_records"], 1)
        json.dumps(out)

    def test_diagnostics_without_a_library(self):
        import test_diagnostics_and_entities as diag_tests

        hass, entry = diag_tests._hass_and_entry()
        out = asyncio.run(diag_tests.DIAG.async_get_config_entry_diagnostics(hass, entry))
        self.assertFalse(out["face_library"]["available"])


if __name__ == "__main__":
    unittest.main()
