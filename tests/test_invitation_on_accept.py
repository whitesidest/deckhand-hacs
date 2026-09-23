"""send_invitation's structured on-accept fields (2026-09-23 UX audit).

``on_accept`` is a raw opaque JSON object — the only shape the dial and
Helm actually consume — but hand-typing JSON in HA's service-call
editor was the last remaining "type raw YAML" gap this integration had
for invitations. The founder approved an ADDITIVE fix, mirroring the
structured "on accept" form Helm and Console already ship
(``apps/invitations/views.py::_build_on_accept_data`` in the sibling
Helm repo): new flat ``on_accept_type`` + ``on_theme_slug`` /
``on_face_id`` / ``on_message_text`` / ``on_subtitle_text`` /
``on_ha_domain`` / ``on_ha_service`` / ``on_ha_entity`` /
``on_webhook_url`` fields assemble the same ``{"type", "data"}`` shape
via ``_build_on_accept_from_fields`` in ``__init__.py``.

The raw ``on_accept`` object is untouched and always wins when it is a
non-empty dict — these tests pin that precedence explicitly, since a
regression there would silently break any existing automation that
relies on ``on_accept`` being genuinely arbitrary.

These tests reuse the AST harness already built for exactly this
handler in ``test_service_schema.py`` (``QuietInvitationTests.
_run_handler``), which lifts ``_send_invitation`` (and now
``_build_on_accept_from_fields`` / ``_split_ha_service`` too — see the
``helpers`` allow-list in that file) out of ``__init__.py`` and runs
them against a recording MQTT stub. No Home Assistant runtime needed.

Run from the tests directory:  python3 -m unittest test_invitation_on_accept
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
INIT_PY = COMPONENT / "__init__.py"


class _HarnessCase(unittest.TestCase):
    """Base class wiring up QuietInvitationTests' handler-execution harness."""

    @classmethod
    def setUpClass(cls):
        from test_service_schema import QuietInvitationTests

        cls._harness = QuietInvitationTests("test_prompt_path_unchanged")
        cls._harness.src = INIT_PY.read_text(encoding="utf-8")

    def _run(self, handler, data):
        return self._harness._run_handler(handler, data)

    def _prompt_on_accept(self, data):
        """Run the prompt-path handler and return the on_accept dict
        published for the first (of two identical) targeted dials."""
        pubs = self._run("_send_invitation", {"device_id": ["d1"], "text": "x", **data})
        self.assertEqual(len(pubs), 2)
        payload = pubs[0][1]
        return payload.get("on_accept")

    def _menu_on_accept(self, data):
        pubs = self._run(
            "_send_invitation",
            {"device_id": ["d1"], "text": "x", "presentation": "menu", **data},
        )
        self.assertEqual(len(pubs), 2)
        payload = pubs[0][1]
        return payload.get("on_accept")


# ── backward compat: raw on_accept is untouched ─────────────────────


class RawOnAcceptUnchangedTests(_HarnessCase):
    def test_prompt_path_raw_on_accept_passes_through_verbatim(self):
        raw = {"type": "fire_ha_service", "data": {"domain": "light", "service": "turn_on"}}
        self.assertEqual(self._prompt_on_accept({"on_accept": raw}), raw)

    def test_menu_path_raw_on_accept_passes_through_verbatim(self):
        raw = {"type": "push_theme", "data": {"theme_slug": "sunset-cove"}}
        self.assertEqual(self._menu_on_accept({"on_accept": raw}), raw)

    def test_arbitrary_opaque_shape_survives_untouched(self):
        # on_accept is documented as OPAQUE — an automation may put
        # anything in it. Nothing about the new flat fields may
        # constrain or reshape it.
        raw = {"whatever": ["a", "b"], "nested": {"x": 1}}
        self.assertEqual(self._prompt_on_accept({"on_accept": raw}), raw)


# ── precedence: raw wins over flat fields ────────────────────────────


class PrecedenceTests(_HarnessCase):
    def test_prompt_path_raw_wins_when_both_are_supplied(self):
        raw = {"type": "webhook", "data": {"url": "https://example.com/hook"}}
        result = self._prompt_on_accept({
            "on_accept": raw,
            "on_accept_type": "push_theme",
            "on_theme_slug": "sunset-cove",
        })
        self.assertEqual(result, raw)

    def test_menu_path_raw_wins_when_both_are_supplied(self):
        raw = {"type": "webhook", "data": {"url": "https://example.com/hook"}}
        result = self._menu_on_accept({
            "on_accept": raw,
            "on_accept_type": "push_theme",
            "on_theme_slug": "sunset-cove",
        })
        self.assertEqual(result, raw)


# ── flat fields assemble the structured shape ────────────────────────


class StructuredFieldsTests(_HarnessCase):
    def test_prompt_path_push_theme(self):
        result = self._prompt_on_accept({
            "on_accept_type": "push_theme",
            "on_theme_slug": "sunset-cove",
        })
        self.assertEqual(result, {"type": "push_theme", "data": {"theme_slug": "sunset-cove"}})

    def test_menu_path_push_theme(self):
        result = self._menu_on_accept({
            "on_accept_type": "push_theme",
            "on_theme_slug": "sunset-cove",
        })
        self.assertEqual(result, {"type": "push_theme", "data": {"theme_slug": "sunset-cove"}})

    def test_prompt_path_fire_ha_service_with_entity(self):
        result = self._prompt_on_accept({
            "on_accept_type": "fire_ha_service",
            "on_ha_domain": "light",
            "on_ha_service": "turn_on",
            "on_ha_entity": "light.galley",
        })
        self.assertEqual(
            result,
            {
                "type": "fire_ha_service",
                "data": {
                    "domain": "light",
                    "service": "turn_on",
                    "service_data": {"entity_id": "light.galley"},
                },
            },
        )

    def test_menu_path_fire_ha_service_with_entity(self):
        result = self._menu_on_accept({
            "on_accept_type": "fire_ha_service",
            "on_ha_domain": "light",
            "on_ha_service": "turn_on",
            "on_ha_entity": "light.galley",
        })
        self.assertEqual(
            result,
            {
                "type": "fire_ha_service",
                "data": {
                    "domain": "light",
                    "service": "turn_on",
                    "service_data": {"entity_id": "light.galley"},
                },
            },
        )

    def test_fire_ha_service_without_entity_omits_service_data(self):
        result = self._prompt_on_accept({
            "on_accept_type": "fire_ha_service",
            "on_ha_domain": "light",
            "on_ha_service": "turn_on",
        })
        self.assertEqual(result, {"type": "fire_ha_service", "data": {"domain": "light", "service": "turn_on"}})

    def test_fully_qualified_service_splits_when_domain_is_blank(self):
        # Mirrors Helm's split_service: a fully-qualified value typed
        # straight into the service field splits into domain+service.
        result = self._prompt_on_accept({
            "on_accept_type": "fire_ha_service",
            "on_ha_service": "light.turn_on",
        })
        self.assertEqual(result, {"type": "fire_ha_service", "data": {"domain": "light", "service": "turn_on"}})

    def test_push_face(self):
        result = self._prompt_on_accept({"on_accept_type": "push_face", "on_face_id": "42"})
        self.assertEqual(result, {"type": "push_face", "data": {"face_pk": "42"}})

    def test_push_message_is_capped_at_240(self):
        result = self._prompt_on_accept({
            "on_accept_type": "push_message",
            "on_message_text": "x" * 300,
        })
        self.assertEqual(result["type"], "push_message")
        self.assertEqual(len(result["data"]["text"]), 240)

    def test_push_subtitle_is_capped_at_120(self):
        result = self._prompt_on_accept({
            "on_accept_type": "push_subtitle",
            "on_subtitle_text": "x" * 200,
        })
        self.assertEqual(result["type"], "push_subtitle")
        self.assertEqual(len(result["data"]["text"]), 120)

    def test_webhook(self):
        result = self._prompt_on_accept({
            "on_accept_type": "webhook",
            "on_webhook_url": "https://example.com/hook",
        })
        self.assertEqual(result, {"type": "webhook", "data": {"url": "https://example.com/hook"}})

    def test_webhook_rejects_a_non_url(self):
        result = self._prompt_on_accept({
            "on_accept_type": "webhook",
            "on_webhook_url": "not-a-url",
        })
        self.assertIsNone(result)

    def test_none_type_produces_no_on_accept_key(self):
        pubs = self._run("_send_invitation", {"device_id": ["d1"], "text": "x"})
        self.assertNotIn("on_accept", pubs[0][1])

    def test_selected_type_missing_its_required_field_produces_nothing(self):
        # A half-filled form (picked push_theme, forgot the slug) must
        # degrade to no on-accept action, not a broken payload.
        result = self._prompt_on_accept({"on_accept_type": "push_theme"})
        self.assertIsNone(result)


# ── services.yaml schema ──────────────────────────────────────────────


class ServicesYamlOnAcceptFieldsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import yaml

        with open(COMPONENT / "services.yaml", encoding="utf-8") as f:
            cls.services = yaml.safe_load(f)

    def test_new_fields_are_declared(self):
        fields = self.services["send_invitation"]["fields"]
        for name in (
            "on_accept_type", "on_theme_slug", "on_face_id", "on_message_text",
            "on_subtitle_text", "on_ha_domain", "on_ha_service", "on_ha_entity",
            "on_webhook_url",
        ):
            self.assertIn(name, fields, f"{name} missing from send_invitation fields")

    def test_on_accept_still_declared_and_not_required(self):
        fields = self.services["send_invitation"]["fields"]
        self.assertIn("on_accept", fields)
        self.assertIn("object", fields["on_accept"]["selector"])
        self.assertFalse(fields["on_accept"].get("required", False))

    def test_on_accept_type_offers_every_kind(self):
        field = self.services["send_invitation"]["fields"]["on_accept_type"]
        values = [
            opt["value"] if isinstance(opt, dict) else opt
            for opt in field["selector"]["select"]["options"]
        ]
        self.assertEqual(
            set(values),
            {"none", "push_theme", "push_face", "push_message", "push_subtitle",
             "fire_ha_service", "webhook"},
        )

    def test_on_ha_entity_uses_a_real_entity_selector(self):
        field = self.services["send_invitation"]["fields"]["on_ha_entity"]
        self.assertIn("entity", field["selector"])

    def test_on_face_id_is_plain_text_not_a_selector_ha_cannot_enumerate(self):
        field = self.services["send_invitation"]["fields"]["on_face_id"]
        self.assertIn("text", field["selector"])


class HandlerReadsNewFieldsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INIT_PY.read_text(encoding="utf-8")

    def test_helper_defined_and_called_from_both_branches(self):
        self.assertIn("def _build_on_accept_from_fields(call)", self.text)
        self.assertEqual(
            self.text.count("_build_on_accept_from_fields(call)"),
            3,  # 1 def + 2 call sites (prompt branch, menu branch)
            "expected the helper's def plus exactly two call sites",
        )

    def test_helper_reads_every_flat_field(self):
        start = self.text.index("def _build_on_accept_from_fields")
        end = self.text.index("\ndef _resolve_targets")
        body = self.text[start:end]
        for field in (
            "on_accept_type", "on_theme_slug", "on_face_id", "on_message_text",
            "on_subtitle_text", "on_ha_domain", "on_ha_service", "on_ha_entity",
            "on_webhook_url",
        ):
            self.assertIn(f'"{field}"', body, f"_build_on_accept_from_fields never reads {field}")


if __name__ == "__main__":
    unittest.main()
