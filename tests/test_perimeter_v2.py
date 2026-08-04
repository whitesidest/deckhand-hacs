"""Perimeter v2 contract tests (1.10.1) — numeric-threshold binding keys.

Source-level pins, same posture as the sibling test modules (no Home
Assistant runtime in this env):

1. **Binding shaper passes the threshold trio through.** Helm's
   ``PerimeterBinding`` schema (apps/faces/payloads.py) grew
   ``numeric_threshold`` / ``above_state`` / ``below_state`` — Helm's
   ``poll_perimeter_faces`` feeder compares float(entity state) >
   threshold and pushes above_state / below_state. The HACS mount
   shaper is a whitelist, so without an explicit pass-through the keys
   silently vanish from the retained mount payload and a ring config
   can't round-trip between the HACS and Helm paths.

2. **update_perimeter_state wire contract** stays pinned to the fw
   pp_on_state schema ({"bindings": [{id, state | value | event}]})
   and now clamps ``value`` to 0-1 — the firmware stores the raw float
   and lerps the gradient without clamping, so out-of-range values
   would extrapolate past active_color on-dial.

3. **services.yaml steering** — both perimeter services must document
   the two-path split (HACS-mounted = self-driven via
   update_perimeter_state; Helm-published = persistent + fed by Helm's
   poller) and that the threshold keys are honored by Helm's feeder
   only, with the grid-energy example present.

Run with:  python3 -m pytest tests/test_perimeter_v2.py
"""

from __future__ import annotations

import ast
import logging
import re
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
INIT_PY = COMPONENT / "__init__.py"
SERVICES_YAML = COMPONENT / "services.yaml"
MANIFEST = COMPONENT / "manifest.json"

THRESHOLD_KEYS = ("numeric_threshold", "above_state", "below_state")


def _load_init_text() -> str:
    return INIT_PY.read_text(encoding="utf-8")


def _load_services() -> dict:
    with open(SERVICES_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── 1. Binding shaper — threshold trio pass-through ─────────────────

class ThresholdBindingShapingTests(unittest.TestCase):
    """Behavioral test of the binding shaper, lifted via AST — same
    approach as PerimeterBindingShapingTests in test_service_schema.py
    (the shapers are nested in ``async_setup_entry`` but close over
    nothing but module globals)."""

    @classmethod
    def setUpClass(cls):
        src = _load_init_text()
        tree = ast.parse(src)
        wanted = {"_build_perimeter_binding", "_normalise_color"}
        fns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name in wanted
        ]
        assert {f.name for f in fns} == wanted, "shaper functions not found"
        ns: dict = {
            "Any": Any,
            "_LOGGER": logging.getLogger("test"),
            "PERIMETER_TREATMENTS": (
                "state_color", "ripple", "gradient", "flash", "sweep",
            ),
        }
        exec(
            compile(ast.Module(body=fns, type_ignores=[]), INIT_PY.name, "exec"),
            ns,
        )
        cls.build = staticmethod(ns["_build_perimeter_binding"])

    def test_threshold_trio_passes_through(self):
        out = self.build({
            "id": "sensor.grid_power",
            "numeric_threshold": 0,
            "above_state": "exporting",
            "below_state": "importing",
        })
        self.assertEqual(out["numeric_threshold"], 0.0)
        self.assertEqual(out["above_state"], "exporting")
        self.assertEqual(out["below_state"], "importing")

    def test_threshold_coerced_to_float(self):
        out = self.build({"id": "x", "numeric_threshold": "1500"})
        self.assertEqual(out["numeric_threshold"], 1500.0)

    def test_absent_threshold_keys_stay_absent(self):
        # exclude-none contract: Helm's feeder treats key-absent as
        # "plain state matching" — a shaped binding must not invent
        # threshold keys (the perimeter_pulse null-vs-containsKey rule).
        out = self.build({"id": "front_door", "active_state": "unlocked"})
        for key in THRESHOLD_KEYS:
            self.assertNotIn(key, out)

    def test_junk_threshold_values_dropped(self):
        out = self.build({
            "id": "x",
            "numeric_threshold": "not-a-number",
            "above_state": "",
            "below_state": 7,
        })
        for key in THRESHOLD_KEYS:
            self.assertNotIn(key, out, f"junk {key} must be dropped, not forwarded")

    def test_existing_keys_unaffected(self):
        # Regression guard: adding the trio must not disturb v1 shaping.
        out = self.build({
            "id": "front_door",
            "treatment": "state_color",
            "active_state": "unlocked",
            "base_color": "68C8D8",
            "value": 1.7,
        })
        self.assertEqual(out["active_state"], "unlocked")
        self.assertEqual(out["base_color"], "#68C8D8")
        self.assertEqual(out["value"], 1.0)


# ── 2. update_perimeter_state wire contract ─────────────────────────

class UpdatePerimeterStateContractTests(unittest.TestCase):
    """Pin the fw pp_on_state contract + the new value clamp."""

    @classmethod
    def setUpClass(cls):
        cls.src = _load_init_text()
        match = re.search(
            r"async def _update_perimeter_state\(.*?(?=\n    async def )",
            cls.src,
            re.DOTALL,
        )
        assert match, "_update_perimeter_state handler not found"
        cls.block = match.group(0)

    def test_canonical_bindings_key(self):
        self.assertIn('json.dumps({"bindings": clean_states})', self.block)

    def test_state_value_event_all_read(self):
        for key in ("state", "value", "event"):
            self.assertIn(f'"{key}"', self.block,
                          f"handler must read the '{key}' update key")

    def test_value_clamped_to_unit_range(self):
        # fw pp_on_state stores the raw float and the gradient lerp
        # doesn't clamp — the integration owns the 0-1 contract, on the
        # update lane exactly like the mount lane.
        self.assertRegex(
            self.block,
            r'min\(max\(float\(entry\["value"\]\),\s*0\.0\),\s*1\.0\)',
            "update lane must clamp value to 0-1 like the mount shaper",
        )

    def test_not_retained(self):
        # State updates are ephemeral event traffic; retaining them
        # would replay stale states at every reconnect.
        self.assertIn("retain=False", self.block)


# ── 3. services.yaml steering + example ─────────────────────────────

class ServicesSteeringTests(unittest.TestCase):
    """The two-path split must be documented where users read it."""

    @classmethod
    def setUpClass(cls):
        cls.services = _load_services()
        cls.mount = cls.services["mount_perimeter_pulse"]
        cls.update = cls.services["update_perimeter_state"]

    def test_mount_documents_threshold_keys(self):
        desc = self.mount["fields"]["bindings"]["description"]
        for key in THRESHOLD_KEYS:
            self.assertIn(key, desc, f"bindings description missing {key}")

    def test_mount_steers_between_paths(self):
        desc = self.mount["description"]
        self.assertIn("update_perimeter_state", desc)
        self.assertIn("Helm", desc)
        self.assertIn("template", desc)

    def test_threshold_keys_marked_helm_feeder_only(self):
        combined = (
            self.mount["description"]
            + self.mount["fields"]["bindings"]["description"]
        )
        self.assertRegex(
            combined, r"(?i)helm'?s feeder\s+only",
            "docs must say the threshold keys are honored by Helm's feeder only",
        )

    def test_grid_energy_example_present(self):
        example = self.mount["fields"]["bindings"]["example"]
        self.assertIn("sensor.grid_power", example)
        parsed = yaml.safe_load(example)
        grid = next(b for b in parsed if b["id"] == "sensor.grid_power")
        self.assertEqual(grid["numeric_threshold"], 0)
        self.assertEqual(grid["above_state"], "exporting")
        self.assertEqual(grid["below_state"], "importing")
        self.assertEqual(grid["active_state"], "exporting")

    def test_update_mentions_template_for_numeric_sensors(self):
        desc = self.update["description"]
        self.assertIn("template", desc)
        self.assertIn("Helm", desc)


# ── 4. Release hygiene ──────────────────────────────────────────────

class ManifestVersionTests(unittest.TestCase):
    def test_version_is_at_least_the_release_this_file_covers(self):
        """Perimeter v2 shipped in 1.10.1, so anything below that means this
        file's features are not in the built integration.

        Was an equality check against "1.10.1", which turned every later
        release into a failure in a file that has nothing to do with
        versioning. The exact current version is asserted once, in
        test_hacs_presence.ManifestTests.
        """
        import json
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        triple = tuple(int(p) for p in manifest["version"].split("."))
        self.assertGreaterEqual(triple, (1, 10, 1), manifest["version"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
