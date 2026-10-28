"""Every shipped blueprint is valid and calls real services (1.24.0).

Covers all of ``blueprints/automation/deckhand/`` — the NFC and DND
blueprints have their own deeper tests (test_nfc_event_paths.py,
test_dnd.py); this pins what every blueprint needs:

* it parses with HA's ``!input`` tag and has name / description /
  domain / a source_url pointing at its own file;
* every declared input is used and every ``!input`` is declared;
* every ``deckhand.*`` service it calls exists in services.yaml, with only
  fields services.yaml declares;
* the README links it with a My Home Assistant import button whose
  blueprint_url is the file's raw GitHub URL.

Plus the specifics of the three 1.24.0 blueprints (doorbell snapshot,
door-open ring flash, laundry-done menu item).

Run with:  python3 -m pytest tests/test_blueprints.py
"""

from __future__ import annotations

import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINT_DIR = ROOT / "blueprints" / "automation" / "deckhand"
SERVICES = yaml.safe_load(
    (ROOT / "custom_components" / "deckhand" / "services.yaml").read_text(encoding="utf-8")
)
README = (ROOT / "README.md").read_text(encoding="utf-8")
RAW_BASE = "https://raw.githubusercontent.com/whitesidest/deckhand-hacs/main/blueprints/automation/deckhand/"
SOURCE_BASE = "https://github.com/whitesidest/deckhand-hacs/blob/main/blueprints/automation/deckhand/"


class _InputRef:
    def __init__(self, name: str):
        self.name = name


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!input", lambda loader, node: _InputRef(loader.construct_scalar(node)))


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _service_calls(body) -> list[dict]:
    return [n for n in _walk(body) if isinstance(n, dict) and isinstance(n.get("service"), str)]


BLUEPRINTS = sorted(BLUEPRINT_DIR.glob("*.yaml"))


class EveryBlueprint(unittest.TestCase):
    def test_there_are_blueprints(self):
        self.assertGreaterEqual(len(BLUEPRINTS), 7)

    def test_metadata(self):
        for path in BLUEPRINTS:
            with self.subTest(blueprint=path.name):
                bp = _load(path)["blueprint"]
                self.assertTrue(str(bp.get("name") or "").startswith("Deckhand:"))
                self.assertTrue(str(bp.get("description") or "").strip())
                self.assertEqual(bp.get("domain"), "automation")
                self.assertEqual(bp.get("source_url"), SOURCE_BASE + path.name)

    def test_inputs_declared_and_used_match(self):
        for path in BLUEPRINTS:
            with self.subTest(blueprint=path.name):
                data = _load(path)
                declared = set(data["blueprint"].get("input") or {})
                used = {
                    n.name for n in _walk({k: v for k, v in data.items() if k != "blueprint"})
                    if isinstance(n, _InputRef)
                }
                self.assertEqual(declared, used)

    def test_deckhand_services_and_fields_exist(self):
        for path in BLUEPRINTS:
            data = _load(path)
            body = {k: v for k, v in data.items() if k != "blueprint"}
            for call in _service_calls(body):
                domain, _, name = call["service"].partition(".")
                if domain != "deckhand":
                    continue
                with self.subTest(blueprint=path.name, service=name):
                    self.assertIn(name, SERVICES, f"deckhand.{name} does not exist")
                    declared = set(SERVICES[name].get("fields") or {})
                    used = set(call.get("data") or {})
                    self.assertLessEqual(used, declared, f"undeclared fields {used - declared}")

    def test_readme_has_an_import_link(self):
        links = {
            parse_qs(urlparse(href).query)["blueprint_url"][0]
            for href in _import_hrefs(README)
        }
        for path in BLUEPRINTS:
            with self.subTest(blueprint=path.name):
                self.assertIn(RAW_BASE + path.name, links)


def _import_hrefs(text: str) -> list[str]:
    prefix = "https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url="
    hrefs = []
    start = 0
    while (i := text.find(prefix, start)) != -1:
        end = text.find(")", i)
        hrefs.append(text[i:end])
        start = end
    return hrefs


def _calls(name: str) -> dict[str, dict]:
    data = _load(BLUEPRINT_DIR / name)
    return {c["service"]: c for c in _service_calls(data["action"])}


class DoorbellSnapshot(unittest.TestCase):
    def test_announces_with_the_camera_snapshot(self):
        call = _calls("doorbell_snapshot_announcement.yaml")["deckhand.send_announcement"]
        self.assertIsInstance(call["data"]["snapshot_entity"], _InputRef)
        self.assertEqual(call["data"]["snapshot_entity"].name, "camera")
        # Every dial ("all") or the chosen ones.
        self.assertIn("'all' if all_dials else dials", call["data"]["device_id"])

    def test_does_not_queue_up_repeat_rings(self):
        data = _load(BLUEPRINT_DIR / "doorbell_snapshot_announcement.yaml")
        self.assertEqual(data["mode"], "single")


class DoorRingFlash(unittest.TestCase):
    def test_mounts_one_live_flashing_segment_for_the_door(self):
        call = _calls("door_open_ring_flash.yaml")["deckhand.mount_perimeter_ring"]
        (binding,) = call["data"]["bindings"]
        self.assertEqual(binding["id"].name, "door")
        self.assertEqual(binding["treatment"], "flash")
        self.assertEqual(binding["active_state"], "on")
        self.assertTrue(call["data"]["live_state"])
        self.assertTrue(call["data"]["hide_when_calm"])

    def test_clears_the_ring_on_close(self):
        self.assertIn("deckhand.clear_perimeter_ring", _calls("door_open_ring_flash.yaml"))


class LaundryDone(unittest.TestCase):
    def test_adds_then_removes_by_the_same_key(self):
        calls = _calls("laundry_done_menu_item.yaml")
        add = calls["deckhand.add_menu_item"]["data"]
        remove = calls["deckhand.remove_menu_item"]["data"]
        self.assertEqual(add["key"].name, "key")
        self.assertEqual(remove["key"].name, "key")
        self.assertEqual(add["ttl"].name, "ttl")

    def test_ttl_default_is_within_the_service_range(self):
        data = _load(BLUEPRINT_DIR / "laundry_done_menu_item.yaml")
        ttl = data["blueprint"]["input"]["ttl"]
        svc = SERVICES["add_menu_item"]["fields"]["ttl"]["selector"]["number"]
        self.assertGreaterEqual(ttl["default"], svc["min"])
        self.assertLessEqual(ttl["default"], svc["max"])

    def test_listens_for_both_press_shapes(self):
        data = _load(BLUEPRINT_DIR / "laundry_done_menu_item.yaml")
        presses = [t["event_data"] for t in data["trigger"] if t.get("platform") == "event"]
        self.assertIn({"type": "button_press"}, presses)
        self.assertIn({"event_type": "button_press"}, presses)


if __name__ == "__main__":
    unittest.main()
