"""NFC event-path regression tests (helm#57, docs/NFC_HA_PLAN.md Epics A1+A2).

Two things are pinned here:

1. **The raw MQTT re-fire stays raw.** The integration's own
   ``_handle_event`` re-fires dial MQTT events onto the HA bus. That
   path must NEVER synthesize the identity keys (``action``, ``role``,
   ...) that belong to the Helm-enriched relay — the documented way to
   tell the two events apart is "the enriched payload carries
   ``action``; the raw one does not." If the raw path ever grows an
   ``action`` key, every shipped blueprint double-fires and the
   enriched/raw distinction in the README becomes a lie. The enriched
   payload itself is regression-pinned on the Helm side in
   ``helm/apps/mqtt/tests/test_nfc_ha_contract.py``.

2. **The shipped blueprints stay importable.** YAML-lint-level checks:
   parseable (with HA's ``!input`` tag), ``blueprint.name`` +
   ``blueprint.domain`` present, every declared input actually
   referenced, and the trigger matched against the enriched event
   (``event_type: nfc_tap`` + the right ``action``).

Like the sibling tests, these are source-level contract guards — no
Home Assistant runtime in this env, so importing blueprints into a
live HA is out of scope.

Run with:  python3 -m pytest tests/test_nfc_event_paths.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
INIT_PY = ROOT / "custom_components" / "deckhand" / "__init__.py"
BLUEPRINT_DIR = ROOT / "blueprints" / "automation" / "deckhand"

BLUEPRINT_ACTIONS = {
    "nfc_known_tap_scene.yaml": "known",
    "nfc_unknown_tap_alert.yaml": "unknown",
    "nfc_revoked_tap_alert.yaml": "revoked",
}


# ── Raw re-fire path stays un-enriched ──────────────────────────────

def _handle_event_block() -> str:
    """Return the source of ``_handle_event`` (the raw MQTT re-fire)."""
    src = INIT_PY.read_text(encoding="utf-8")
    match = re.search(
        r"def _handle_event\(.*?(?=\n    entry\.async_on_unload)",
        src,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(
            "_handle_event (raw MQTT re-fire) not found in __init__.py"
        )
    return match.group(0)


class RawRefireStaysRawTests(unittest.TestCase):
    """The raw path must never look like the Helm-enriched relay."""

    @classmethod
    def setUpClass(cls):
        cls.block = _handle_event_block()

    def test_refire_present_and_shape_pinned(self):
        # The raw path must keep existing (plan §2.1: do NOT remove it)
        # and keep its documented shape.
        self.assertIn('f"{DOMAIN}_dial_event"', self.block)
        for key in ('"dial_id"', '"type"', '"payload"', '"ts"'):
            self.assertIn(
                key, self.block,
                f"raw re-fire lost its {key} key — existing 'any tap "
                "happened' automations break.",
            )

    def test_refire_never_synthesizes_enriched_keys(self):
        # "The enriched payload carries `action`; raw does not" is the
        # documented discriminator. Blueprints rely on it to avoid
        # double-firing. Keep the raw path identity-free.
        for enriched_key in ('"action"', "'action'", '"role"', "'role'",
                             '"item_id"', '"item_label"', '"credential_id"'):
            self.assertNotIn(
                enriched_key, self.block,
                f"raw MQTT re-fire now emits {enriched_key} — that key "
                "belongs to the Helm-enriched relay only. Synthesizing "
                "it here collapses the enriched/raw distinction and "
                "double-fires every shipped blueprint.",
            )


# ── Blueprint lint ──────────────────────────────────────────────────

class _InputRef:
    """Stand-in for HA's ``!input`` tag."""

    def __init__(self, name: str):
        self.name = name


class _BlueprintLoader(yaml.SafeLoader):
    pass


_BlueprintLoader.add_constructor(
    "!input", lambda loader, node: _InputRef(loader.construct_scalar(node))
)


def _load_blueprint(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=_BlueprintLoader)


def _collect_input_refs(node) -> set[str]:
    refs: set[str] = set()
    if isinstance(node, _InputRef):
        refs.add(node.name)
    elif isinstance(node, dict):
        for value in node.values():
            refs |= _collect_input_refs(value)
    elif isinstance(node, list):
        for item in node:
            refs |= _collect_input_refs(item)
    return refs


class BlueprintLintTests(unittest.TestCase):
    """YAML-lint-level guarantees for the shipped NFC blueprints."""

    def test_expected_blueprints_exist(self):
        for name in BLUEPRINT_ACTIONS:
            self.assertTrue(
                (BLUEPRINT_DIR / name).is_file(),
                f"blueprint {name} missing from {BLUEPRINT_DIR}",
            )

    def test_no_stray_files_in_blueprint_dir(self):
        # Anything unexpected here ships to every HACS user.
        actual = {p.name for p in BLUEPRINT_DIR.iterdir()}
        self.assertEqual(actual, set(BLUEPRINT_ACTIONS))

    def test_blueprints_parse_with_metadata(self):
        for name in BLUEPRINT_ACTIONS:
            with self.subTest(blueprint=name):
                data = _load_blueprint(BLUEPRINT_DIR / name)
                bp = data.get("blueprint")
                self.assertIsInstance(bp, dict, f"{name}: no blueprint: block")
                self.assertTrue(
                    str(bp.get("name", "")).strip(),
                    f"{name}: blueprint.name missing",
                )
                self.assertEqual(
                    bp.get("domain"), "automation",
                    f"{name}: blueprint.domain must be 'automation'",
                )
                self.assertTrue(
                    str(bp.get("description", "")).strip(),
                    f"{name}: blueprint.description missing",
                )

    def test_every_declared_input_is_referenced(self):
        for name in BLUEPRINT_ACTIONS:
            with self.subTest(blueprint=name):
                data = _load_blueprint(BLUEPRINT_DIR / name)
                declared = set((data["blueprint"].get("input") or {}))
                self.assertTrue(declared, f"{name}: declares no inputs")
                referenced = _collect_input_refs(
                    {k: v for k, v in data.items() if k != "blueprint"}
                )
                self.assertEqual(
                    declared, referenced,
                    f"{name}: declared inputs {sorted(declared)} != "
                    f"referenced !input tags {sorted(referenced)}",
                )

    def test_triggers_match_enriched_event_only(self):
        # Each blueprint must key on event_type=nfc_tap AND an action
        # value — the `action` key is what restricts matching to the
        # Helm-enriched relay (the raw re-fire has no such key).
        for name, expected_action in BLUEPRINT_ACTIONS.items():
            with self.subTest(blueprint=name):
                data = _load_blueprint(BLUEPRINT_DIR / name)
                triggers = data.get("trigger")
                self.assertIsInstance(triggers, list, f"{name}: no trigger list")
                self.assertEqual(len(triggers), 1, f"{name}: expected one trigger")
                trig = triggers[0]
                self.assertEqual(trig.get("platform"), "event")
                self.assertEqual(trig.get("event_type"), "deckhand_dial_event")
                event_data = trig.get("event_data") or {}
                self.assertEqual(event_data.get("event_type"), "nfc_tap")
                self.assertEqual(
                    event_data.get("action"), expected_action,
                    f"{name}: trigger must pin action={expected_action}",
                )

    def test_notify_blueprints_call_the_notify_input(self):
        for name in ("nfc_unknown_tap_alert.yaml", "nfc_revoked_tap_alert.yaml"):
            with self.subTest(blueprint=name):
                data = _load_blueprint(BLUEPRINT_DIR / name)
                actions = data.get("action") or []
                services = [
                    step.get("service") for step in actions
                    if isinstance(step, dict)
                ]
                self.assertTrue(
                    any(
                        isinstance(svc, _InputRef) and svc.name == "notify_service"
                        for svc in services
                    ),
                    f"{name}: action must call the notify_service input",
                )


if __name__ == "__main__":
    unittest.main()
