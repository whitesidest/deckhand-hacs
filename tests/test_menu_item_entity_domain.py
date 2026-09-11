"""add_menu_item refuses an item whose entity its type cannot act on (helm#459).

Helm refuses a contextual item whose entity is outside its type's domains
(today: an Art Gallery ``ha_art`` item on anything but a ``media_player``,
helm#450). ``menu_request`` carries no reply, so that refusal was a WARNING in
Helm's log while Home Assistant reported success and no item appeared. HACS
now applies the same rule before publishing and raises a translated
ServiceValidationError, which Home Assistant shows.

The handler tests run the REAL ``_add_menu_item`` through the harness in
test_emoji_strip_every_service.py, with an error class that records the
translation key and placeholders. Each refusal is paired with the nearest
accepted call, so a check that refused everything (or nothing) fails.

The parity tests read Helm's table from the helm checkout beside this repo
(or ``DECKHAND_HELM_DIR``) and skip when there is none, so the two copies
cannot drift.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import test_emoji_strip_every_service as harness

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
RULES = sys.modules[f"{harness.PKG}.menu_item_types"]  # the copy the handler uses


class _RecordingSVE(Exception):
    def __init__(self, *args, translation_domain=None, translation_key=None,
                 translation_placeholders=None):
        super().__init__(*args or (translation_key,))
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders or {}


class AddMenuItemEntityDomain(harness._ServiceTest):
    def setUp(self):
        super().setUp()  # its tearDown restores the real error class
        harness.MOD.ServiceValidationError = _RecordingSVE

    def add(self, **data):
        data.setdefault("key", "gallery")
        data.setdefault("label", "Art Gallery")
        return self.call("add_menu_item", **data)

    def refused(self, **data):
        with self.assertRaises(_RecordingSVE) as ctx:
            self.add(**data)
        self.assertEqual(self.mqtt.sent, [], "a refused add must publish to no dial")
        return ctx.exception

    def published(self, **data):
        sent = [p for t, p in self.add(**data) if t.endswith("/menu_request")]
        self.assertEqual(len(sent), len(harness.TARGETS), f"expected one menu_request per dial, got {sent}")
        return sent[0]

    def test_an_art_item_on_a_remote_is_refused_with_the_art_message(self):
        err = self.refused(item_type="ha_art", action_data={"entity_id": "remote.frame_tv"})
        self.assertEqual(err.translation_domain, "deckhand")
        self.assertEqual(err.translation_key, "art_entity_not_media_player")
        self.assertEqual(err.translation_placeholders["entity_id"], "remote.frame_tv")
        self.assertEqual(err.translation_placeholders["domains"], "media_player")

    def test_negative_control_an_art_item_on_a_media_player_is_published_unchanged(self):
        p = self.published(item_type="ha_art", action_data={"entity_id": "media_player.frame_tv"})
        self.assertEqual(p["item_type"], "ha_art")
        self.assertEqual(p["action_data"], {"entity_id": "media_player.frame_tv"})

    def test_an_art_item_with_no_entity_is_published(self):
        """A FrameCast-driven gallery names no entity; Helm accepts it."""
        self.assertEqual(self.published(item_type="ha_art")["item_type"], "ha_art")
        self.assertEqual(
            self.published(item_type="ha_art", action_data={"entity_id": ""})["action_data"],
            {"entity_id": ""},
        )

    def test_the_domain_is_a_prefix_with_its_dot_not_a_substring(self):
        for entity in ("media_players.tv", "media_playerx.tv", "remote.media_player", "media_player"):
            with self.subTest(entity=entity):
                self.refused(item_type="ha_art", action_data={"entity_id": entity})

    def test_whitespace_is_trimmed_as_helm_trims_it(self):
        """Helm strips the item type and the entity before checking, so a
        padded remote is still a remote and a padded media player is fine."""
        err = self.refused(item_type=" ha_art ", action_data={"entity_id": "  remote.frame_tv "})
        self.assertEqual(err.translation_placeholders["entity_id"], "remote.frame_tv")
        p = self.published(item_type=" ha_art ", action_data={"entity_id": " media_player.frame_tv"})
        self.assertEqual(p["action_data"]["entity_id"], " media_player.frame_tv", "sent as given")

    def test_types_without_a_rule_take_any_entity(self):
        self.assertEqual(
            self.published(item_type="ha_toggle", action_data={"entity_id": "remote.frame_tv"})["item_type"],
            "ha_toggle",
        )
        # No item_type: Helm makes it an ha_service, which has no rule.
        self.assertNotIn("item_type", self.published(action_data={"entity_id": "remote.frame_tv"}))

    def test_the_climate_range_fields_do_not_mask_the_entity(self):
        """The first-class climate fields merge into action_data too; the
        check still sees the entity that goes on the wire."""
        self.refused(item_type="ha_art", action_data={"entity_id": "remote.frame_tv"}, climate_min_temp=65)

    def test_a_future_rule_gets_the_generic_message(self):
        """Helm's table may grow a row; the fallback key names the type and
        the domains, as Helm's fallback message does."""
        with mock.patch.dict(RULES.ITEM_TYPE_ENTITY_DOMAINS, {"ha_climate": ("climate", "water_heater")}):
            err = self.refused(item_type="ha_climate", action_data={"entity_id": "sensor.pool"})
            self.published(item_type="ha_climate", action_data={"entity_id": "water_heater.tank"})
        self.assertEqual(err.translation_key, "menu_item_entity_wrong_domain")
        self.assertEqual(
            err.translation_placeholders,
            {"entity_id": "sensor.pool", "item_type": "ha_climate", "domains": "climate, water_heater"},
        )

    def test_both_messages_exist_and_name_only_the_placeholders_passed(self):
        passed = {"entity_id", "item_type", "domains"}
        for name in ("strings.json", "translations/en.json"):
            exceptions = json.loads((COMPONENT / name).read_text())["exceptions"]
            for key in ("art_entity_not_media_player", "menu_item_entity_wrong_domain"):
                with self.subTest(file=name, key=key):
                    message = exceptions[key]["message"]
                    named = set(re.findall(r"{(\w+)}", message))
                    self.assertIn("entity_id", named)
                    self.assertLessEqual(named, passed)
        art = json.loads((COMPONENT / "strings.json").read_text())["exceptions"]["art_entity_not_media_player"]
        self.assertIn("is not a media player", art["message"])


def _helm_item_types() -> Path | None:
    base = Path(os.environ.get("DECKHAND_HELM_DIR") or ROOT.parent / "helm")
    path = base / "apps" / "menus" / "item_types.py"
    return path if path.exists() else None


class RuleMatchesHelm(unittest.TestCase):
    """HACS and Helm must refuse the same pairs. A pair only Helm refuses is
    helm#459 again (HA says success, nothing appears); one only HACS refuses
    blocks an item Helm would have taken."""

    def setUp(self):
        self.path = _helm_item_types()
        if self.path is None:
            self.skipTest("helm checkout not present (set DECKHAND_HELM_DIR to point at one)")
        self.source = self.path.read_text()
        if "def entity_domain_error" not in self.source:
            self.skipTest(f"{self.path} predates Helm's entity-domain rule (helm#438)")

    def test_the_tables_are_identical(self):
        tree = ast.parse(self.source)
        for node in tree.body:
            target = node.target if isinstance(node, ast.AnnAssign) else (
                node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
            if isinstance(target, ast.Name) and target.id == "ITEM_TYPE_ENTITY_DOMAINS":
                helm_table = ast.literal_eval(node.value)
                break
        else:
            self.fail(f"{self.path} has entity_domain_error but no ITEM_TYPE_ENTITY_DOMAINS literal; "
                      "the table moved or was renamed — update this test and menu_item_types.py")
        self.assertEqual(
            {k: tuple(v) for k, v in helm_table.items()},
            RULES.ITEM_TYPE_ENTITY_DOMAINS,
            "custom_components/deckhand/menu_item_types.py must mirror Helm's ITEM_TYPE_ENTITY_DOMAINS",
        )

    def test_the_checks_agree_on_every_input(self):
        """Runs Helm's own entity_domain_error with Django's gettext stubbed,
        so a change to how Helm matches (not just what) is caught too."""
        django = types.ModuleType("django")
        utils = types.ModuleType("django.utils")
        translation = types.ModuleType("django.utils.translation")
        translation.gettext_lazy = lambda s: s
        stubs = {"django": django, "django.utils": utils, "django.utils.translation": translation}
        spec = importlib.util.spec_from_file_location("helm_item_types_under_test", self.path)
        helm = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, stubs):
            try:
                spec.loader.exec_module(helm)
            except ImportError as exc:
                self.skipTest(f"Helm's item_types.py needs more than gettext to import now: {exc}")

        item_types = sorted(set(helm.ITEM_TYPE_ENTITY_DOMAINS) | set(RULES.ITEM_TYPE_ENTITY_DOMAINS)) + [
            "ha_service", "ha_toggle", "HA_ART", "", None,
        ]
        entities = [None, "", "   ", "media_player.frame_tv", " media_player.frame_tv ", "remote.frame_tv",
                    "media_player", "media_players.tv", "remote.media_player", "MEDIA_PLAYER.tv",
                    "climate.salon", "sensor.pool", 0, ["media_player.frame_tv"]]
        for domains in helm.ITEM_TYPE_ENTITY_DOMAINS.values():
            entities += [f"{d}.x" for d in domains]
        for item_type in item_types:
            for entity in entities:
                with self.subTest(item_type=item_type, entity=entity):
                    helm_refuses = helm.entity_domain_error(item_type, entity) is not None
                    hacs_refuses = RULES.disallowed_entity_domains(item_type, entity) is not None
                    self.assertEqual(hacs_refuses, helm_refuses)
        # Positive control: the comparison above saw both outcomes.
        self.assertIsNotNone(helm.entity_domain_error("ha_art", "remote.frame_tv"))
        self.assertIsNone(helm.entity_domain_error("ha_art", "media_player.frame_tv"))


if __name__ == "__main__":
    unittest.main()
