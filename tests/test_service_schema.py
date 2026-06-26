"""Regression tests pinning the public service schema.

This file exists because of a real regression: a refactor (2026-05-11
Theme/Face schema split) deleted ``home_face`` + ``sensor_quad_*`` +
``sensor_marquee`` + friends from ``apply_overlay`` without a
deprecation path, breaking every existing user automation. There is
no other test infrastructure in the integration — the user's
automations were the only thing keeping the surface alive, and
"existing automations keep working" is not a property your linter
can check.

These tests are intentionally low-leverage: they only verify the
*service schema* (what fields appear in ``services.yaml`` and which
ones the handler in ``__init__.py`` reads). They do NOT exercise
runtime behavior — that requires Home Assistant + a live MQTT broker.
The goal is to catch silent surface-shrink the way "I deleted a
public field" should be caught at code-review time, not at
"the user's air-quality automation stopped working" time.

Run with:  python3 -m pytest tests/  (or just  python3 tests/test_service_schema.py)
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SERVICES_YAML = ROOT / "custom_components" / "deckhand" / "services.yaml"
INIT_PY = ROOT / "custom_components" / "deckhand" / "__init__.py"


def _load_services() -> dict:
    with open(SERVICES_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_init_text() -> str:
    return INIT_PY.read_text(encoding="utf-8")


# ── Pinned per-service field surfaces ───────────────────────────────
#
# Each entry: service name → frozenset of field names that MUST be
# accepted. Removing a field from any of these sets is a breaking
# change that needs to be intentional. Tests below assert both:
#   (a) services.yaml declares the field for the HA UI, AND
#   (b) the handler in __init__.py reads it via call.data.get(...)
# so we can't drift declaration vs. parsing.
#
# Adding new fields is fine — these are minimum guarantees, not
# exhaustive lists.

APPLY_OVERLAY_FIELDS = frozenset({
    "device_id",
    "subtitle_mode",
    "subtitle_text",
    "home_face",
    "home_message",
    "sensor_entity_id",
    "sensor_label",
    "sensor_quad_2_entity_id",
    "sensor_quad_2_label",
    "sensor_quad_3_entity_id",
    "sensor_quad_3_label",
    "sensor_quad_4_entity_id",
    "sensor_quad_4_label",
    "sensor_marquee",
    "marquee_position",
    "weather_entity_id",
    "framecast_frame_id",
    "brightness",
    "hide_label",
    "ttl_s",
})

MOUNT_FACE_FIELDS = frozenset({"device_id", "face_id", "payload", "retained"})

PUSH_THEME_FIELDS = frozenset({"device_id", "theme"})

SEND_ANNOUNCEMENT_FIELDS = frozenset({
    "device_id", "message", "from_name", "duration", "animation",
})

UPDATE_SENSOR_VALUE_FIELDS = frozenset({
    "device_id", "entity_id", "value", "label", "unit", "icon", "color",
})

SEND_INVITATION_FIELDS = frozenset({
    "device_id", "text", "subtitle", "invitation_id",
    "accept_label", "decline_label", "hold_seconds", "ttl_s",
    "priority", "theme_override", "solid_color", "from_name", "on_accept",
})

CANCEL_INVITATION_FIELDS = frozenset({"device_id", "invitation_id"})

# Services that MUST exist. The integration ships many; this is the
# subset whose absence would break documented user workflows.
REQUIRED_SERVICES = frozenset({
    "apply_overlay",
    "mount_face",
    "mount_perimeter_pulse",
    "push_theme",
    "reboot",
    "send_announcement",
    "send_countdown",
    "send_invitation",
    "cancel_invitation",
    "set_timezone",
    "update_now_playing",
    "update_sensor_value",
    "update_perimeter_state",
    "update_from_media_player",
})


class ServicesYamlContractTests(unittest.TestCase):
    """services.yaml is the HA UI contract — pin every published field."""

    @classmethod
    def setUpClass(cls):
        cls.services = _load_services()

    def test_required_services_present(self):
        actual = set(self.services)
        missing = REQUIRED_SERVICES - actual
        self.assertFalse(
            missing,
            f"services.yaml is missing required services: {sorted(missing)}",
        )

    def _assert_fields(self, service_name: str, required: frozenset[str]):
        self.assertIn(service_name, self.services, f"service '{service_name}' missing")
        declared = set((self.services[service_name].get("fields") or {}).keys())
        missing = required - declared
        self.assertFalse(
            missing,
            f"service '{service_name}' is missing fields: {sorted(missing)}. "
            f"Declared: {sorted(declared)}",
        )

    def test_apply_overlay_fields(self):
        self._assert_fields("apply_overlay", APPLY_OVERLAY_FIELDS)

    def test_mount_face_fields(self):
        self._assert_fields("mount_face", MOUNT_FACE_FIELDS)

    def test_push_theme_fields(self):
        self._assert_fields("push_theme", PUSH_THEME_FIELDS)

    def test_send_announcement_fields(self):
        self._assert_fields("send_announcement", SEND_ANNOUNCEMENT_FIELDS)

    def test_update_sensor_value_fields(self):
        self._assert_fields("update_sensor_value", UPDATE_SENSOR_VALUE_FIELDS)

    def test_send_invitation_fields(self):
        self._assert_fields("send_invitation", SEND_INVITATION_FIELDS)

    def test_cancel_invitation_fields(self):
        self._assert_fields("cancel_invitation", CANCEL_INVITATION_FIELDS)

    def test_apply_overlay_subtitle_mode_options(self):
        """The subtitle_mode selector must offer every supported mode.

        ``ical_next_event`` is server-materialized (Helm/Console resolve
        it to ``custom`` text) but the dial still accepts it via
        ``cmd/overlay``, so the HA picker must expose it. The handler
        passes the validated mode straight through; see
        _OVERLAY_SUBTITLE_MODES in __init__.py.
        """
        fields = self.services["apply_overlay"].get("fields") or {}
        options = (
            fields.get("subtitle_mode", {})
            .get("selector", {})
            .get("select", {})
            .get("options", [])
        )
        for mode in ("theme", "custom", "date", "date_year",
                     "ical_next_event", "none"):
            self.assertIn(
                mode, options,
                f"subtitle_mode selector is missing '{mode}': {options}",
            )


class HandlerReadsDeclaredFieldsTests(unittest.TestCase):
    """services.yaml declaring a field is necessary but not sufficient.

    The Python handler also needs to call ``call.data.get("field")``
    or the field is silently dropped at runtime — which is exactly the
    regression that motivated this test file. Grep the handler source
    for the field name to catch that.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = _load_init_text()

    def _assert_handler_reads(self, fields: frozenset[str]):
        """Every field name should appear at least once in the handler.

        Match shapes:
          1. Literal: ``call.data.get("foo")`` or ``"foo"`` in a list.
          2. F-string template: ``f"sensor_quad_{slot}_entity_id"`` —
             treat the literal-prefix + literal-suffix around ``{slot}``
             or ``{i}`` as a match for any ``sensor_quad_N_*`` field.
        """
        # Build f-string templates the handler is known to use.
        # If the handler grows new templates, add them here.
        fstring_templates = (
            ("sensor_quad_{slot}_entity_id", r"sensor_quad_\d+_entity_id"),
            ("sensor_quad_{slot}_label",     r"sensor_quad_\d+_label"),
        )

        def matched(field: str) -> bool:
            if f"'{field}'" in self.text or f'"{field}"' in self.text:
                return True
            for template_literal, field_pattern in fstring_templates:
                # The handler source must contain the template literal,
                # AND this field name must match the pattern the template
                # produces.
                if template_literal in self.text and re.fullmatch(field_pattern, field):
                    return True
            return False

        missing = sorted(f for f in fields if not matched(f))
        self.assertFalse(
            missing,
            f"handler doesn't reference these declared fields anywhere "
            f"in __init__.py: {missing}. Either the handler is silently "
            f"dropping them (regression!) or the literal string is "
            f"obscured behind a constant — in which case extend the "
            f"f-string template list in this test.",
        )

    def test_apply_overlay_handler_reads_all_fields(self):
        # device_id is read indirectly via _resolve_targets; skip it.
        # Same for the slot helpers that build field names from
        # _OVERLAY_QUAD_SLOTS — those literal strings still appear in
        # the slot-2/3/4 iteration so substring match catches them.
        skip = {"device_id"}
        self._assert_handler_reads(APPLY_OVERLAY_FIELDS - skip)

    def test_send_invitation_handler_reads_all_fields(self):
        # device_id is read via _resolve_targets; skip.
        skip = {"device_id"}
        self._assert_handler_reads(SEND_INVITATION_FIELDS - skip)

    def test_cancel_invitation_handler_reads_all_fields(self):
        skip = {"device_id"}
        self._assert_handler_reads(CANCEL_INVITATION_FIELDS - skip)

    def test_handler_accepts_ical_next_event_subtitle_mode(self):
        """The handler validates subtitle_mode against an allow-set.

        If ``ical_next_event`` isn't in _OVERLAY_SUBTITLE_MODES the
        handler raises before the value ever reaches cmd/overlay, so the
        services.yaml option would be silently rejected at runtime.
        """
        self.assertIn(
            '"ical_next_event"', self.text,
            "ical_next_event must appear in _OVERLAY_SUBTITLE_MODES so the "
            "apply_overlay handler accepts it instead of raising.",
        )


class SmokeYamlValidityTests(unittest.TestCase):
    """Catch the obvious — services.yaml + __init__.py parse cleanly."""

    def test_services_yaml_loads(self):
        d = _load_services()
        self.assertIsInstance(d, dict)
        self.assertGreater(len(d), 0)

    def test_init_py_parses(self):
        import ast
        ast.parse(_load_init_text())

    def test_manifest_version_format(self):
        import json
        manifest = json.loads(
            (ROOT / "custom_components" / "deckhand" / "manifest.json")
            .read_text(encoding="utf-8")
        )
        v = manifest.get("version", "")
        self.assertRegex(v, r"^\d+\.\d+\.\d+$", "manifest version not semver")


if __name__ == "__main__":
    unittest.main(verbosity=2)
