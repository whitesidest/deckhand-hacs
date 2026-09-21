"""Album-art transcoder + HTTP view for cmd/now_playing.

Why this exists
---------------
The dial fetches ``album_art_url`` itself — a plain HTTP GET, no auth header,
no session — and decodes the body with JPEGDEC, which is **baseline JPEG
only**. Until now the integration published Home Assistant's own
``entity_picture`` URL (``/api/media_player_proxy/media_player.x?token=…``),
and that proxy hands back whatever the source has: PNG covers from
Pandora/AmpliPi, progressive JPEGs, or an HTML error body once the signed
token has rotated. Each of those is downloaded in full and then rejected
with ``ack_failure {cmd: album_art, reason: not_jpeg}`` (``JPEG openRAM
failed`` in the serial log).

Helm fixed this for its own pushes in helm#549
(``apps/integrations/services/album_art.py``); this module is the same
contract for the integration, which cannot mint Helm tokens and must also
work on Console installs that have no Helm at all. The rules mirror Helm's
exactly: flatten alpha onto black, RGB, downscale-only to
:data:`ART_MAX_EDGE`, ``progressive=False``, and a quality ladder that steps
down until the body fits :data:`ART_MAX_BYTES` — else no art.

How it fits together
--------------------
* :func:`album_art_url_for` is what ``_extract_now_playing`` publishes:
  ``{base}/api/deckhand/art/{key}.jpg?v={hash}``. ``key`` is an HMAC of the
  entity id under a per-runtime random secret, so the route is neither
  enumerable nor an oracle (an unknown key is a bare 404). ``?v=`` is a hash
  of the ``entity_picture`` value: the dial hashes the whole URL, so this is
  what makes it refetch on a track change.
* :class:`DeckhandAlbumArtView` serves that URL. It gets the raw image
  without an HTTP round trip — straight from the ``media_player`` entity via
  ``async_get_media_image()`` — falling back to fetching ``entity_picture``
  through HA's client session only when the entity component cannot be found.
  Transcoding runs in the executor. Results are cached per
  ``(entity_id, entity_picture)`` and served with an ETag (content hash),
  ``Cache-Control`` and ``If-None-Match`` → 304. When the upstream yields
  nothing usable the last good art for that entity is served instead, so a
  rotated token never puts an HTML body on the face.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from collections import OrderedDict
from io import BytesIO
from typing import TYPE_CHECKING, Any

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

try:  # get_url is in core since 2021 but guard for older installs
    from homeassistant.helpers.network import NoURLAvailableError, get_url
except ImportError:  # pragma: no cover - defensive fallback
    get_url = None  # type: ignore[assignment]

    class NoURLAvailableError(Exception):  # type: ignore[no-redef]
        """Fallback when helpers.network is unavailable."""


# Pillow ships with HA core, but guard the import so the module (and the
# tests that import it) load even in a bare env without PIL.
try:
    from PIL import Image

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - PIL is present in real HA
    Image = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)

# --- firmware-derived limits (keep in step with Helm's album_art.py) --------

# The dial scales art to 360x360; sending more is bandwidth for no gain.
ART_MAX_EDGE = 360
# Hard body cap enforced by the firmware fetcher.
ART_MAX_BYTES = 768 * 1024
JPEG_QUALITY_LADDER = (85, 75, 65, 55, 45, 35)
# Refuse to buffer a "cover" that is really a video file (fallback fetch).
MAX_SOURCE_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT_S = 10

ART_URL_PATH = "/api/deckhand/art"
# Transcoded JPEGs kept per (entity_id, entity_picture). Each is ~20 KB.
ART_CACHE_SIZE = 32
# Slot under hass.data[DOMAIN]. Not an entry store: it is a runtime-wide
# object (the view is registered once), and _resolve_all_dials skips it
# because it is not a dict.
ART_STORE_KEY = "_album_art"


class AlbumArtStore:
    """Per-runtime state behind the art view.

    Lives for the lifetime of the HA process: views cannot be unregistered,
    and the secret must stay stable so URLs already on a dial keep resolving.
    """

    def __init__(self) -> None:
        self.secret = secrets.token_bytes(32)
        self.entities: dict[str, str] = {}  # key -> entity_id
        # (entity_id, entity_picture) -> (jpeg bytes, etag); LRU-bounded.
        self.cache: OrderedDict[tuple[str, str], tuple[bytes, str]] = OrderedDict()
        # entity_id -> (jpeg bytes, etag) — what an unusable upstream falls back to.
        self.last_good: dict[str, tuple[bytes, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.view_registered = False

    def key_for(self, entity_id: str) -> str:
        """Return the (stable, unguessable) URL key for ``entity_id``."""
        key = hmac.new(self.secret, entity_id.encode(), hashlib.sha256).hexdigest()[:32]
        self.entities[key] = entity_id
        return key

    def entity_for(self, key: str) -> str | None:
        return self.entities.get(key)

    def cached(self, entity_id: str, picture: str) -> tuple[bytes, str] | None:
        hit = self.cache.get((entity_id, picture))
        if hit is not None:
            self.cache.move_to_end((entity_id, picture))
        return hit

    def remember(self, entity_id: str, picture: str, data: bytes) -> str:
        """Cache ``data`` for ``(entity_id, picture)``; return its ETag."""
        etag = hashlib.sha256(data).hexdigest()
        self.cache[(entity_id, picture)] = (data, etag)
        self.cache.move_to_end((entity_id, picture))
        while len(self.cache) > ART_CACHE_SIZE:
            self.cache.popitem(last=False)
        self.last_good[entity_id] = (data, etag)
        return etag

    def lock(self, entity_id: str) -> asyncio.Lock:
        """One transcode per entity at a time — four dials on one player
        must not fan out into four fetches of the same cover."""
        lock = self._locks.get(entity_id)
        if lock is None:
            lock = self._locks[entity_id] = asyncio.Lock()
        return lock


def album_art_store(hass: HomeAssistant) -> AlbumArtStore:
    """The runtime-wide store, created on first use."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(ART_STORE_KEY)
    if not isinstance(store, AlbumArtStore):
        store = domain_data[ART_STORE_KEY] = AlbumArtStore()
    return store


def register_album_art_view(hass: HomeAssistant) -> None:
    """Register the view once per runtime (idempotent across config entries)."""
    store = album_art_store(hass)
    if store.view_registered:
        return
    hass.http.register_view(DeckhandAlbumArtView(hass))
    store.view_registered = True


# ---------------------------------------------------------------------------
# URL minting
# ---------------------------------------------------------------------------


def _base_url(hass: HomeAssistant) -> str:
    """Base URL the DIAL should GET from.

    Prefer the internal URL (LAN-local, no Nabu Casa round-trip) and fall
    back to whatever ``get_url`` returns. Empty when HA has no URL at all.
    """
    if get_url is None:
        return ""
    try:
        base = get_url(hass, allow_ip=True, prefer_external=False)
    except NoURLAvailableError:
        try:
            base = get_url(hass, allow_ip=True, prefer_external=True)
        except NoURLAvailableError:
            return ""
    return str(base or "").rstrip("/")


def picture_version(entity_picture: str) -> str:
    """Short hash of the ``entity_picture`` value — the ``?v=`` cache-buster."""
    return hashlib.sha256(entity_picture.encode()).hexdigest()[:10]


def album_art_url_for(hass: HomeAssistant, entity_id: str, entity_picture: str) -> str:
    """Return the integration-served art URL for ``entity_id``, or "".

    Empty when the entity has no picture, or when HA has no URL to publish —
    the raw ``entity_picture`` is never handed out: a proxy URL the dial
    cannot decode is worse than no art.
    """
    picture = (entity_picture or "").strip()
    if not picture:
        return ""
    base = _base_url(hass)
    if not base:
        _LOGGER.warning("album_art: Home Assistant has no URL to publish; omitting art for %s", entity_id)
        return ""
    key = album_art_store(hass).key_for(entity_id)
    return f"{base}{ART_URL_PATH}/{key}.jpg?v={picture_version(picture)}"


# ---------------------------------------------------------------------------
# transcode
# ---------------------------------------------------------------------------


def transcode(raw: bytes) -> bytes | None:
    """Convert arbitrary image bytes to a dial-safe baseline JPEG.

    Returns None when Pillow cannot open the body at all — which is exactly
    what an HTML error page looks like, so this doubles as the "is the
    upstream still good?" check. Mirrors Helm's ``transcode`` rule for rule.
    """
    if not raw or not _PIL_AVAILABLE:
        return None
    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception:  # noqa: BLE001 - any undecodable body is "no art"
        _LOGGER.info("album_art: body is not a decodable image (%d bytes)", len(raw))
        return None

    # Flatten alpha onto black rather than letting convert("RGB") turn
    # transparent pixels black-with-fringing; the now-playing face is dark.
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (0, 0, 0))
        flat.paste(img, mask=img.split()[-1])
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Downscale only — upscaling a 64px thumbnail to 360 just wastes bytes.
    if max(img.size) > ART_MAX_EDGE:
        img.thumbnail((ART_MAX_EDGE, ART_MAX_EDGE), Image.LANCZOS)

    data = b""
    for quality in JPEG_QUALITY_LADDER:
        buf = BytesIO()
        # progressive=False is the whole point: JPEGDEC on the dial decodes
        # baseline only, and a progressive JPEG acks `not_jpeg`.
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=False)
        data = buf.getvalue()
        if len(data) <= ART_MAX_BYTES:
            return data

    _LOGGER.warning("album_art: %d bytes even at q%d; dropping", len(data), JPEG_QUALITY_LADDER[-1])
    return None


# ---------------------------------------------------------------------------
# raw image acquisition
# ---------------------------------------------------------------------------


def _media_player_entity(hass: HomeAssistant, entity_id: str) -> Any | None:
    """The live ``MediaPlayerEntity`` for ``entity_id``, or None.

    ``hass.data["entity_components"]`` is where every EntityComponent
    registers itself (``DATA_INSTANCES``, unchanged since well before the
    2024.1 floor in manifest.json); ``hass.data["media_player"]`` is the
    component's own slot, kept as a second chance.
    """
    components = hass.data.get("entity_components")
    component = components.get("media_player") if isinstance(components, dict) else None
    if component is None:
        component = hass.data.get("media_player")
    get_entity = getattr(component, "get_entity", None)
    if get_entity is None:
        return None
    return get_entity(entity_id)


async def _fetch_via_session(hass: HomeAssistant, entity_picture: str) -> bytes | None:
    """Fallback: GET ``entity_picture`` through HA's client session."""
    from aiohttp import ClientTimeout  # lazy: keep the module importable without aiohttp

    url = entity_picture
    if not url.startswith(("http://", "https://")):
        base = _base_url(hass)
        if not base:
            return None
        url = f"{base}{url}"
    try:
        session = async_get_clientsession(hass)
        async with session.get(
            url, timeout=ClientTimeout(total=FETCH_TIMEOUT_S), headers={"Accept": "image/*"}
        ) as resp:
            if resp.status != 200:
                _LOGGER.info("album_art: upstream %s for %s", resp.status, url)
                return None
            if (resp.content_length or 0) > MAX_SOURCE_BYTES:
                return None
            raw = await resp.content.read(MAX_SOURCE_BYTES + 1)
    except Exception:  # noqa: BLE001 - best-effort; the caller serves last-good
        _LOGGER.info("album_art: fetch failed for %s", url, exc_info=True)
        return None
    if len(raw) > MAX_SOURCE_BYTES:
        _LOGGER.warning("album_art: source over %d bytes; dropping", MAX_SOURCE_BYTES)
        return None
    return raw


async def _raw_image(hass: HomeAssistant, entity_id: str, entity_picture: str) -> bytes | None:
    """Raw cover bytes for ``entity_id``, straight from the entity when possible."""
    entity = _media_player_entity(hass, entity_id)
    if entity is not None and hasattr(entity, "async_get_media_image"):
        try:
            data, _content_type = await entity.async_get_media_image()
        except Exception:  # noqa: BLE001 - a broken source is "no art", not a 500
            _LOGGER.info("album_art: %s.async_get_media_image failed", entity_id, exc_info=True)
            data = None
        return data or None
    return await _fetch_via_session(hass, entity_picture)


async def _fetch_and_transcode(hass: HomeAssistant, entity_id: str, entity_picture: str) -> bytes | None:
    raw = await _raw_image(hass, entity_id, entity_picture)
    if not raw:
        return None
    return await hass.async_add_executor_job(transcode, raw)


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    for tag in if_none_match.split(","):
        tag = tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag.strip('"') == etag:
            return True
    return False


class DeckhandAlbumArtView(HomeAssistantView):
    """Serve ``/api/deckhand/art/{key}.jpg`` — baseline JPEG, dial-sized.

    Unauthenticated by necessity (the dial has no session); safe because
    ``key`` is an HMAC the caller cannot derive and an unknown key is a bare
    404 with no detail.
    """

    url = f"{ART_URL_PATH}/{{key}}.jpg"
    name = "api:deckhand:art"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request, key: str):
        from aiohttp import web  # lazy: keep the module importable without aiohttp

        store = album_art_store(self.hass)
        entity_id = store.entity_for(key)
        if entity_id is None:
            return web.Response(status=404)

        state = self.hass.states.get(entity_id)
        attributes = getattr(state, "attributes", None) or {}
        picture = str(attributes.get("entity_picture") or "").strip()

        result: tuple[bytes, str] | None = None
        if picture:
            result = store.cached(entity_id, picture)
            if result is None:
                async with store.lock(entity_id):
                    result = store.cached(entity_id, picture)  # a peer may have filled it
                    if result is None:
                        data = await _fetch_and_transcode(self.hass, entity_id, picture)
                        if data:
                            result = (data, store.remember(entity_id, picture, data))

        if result is None:
            result = store.last_good.get(entity_id)
            if result is None:
                if not picture:
                    return web.Response(status=404)
                return web.Response(status=502, text="album art unavailable", content_type="text/plain")
            _LOGGER.info("album_art: upstream unusable for %s; serving last good art", entity_id)

        data, etag = result
        headers = {"ETag": f'"{etag}"', "Cache-Control": "public, max-age=86400"}
        if _etag_matches(request.headers.get("If-None-Match"), etag):
            return web.Response(status=304, headers=headers)
        headers["Content-Length"] = str(len(data))
        return web.Response(body=data, content_type="image/jpeg", headers=headers)
