"""``async_step_mqtt`` must not do dict-style access on HA's discovery info.

The incident (2026-09-04)
------------------------
HA's MQTT discovery payload used to be a plain dict. It is now the
``MqttServiceInfo`` DATACLASS. ``config_flow.async_step_mqtt`` still called
``discovery_info.get("topic", "")``, which raises AttributeError against a
dataclass.

That is unusually expensive *here*. ``manifest.json`` subscribes this
integration to ``deckhand/+/dial/+/status``, so every status message from every
dial enters this flow, and HA builds a full traceback on MainThread for each
one. A normal fleet produced a rock-steady ~18 tracebacks per minute:

    16,325 occurrences in 16 hours — 65% of every line in the core log.

Why this test is source-level
-----------------------------
Same reason as ``test_sensor_watches_wiring.py`` and ``test_hacs_presence.py``:
``config_flow.py`` imports ``homeassistant.*`` at module scope and there is no
HA runtime in this environment, so the module cannot be imported to be called.
A source guard is what is available, and it is enough — the failure mode is a
specific *call shape*, not a subtle computation.

Run with:  python3 -m pytest tests/test_mqtt_discovery_info_access.py
       or:  python3 -m unittest tests.test_mqtt_discovery_info_access
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FLOW = ROOT / "custom_components" / "deckhand" / "config_flow.py"


def _async_step_mqtt_body(source: str) -> str:
    """The text of async_step_mqtt, up to the next def at the same indent."""
    start = source.index("async def async_step_mqtt")
    rest = source[start:]
    end = re.search(r"\n    (?:@|async def |def )", rest[1:])
    return rest[: end.start() + 1] if end else rest


class MqttDiscoveryInfoAccessTests(unittest.TestCase):
    def setUp(self):
        self.source = CONFIG_FLOW.read_text()
        self.body = _async_step_mqtt_body(self.source)

    def test_no_unguarded_dict_get_on_discovery_info(self):
        """The exact regression: .get() against a dataclass.

        A ``.get()`` is only safe once ``isinstance(discovery_info, dict)`` has
        established that it really is one — that branch is how the legacy
        pre-dataclass core is still supported. What must never come back is an
        *unguarded* call, which is the shape that raised on every message.
        """
        lines = self.body.splitlines()
        for i, line in enumerate(lines):
            if re.search(r"discovery_info\s*\.\s*get\s*\(", line):
                window = "\n".join(lines[max(0, i - 3) : i + 1])
                self.assertRegex(
                    window,
                    r"isinstance\(\s*discovery_info\s*,\s*dict\s*\)",
                    f"Unguarded discovery_info.get() at line {i + 1} of "
                    "async_step_mqtt. HA's MqttServiceInfo is a dataclass, so this "
                    "raises AttributeError for EVERY dial status message and HA "
                    "builds a full traceback on MainThread each time (16,325 in 16 "
                    "hours, 65% of the core log). Read it with getattr(), or guard "
                    "the call with isinstance(discovery_info, dict).",
                )

    def test_topic_is_read_as_an_attribute(self):
        """Positive control: asserting the absence of .get() is not enough.

        Without this, deleting the topic read entirely would pass the test above.
        """
        self.assertRegex(
            self.body,
            r"getattr\(\s*discovery_info\s*,\s*[\"']topic[\"']",
            "async_step_mqtt should read the topic via getattr(discovery_info, "
            "'topic', ...), which works against both the dataclass and the "
            "legacy dict the manifest's min HA version (2024.1.0) still had.",
        )

    def test_discovery_info_is_not_annotated_as_a_dict(self):
        """The stale annotation is what made the dict-style call look correct."""
        self.assertNotRegex(
            self.body,
            r"discovery_info\s*:\s*dict\[",
            "discovery_info is annotated as a dict. It is HA's MqttServiceInfo "
            "dataclass on current cores; the annotation invites another .get().",
        )

    def test_manifest_still_subscribes_the_flow_to_dial_status(self):
        """Pins WHY this matters, so the blast radius isn't lost.

        If this subscription ever goes away the cost of a mistake here drops
        enormously — and if it is still here, it must stay cheap.
        """
        manifest = (ROOT / "custom_components" / "deckhand" / "manifest.json").read_text()
        self.assertIn(
            "deckhand/+/dial/+/status",
            manifest,
            "The mqtt discovery subscription changed; revisit async_step_mqtt's "
            "cost assumptions, which are written for every dial status message "
            "entering the flow.",
        )


if __name__ == "__main__":
    unittest.main()
