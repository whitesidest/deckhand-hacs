"""Doorbell / snapshot image push over MQTT (cmd/image).

This is the LAN-direct image path for the Deckhand HACS integration.
Unlike Helm — which processes + chunks images server-side and the dial
fetches them — HACS publishes to the MQTT broker DIRECTLY, so the image
conversion (JPEG/PNG → RGB565) and chunking must happen here.

The wire protocol MUST match the firmware fast-path parser in
``deckhand-firmware/src/network.h`` (the ``/cmd/image`` branch) and
Helm's reference implementation:

* ``apps/themes/image_utils.py`` — ``process_theme_image`` (PIL resize +
  RGB→RGB565 little-endian packing) and ``rgb565_to_chunks``.
* ``apps/mqtt/tasks.py`` — ``push_image_to_scope_url`` / ``_push_rgb565_to_dial``
  (publish start → chunk×N → done).

Three cmd/image JSON messages, all under
``deckhand/{team_id}/dial/{dial_id}/cmd/image``:

    {"action":"start","w":W,"h":H,"size":TOTAL_RGB565_BYTES,"chunks":N}
    {"action":"chunk","i":INDEX,"d":"<base64 slice of RGB565 stream>"}   (×N)
    {"action":"done"}

Camera snapshots go through with ``apply_vignette=False`` — the theme
vignette darkens center pixels and rendered doorbell subjects as
near-black (Helm learned this the hard way, see image_utils.py docstring).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct

from .const import IMAGE_CHUNK_SIZE_BYTES, IMAGE_DEFAULT_SIZE, TOPIC_CMD_IMAGE

_LOGGER = logging.getLogger(__name__)

# Pillow ships with HA core, but guard the import so the module (and the
# schema tests that import it) load even in a bare test env without PIL.
try:
    from PIL import Image

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - PIL is present in real HA
    Image = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False


def rgb565_from_bytes(source_bytes: bytes, size: int = IMAGE_DEFAULT_SIZE) -> bytes:
    """Convert encoded image bytes (JPEG/PNG/…) to a packed RGB565 buffer.

    Mirrors Helm's ``process_theme_image(..., apply_vignette=False)``:
      1. Open + convert to RGB
      2. Center-crop to square
      3. Resize to ``size`` x ``size`` (LANCZOS)
      4. Pack to RGB565 little-endian

    CPU-bound (PIL + a tight pack loop) — callers on the event loop MUST
    run this via ``hass.async_add_executor_job``.
    """
    if not _PIL_AVAILABLE:  # pragma: no cover - only in bare test env
        raise RuntimeError("Pillow (PIL) is not available")

    import io

    img = Image.open(io.BytesIO(source_bytes)).convert("RGB")

    # Center-crop to square.
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))

    # Resize to target.
    img = img.resize((size, size), Image.LANCZOS)

    # Pack RGB565 little-endian. Same bit math as image_utils.py:
    #   rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    pixels = img.load()
    buf = bytearray(size * size * 2)
    idx = 0
    for y in range(size):
        for x in range(size):
            r, g, b = pixels[x, y]
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            struct.pack_into("<H", buf, idx, rgb565)
            idx += 2

    return bytes(buf)


def rgb565_to_chunks(data: bytes, chunk_size: int = IMAGE_CHUNK_SIZE_BYTES) -> list[str]:
    """Slice RGB565 bytes into base64-encoded chunks (matches Helm)."""
    chunks: list[str] = []
    for offset in range(0, len(data), chunk_size):
        chunks.append(base64.b64encode(data[offset : offset + chunk_size]).decode("ascii"))
    return chunks


async def publish_image_to_dial(
    hass,
    team_id: str,
    dial_id: str,
    source_bytes: bytes,
    size: int = IMAGE_DEFAULT_SIZE,
) -> int:
    """Convert + publish an image backdrop to one dial as chunked cmd/image.

    Runs the CPU-bound PIL/RGB565 work in HA's executor so the event
    loop is never blocked, then publishes ``start`` → ``chunk``×N →
    ``done``. Returns the number of chunks sent.

    Assumes the caller has already published ``cmd/announce`` with
    ``"image": true`` so the dial suppresses theme animation while it
    waits for the stream.
    """
    from homeassistant.components import mqtt

    rgb565 = await hass.async_add_executor_job(rgb565_from_bytes, source_bytes, size)
    chunks = rgb565_to_chunks(rgb565)

    topic = TOPIC_CMD_IMAGE.format(team_id=team_id, dial_id=dial_id)

    await mqtt.async_publish(
        hass,
        topic,
        json.dumps(
            {
                "action": "start",
                "w": size,
                "h": size,
                "size": len(rgb565),
                "chunks": len(chunks),
            }
        ),
    )

    # The firmware handles a fast burst (Helm sends ~50 chunks in ~1.5 s
    # with a 5 ms per-chunk sleep). We yield to the loop between chunks so
    # the ~51 publishes don't starve other HA work; the QoS-0 broker
    # fan-out has breathing room without an explicit sleep.
    for i, chunk_b64 in enumerate(chunks):
        await mqtt.async_publish(
            hass,
            topic,
            json.dumps({"action": "chunk", "i": i, "d": chunk_b64}),
        )
        await asyncio.sleep(0)

    await mqtt.async_publish(hass, topic, json.dumps({"action": "done"}))

    _LOGGER.info(
        "Pushed %d-byte RGB565 image (%dx%d, %d chunks) to dial %s",
        len(rgb565),
        size,
        size,
        len(chunks),
        dial_id,
    )
    return len(chunks)
