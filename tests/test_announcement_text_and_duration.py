"""send_announcement: emoji are stripped, and no duration means until touched.

Two founder requests, 2026-09-10, both landing in the same handler:

* A 💧 in a Home Assistant announcement reached the dial as a tofu box. HACS
  publishes cmd/announce straight to the broker, so Helm's publisher-side
  strip never sees it; the same rule has to live here.
* "If someone sends an Announcement without a TTL, have it stay up until touch
  dismissed." The dial already holds a duration_s of 0 until touched (its
  auto-dismiss only runs when the duration is positive). HACS was inventing a
  30-second timeout nobody asked for — in the handler AND in the UI form.

Runs the REAL handler through test_service_schema's AST harness against a
recording MQTT stub, so these pin the payload that actually goes on the wire.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deckhand"
INIT_PY = COMPONENT / "__init__.py"

_spec = importlib.util.spec_from_file_location("dial_text", COMPONENT / "dial_text.py")
dial_text = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dial_text)
strip = dial_text.strip_emoji_for_dial


class _Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_service_schema import QuietInvitationTests

        cls._harness = QuietInvitationTests("test_prompt_path_unchanged")
        cls._harness.src = INIT_PY.read_text(encoding="utf-8")

    def _announce(self, **data):
        data.setdefault("device_id", ["d1"])
        pubs = self._harness._run_handler("_send_announcement", data)
        self.assertTrue(pubs, "nothing was published")
        return pubs[0][1]


class DurationTests(_Harness):
    def test_no_duration_means_until_touched(self):
        self.assertEqual(self._announce(message="Leak in the laundry")["duration_s"], 0)

    def test_a_blank_templated_duration_is_treated_as_none(self):
        """`duration: "{{ use_duration }}"` with use_duration unset renders
        "" — "Alert - Deckhand Announce" does exactly this. int("") would
        fail the whole service call."""
        self.assertEqual(self._announce(message="Alert", duration="")["duration_s"], 0)
        self.assertEqual(self._announce(message="Alert", duration="  ")["duration_s"], 0)

    def test_an_explicit_duration_is_kept(self):
        """Every existing automation passes one (the UI used to pre-fill 30),
        so none of them change behaviour."""
        self.assertEqual(self._announce(message="Doorbell", duration=30)["duration_s"], 30)
        self.assertEqual(self._announce(message="Doorbell", duration="45")["duration_s"], 45)

    def test_the_form_no_longer_prefills_a_timeout(self):
        """Without this, every announcement built in the HA UI would still
        get 30s written into the automation, and the change would only reach
        YAML authors."""
        services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
        field = services["send_announcement"]["fields"]["duration"]
        self.assertNotIn("default", field)
        self.assertIn("until someone touches", field["description"])


class EmojiTests(_Harness):
    def test_the_reported_droplet_is_stripped(self):
        self.assertEqual(self._announce(message="Water leak 💧")["message"], "Water leak")

    def test_the_sender_name_is_stripped_too(self):
        payload = self._announce(message="Leak", from_name="Home 🏠 Assistant")
        self.assertEqual(payload["from"], "Home Assistant")

    def test_text_the_dial_can_draw_is_left_alone(self):
        text = "Café — Привет, こんにちは … “ready”"
        self.assertEqual(self._announce(message=text)["message"], text)

    def test_an_emoji_only_message_is_refused_rather_than_sent_blank(self):
        """The existing empty-message check runs on the stripped text, so a
        message the dial could not have shown fails visibly in the automation."""
        with self.assertRaises(Exception):
            self._announce(message="💧💧")


class StripRuleTests(unittest.TestCase):
    """The rule itself, and parity with Helm's copy."""

    def test_sequences_go_entirely_and_keycaps_keep_their_digit(self):
        self.assertEqual(strip("🇺🇸 👨‍👩‍👧 👍🏽 ❤️ Table 1️⃣"), "Table 1")

    def test_line_breaks_survive(self):
        self.assertEqual(strip("Doorbell 🔔 \n  Front 🚪 door"), "Doorbell\nFront door")

    def test_non_strings_are_left_for_validation(self):
        self.assertEqual(strip(5), 5)
        self.assertEqual(strip(None), "")

    def test_ranges_match_helm(self):
        """HACS and Helm must strip the same set, or an announcement looks
        different depending on which side sent it. Skips when the Helm
        sibling is not checked out."""
        helm = ROOT.parent / "helm" / "apps" / "utils" / "dial_units.py"
        if not helm.exists():
            self.skipTest("helm sibling not present")
        spec = importlib.util.spec_from_file_location("dial_units", helm)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(tuple(mod._EMOJI_RANGES), tuple(dial_text._EMOJI_RANGES))


if __name__ == "__main__":
    unittest.main()
