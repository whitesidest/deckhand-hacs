"""services.yaml is complete enough to use from the UI (1.24.0).

The service editor shows a field's ``name`` and ``description`` and nothing
else, so a field without them is a guess for whoever builds the automation.
These tests pin:

* every service has a name and a description, and every field a name, a
  description and a selector;
* services.yaml and the handlers registered in ``__init__.py`` list the same
  services;
* ``create_schedule.announcement_animation`` offers exactly the choices
  ``send_announcement.animation`` does;
* the readout entity pickers only offer entities whose state reads as a
  value, and the sun picker only sun / timestamp entities;
* ``update_sensor_value.icon`` is gone from the UI but still accepted by
  the handler, so an automation that passes it keeps working;
* strings.json and translations/en.json stay identical.

Run with:  python3 -m pytest tests/test_services_yaml_lint.py
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from test_emoji_strip_every_service import _ServiceTest

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
SERVICES = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))

READOUT_DOMAINS = {"sensor", "binary_sensor", "number", "input_number"}


def _selector(service: str, field: str) -> dict:
    return SERVICES[service]["fields"][field]["selector"]


class EveryServiceIsDescribed(unittest.TestCase):
    def test_every_service_has_a_name_and_description(self):
        for name, svc in SERVICES.items():
            with self.subTest(service=name):
                self.assertTrue(str(svc.get("name") or "").strip(), f"{name}: no name")
                self.assertTrue(str(svc.get("description") or "").strip(), f"{name}: no description")

    def test_every_field_has_a_name_description_and_selector(self):
        for name, svc in SERVICES.items():
            for field, spec in (svc.get("fields") or {}).items():
                with self.subTest(service=name, field=field):
                    self.assertTrue(str(spec.get("name") or "").strip(), f"{name}.{field}: no name")
                    self.assertTrue(
                        str(spec.get("description") or "").strip(),
                        f"{name}.{field}: no description",
                    )
                    self.assertIsInstance(spec.get("selector"), dict, f"{name}.{field}: no selector")



class SelectorsMatchTheHandlers(unittest.TestCase):
    def test_schedule_announcement_animation_matches_send_announcement(self):
        sched = _selector("create_schedule", "announcement_animation")["select"]["options"]
        announce = _selector("send_announcement", "animation")["select"]["options"]
        self.assertEqual(sched, announce)

    def test_readout_pickers_offer_value_entities_only(self):
        # The readout shows the entity's STATE, so a climate entity would
        # read "heat", not a temperature: climate is left out on purpose.
        pickers = [
            ("apply_overlay", "sensor_entity_id"),
            ("apply_overlay", "sensor_quad_2_entity_id"),
            ("apply_overlay", "sensor_quad_3_entity_id"),
            ("apply_overlay", "sensor_quad_4_entity_id"),
            ("update_sensor_value", "entity_id"),
        ]
        for service, field in pickers:
            with self.subTest(service=service, field=field):
                domains = _selector(service, field)["entity"]["domain"]
                self.assertEqual(set(domains), READOUT_DOMAINS)

    def test_sunset_picker_offers_timestamp_entities(self):
        domains = _selector("set_sunset", "entity_id")["entity"]["domain"]
        self.assertEqual(set(domains), {"sun", "sensor", "input_datetime"})

    def test_colour_fields_stay_text(self):
        # The handlers put the value on the wire as a "#RRGGBB" string; a
        # color_rgb selector would hand them an [r, g, b] list instead.
        for service, field in (
            ("start_timer", "color"),
            ("update_sensor_value", "color"),
            ("create_alarm", "sunrise_color"),
            ("send_invitation", "solid_color"),
        ):
            with self.subTest(service=service, field=field):
                self.assertIn("text", _selector(service, field))
                self.assertIn("#RRGGBB", SERVICES[service]["fields"][field]["description"])

    def test_theme_fields_point_at_the_theme_select(self):
        for service, field in (
            ("push_theme", "theme"),
            ("send_countdown", "celebration_theme"),
            ("create_schedule", "theme"),
            ("send_invitation", "theme_override"),
        ):
            with self.subTest(service=service, field=field):
                self.assertIn("text", _selector(service, field))
                self.assertIn("Theme", SERVICES[service]["fields"][field]["description"])

    def test_sensor_value_icon_is_gone_from_the_ui(self):
        self.assertNotIn("icon", SERVICES["update_sensor_value"]["fields"])


class YamlMatchesTheRegisteredHandlers(_ServiceTest):
    def test_same_services_both_sides(self):
        # _ServiceTest.setUp ran the real _register_services.
        self.assertEqual(set(SERVICES), set(self.hass.services.handlers))


class SensorValueIconStillAccepted(_ServiceTest):
    def test_an_automation_passing_icon_still_works(self):
        p = self.one(
            "update_sensor_value", "/cmd/sensor_value",
            entity_id="sensor.pool", value="27.5", icon="fa-thermometer-half",
        )
        self.assertEqual(p["value"], "27.5")


class ScheduleAnimationPassesThrough(_ServiceTest):
    def test_a_free_text_animation_still_reaches_the_server(self):
        # The selector is a dropdown now; YAML may still carry any string.
        sent = self.call(
            "create_schedule", name="Evening", announcement_message="Hi",
            announcement_animation="custom_thing",
        )
        self.assertEqual(sent[0][1]["announcement_animation"], "custom_thing")


class TranslationsInSync(unittest.TestCase):
    def test_strings_and_english_are_identical(self):
        strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
        english = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
        self.assertEqual(strings, english)


if __name__ == "__main__":
    unittest.main()
