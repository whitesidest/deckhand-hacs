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

    def test_transient_options_keep_the_direct_cmd_theme_path(self):
        # theme_options are the one thing theme_request cannot carry, so
        # that push still goes straight to the dial (and stays unaudited —
        # documented in helm#521).
        pubs = self._push(theme="wedding", theme_options={"initials": "T+J"})
        self.assertTrue(pubs)
        for topic, payload, _retain in pubs:
            self.assertTrue(topic.endswith("/cmd/theme"))
            self.assertEqual(payload, {"id": "wedding", "config": {"initials": "T+J"}})


if __name__ == "__main__":
    unittest.main()
