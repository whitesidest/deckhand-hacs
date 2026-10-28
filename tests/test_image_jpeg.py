"""JPEG image push (1.23.0), gated on the dial's ``img_jpeg`` feature.

Firmware 0.4.220 (deckhand-firmware#234) decodes a baseline JPEG itself
when the cmd/image start says ``"fmt":"jpeg"``, so a doorbell picture is a
few chunks instead of 51. Older firmware has no idea: it would paint the
JPEG bytes as RGB565 pixel garbage. So:

  * only a dial whose status heartbeat lists ``img_jpeg`` in ``features``
    gets JPEG; everything else (no heartbeat, features missing, None, a
    string, junk entries) gets the raw RGB565 360 stream byte-for-byte;
  * a JPEG-capable AMOLED (hardware_type ``amoled175``) gets its native
    466; raw never changes size;
  * each variant a send needs is built once, and none it doesn't need.

The handler-level tests run the REAL send_announcement through
test_service_schema's AST harness with the REAL ``image_variant_for`` and
``prepare_variants``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import types
import unittest
from pathlib import Path

from test_image_push import _FakeHass, _RecordingMqtt, image_push

ROOT = Path(__file__).resolve().parent.parent
INIT_PY = ROOT / "custom_components" / "deckhand" / "__init__.py"

RAW = ("raw", 360)
JPEG_360 = ("jpeg", 360)
JPEG_466 = ("jpeg", 466)


def _photo(w: int = 640, h: int = 480) -> bytes:
    """A noisy (so not trivially compressible) landscape JPEG."""
    from PIL import Image

    img = Image.effect_noise((w, h), 40).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _old_raw(source_bytes: bytes, size: int = 360) -> bytes:
    """The 1.22.3 conversion, copied verbatim: the pin for byte-identity."""
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(source_bytes))
    img.draft("RGB", (size, size))
    img = img.convert("RGB")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((size, size), Image.LANCZOS)
    a = np.asarray(img, dtype=np.uint16)
    packed = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
    return packed.astype("<u2").tobytes()


def _install_mqtt_stub() -> _RecordingMqtt:
    mqtt = _RecordingMqtt()
    comp = types.ModuleType("homeassistant.components")
    comp.__path__ = []
    mqtt_mod = types.ModuleType("homeassistant.components.mqtt")
    mqtt_mod.async_publish = mqtt.async_publish
    ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    ha.__path__ = []
    sys.modules["homeassistant.components"] = comp
    sys.modules["homeassistant.components.mqtt"] = mqtt_mod
    return mqtt


def _reassemble(image) -> bytes:
    return b"".join(base64.b64decode(c) for c in image.chunks)


class VariantChoiceTests(unittest.TestCase):
    def test_img_jpeg_feature_selects_jpeg_360(self):
        self.assertEqual(
            image_push.image_variant_for(
                {"features": ["nfc", "img_jpeg"], "hardware_type": "waveshare18"}
            ),
            JPEG_360,
        )

    def test_amoled_with_img_jpeg_gets_466(self):
        self.assertEqual(
            image_push.image_variant_for(
                {"features": ["img_jpeg"], "hardware_type": "amoled175"}
            ),
            JPEG_466,
        )

    def test_amoled_without_img_jpeg_stays_raw_360(self):
        self.assertEqual(
            image_push.image_variant_for({"hardware_type": "amoled175", "features": []}),
            RAW,
        )

    def test_missing_none_or_garbage_features_are_raw(self):
        for status in (
            None,
            {},
            "img_jpeg",
            ["img_jpeg"],
            {"features": None},
            {"features": "img_jpeg"},  # a string, not a list
            {"features": {"img_jpeg": True}},
            {"features": [1, None, {"img_jpeg": 1}, ["img_jpeg"]]},
            {"features": ["IMG_JPEG", "img_jpeg "]},
        ):
            with self.subTest(status=status):
                self.assertEqual(image_push.image_variant_for(status), RAW)

    def test_garbage_hardware_type_falls_back_to_360(self):
        self.assertEqual(
            image_push.image_variant_for({"features": ["img_jpeg"], "hardware_type": ["amoled175"]}),
            JPEG_360,
        )


@unittest.skipUnless(image_push._PIL_AVAILABLE, "Pillow not available")
class JpegEncodeTests(unittest.TestCase):
    def test_jpeg_variant_decodes_to_exact_size_and_is_baseline(self):
        from PIL import Image

        for variant in (JPEG_360, JPEG_466):
            with self.subTest(variant=variant):
                img = image_push.prepare_variants(_photo(), [variant])[variant]
                self.assertEqual(img.fmt, "jpeg")
                data = _reassemble(img)
                self.assertEqual(len(data), img.nbytes)
                self.assertTrue(data.startswith(b"\xff\xd8"))
                self.assertLessEqual(img.nbytes, 256 * 1024)
                decoded = Image.open(io.BytesIO(data))
                self.assertEqual(decoded.format, "JPEG")
                self.assertEqual(decoded.size, (variant[1], variant[1]))
                self.assertFalse(decoded.info.get("progressive"))
                self.assertFalse(decoded.info.get("progression"))
                # SOF0/SOF1 (baseline/extended sequential), never SOF2.
                self.assertNotIn(b"\xff\xc2", data[: data.find(b"\xff\xda")])
                self.assertEqual(len(img.chunks), -(-img.nbytes // 5120))

    def test_raw_variant_is_byte_identical_to_1_22_3(self):
        src = _photo()
        old = _old_raw(src)
        raw = image_push.prepare_variants(src, [RAW])[RAW]
        self.assertEqual(raw.fmt, "raw")
        self.assertEqual(raw.size, 360)
        self.assertEqual(_reassemble(raw), old)
        # Same when built alongside the JPEG variants (shared decode).
        mixed = image_push.prepare_variants(src, [JPEG_466, RAW, JPEG_360])
        self.assertEqual(_reassemble(mixed[RAW]), old)
        self.assertEqual(_reassemble(image_push.prepare_image(src)), old)

    def test_oversized_jpeg_falls_back_to_raw(self):
        saved = image_push.IMAGE_JPEG_MAX_BYTES
        image_push.IMAGE_JPEG_MAX_BYTES = 100
        try:
            out = image_push.prepare_variants(_photo(), [JPEG_466])
        finally:
            image_push.IMAGE_JPEG_MAX_BYTES = saved
        self.assertEqual(out[JPEG_466].fmt, "raw")
        self.assertEqual(out[JPEG_466].size, 360)
        self.assertIs(out[JPEG_466], out[RAW])


@unittest.skipUnless(image_push._PIL_AVAILABLE, "Pillow not available")
class BuildOnceTests(unittest.TestCase):
    """Each variant is built at most once per send, and only if needed."""

    def setUp(self):
        self.calls = {"square": [], "pack": 0, "jpeg": 0}
        self._saved = (image_push._square_image, image_push._pack_rgb565, image_push._encode_jpeg)
        sq, pk, jp = self._saved

        def _sq(b, size):
            self.calls["square"].append(size)
            return sq(b, size)

        def _pk(img, size):
            self.calls["pack"] += 1
            return pk(img, size)

        def _jp(img):
            self.calls["jpeg"] += 1
            return jp(img)

        image_push._square_image, image_push._pack_rgb565, image_push._encode_jpeg = _sq, _pk, _jp

    def tearDown(self):
        image_push._square_image, image_push._pack_rgb565, image_push._encode_jpeg = self._saved

    def test_all_old_dials_do_only_the_raw_work(self):
        image_push.prepare_variants(_photo(), [RAW, RAW, RAW])
        self.assertEqual(self.calls, {"square": [360], "pack": 1, "jpeg": 0})

    def test_mixed_house_builds_each_variant_once(self):
        out = image_push.prepare_variants(
            _photo(), [RAW, JPEG_466, JPEG_360, RAW, JPEG_466, JPEG_360]
        )
        self.assertEqual(set(out), {RAW, JPEG_360, JPEG_466})
        # raw 360 and JPEG 360 share one decode+resize.
        self.assertEqual(sorted(self.calls["square"]), [360, 466])
        self.assertEqual(self.calls["pack"], 1)
        self.assertEqual(self.calls["jpeg"], 2)

    def test_all_jpeg_dials_never_pack_raw(self):
        image_push.prepare_variants(_photo(), [JPEG_360, JPEG_360])
        self.assertEqual(self.calls, {"square": [360], "pack": 0, "jpeg": 1})


@unittest.skipUnless(image_push._PIL_AVAILABLE, "Pillow not available")
class PublishStartTests(unittest.TestCase):
    def setUp(self):
        self.mqtt = _install_mqtt_stub()

    def _publish(self, image, source=None):
        asyncio.run(
            image_push.publish_prepared_image(_FakeHass(), "t", "d", image, source=source)
        )
        return [json.loads(p) for _, p in self.mqtt.published]

    def test_jpeg_start_carries_fmt_size_and_src(self):
        img = image_push.prepare_variants(_photo(), [JPEG_466])[JPEG_466]
        src = {"ch": "home_assistant", "by": "Tyler"}
        msgs = self._publish(img, src)
        start = msgs[0]
        self.assertEqual(
            start,
            {"action": "start", "w": 466, "h": 466, "size": img.nbytes,
             "chunks": len(img.chunks), "fmt": "jpeg", "_src": src},
        )
        chunks = msgs[1:-1]
        self.assertEqual([c["i"] for c in chunks], list(range(len(img.chunks))))
        self.assertTrue(all("_src" not in c for c in msgs[1:]))
        self.assertEqual(msgs[-1], {"action": "done"})
        self.assertEqual(
            b"".join(base64.b64decode(c["d"]) for c in chunks), _reassemble(img)
        )
        for _, payload in self.mqtt.published:
            self.assertLess(len(payload.encode("utf-8")), 8192)

    def test_raw_start_has_the_historical_shape(self):
        img = image_push.prepare_variants(_photo(), [RAW])[RAW]
        start = self._publish(img)[0]
        self.assertEqual(
            start,
            {"action": "start", "w": 360, "h": 360, "size": 259200, "chunks": 51},
        )


@unittest.skipUnless(image_push._PIL_AVAILABLE, "Pillow not available")
class AnnouncementVariantTests(unittest.TestCase):
    """The real send_announcement routes each dial its own variant."""

    @classmethod
    def setUpClass(cls):
        from test_service_schema import QuietInvitationTests

        cls._harness = QuietInvitationTests("test_prompt_path_unchanged")
        cls._harness.src = INIT_PY.read_text(encoding="utf-8")

    def _run(self, heartbeats: dict[str, object]):
        targets = [(d, "team-1") for d in heartbeats]
        got: dict[str, object] = {}
        built: list[set] = []
        src_bytes = _photo()

        def _prepare(image_bytes, variants):
            built.append(set(variants))
            return image_push.prepare_variants(image_bytes, variants)

        async def _fetch(hass, snapshot_entity, image_url):
            return src_bytes

        async def _executor(func, *args):
            return func(*args)

        async def _push(hass, team_id, dial_id, image, source=None):
            got[dial_id] = image

        self._harness.harness_overrides = {
            "_resolve_targets": lambda hass, device_id: list(targets),
            "_dial_heartbeat": lambda hass, dial_id, team_id: heartbeats[dial_id],
            "image_variant_for": image_push.image_variant_for,
            "_fetch_announcement_image": _fetch,
            "prepare_variants": _prepare,
            "publish_prepared_image": _push,
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
        return got, built, pubs, src_bytes

    def test_mixed_house_each_dial_gets_its_variant_built_once(self):
        got, built, pubs, src = self._run({
            "DECK-OLD1": {"hardware_type": "waveshare18", "fw_ver": "0.4.219"},
            "DECK-OLD2": {"features": None, "hardware_type": "amoled175"},
            "DECK-NEW1": {"features": ["img_jpeg"], "hardware_type": "waveshare18"},
            "DECK-AMO1": {"features": ["img_jpeg"], "hardware_type": "amoled175"},
            "DECK-AMO2": {"features": ["img_jpeg"], "hardware_type": "amoled175"},
            "DECK-GONE": None,
        })
        self.assertEqual(built, [{RAW, JPEG_360, JPEG_466}])
        self.assertEqual(len(pubs), 6)
        self.assertTrue(all(body["image"] is True for _, body, _ in pubs))
        for dial in ("DECK-OLD1", "DECK-OLD2", "DECK-GONE"):
            self.assertEqual((got[dial].fmt, got[dial].size), RAW)
            self.assertEqual(_reassemble(got[dial]), _old_raw(src))
        self.assertEqual((got["DECK-NEW1"].fmt, got["DECK-NEW1"].size), JPEG_360)
        self.assertEqual((got["DECK-AMO1"].fmt, got["DECK-AMO1"].size), JPEG_466)
        # Shared, not rebuilt per dial.
        self.assertIs(got["DECK-AMO1"], got["DECK-AMO2"])
        self.assertIs(got["DECK-OLD1"], got["DECK-GONE"])

    def test_house_of_old_dials_builds_only_raw(self):
        got, built, _, src = self._run({
            "DECK-OLD1": {"hardware_type": "waveshare18"},
            "DECK-OLD2": {"features": ["nfc"], "hardware_type": "amoled175"},
        })
        self.assertEqual(built, [{RAW}])
        for img in got.values():
            self.assertEqual(_reassemble(img), _old_raw(src))


class DialHeartbeatLookupTests(unittest.TestCase):
    """``_dial_heartbeat`` reads the status payload ``_handle_status`` cached."""

    @classmethod
    def setUpClass(cls):
        import ast
        from types import SimpleNamespace

        tree = ast.parse(INIT_PY.read_text(encoding="utf-8"))
        fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_dial_heartbeat"
        )
        ns = {"DOMAIN": "deckhand", "HomeAssistant": object, "Any": object}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "x", "exec"), ns)
        cls.lookup = staticmethod(ns["_dial_heartbeat"])
        status = {"features": ["img_jpeg"], "hardware_type": "amoled175"}
        cls.status = status
        cls.hass = SimpleNamespace(data={"deckhand": {
            "entry-a": {"team_id": "team-1", "dials": {"DECK-AAAA": status}},
            "entry-b": {"team_id": "team-2", "dials": {"DECK-BBBB": "junk"}},
            "not-a-store": object(),
        }})

    def test_finds_the_dial_in_its_team(self):
        self.assertIs(self.lookup(self.hass, "DECK-AAAA", "team-1"), self.status)

    def test_wrong_team_unknown_dial_or_junk_is_none(self):
        self.assertIsNone(self.lookup(self.hass, "DECK-AAAA", "team-2"))
        self.assertIsNone(self.lookup(self.hass, "DECK-ZZZZ", "team-1"))
        self.assertIsNone(self.lookup(self.hass, "DECK-BBBB", "team-2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
