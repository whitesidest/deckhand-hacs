"""Repairs, the setup / options summary and the Climate face service (1.26.0).

Pinned here:

* ``_repairs``: version parsing (a bench ``0.4.0-lan`` build is never "too
  old"), the gates, the offline arithmetic, and that BLUEPRINT_FILES is the
  repository's blueprints folder exactly, with byte-identical copies
  packaged in the integration (1.27.0).
* ``_issues`` against a fake issue registry: a firmware issue per dial,
  widened by a second feature and cleared by the first heartbeat on new
  enough firmware (after a restart, only once every gate is met); an
  offline issue at 24 h — counted from startup for a dial not heard since —
  cleared by a heartbeat or a deregister; the blueprints issue raised and
  cleared from what is on disk, and its fix flow: Submit installs the
  missing blueprints into config/blueprints/automation/deckhand/ (never
  overwriting one already there), refreshes the automation blueprint cache
  and clears the issue; a failed copy shows an error and keeps it.
* The REAL services: ``set_dnd`` / ``mount_perimeter_ring`` /
  ``mount_climate_face`` raise the firmware issue on an old dial and still
  publish; ``mount_climate_face`` validates like Helm's ClimatePayload,
  never sends ``unit``, and seeds the face from the thermostat's state.
* The config flow's finish page and the options summary.

Run with:  python3 -m pytest tests/test_repairs_and_climate.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import yaml

import test_entities_follow_heartbeats as standin  # installs the HA stand-in
import test_emoji_strip_every_service as harness  # the real services
import test_reconfigure_team_id as reconf  # the real config flow

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
PKG = "deckhand_repairs_under_test"
DOMAIN = "deckhand"


def _voluptuous_stub():
    vol = types.ModuleType("voluptuous")

    class Schema:
        def __init__(self, schema, **kwargs):
            self.schema = schema

    vol.Schema = Schema
    return vol


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(COMPONENT)]
    modules = {PKG: pkg}
    try:
        importlib.import_module("voluptuous")
    except ImportError:
        modules["voluptuous"] = _voluptuous_stub()
    with mock.patch.dict(sys.modules, modules):
        return {
            name: importlib.import_module(f"{PKG}.{name}")
            for name in ("_repairs", "_issues", "_climate", "repairs")
        }


MODS = _load()
REP = MODS["_repairs"]
ISS = MODS["_issues"]
CLIMATE = MODS["_climate"]
FLOW = MODS["repairs"]


class _IssueRegistry:
    IssueSeverity = types.SimpleNamespace(WARNING="warning")

    def __init__(self):
        self.issues: dict[str, dict] = {}

    def async_get(self, hass):
        return self

    def async_get_issue(self, domain, issue_id):
        return self.issues.get(issue_id) if domain == DOMAIN else None

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        assert domain == DOMAIN
        self.issues[issue_id] = kwargs

    def async_delete_issue(self, hass, domain, issue_id):
        self.issues.pop(issue_id, None)


def _patch_ir(test, module):
    ir = _IssueRegistry()
    p = mock.patch.object(module, "ir", ir)
    p.start()
    test.addCleanup(p.stop)
    return ir


def _store(**dials):
    return {"team_id": "team-1", "dials": dials, "labels": {"DECK-AAAA": "Kitchen"}, "_fw_gaps": {}}


def _hass(store):
    return types.SimpleNamespace(data={DOMAIN: {"entry-1": store}})


# ---------------------------------------------------------------- pure
class PureRepairs(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(REP.parse_version("0.4.161"), (0, 4, 161))
        self.assertEqual(REP.parse_version(" 1.0.0 "), (1, 0, 0))
        for junk in (None, "", "0.4", "0.4.0-lan", "v0.4.1", 4):
            self.assertIsNone(REP.parse_version(junk))
        # An unknown version is never flagged, and never "satisfies".
        self.assertEqual(REP.missing_features("0.4.0-lan", ["dnd"]), [])
        self.assertFalse(REP.firmware_satisfies(None, ["dnd"]))

    def test_gates(self):
        self.assertEqual(REP.missing_features("0.4.43", ["dnd"]), ["dnd"])
        self.assertEqual(REP.missing_features("0.4.44", ["dnd"]), [])
        self.assertEqual(
            REP.missing_features("0.4.158", ["ring_theme_colors", "ring_hide_when_calm", "nope"]),
            ["ring_hide_when_calm"],
        )
        self.assertEqual(REP.required_version(["dnd", "ring_hide_when_calm"]), "0.4.161")
        self.assertEqual(REP.feature_labels(["climate_face"]), "the Climate face (0.4.98+)")
        self.assertTrue(REP.firmware_satisfies("0.4.50", ["dnd"]))
        self.assertFalse(REP.firmware_satisfies("0.4.50", None))  # every gate
        self.assertTrue(REP.firmware_satisfies("0.4.161", None))

    def test_silence(self):
        start = datetime(2026, 10, 28, 8, 0)
        now = start + timedelta(hours=30)
        self.assertEqual(REP.seconds_silent(None, start, now), 30 * 3600)
        seen = (start + timedelta(hours=10)).isoformat()
        self.assertEqual(REP.seconds_silent(seen, start, now), 20 * 3600)
        self.assertEqual(REP.seconds_silent("garbage", start, now), 30 * 3600)

    def test_blueprint_list_is_the_repository_folder(self):
        source = ROOT / "blueprints" / "automation" / "deckhand"
        on_disk = sorted(p.name for p in source.glob("*.yaml"))
        self.assertEqual(sorted(REP.BLUEPRINT_FILES), on_disk)
        # The package carries the same files (HACS installs only
        # custom_components/deckhand), byte for byte, and only those.
        packaged = Path(ISS.PACKAGED_BLUEPRINTS_DIR)
        self.assertEqual(packaged, COMPONENT / "blueprints" / "automation")
        self.assertEqual(sorted(p.name for p in packaged.iterdir()), on_disk)
        for name in REP.BLUEPRINT_FILES:
            self.assertEqual((packaged / name).read_bytes(), (source / name).read_bytes(),
                             f"{name}: packaged copy differs from blueprints/automation/deckhand/")

    def test_import_links(self):
        url = REP.blueprint_import_url("nfc_known_tap_scene.yaml")
        self.assertTrue(url.startswith("https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url="))
        self.assertIn("https%3A%2F%2Fraw.githubusercontent.com%2Fwhitesidest%2Fdeckhand-hacs%2Fmain%2F"
                      "blueprints%2Fautomation%2Fdeckhand%2Fnfc_known_tap_scene.yaml", url)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(url, readme, "README and the Repairs links disagree")
        self.assertEqual(REP.blueprint_links_markdown().count("](https://my.home-assistant.io/"),
                         len(REP.BLUEPRINT_FILES))
        self.assertTrue(REP.blueprints_present(["x.yaml", "dnd_hold_calls.yaml"]))
        self.assertFalse(REP.blueprints_present(["x.yaml"]))


# ---------------------------------------------------------------- firmware issue
class FirmwareIssue(unittest.TestCase):
    def setUp(self):
        self.ir = _patch_ir(self, ISS)
        self.store = _store(**{"DECK-AAAA": {"fw_ver": "0.4.40"}, "DECK-BBBB": {"fw_ver": "0.4.200"}})
        self.hass = _hass(self.store)
        self.targets = [("DECK-AAAA", "team-1"), ("DECK-BBBB", "team-1"), ("DECK-GONE", "team-1")]

    def test_only_the_old_dial_is_flagged_and_the_issue_widens(self):
        self.assertEqual(ISS.note_firmware_gaps(self.hass, self.targets, ["dnd"]), ["DECK-AAAA"])
        issue = self.ir.issues["firmware_too_old_DECK-AAAA"]
        self.assertEqual(issue["translation_key"], "firmware_too_old")
        self.assertEqual(issue["translation_placeholders"]["dial"], "Kitchen (DECK-AAAA)")
        self.assertEqual(issue["translation_placeholders"]["required"], "0.4.44")
        ISS.note_firmware_gaps(self.hass, self.targets, ["climate_face"])
        p = self.ir.issues["firmware_too_old_DECK-AAAA"]["translation_placeholders"]
        self.assertEqual(p["required"], "0.4.98")
        self.assertIn("Do Not Disturb", p["features"])
        self.assertIn("Climate face", p["features"])
        self.assertEqual(list(self.ir.issues), ["firmware_too_old_DECK-AAAA"])

    def test_a_heartbeat_on_new_enough_firmware_clears_it(self):
        ISS.note_firmware_gaps(self.hass, self.targets, ["dnd"])
        ISS.on_heartbeat(self.hass, self.store, "DECK-AAAA", {"fw_ver": "0.4.43"})
        self.assertIn("firmware_too_old_DECK-AAAA", self.ir.issues)
        ISS.on_heartbeat(self.hass, self.store, "DECK-AAAA", {"fw_ver": "0.4.44"})
        self.assertNotIn("firmware_too_old_DECK-AAAA", self.ir.issues)

    def test_after_a_restart_every_gate_must_be_met(self):
        self.ir.issues["firmware_too_old_DECK-AAAA"] = {}
        ISS.on_heartbeat(self.hass, self.store, "DECK-AAAA", {"fw_ver": "0.4.100"})
        self.assertIn("firmware_too_old_DECK-AAAA", self.ir.issues)
        ISS.on_heartbeat(self.hass, self.store, "DECK-AAAA", {"fw_ver": "0.4.161"})
        self.assertNotIn("firmware_too_old_DECK-AAAA", self.ir.issues)


# ---------------------------------------------------------------- offline issue
class OfflineIssue(unittest.TestCase):
    def setUp(self):
        self.ir = _patch_ir(self, ISS)
        self.start = datetime(2026, 10, 28, 8, 0)
        self.store = _store(**{"DECK-AAAA": {"_last_seen": (self.start + timedelta(hours=2)).isoformat()}})
        self.hass = _hass(self.store)

    def check(self, hours, registry_dials=("DECK-AAAA", "DECK-CCCC")):
        return ISS.check_offline(self.hass, self.store, registry_dials, self.start,
                                 now=self.start + timedelta(hours=hours))

    def test_a_day_of_silence(self):
        self.assertEqual(self.check(23), [])
        # DECK-CCCC never reported since startup: counted from startup.
        self.assertEqual(self.check(25), ["DECK-CCCC"])
        self.assertEqual(self.check(27), ["DECK-AAAA", "DECK-CCCC"])
        issue = self.ir.issues["dial_offline_DECK-AAAA"]
        self.assertEqual(issue["translation_placeholders"], {"dial": "Kitchen (DECK-AAAA)", "hours": "25"})

    def test_a_heartbeat_or_a_deregister_clears_it(self):
        self.check(30)
        ISS.on_heartbeat(self.hass, self.store, "DECK-AAAA", {"fw_ver": "0.4.200"})
        self.assertNotIn("dial_offline_DECK-AAAA", self.ir.issues)
        ISS.forget_dial(self.hass, "DECK-CCCC")
        self.assertEqual(self.ir.issues, {})

    def test_registry_dials_exclude_rooms(self):
        devices = [types.SimpleNamespace(identifiers={(DOMAIN, "DECK-AAAA")}),
                   types.SimpleNamespace(identifiers={(DOMAIN, "group:12")}),
                   types.SimpleNamespace(identifiers={("other", "x")})]
        dr = types.SimpleNamespace(async_get=lambda hass: None,
                                   async_entries_for_config_entry=lambda reg, entry_id: devices)
        with mock.patch.object(ISS, "dr", dr):
            self.assertEqual(ISS.entry_dial_ids(self.hass, "entry-1"), ["DECK-AAAA"])


# ---------------------------------------------------------------- blueprints
class _Config:
    def __init__(self, config_dir):
        self.config_dir = config_dir


class BlueprintsIssue(unittest.TestCase):
    def setUp(self):
        self.ir = _patch_ir(self, ISS)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        async def _executor(func, *args):
            return func(*args)

        self.hass = types.SimpleNamespace(config=_Config(self.tmp.name), async_add_executor_job=_executor,
                                          data={})

    def put(self, folder, name, text="blueprint: {}\n"):
        path = os.path.join(self.tmp.name, "blueprints", "automation", folder)
        os.makedirs(path, exist_ok=True)
        Path(path, name).write_text(text)

    def installed(self, folder="deckhand"):
        path = Path(self.tmp.name, "blueprints", "automation", folder)
        return sorted(p.name for p in path.iterdir()) if path.is_dir() else []

    def flow(self):
        flow = asyncio.run(FLOW.async_create_fix_flow(self.hass, "blueprints_not_imported", None))
        flow.hass = self.hass
        flow.async_show_form = lambda **kw: {"type": "form", **kw}
        flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
        return flow

    def test_raised_until_one_is_imported(self):
        self.assertFalse(asyncio.run(ISS.async_check_blueprints(self.hass)))
        issue = self.ir.issues["blueprints_not_imported"]
        self.assertTrue(issue["is_fixable"])
        self.assertIn("my.home-assistant.io", issue["translation_placeholders"]["links"])
        self.put("homeassistant", "motion_light.yaml")  # someone else's blueprint
        self.assertFalse(asyncio.run(ISS.async_check_blueprints(self.hass)))
        self.put("whitesidest", "dnd_hold_calls.yaml")  # where a GitHub import lands
        self.assertTrue(asyncio.run(ISS.async_check_blueprints(self.hass)))
        self.assertNotIn("blueprints_not_imported", self.ir.issues)

    def test_fix_flow_installs_on_submit(self):
        self.assertFalse(asyncio.run(ISS.async_check_blueprints(self.hass)))
        self.assertIn("blueprints_not_imported", self.ir.issues)
        reset = mock.AsyncMock()
        self.hass.data["blueprint"] = {"automation": types.SimpleNamespace(async_reset_cache=reset)}
        flow = self.flow()
        first = asyncio.run(flow.async_step_init())
        self.assertEqual((first["type"], first["step_id"], first["errors"]), ("form", "confirm", {}))
        self.assertIn("my.home-assistant.io", first["description_placeholders"]["links"])
        self.assertEqual(self.installed(), [], "the form alone must not write anything")
        done = asyncio.run(flow.async_step_confirm({}))
        self.assertEqual(done["type"], "create_entry")
        # Positive control: every shipped blueprint is now on disk, the
        # packaged bytes exactly, so the issue is gone and stays gone.
        self.assertEqual(self.installed(), sorted(REP.BLUEPRINT_FILES))
        for name in REP.BLUEPRINT_FILES:
            self.assertEqual(Path(self.tmp.name, "blueprints", "automation", "deckhand", name).read_bytes(),
                             (COMPONENT / "blueprints" / "automation" / name).read_bytes())
        self.assertNotIn("blueprints_not_imported", self.ir.issues)
        self.assertTrue(asyncio.run(ISS.async_check_blueprints(self.hass)))
        reset.assert_awaited_once()
        with self.assertRaises(ValueError):
            asyncio.run(FLOW.async_create_fix_flow(self.hass, "dial_offline_X", None))

    def test_never_overwrites(self):
        edited = "blueprint: {name: mine}\n# edited by hand\n"
        self.put("deckhand", "dnd_hold_calls.yaml", edited)
        self.put("whitesidest", "nfc_known_tap_scene.yaml", edited)  # a GitHub import
        copied = asyncio.run(ISS.async_install_blueprints(self.hass))  # no blueprint data: no reset
        self.assertNotIn("dnd_hold_calls.yaml", copied)
        self.assertNotIn("nfc_known_tap_scene.yaml", copied)
        self.assertEqual(len(copied), len(REP.BLUEPRINT_FILES) - 2)
        self.assertEqual(
            Path(self.tmp.name, "blueprints", "automation", "deckhand", "dnd_hold_calls.yaml").read_text(), edited)
        self.assertEqual(
            Path(self.tmp.name, "blueprints", "automation", "whitesidest", "nfc_known_tap_scene.yaml").read_text(),
            edited)
        self.assertNotIn("nfc_known_tap_scene.yaml", self.installed(), "no duplicate of an imported one")
        # A second run has nothing left to do.
        self.assertEqual(asyncio.run(ISS.async_install_blueprints(self.hass)), [])

    def test_copy_failure_keeps_the_issue(self):
        asyncio.run(ISS.async_check_blueprints(self.hass))
        # A file where the deckhand folder should be: makedirs fails.
        os.makedirs(os.path.join(self.tmp.name, "blueprints", "automation"))
        Path(self.tmp.name, "blueprints", "automation", "deckhand").write_text("not a folder")
        flow = self.flow()
        with self.assertLogs(FLOW._LOGGER, "WARNING"):
            result = asyncio.run(flow.async_step_confirm({}))
        self.assertEqual((result["type"], result["errors"]), ("form", {"base": "install_failed"}))
        self.assertIn("blueprints_not_imported", self.ir.issues)

    def test_partial_copy_is_removed(self):
        def _broken(src, out):
            out.write(b"half")
            raise OSError("disk full")

        with mock.patch.object(ISS.shutil, "copyfileobj", _broken):
            with self.assertRaises(OSError):
                ISS.install_blueprint_files(self.tmp.name)
        self.assertEqual(self.installed(), [])


# ---------------------------------------------------------------- real services
class ServicesRaiseFirmwareIssues(harness._ServiceTest):
    def setUp(self):
        super().setUp()
        issues_mod = sys.modules[f"{harness.PKG}._issues"]
        self.ir = _patch_ir(self, issues_mod)
        self.hass.data[DOMAIN] = {"entry-1": {
            "team_id": "team-1", "labels": {}, "_fw_gaps": {},
            "dials": {"DECK-AAAA": {"fw_ver": "0.4.40"}, "DECK-BBBB": {"fw_ver": "0.4.200"}},
        }}

    def test_set_dnd_on_old_firmware_is_flagged_and_still_sent(self):
        sent = self.call("set_dnd", on=True)
        self.assertEqual(len([p for t, p in sent if t.endswith("/cmd/dnd")]), 2)
        self.assertEqual(list(self.ir.issues), ["firmware_too_old_DECK-AAAA"])

    def test_ring_switches_flag_only_when_turned_on(self):
        self.call("mount_perimeter_ring", bindings=[{"id": "binary_sensor.door"}], theme_colors=False)
        self.assertEqual(self.ir.issues, {})
        self.call("mount_perimeter_ring", bindings=[{"id": "binary_sensor.door"}], hide_when_calm=True)
        p = self.ir.issues["firmware_too_old_DECK-AAAA"]["translation_placeholders"]
        self.assertEqual(p["required"], "0.4.161")

    def test_a_registry_failure_never_blocks_the_command(self):
        broken = types.SimpleNamespace(async_get=lambda hass: (_ for _ in ()).throw(RuntimeError("x")),
                                       async_create_issue=None, IssueSeverity=None)
        with mock.patch.object(sys.modules[f"{harness.PKG}._issues"], "ir", broken):
            sent = self.call("set_dnd", on=True)
        self.assertEqual(len([p for t, p in sent if t.endswith("/cmd/dnd")]), 2)


class ClimateFaceService(ServicesRaiseFirmwareIssues):
    def setUp(self):
        super().setUp()
        self.hass.states = harness._States({
            "climate.salon": harness._state("heat", temperature=70.6, current_temperature=67.2,
                                            friendly_name="Salon"),
        })

    def test_payload_matches_helm_and_seeds_live_state(self):
        p = self.one("mount_climate_face", "/cmd/face/climate/mount",
                     entity_id="climate.salon", title="Salon 🌡️", min=62, max=80, step=2,
                     hvac_modes=["heat", "cool", "off", "heat"])
        self.assertEqual(p["face_id"], "climate")
        self.assertEqual((p["entity_id"], p["title"], p["min"], p["max"], p["step"]),
                         ("climate.salon", "Salon", 62, 80, 2))
        self.assertEqual(p["hvac_modes"], ["heat", "cool", "off"])
        self.assertEqual((p["value"], p["current_temp"], p["hvac_mode"]), (71, 67, "heat"))
        self.assertNotIn("unit", p)
        self.assertEqual(p["_src"]["ch"], "home_assistant")
        # DECK-AAAA is on 0.4.40: the Climate face needs 0.4.98.
        flagged = self.ir.issues["firmware_too_old_DECK-AAAA"]["translation_placeholders"]
        self.assertEqual(flagged["required"], "0.4.98")
        self.assertEqual(list(self.ir.issues), ["firmware_too_old_DECK-AAAA"])

    def test_defaults(self):
        p = self.one("mount_climate_face", "/cmd/face/climate/mount", entity_id="climate.unknown")
        self.assertEqual((p["title"], p["min"], p["max"], p["step"]), ("Climate", 60, 85, 1))
        self.assertNotIn("hvac_modes", p)
        self.assertNotIn("value", p)

    def test_refusals_send_nothing(self):
        bad = [
            {"entity_id": "sensor.temp"},
            {"entity_id": "climate.salon", "title": "x" * 23},
            {"entity_id": "climate.salon", "min": 80, "max": 80},
            {"entity_id": "climate.salon", "min": 90},  # above the default max
            {"entity_id": "climate.salon", "max": 201},
            {"entity_id": "climate.salon", "min": -1},
            {"entity_id": "climate.salon", "step": 11},
            {"entity_id": "climate.salon", "step": 1.5},
            {"entity_id": "climate.salon", "hvac_modes": ["heat", "turbo"]},
            {"entity_id": "climate.salon",
             "hvac_modes": ["off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"]},
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(harness._SVE):
                    self.call("mount_climate_face", **data)
                self.assertEqual(self.mqtt.sent, [])


class ClimatePayloadPure(unittest.TestCase):
    def test_payload(self):
        state = types.SimpleNamespace(state="cool", attributes={"temperature": "72", "current_temperature": None})
        p = CLIMATE.build_payload({"entity_id": ["climate.salon"], "title": "Salon", "min": 62.0,
                                   "max": "80", "step": 2, "hvac_modes": "heat, cool"}, state)
        self.assertEqual(p, {
            "face_id": "climate", "schema_version": 1, "title": "Salon",
            "entity_id": "climate.salon", "min": 62, "max": 80, "step": 2,
            "hvac_modes": ["heat", "cool"], "value": 72, "hvac_mode": "cool",
        })

    def test_limits_mirror_helm(self):
        self.assertEqual((CLIMATE.TITLE_MAX, CLIMATE.MAX_MODES, CLIMATE.TEMP_MIN, CLIMATE.TEMP_MAX,
                          CLIMATE.STEP_MIN, CLIMATE.STEP_MAX), (22, 6, 0, 200, 1, 10))
        with self.assertRaises(CLIMATE.ClimateError):
            CLIMATE.build_payload({"entity_id": "climate.x", "min": True})


class ClimateServiceYaml(unittest.TestCase):
    def test_fields(self):
        svc = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))["mount_climate_face"]
        fields = svc["fields"]
        self.assertNotIn("unit", fields)
        self.assertEqual(fields["entity_id"]["selector"]["entity"]["domain"], "climate")
        self.assertTrue(fields["entity_id"]["required"])
        self.assertEqual(fields["hvac_modes"]["selector"]["select"]["options"], list(CLIMATE.HVAC_MODES))
        for name, lo, hi in (("min", 0, 200), ("max", 0, 200), ("step", 1, 10)):
            sel = fields[name]["selector"]["number"]
            self.assertEqual((sel["min"], sel["max"]), (lo, hi), name)


# ---------------------------------------------------------------- config / options flow
class FinishAndSummary(unittest.TestCase):
    def test_finish_page_then_entry(self):
        flow = reconf.CF.DeckhandConfigFlow()
        flow.hass = reconf._Hass([])
        page = asyncio.run(flow.async_step_user({"team_id": "1"}))
        self.assertEqual(page["step_id"], "finish")
        placeholders = page["description_placeholders"]
        self.assertEqual(placeholders["team_id"], "1")
        self.assertIn("my.home-assistant.io", placeholders["links"])
        self.assertIn("github.com/whitesidest/deckhand-hacs", placeholders["docs"])
        entry = asyncio.run(flow.async_step_finish({}))
        self.assertEqual((entry["type"], entry["data"]), ("create_entry", {"team_id": "1"}))

    def test_finish_without_a_team_goes_back_to_the_form(self):
        flow = reconf.CF.DeckhandConfigFlow()
        flow.hass = reconf._Hass([])
        self.assertEqual(asyncio.run(flow.async_step_finish({}))["step_id"], "user")

    def test_options_summary(self):
        entry = types.SimpleNamespace(entry_id="entry-1", options={})
        flow = reconf.CF.DeckhandOptionsFlow(entry)
        flow.hass = types.SimpleNamespace(data={DOMAIN: {"entry-1": {
            "dials": {"DECK-BBBB": {"fw_ver": "0.4.200"}, "DECK-AAAA": {}},
            "labels": {"DECK-AAAA": "Kitchen"},
        }}})
        flow.async_show_form = lambda **kw: {"type": "form", **kw}
        flow.async_show_menu = lambda **kw: {"type": "menu", **kw}

        async def _imported(hass):
            return False

        with mock.patch.object(reconf.CF._issues, "async_blueprints_imported", _imported):
            menu = asyncio.run(flow.async_step_init())
            self.assertEqual(menu["menu_options"], ["summary", "add", "remove", "done"])
            page = asyncio.run(flow.async_step_summary())
        p = page["description_placeholders"]
        self.assertEqual(p["count"], "2")
        self.assertEqual(p["dials"], "- Kitchen (DECK-AAAA)\n- DECK-BBBB — firmware 0.4.200")
        self.assertEqual(p["blueprints"], "not imported yet")
        back = asyncio.run(flow.async_step_summary({}))
        self.assertEqual(back["type"], "menu")


class Translations(unittest.TestCase):
    def test_new_strings(self):
        strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
        issues = strings["issues"]
        self.assertIn("{required}", issues["firmware_too_old"]["description"])
        self.assertIn("{hours}", issues["dial_offline"]["title"])
        fix = issues["blueprints_not_imported"]["fix_flow"]
        self.assertIn("{links}", fix["step"]["confirm"]["description"])
        self.assertIn("still_missing", fix["error"])
        self.assertIn("install_failed", fix["error"])
        self.assertIn("config/blueprints/automation/deckhand", fix["step"]["confirm"]["description"])
        self.assertEqual(strings["issues"],
                         json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))["issues"])
        self.assertIn("{links}", strings["config"]["step"]["finish"]["description"])
        self.assertIn("summary", strings["options"]["step"]["init"]["menu_options"])
        for key in ("{count}", "{dials}", "{blueprints}", "{links}", "{docs}"):
            self.assertIn(key, strings["options"]["step"]["summary"]["description"])


if __name__ == "__main__":
    unittest.main()
