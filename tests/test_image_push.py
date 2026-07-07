"""Tests for the doorbell / visitor image push (cmd/image).

Exercises the LAN-direct image path added in
``custom_components/deckhand/image_push.py``:

  * RGB565 conversion matches Helm's wire format bit-for-bit.
  * A publish emits the exact ``start`` → ``chunk``×N → ``done`` frame
    sequence the firmware fast-path expects.

Home Assistant is not importable in this bare test env, so the
``homeassistant.components.mqtt`` module ``image_push`` imports lazily
is stubbed in ``sys.modules`` with an async-recording double. Pillow IS
present in HA core (and in this env), so the conversion is exercised for
real; if it's ever missing the conversion tests skip.

Run with:  python3 -m pytest tests/test_image_push.py
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import struct
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "deckhand"


def _load_module(name: str, filename: str):
    """Import a single module from the deckhand package without importing
    the whole ``__init__`` (which pulls in Home Assistant)."""
    # Register a minimal stub package so ``from .const import ...`` inside
    # the target module resolves relative imports.
    pkg_name = "deckhand_test_pkg"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[pkg_name] = pkg
    full = f"{pkg_name}.{name}"
    spec = importlib.util.spec_from_file_location(full, PKG_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# const has no HA deps; image_push only imports PIL + const at module load.
const = _load_module("const", "const.py")
image_push = _load_module("image_push", "image_push.py")


def _make_test_jpeg(w: int = 8, h: int = 8) -> bytes:
    """Encode a tiny solid-color image to JPEG bytes."""
    from PIL import Image
    import io

    img = Image.new("RGB", (w, h), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@unittest.skipUnless(image_push._PIL_AVAILABLE, "Pillow not available")
class Rgb565ConversionTests(unittest.TestCase):
    def test_output_size_matches_resolution(self):
        data = image_push.rgb565_from_bytes(_make_test_jpeg(), size=360)
        # W*H*2 — exactly what the firmware allocates from ``size``.
        self.assertEqual(len(data), 360 * 360 * 2)

    def test_360_produces_51_chunks(self):
        data = image_push.rgb565_from_bytes(_make_test_jpeg(), size=360)
        chunks = image_push.rgb565_to_chunks(data)
        # 259200 bytes / 5120 = 50.6 → 51 chunks (matches Helm).
        self.assertEqual(len(chunks), 51)

    def test_chunks_are_base64_and_reassemble(self):
        data = image_push.rgb565_from_bytes(_make_test_jpeg(), size=32)
        chunks = image_push.rgb565_to_chunks(data)
        reassembled = b"".join(base64.b64decode(c) for c in chunks)
        self.assertEqual(reassembled, data)

    def test_packing_bit_math_matches_helm(self):
        # Pure-red (255,0,0) packs to little-endian RGB565 0xF800.
        data = image_push.rgb565_from_bytes(_make_test_jpeg(w=16, h=16), size=16)
        first = struct.unpack_from("<H", data, 0)[0]
        expected = ((255 & 0xF8) << 8) | ((0 & 0xFC) << 3) | (0 >> 3)
        self.assertEqual(first, expected)
        self.assertEqual(first, 0xF800)


class _RecordingMqtt:
    """Stub for ``homeassistant.components.mqtt`` that records publishes."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def async_publish(self, hass, topic, payload, *args, **kwargs):
        self.published.append((topic, payload))


class _FakeHass:
    """Minimal hass double — runs executor jobs inline (sync)."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@unittest.skipUnless(image_push._PIL_AVAILABLE, "Pillow not available")
class PublishSequenceTests(unittest.TestCase):
    def setUp(self):
        # Install the mqtt stub image_push imports lazily.
        self.mqtt = _RecordingMqtt()
        comp = types.ModuleType("homeassistant.components")
        comp.__path__ = []  # mark as package
        mqtt_mod = types.ModuleType("homeassistant.components.mqtt")
        mqtt_mod.async_publish = self.mqtt.async_publish
        ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
        ha.__path__ = []
        sys.modules["homeassistant.components"] = comp
        sys.modules["homeassistant.components.mqtt"] = mqtt_mod

    def test_publish_emits_start_chunks_done(self):
        jpeg = _make_test_jpeg()
        n = asyncio.run(
            image_push.publish_image_to_dial(
                _FakeHass(), "team123", "DECK-ABCD", jpeg, size=360
            )
        )
        self.assertEqual(n, 51)

        topics = {t for t, _ in self.mqtt.published}
        self.assertEqual(topics, {"deckhand/team123/dial/DECK-ABCD/cmd/image"})

        payloads = [json.loads(p) for _, p in self.mqtt.published]
        # First = start with correct geometry.
        self.assertEqual(payloads[0]["action"], "start")
        self.assertEqual(payloads[0]["w"], 360)
        self.assertEqual(payloads[0]["h"], 360)
        self.assertEqual(payloads[0]["size"], 360 * 360 * 2)
        self.assertEqual(payloads[0]["chunks"], 51)
        # Middle = 51 sequential chunks, indices 0..50.
        chunk_payloads = payloads[1:-1]
        self.assertEqual(len(chunk_payloads), 51)
        self.assertEqual([c["i"] for c in chunk_payloads], list(range(51)))
        for c in chunk_payloads:
            self.assertEqual(c["action"], "chunk")
            self.assertIn("d", c)
        # Last = done.
        self.assertEqual(payloads[-1], {"action": "done"})

    def test_each_image_message_under_8kb(self):
        """Firmware drops any cmd/image payload >= 8192 bytes."""
        jpeg = _make_test_jpeg()
        asyncio.run(
            image_push.publish_image_to_dial(
                _FakeHass(), "t", "d", jpeg, size=360
            )
        )
        for _, payload in self.mqtt.published:
            self.assertLess(len(payload.encode("utf-8")), 8192)


if __name__ == "__main__":
    unittest.main(verbosity=2)
