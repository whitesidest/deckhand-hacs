"""DND Phase 2 contract tests (helm#179 — HACS surface).

Source-level pins, same posture as the sibling test modules (no Home
Assistant runtime in this env):

1. **Wire contract mirrors Helm's ``push_dnd`` exactly.** The
   ``set_dnd`` handler must publish retained ``{"on": ..., "source":
   "ha"}`` to ``cmd/dnd`` and, on off, follow with an EMPTY retained
   payload (the invitation-terminal-state hygiene rule). If the
   empty-clear ever disappears, a reconnecting dial replays a stale
   quiet mode — the exact bug the Helm contract exists to prevent.

2. **Service schema** — ``set_dnd`` declared in services.yaml with
   ``device_id`` + ``on``, and the handler reads both.

3. **Binary sensor** — ``DeckhandDndSensor`` exists, keys off the
   ``dnd`` status field, has no device_class (deliberate — HA has no
   honest class for "the room asks for quiet"), and reports
   *unavailable* (not off) when the field is absent (firmware
   < 0.4.44) so automations can't confuse "can't report DND" with
   "not in DND".

4. **Blueprint lint** for ``dnd_hold_calls.yaml`` — parseable with
   HA's ``!input`` tag, metadata present, every declared input
   referenced, and both triggers pinned to real on/off transitions
   (``from``/``to`` explicit, so ``unavailable`` never fires a branch).

5. **R1 arbitration stays hands-off** — DND is idempotent state, not a
   media action: ``set_dnd`` must not grow a presence/``owns`` entry.

Run with:  python3 -m pytest tests/test_dnd.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
INIT_PY = COMPONENT / "__init__.py"
BINARY_SENSOR_PY = COMPONENT / "binary_sensor.py"
CONST_PY = COMPONENT / "const.py"
SERVICES_YAML = COMPONENT / "services.yaml"
BLUEPRINT = ROOT / "blueprints" / "automation" / "deckhand" / "dnd_hold_calls.yaml"


def _set_dnd_block() -> str:
    src = INIT_PY.read_text(encoding="utf-8")
    match = re.search(
        r"async def _set_dnd\(.*?(?=\n    async def )",
        src,
        re.DOTALL,
    )
    if not match:
        raise AssertionError("_set_dnd handler not found in __init__.py")
    return match.group(0)


# ── 1+2. Service handler + schema ───────────────────────────────────

class SetDndServiceTests(unittest.TestCase):
    """set_dnd mirrors Helm's push_dnd wire contract."""

    @classmethod
    def setUpClass(cls):
        cls.block = _set_dnd_block()
        with open(SERVICES_YAML, encoding="utf-8") as f:
            cls.services = yaml.safe_load(f)

    def test_declared_in_services_yaml(self):
        self.assertIn("set_dnd", self.services)
        fields = self.services["set_dnd"].get("fields") or {}
        for field in ("device_id", "on"):
            self.assertIn(field, fields, f"set_dnd missing field {field}")
        self.assertTrue(fields["device_id"].get("required"))
        self.assertTrue(fields["on"].get("required"))

    def test_registered(self):
        src = INIT_PY.read_text(encoding="utf-8")
        self.assertIn('async_register(DOMAIN, "set_dnd", _set_dnd)', src)

    def test_handler_reads_declared_fields(self):
        for field in ("device_id", "on"):
            self.assertIn(
                f'call.data.get("{field}")', self.block,
                f"_set_dnd never reads declared field {field}",
            )

    def test_topic_matches_helm_contract(self):
        const_src = CONST_PY.read_text(encoding="utf-8")
        self.assertIn(
            'TOPIC_CMD_DND = "deckhand/{team_id}/dial/{dial_id}/cmd/dnd"',
            const_src,
            "cmd/dnd topic template drifted from the Phase-1 wire contract",
        )
        self.assertIn("TOPIC_CMD_DND.format", self.block)

    def test_payload_shape_matches_helm(self):
        # Helm's push_dnd publishes {"on": bool, "source": str}. The
        # HACS source tag is "ha" (Helm strips/reads it for logging).
        self.assertIn('{"on": on, "source": "ha"}', self.block)

    def test_set_is_retained(self):
        # The on-payload MUST be retained so an offline dial converges
        # on reconnect — that is the whole point of the topic.
        self.assertIn("retain=True", self.block)

    def test_off_empty_clears_retained_slot(self):
        # Helm's push_dnd: on=False → publish the clear, THEN empty-clear
        # the retained slot. Pin both the empty publish and its
        # conditionality on the off-path.
        self.assertIn("if not on:", self.block)
        self.assertTrue(
            re.search(
                r'if not on:.*?async_publish\(hass, topic, "", qos=1, retain=True\)',
                self.block,
                re.DOTALL,
            ),
            "off-path must publish an EMPTY retained payload to cmd/dnd "
            "(invitation-terminal-state hygiene) — without it the broker "
            "replays a stale quiet mode at every reconnect",
        )

    def test_admin_gated(self):
        self.assertIn("_require_admin(call)", self.block)

    def test_r1_arbitration_documented_and_hands_off(self):
        # DND is idempotent state, not a media action — no presence/owns
        # change. Pin the code comment AND that _presence.py was not
        # taught about dnd.
        self.assertIn("idempotent", self.block)
        presence_src = (COMPONENT / "_presence.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "dnd", presence_src.lower(),
            "_presence.py must not claim DND in owns — the dial is the "
            "single executor; every remote setter converges through the "
            "retained topic",
        )


# ── 3. Binary sensor ────────────────────────────────────────────────

class DndBinarySensorTests(unittest.TestCase):
    """DeckhandDndSensor source-level pins."""

    @classmethod
    def setUpClass(cls):
        cls.src = BINARY_SENSOR_PY.read_text(encoding="utf-8")
        match = re.search(
            r"class DeckhandDndSensor\(.*$", cls.src, re.DOTALL
        )
        if not match:
            raise AssertionError("DeckhandDndSensor not found")
        cls.block = match.group(0)

    def test_registered_at_discovery(self):
        # Both sensors must be added in the same discovery callback so
        # every dial gets a DND sensor under its existing HA device.
        self.assertRegex(
            self.src,
            r"async_add_entities\(\s*\[\s*DeckhandConnectivitySensor\(dial_id, data\),"
            r"\s*DeckhandDndSensor\(dial_id, data\),",
        )

    def test_unique_id(self):
        self.assertIn('f"deckhand_{dial_id}_dnd"', self.block)

    def test_no_device_class(self):
        # Deliberate: no honest BinarySensorDeviceClass exists for DND.
        self.assertNotIn("_attr_device_class", self.block)

    def test_driven_by_status_dnd_field(self):
        self.assertIn('self._dial_data.get("dnd")', self.block)

    def test_unavailable_when_field_absent(self):
        # THE old-firmware decision: absent field (fw < 0.4.44) →
        # unavailable, never a confident "off".
        self.assertIn('"dnd" not in self._dial_data', self.block)

    def test_listens_for_status_updates(self):
        self.assertIn('_status_update', self.block)


# ── 4. Blueprint lint (loader shared with the NFC lint style) ───────

class _InputRef:
    def __init__(self, name: str):
        self.name = name


class _BlueprintLoader(yaml.SafeLoader):
    pass


_BlueprintLoader.add_constructor(
    "!input", lambda loader, node: _InputRef(loader.construct_scalar(node))
)


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


class DndBlueprintLintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(BLUEPRINT, encoding="utf-8") as f:
            cls.data = yaml.load(f, Loader=_BlueprintLoader)

    def test_metadata(self):
        bp = self.data.get("blueprint")
        self.assertIsInstance(bp, dict)
        self.assertTrue(str(bp.get("name", "")).strip())
        self.assertEqual(bp.get("domain"), "automation")
        self.assertTrue(str(bp.get("description", "")).strip())

    def test_every_declared_input_is_referenced(self):
        declared = set(self.data["blueprint"].get("input") or {})
        self.assertEqual(
            declared, {"dnd_sensor", "indicator_light", "notify_service"}
        )
        referenced = _collect_input_refs(
            {k: v for k, v in self.data.items() if k != "blueprint"}
        )
        self.assertEqual(declared, referenced)

    def test_triggers_are_unavailable_safe(self):
        # Explicit from/to on both triggers: `unavailable` (old firmware,
        # dial offline, HA restart) must never fire either branch.
        triggers = self.data.get("trigger")
        self.assertIsInstance(triggers, list)
        self.assertEqual(len(triggers), 2)
        transitions = set()
        for trig in triggers:
            self.assertEqual(trig.get("platform"), "state")
            self.assertIsInstance(trig.get("entity_id"), _InputRef)
            transitions.add((trig.get("from"), trig.get("to")))
        self.assertEqual(transitions, {("off", "on"), ("on", "off")})

    def test_actions_cover_both_branches(self):
        text = BLUEPRINT.read_text(encoding="utf-8")
        self.assertIn("light.turn_on", text)
        self.assertIn("light.turn_off", text)
        # Notify fires on both edges.
        self.assertEqual(text.count("service: !input notify_service"), 2)


if __name__ == "__main__":
    unittest.main()
