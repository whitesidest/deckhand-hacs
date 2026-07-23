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
    # weather_entity_id removed 2026-06-06 with the Weather face (all
    # surfaces); cmd/weather data plumbing survives for marquees only.
    # Pin updated 2026-07-17 — intentional removal, not surface-shrink.
    "framecast_frame_id",
    "brightness",
    "hide_label",
    "ttl_s",
})

MOUNT_FACE_FIELDS = frozenset({"device_id", "face_id", "payload", "retained"})

PUSH_THEME_FIELDS = frozenset({"device_id", "theme"})

SEND_ANNOUNCEMENT_FIELDS = frozenset({
    "device_id", "message", "from_name", "duration", "animation",
    # Doorbell / visitor image backdrop (LAN-direct cmd/image push).
    "snapshot_entity", "image_url",
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
    "set_sunset",
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

    def test_send_announcement_handler_reads_image_fields(self):
        """The doorbell fields must be read by the handler, not just
        declared in services.yaml — otherwise the image never streams."""
        self._assert_handler_reads(frozenset({"snapshot_entity", "image_url"}))

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




class FleetTargetingTests(unittest.TestCase):
    """Pin the fleet-wide targeting surface (2026-07-17).

    Every service resolves its device_id through _resolve_targets, which
    accepts a list (selectors are multiple: true) and the literal "all"
    sentinel for every discovered dial. Removing either is a breaking
    change for user automations that broadcast to the fleet.
    """

    def test_every_device_selector_allows_multiple(self):
        services = _load_services()
        missing = []
        for name, svc in services.items():
            dev = (svc.get("fields") or {}).get("device_id") or {}
            sel = (dev.get("selector") or {}).get("device")
            if sel is None:
                continue  # service without a deckhand device selector
            if sel.get("integration") == "deckhand" and not sel.get("multiple"):
                missing.append(name)
        self.assertFalse(
            missing,
            f"device selectors missing multiple: true: {missing}",
        )

    def test_resolver_supports_all_sentinel_and_lists(self):
        src = _load_init_text()
        # The central resolver must normalise list input...
        self.assertIn("isinstance(device_id, list)", src)
        # ...and honor the explicit "all" sentinel (never omitted-field).
        self.assertIn('== "all"', src)
        self.assertIn("def _resolve_all_dials", src)

    def test_all_sentinel_is_explicit_not_default(self):
        # Omitted device_id must resolve to NOTHING — forgetting the field
        # must never broadcast to the fleet.
        src = _load_init_text()
        m = re.search(
            r"def _resolve_targets\(.*?\n(.*?)\n    ids =",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "_resolve_targets body not found")
        self.assertIn("return []", m.group(1))


class TemporaryMenuItemTests(unittest.TestCase):
    """Pin the temporary-menu-item surface (2026-07-17, SF parity)."""

    REQUIRED_FIELDS = {
        "add_menu_item": frozenset({"device_id", "key", "label", "icon", "ttl", "item_type", "action_data"}),
        "remove_menu_item": frozenset({"device_id", "key"}),
    }

    def test_services_declare_fields(self):
        services = _load_services()
        for svc, fields in self.REQUIRED_FIELDS.items():
            self.assertIn(svc, services, f"{svc} missing from services.yaml")
            declared = set((services[svc].get("fields") or {}).keys())
            missing = fields - declared
            self.assertFalse(missing, f"{svc} missing fields: {sorted(missing)}")

    def test_handlers_read_fields(self):
        src = _load_init_text()
        for svc in self.REQUIRED_FIELDS:
            self.assertIn(f'"{svc}"', src, f"{svc} not registered")
        # add handler reads its optional knobs
        for field in ("key", "label", "icon", "ttl", "item_type", "action_data"):
            self.assertIn(f'call.data.get("{field}"', src.replace("call.data[\"item_type\"]", 'call.data.get("item_type"'),
                          f"add_menu_item handler doesn't read {field}")

    def test_menu_request_topic_shape(self):
        # Helm's listener subscribes deckhand/+/dial/+/menu_request — the
        # HACS topic template must match that shape exactly.
        from pathlib import Path
        const = (ROOT / "custom_components" / "deckhand" / "const.py").read_text()
        self.assertIn('TOPIC_MENU_REQUEST = "deckhand/{team_id}/dial/{dial_id}/menu_request"', const)


class AlarmServicesTests(unittest.TestCase):
    """Pin the alarm-service surface (2026-07-17, SF invocable parity)."""

    REQUIRED = {
        "create_alarm": frozenset({
            "device_id", "name", "time", "days", "one_time_date", "label",
            "snooze_minutes", "sunrise_duration_s", "sunrise_color", "enabled",
        }),
        "enable_alarm": frozenset({"device_id", "name"}),
        "disable_alarm": frozenset({"device_id", "name"}),
        "snooze_alarm": frozenset({"device_id"}),
        "dismiss_alarm": frozenset({"device_id"}),
    }

    def test_services_declare_fields(self):
        services = _load_services()
        for svc, fields in self.REQUIRED.items():
            self.assertIn(svc, services, f"{svc} missing")
            declared = set((services[svc].get("fields") or {}).keys())
            missing = fields - declared
            self.assertFalse(missing, f"{svc} missing fields: {sorted(missing)}")

    def test_handlers_registered(self):
        src = _load_init_text()
        for svc in self.REQUIRED:
            self.assertIn(f'"{svc}"', src, f"{svc} not registered")

    def test_alarm_request_topic_shape(self):
        const = (ROOT / "custom_components" / "deckhand" / "const.py").read_text()
        self.assertIn('TOPIC_ALARM_REQUEST = "deckhand/{team_id}/dial/{dial_id}/alarm_request"', const)


class CredentialServicesTests(unittest.TestCase):
    """Pin the credential-service surface (2026-07-17, SF parity)."""

    REQUIRED = {
        "enroll_credential": frozenset({"device_id", "identity_name", "role", "label", "uid_hash"}),
        "revoke_credential": frozenset({"device_id", "identity_name"}),
        "restore_credential": frozenset({"device_id", "identity_name"}),
    }

    def test_services_declare_fields(self):
        services = _load_services()
        for svc, fields in self.REQUIRED.items():
            self.assertIn(svc, services, f"{svc} missing")
            declared = set((services[svc].get("fields") or {}).keys())
            self.assertFalse(fields - declared, f"{svc} missing fields")

    def test_handlers_registered(self):
        src = _load_init_text()
        for svc in self.REQUIRED:
            self.assertIn(f'"{svc}"', src, f"{svc} not registered")

    def test_topic_shape(self):
        const = (ROOT / "custom_components" / "deckhand" / "const.py").read_text()
        self.assertIn('TOPIC_CREDENTIAL_REQUEST = "deckhand/{team_id}/dial/{dial_id}/credential_request"', const)



class ScheduleAndMenuPushTests(unittest.TestCase):
    """Pin schedules + push_menu surface (2026-07-17, SF parity complete)."""

    REQUIRED = {
        "create_schedule": frozenset({
            "device_id", "name", "trigger_type", "time", "days", "recurrence",
            "one_time_date", "interval_hours", "minute_offset", "hour_start",
            "hour_end", "webhook_slug", "theme", "menu_profile",
            "announcement_message", "announcement_from",
            "announcement_duration_s", "announcement_animation",
            "target_scope", "enabled",
        }),
        "enable_schedule": frozenset({"device_id", "name"}),
        "disable_schedule": frozenset({"device_id", "name"}),
        "fire_schedule": frozenset({"device_id", "name"}),
        "push_menu": frozenset({"device_id", "profile"}),
    }

    def test_services_declare_fields(self):
        services = _load_services()
        for svc, fields in self.REQUIRED.items():
            self.assertIn(svc, services, f"{svc} missing")
            declared = set((services[svc].get("fields") or {}).keys())
            self.assertFalse(fields - declared, f"{svc} missing fields: {sorted(fields - declared)}")

    def test_handlers_registered(self):
        src = _load_init_text()
        for svc in self.REQUIRED:
            self.assertIn(f'"{svc}"', src, f"{svc} not registered")

    def test_schedule_topic_shape(self):
        const = (ROOT / "custom_components" / "deckhand" / "const.py").read_text()
        self.assertIn('TOPIC_SCHEDULE_REQUEST = "deckhand/{team_id}/dial/{dial_id}/schedule_request"', const)


class SetSunsetServiceTests(unittest.TestCase):
    """Pin the Sundowner sunset-push surface (2026-07-22)."""

    REQUIRED_FIELDS = frozenset({
        "device_id", "entity_id", "attribute", "sunset_at",
    })

    def test_service_declares_fields(self):
        services = _load_services()
        self.assertIn("set_sunset", services, "set_sunset missing from services.yaml")
        declared = set((services["set_sunset"].get("fields") or {}).keys())
        missing = self.REQUIRED_FIELDS - declared
        self.assertFalse(missing, f"set_sunset missing fields: {sorted(missing)}")

    def test_handler_registered_and_reads_fields(self):
        src = _load_init_text()
        self.assertIn('"set_sunset"', src, "set_sunset not registered")
        # Handler must actually read each knob or it's silently dropped.
        for field in ("sunset_at", "entity_id", "attribute"):
            self.assertIn(
                f'call.data.get("{field}")', src,
                f"set_sunset handler doesn't read {field}",
            )

    def test_topic_shape(self):
        const = (ROOT / "custom_components" / "deckhand" / "const.py").read_text()
        self.assertIn(
            'TOPIC_CMD_SUNSET = "deckhand/{team_id}/dial/{dial_id}/cmd/sunset"',
            const,
        )

    def test_publishes_epoch_key(self):
        # The firmware INBOUND_SUNSET handler keys on {"epoch": N}; make
        # sure the handler emits that exact key (not just sunset_at).
        src = _load_init_text()
        self.assertIn('json.dumps({"epoch": epoch})', src)


class EpochCoercionTests(unittest.TestCase):
    """Unit-test the epoch conversion helper directly.

    ``__init__.py`` can't be imported standalone (it uses package-
    relative imports and pulls the whole homeassistant surface at load).
    Rather than stub all of that, we lift the ``_coerce_epoch`` function
    source out of the file via AST and exec just it — it depends only on
    ``datetime`` and ``Any``. This keeps the actual shipped logic under
    test without a running HA, and fails loudly if the function is
    renamed or deleted.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        from datetime import datetime, timezone
        from typing import Any

        cls.datetime = datetime
        cls.timezone = timezone

        tree = ast.parse(_load_init_text())
        fn = next(
            (
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_coerce_epoch"
            ),
            None,
        )
        assert fn is not None, "_coerce_epoch not found in __init__.py"
        ns: dict = {"datetime": datetime, "Any": Any}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), INIT_PY.name, "exec"), ns)
        cls._coerce_epoch = staticmethod(ns["_coerce_epoch"])

    def test_coerce_epoch_variants(self):
        f = self._coerce_epoch
        # Bare epoch int and its string form.
        self.assertEqual(f(1_753_000_000), 1_753_000_000)
        self.assertEqual(f("1753000000"), 1_753_000_000)
        self.assertEqual(f(1_753_000_000.9), 1_753_000_000)
        # tz-aware ISO — the sun.sun shape — must be exact UTC epoch.
        dt = self.datetime(2026, 7, 22, 20, 14, tzinfo=self.timezone.utc)
        self.assertEqual(f("2026-07-22T20:14:00+00:00"), int(dt.timestamp()))
        # Trailing Z tolerated.
        self.assertEqual(f("2026-07-22T20:14:00Z"), int(dt.timestamp()))
        # A real datetime instance.
        self.assertEqual(f(dt), int(dt.timestamp()))
        # Junk / empty / bool → None.
        self.assertIsNone(f(""))
        self.assertIsNone(f("not-a-time"))
        self.assertIsNone(f(None))
        self.assertIsNone(f(True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
