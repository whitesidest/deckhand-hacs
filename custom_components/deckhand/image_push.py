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
from dataclasses import dataclass

from ._source import dumps_with_source
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

try:
    import numpy as _np
except ImportError:  # pragma: no cover - numpy is present in real HA
    _np = None


def rgb565_from_bytes(source_bytes: bytes, size: int = IMAGE_DEFAULT_SIZE) -> bytes:
    """Convert encoded image bytes (JPEG/PNG/…) to a packed RGB565 buffer.

    Mirrors Helm's ``process_theme_image(..., apply_vignette=False)``:
      1. Open + convert to RGB
      2. Center-crop to square
      3. Resize to ``size`` x ``size`` (LANCZOS)
      4. Pack to RGB565 little-endian

    ``draft`` lets the JPEG decoder scale by 1/2, 1/4 or 1/8 while it
    decodes (never below ``size``), so a 1080p doorbell frame is decoded
    at 540p instead of being fully decoded and then thrown away by the
    resize. It is a no-op for PNG and other formats.

    CPU-bound — callers on the event loop MUST run this via
    ``hass.async_add_executor_job``.
    """
    if not _PIL_AVAILABLE:  # pragma: no cover - only in bare test env
        raise RuntimeError("Pillow (PIL) is not available")

    import io

    img = Image.open(io.BytesIO(source_bytes))
    img.draft("RGB", (size, size))
    img = img.convert("RGB")

    # Center-crop to square.
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))

    # Resize to target.
    img = img.resize((size, size), Image.LANCZOS)

    return _pack_rgb565(img, size)


def _pack_rgb565(img, size: int) -> bytes:
    """Pack an RGB image to RGB565 little-endian. Same bit math as
    image_utils.py: ``((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)``.

    numpy ships with HA core and does the 129,600 pixels of a 360x360
    frame in a few ms; the per-pixel loop is the fallback and took most
    of a second per dial on HA-class hardware.
    """
    if _np is not None:
        a = _np.asarray(img, dtype=_np.uint16)
        packed = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
        return packed.astype("<u2").tobytes()

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


@dataclass(frozen=True)
class PreparedImage:
    """An image converted and chunked once, ready to stream to any dial."""

    size: int
    nbytes: int
    chunks: list[str]


def prepare_image(source_bytes: bytes, size: int = IMAGE_DEFAULT_SIZE) -> PreparedImage:
    """Convert + chunk ``source_bytes``. CPU-bound: run it in the executor.

    A room announcement sends the same picture to every dial, so the
    conversion happens once per send, not once per dial.
    """
    rgb565 = rgb565_from_bytes(source_bytes, size)
    return PreparedImage(size=size, nbytes=len(rgb565), chunks=rgb565_to_chunks(rgb565))


async def publish_prepared_image(
    hass,
    team_id: str,
    dial_id: str,
    image: PreparedImage,
    *,
    source: dict[str, str] | None = None,
) -> int:
    """Publish ``start`` → ``chunk``×N → ``done`` for one dial.

    Assumes the caller has already published ``cmd/announce`` with
    ``"image": true`` so the dial suppresses theme animation while it
    waits for the stream.

    ``source`` (the ``_src`` stamp of whoever sent the announcement) rides
    the ``start`` message only: that is the action; the chunks and ``done``
    are its transport and stay byte-identical (a stamp per chunk would cost
    ~50 x the bytes and say nothing new).
    """
    from homeassistant.components import mqtt

    topic = TOPIC_CMD_IMAGE.format(team_id=team_id, dial_id=dial_id)

    await mqtt.async_publish(
        hass,
        topic,
        dumps_with_source(
            {
                "action": "start",
                "w": image.size,
                "h": image.size,
                "size": image.nbytes,
                "chunks": len(image.chunks),
            },
            source,
        ),
    )

    # The firmware handles a fast burst (Helm sends ~50 chunks in ~1.5 s
    # with a 5 ms per-chunk sleep). We yield to the loop between chunks so
    # the ~51 publishes don't starve other HA work — and so a room's dials
    # streaming concurrently take turns rather than one waiting on another.
    for i, chunk_b64 in enumerate(image.chunks):
        await mqtt.async_publish(
            hass,
            topic,
            json.dumps({"action": "chunk", "i": i, "d": chunk_b64}),
        )
        await asyncio.sleep(0)

    await mqtt.async_publish(hass, topic, json.dumps({"action": "done"}))

    _LOGGER.info(
        "Pushed %d-byte RGB565 image (%dx%d, %d chunks) to dial %s",
        image.nbytes,
        image.size,
        image.size,
        len(image.chunks),
        dial_id,
    )
    return len(image.chunks)


async def publish_image_to_dial(
    hass,
    team_id: str,
    dial_id: str,
    source_bytes: bytes,
    size: int = IMAGE_DEFAULT_SIZE,
    *,
    source: dict[str, str] | None = None,
) -> int:
    """Convert + publish an image backdrop to one dial as chunked cmd/image.

    Single-dial convenience over ``prepare_image`` + ``publish_prepared_image``;
    a multi-dial send should prepare once and publish the result to each.
    Returns the number of chunks sent.
    """
    image = await hass.async_add_executor_job(prepare_image, source_bytes, size)
    return await publish_prepared_image(hass, team_id, dial_id, image, source=source)
