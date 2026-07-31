"""Per-send screen transition parity (helm#224).

HACS publishes to the broker directly, so its half of the
no-regression guarantee is a publisher rule rather than an API call:
"fade" (the default) must never reach the wire, so an automation
written before this field existed keeps producing byte-identical
cmd/announce and cmd/face/invitation/mount payloads.

No Home Assistant is needed: the schema checks read services.yaml and
const.py, and the wire-shape checks reuse the AST harness in
test_service_schema.py, which lifts the real handlers out of
__init__.py and runs them against a recording MQTT stub.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
SERVICES_YAML = COMPONENT / "services.yaml"
INIT_PY = COMPONENT / "__init__.py"
CONST_PY = COMPONENT / "const.py"

EXPECTED = ("fade", "none", "iris", "dissolve")


def _services() -> dict:
    with open(SERVICES_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


class VocabularyTests(unittest.TestCase):
    """const.py is the single source; everything else must agree."""

    def test_const_matches_helm_and_firmware(self):
        tree = ast.parse(CONST_PY.read_text(encoding="utf-8"))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in ("TRANSITIONS", "DEFAULT_TRANSITION"):
                    found[name] = ast.literal_eval(node.value)
        self.assertEqual(found.get("TRANSITIONS"), EXPECTED)
        self.assertEqual(found.get("DEFAULT_TRANSITION"), "fade")


class ServiceSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.services = _services()

    def _field(self, service):
        fields = self.services[service].get("fields") or {}
        self.assertIn("transition", fields, service)
        return fields["transition"]

    def test_announcement_offers_every_option(self):
        field = self._field("send_announcement")
        self.assertEqual(tuple(field["selector"]["select"]["options"]), EXPECTED)
        self.assertEqual(field["default"], "fade")

    def test_invitation_offers_every_option(self):
        field = self._field("send_invitation")
        self.assertEqual(tuple(field["selector"]["select"]["options"]), EXPECTED)
        self.assertEqual(field["default"], "fade")

    def test_cancel_invitation_has_no_transition(self):
        # Arrival only, by founder decision — a cancel is usually a
        # correction or a timeout, and offering to make it theatrical
        # would be offering the wrong thing.
        fields = self.services["cancel_invitation"].get("fields") or {}
        self.assertNotIn("transition", fields)

    def test_invitation_field_documents_the_arrival_only_rule(self):
        text = self._field("send_invitation")["description"]
        self.assertIn("cancel_invitation", text)


class HandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INIT_PY.read_text(encoding="utf-8")

    def test_both_handlers_read_the_field(self):
        self.assertIn('call.data.get("transition")', self.text)
        self.assertEqual(self.text.count('_resolve_transition(call.data.get("transition"))'), 2)

    def test_default_is_never_published(self):
        # The publisher rule, pinned: every place that writes the key
        # into a payload/request dict is guarded by a != default check.
        writes = re.findall(r'^\s*(\w+)\["transition"\] = transition$', self.text, re.M)
        self.assertTrue(writes, "no publish site found — did the field get dropped?")
        guards = re.findall(r'^\s*if transition != DEFAULT_TRANSITION:$', self.text, re.M)
        self.assertEqual(
            len(guards), len(writes),
            "every 'x[\"transition\"] = transition' must sit under an "
            "'if transition != DEFAULT_TRANSITION' guard, or a default send "
            "stops being byte-identical to what it was before helm#224.",
        )

    def test_cancel_handler_never_publishes_a_transition(self):
        start = self.text.index("async def _cancel_invitation")
        end = self.text.index("def ", start + 10)
        body = self.text[start:end]
        self.assertNotIn("transition", body)

    def test_validator_rejects_unknown_values(self):
        # HA service calls come from an automation someone wrote and is
        # watching; a silently-ignored curtain reads as a broken dial.
        start = self.text.index("def _resolve_transition")
        end = self.text.index("def _resolve_targets")
        body = self.text[start:end]
        self.assertIn("ServiceValidationError", body)
        self.assertIn("TRANSITIONS", body)

    def test_init_py_still_parses(self):
        ast.parse(self.text)


class WireShapeTests(unittest.TestCase):
    """Run the real handlers and inspect what they publish.

    Reuses QuietInvitationTests' AST harness (which lifts the handlers
    out of __init__.py and runs them against a recording MQTT stub) so
    these assertions are about actual behaviour, not source text.
    """

    @classmethod
    def setUpClass(cls):
        from test_service_schema import QuietInvitationTests

        cls._harness = QuietInvitationTests("test_prompt_path_unchanged")
        cls._harness.src = INIT_PY.read_text(encoding="utf-8")

    def _run(self, handler, data):
        return self._harness._run_handler(handler, data)

    # ── announcements ────────────────────────────────────────────

    def test_announce_default_publishes_no_transition(self):
        pubs = self._run(
            "_send_announcement",
            {"device_id": ["d1"], "message": "Dinner is ready"},
        )
        self.assertTrue(pubs)
        for _topic, payload, _retain in pubs:
            self.assertNotIn("transition", payload)

    def test_announce_explicit_fade_also_publishes_nothing(self):
        pubs = self._run(
            "_send_announcement",
            {"device_id": ["d1"], "message": "Dinner", "transition": "fade"},
        )
        for _topic, payload, _retain in pubs:
            self.assertNotIn("transition", payload)

    def test_announce_curtain_is_published(self):
        for value in ("iris", "dissolve", "none"):
            with self.subTest(value=value):
                pubs = self._run(
                    "_send_announcement",
                    {"device_id": ["d1"], "message": "Dinner", "transition": value},
                )
                for _topic, payload, _retain in pubs:
                    self.assertEqual(payload["transition"], value)

    def test_announce_rejects_unknown_before_publishing(self):
        with self.assertRaises(Exception):
            self._run(
                "_send_announcement",
                {"device_id": ["d1"], "message": "Dinner", "transition": "wipe"},
            )

    # ── invitations ──────────────────────────────────────────────

    def test_prompt_default_payload_is_unchanged(self):
        pubs = self._run(
            "_send_invitation",
            {"device_id": ["d1"], "text": "Open the bar?"},
        )
        self.assertTrue(pubs)
        for _topic, payload, _retain in pubs:
            self.assertNotIn("transition", payload)

    def test_prompt_curtain_is_published(self):
        pubs = self._run(
            "_send_invitation",
            {"device_id": ["d1"], "text": "Open the bar?", "transition": "iris"},
        )
        for _topic, payload, _retain in pubs:
            self.assertEqual(payload["transition"], "iris")

    def test_quiet_invitation_carries_it_through_the_request_plane(self):
        # A menu invitation still mounts a prompt when the guest selects
        # the item, so the choice is meaningful on this transport too.
        pubs = self._run(
            "_send_invitation",
            {
                "device_id": ["d1"],
                "text": "Tea at 4?",
                "presentation": "menu",
                "transition": "dissolve",
            },
        )
        self.assertTrue(pubs)
        for topic, payload, _retain in pubs:
            self.assertIn("invitation_request", topic)
            self.assertEqual(payload["transition"], "dissolve")

    def test_invitation_rejects_unknown_before_publishing(self):
        with self.assertRaises(Exception):
            self._run(
                "_send_invitation",
                {"device_id": ["d1"], "text": "hi", "transition": "spin"},
            )

    def test_cancel_never_publishes_a_transition(self):
        # Arrival only. Both the face cancel and the request-plane
        # cancel must stay clean.
        pubs = self._run(
            "_cancel_invitation",
            {"device_id": ["d1"], "invitation_id": "abc123"},
        )
        self.assertTrue(pubs)
        for _topic, payload, _retain in pubs:
            self.assertNotIn("transition", payload)


if __name__ == "__main__":
    unittest.main()
