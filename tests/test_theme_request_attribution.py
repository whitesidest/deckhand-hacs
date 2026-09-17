"""push_theme goes through Helm and says who asked (helm#521).

Until 1.15.2 only "random" went to Helm's theme_request topic; a concrete
slug was published straight to cmd/theme, which Helm never sees — so the
audit log had no row for it, and the "random" rows it did have read
"unattributed". Now every push without transient theme_options is a
theme_request carrying ``requested_by`` (the HA user's name, or
"automation"), and Helm files the audit row under that name.

Runs the REAL handler through test_service_schema's AST harness.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_PY = ROOT / "custom_components" / "deckhand" / "__init__.py"


class ThemeRequestAttributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_service_schema import QuietInvitationTests

        cls._harness = QuietInvitationTests("test_prompt_path_unchanged")
        cls._harness.src = INIT_PY.read_text(encoding="utf-8")

    def _push(self, **data):
        data.setdefault("device_id", ["d1"])
        return self._harness._run_handler("_push_theme", data)

    def test_a_concrete_slug_is_requested_through_helm_with_the_requester(self):
        pubs = self._push(theme="arcanum")
        self.assertEqual(len(pubs), 2, "one request per resolved dial")
        for topic, payload, _retain in pubs:
            self.assertRegex(topic, r"^deckhand/team-1/dial/DECK-[AB]{4}/theme_request$")
            self.assertEqual(payload, {"slug": "arcanum", "requested_by": "automation"})

    def test_random_still_goes_to_helm(self):
        pubs = self._push(theme="random")
        self.assertTrue(pubs)
        for topic, payload, _retain in pubs:
            self.assertTrue(topic.endswith("/theme_request"))
            self.assertEqual(payload["slug"], "random")
            self.assertEqual(payload["requested_by"], "automation")

    def test_transient_options_ride_the_helm_request(self):
        # helm#538: until 1.16.0 a push WITH options bypassed Helm and
        # published cmd/theme unretained, so the broker's retained slot
        # kept Helm's previous theme and every reconnect replayed it. Now
        # the options ride the theme_request as ``config`` and Helm merges
        # them transiently over the dial's stored config.
        pubs = self._push(theme="wedding", theme_options={"initials": "T+J", "_private": 1})
        self.assertEqual(len(pubs), 2, "one request per resolved dial")
        for topic, payload, _retain in pubs:
            self.assertTrue(topic.endswith("/theme_request"), topic)
            self.assertEqual(
                payload,
                {"slug": "wedding", "requested_by": "automation", "config": {"initials": "T+J"}},
            )

    def test_nothing_publishes_to_cmd_theme_directly(self):
        # The direct cmd/theme path is gone: with or without options every
        # push is a theme_request, so Helm owns the single retained slot.
        for data in ({"theme": "arcanum"}, {"theme": "wedding", "theme_options": {"initials": "X"}}):
            for topic, _payload, _retain in self._push(**data):
                self.assertFalse(topic.endswith("/cmd/theme"), topic)


if __name__ == "__main__":
    unittest.main()
