"""send_announcement with a doorbell picture: convert once, announce everyone
first, then stream the picture to every dial at the same time.

Founder, 2026-10-28: the doorbell automation shows the visitor on every dial,
"however, it takes several seconds for the image to load." The handler used
to run, per dial and one dial after another: decode the snapshot, resize it,
pack 129,600 pixels in a Python loop, publish 53 messages. Only then did it
move on to the next dial's cmd/announce. The last dial in the house waited
for every earlier dial's conversion AND stream before its text even arrived.

Runs the REAL handler through test_service_schema's AST harness.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_PY = ROOT / "custom_components" / "deckhand" / "__init__.py"


class AnnouncementImageFanoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_service_schema import QuietInvitationTests

        cls._harness = QuietInvitationTests("test_prompt_path_unchanged")
        cls._harness.src = INIT_PY.read_text(encoding="utf-8")

    def _run(self, *, fetched=b"JPEG", prepare=None, push=None):
        """Run send_announcement with an image; returns (log, publishes).

        ``log`` interleaves ("announce", dial) and ("image", dial) in the
        order they happened, so ordering across dials is observable.
        """
        log: list[tuple[str, str]] = []
        prepared: list[bytes] = []

        async def _fetch(hass, snapshot_entity, image_url):
            return fetched

        def _prepare(image_bytes, variants):
            prepared.append(image_bytes)
            return {v: "PREPARED" for v in variants}

        async def _executor(func, *args):
            return func(*args)

        async def _push(hass, team_id, dial_id, image, source=None):
            self.assertEqual(image, "PREPARED")
            log.append(("image-start", dial_id))
            await asyncio.sleep(0)
            log.append(("image-done", dial_id))

        self._harness.harness_overrides = {
            "_fetch_announcement_image": _fetch,
            "prepare_variants": prepare or _prepare,
            "publish_prepared_image": push or _push,
            "async_add_executor_job": _executor,
        }
        try:
            pubs = self._harness._run_handler(
                "_send_announcement",
                {"device_id": ["room"], "message": "Someone is at the door",
                 "snapshot_entity": "camera.front_door"},
            )
        finally:
            self._harness.harness_overrides = None
        self.prepared = prepared
        return log, pubs

    def test_the_picture_is_converted_once_for_the_whole_room(self):
        self._run()
        self.assertEqual(self.prepared, [b"JPEG"])

    def test_every_dial_is_announced_before_any_picture_streams(self):
        log, pubs = self._run()
        announces = [p for p in pubs if p[0].endswith("/cmd/announce")]
        self.assertEqual(
            [t for t, _, _ in announces],
            [
                "deckhand/team-1/dial/DECK-AAAA/cmd/announce",
                "deckhand/team-1/dial/DECK-BBBB/cmd/announce",
            ],
        )
        self.assertTrue(all(body["image"] is True for _, body, _ in announces))
        # Both dials stream.
        self.assertEqual(
            sorted(d for kind, d in log if kind == "image-done"),
            ["DECK-AAAA", "DECK-BBBB"],
        )

    def test_the_dials_stream_concurrently_not_one_after_another(self):
        log, _ = self._run()
        # Concurrent: the second dial starts before the first has finished.
        self.assertEqual(
            [k for k, _ in log],
            ["image-start", "image-start", "image-done", "image-done"],
        )

    def test_one_dial_failing_does_not_stop_the_others(self):
        done: list[str] = []

        async def _flaky(hass, team_id, dial_id, image, source=None):
            if dial_id == "DECK-AAAA":
                raise RuntimeError("broker hiccup")
            done.append(dial_id)

        self._run(push=_flaky)
        self.assertEqual(done, ["DECK-BBBB"])

    def test_an_undecodable_picture_still_sends_the_text(self):
        def _bad(image_bytes, variants):
            raise OSError("cannot identify image file")

        log, pubs = self._run(prepare=_bad)
        self.assertEqual(len(pubs), 2)
        # No image flag, so the dials do not sit waiting for a stream.
        self.assertTrue(all("image" not in body for _, body, _ in pubs))
        self.assertEqual(log, [])

    def test_a_failed_snapshot_still_sends_the_text(self):
        log, pubs = self._run(fetched=None)
        self.assertEqual(len(pubs), 2)
        self.assertTrue(all("image" not in body for _, body, _ in pubs))
        self.assertEqual(self.prepared, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
