"""Album art reaches the dial as a baseline JPEG its decoder accepts.

``cmd/now_playing`` used to carry Home Assistant's own ``entity_picture``
URL (``/api/media_player_proxy/media_player.x?token=…``). The dial fetches
that with no session and decodes it with JPEGDEC — baseline JPEG only — so
a PNG cover, a progressive JPEG, or the HTML body HA returns once the token
has rotated all end in ``ack_failure {cmd: album_art, reason: not_jpeg}``
(seen on four dials on 2026-09-21). Helm solved this for its own pushes in
helm#549; ``custom_components/deckhand/album_art.py`` is the integration's
equivalent, and these tests pin its contract:

* the transcode rules (alpha onto black, downscale-only to 360, baseline
  ``FFC0`` never progressive ``FFC2``, the quality ladder under 768 KB);
* the view (unknown key → bare 404, ETag / 304, cache, last-good fallback,
  the entity-component path and the session fallback);
* the published ``album_art_url`` — ours, never the proxy — and that it
  changes exactly when ``entity_picture`` does.

The view and transcoder load under the Home Assistant stand-in from
test_entities_follow_heartbeats.py; the payload tests run the REAL
``_extract_now_playing`` through the service harness in
test_emoji_strip_every_service.py. Pillow and aiohttp are both present in
HA core; the tests that need them skip when they are missing here.

Run with:  python3 -m pytest tests/test_album_art.py
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import random
import re
import sys
import types
import unittest
from unittest import mock

import test_entities_follow_heartbeats as standin  # installs the HA stand-in
import test_emoji_strip_every_service as harness  # the real __init__ under it

art = importlib.import_module(f"{standin.PKG}.album_art")
INIT_ART = sys.modules[f"{harness.PKG}.album_art"]  # the copy _extract_now_playing uses

try:
    from PIL import Image

    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False
try:
    import aiohttp  # noqa: F401

    HAVE_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAVE_AIOHTTP = False

BASE = "http://192.168.1.191:8123"
ENTITY = "media_player.office_2"
PROXY = "/api/media_player_proxy/media_player.office_2?token=abc123&cache=deadbeef"
PROXY_NEXT_TRACK = "/api/media_player_proxy/media_player.office_2?token=abc123&cache=cafef00d"
HTML_401 = b"<html><body>401: Unauthorized</body></html>"

SOF0 = b"\xff\xc0"  # baseline DCT
SOF2 = b"\xff\xc2"  # progressive DCT

OUR_URL = re.compile(r"^http://192\.168\.1\.191:8123/api/deckhand/art/([0-9a-f]{32})\.jpg\?v=([0-9a-f]{10})$")


# ------------------------------------------------------------------ fixtures
def _png_with_alpha(w: int = 64, h: int = 64) -> bytes:
    """Left half opaque red, right half fully transparent."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for x in range(w // 2):
        for y in range(h):
            img.putpixel((x, y), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(w: int, h: int, progressive: bool) -> bytes:
    img = Image.new("RGB", (w, h), (40, 90, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, progressive=progressive)
    return buf.getvalue()


def _noise(w: int = 360, h: int = 360) -> "Image.Image":
    rng = random.Random(7)
    return Image.frombytes("RGB", (w, h), bytes(rng.getrandbits(8) for _ in range(w * h * 3)))


def _save(img, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=False)
    return buf.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


def _pixel(data: bytes, xy) -> tuple[int, int, int]:
    return Image.open(io.BytesIO(data)).convert("RGB").getpixel(xy)


class _States:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)


def _state(**attributes):
    return types.SimpleNamespace(state="playing", attributes=attributes)


class _Entity:
    """Just enough of MediaPlayerEntity: async_get_media_image."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.calls = 0

    async def async_get_media_image(self):
        self.calls += 1
        return self.raw, "image/png"


class _Component:
    def __init__(self, entities):
        self._entities = entities

    def get_entity(self, entity_id):
        return self._entities.get(entity_id)


class _Hass:
    def __init__(self, states=None, entities=None):
        self.data = {}
        self.states = _States(states or {})
        if entities is not None:
            self.data["entity_components"] = {"media_player": _Component(entities)}
        self.views = []
        self.http = types.SimpleNamespace(register_view=self.views.append)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self.status = status
        self.content_length = len(body)
        self._body = body
        self.content = types.SimpleNamespace(read=self._read)

    async def _read(self, n=-1):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, body: bytes):
        self.body = body
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return _FakeResp(self.body)


def _request(**headers):
    return types.SimpleNamespace(headers=headers)


class _NoURL(Exception):
    pass


# ------------------------------------------------------------------ transcode
@unittest.skipUnless(HAVE_PIL, "Pillow not available")
class TranscodeTests(unittest.TestCase):
    def test_png_with_alpha_becomes_a_baseline_jpeg_flattened_onto_black(self):
        out = art.transcode(_png_with_alpha())
        self.assertIsNotNone(out)
        self.assertTrue(out.startswith(b"\xff\xd8"), "not a JPEG")
        self.assertIn(SOF0, out)
        self.assertNotIn(SOF2, out)
        # The transparent half is black, not white or fringed.
        r, g, b = _pixel(out, (60, 32))
        self.assertLess(max(r, g, b), 24, (r, g, b))
        r, g, b = _pixel(out, (4, 32))
        self.assertGreater(r, 200, "positive control: the opaque half is still red")

    def test_a_progressive_jpeg_comes_out_baseline(self):
        src = _jpeg(200, 200, progressive=True)
        self.assertIn(SOF2, src, "premise: the source really is progressive")
        out = art.transcode(src)
        self.assertIn(SOF0, out)
        self.assertNotIn(SOF2, out)

    def test_oversized_art_is_downscaled_to_the_dials_edge(self):
        out = art.transcode(_jpeg(1200, 800, progressive=False))
        self.assertEqual(_size(out), (360, 240))
        self.assertLessEqual(max(_size(out)), art.ART_MAX_EDGE)

    def test_small_art_is_not_upscaled(self):
        self.assertEqual(_size(art.transcode(_jpeg(64, 64, progressive=False))), (64, 64))

    def test_the_quality_ladder_steps_down_until_the_body_fits(self):
        src = _save(_noise(), 95)
        decoded = Image.open(io.BytesIO(src))  # what transcode re-encodes
        decoded.load()
        sizes = {q: len(_save(decoded, q)) for q in art.JPEG_QUALITY_LADDER}
        self.assertGreater(sizes[85], sizes[75], "premise: lower quality really is smaller")
        cap = sizes[85] - 1  # q85 must not fit
        expected_q = next(q for q in art.JPEG_QUALITY_LADDER if sizes[q] <= cap)
        self.assertLess(expected_q, 85)
        with mock.patch.object(art, "ART_MAX_BYTES", cap):
            out = art.transcode(src)
        self.assertLessEqual(len(out), cap)
        self.assertEqual(out, _save(decoded, expected_q))
        self.assertIn(SOF0, out)

    def test_nothing_on_the_ladder_fitting_means_no_art(self):
        with mock.patch.object(art, "ART_MAX_BYTES", 100):
            self.assertIsNone(art.transcode(_save(_noise(), 95)))

    def test_an_html_error_body_is_not_art(self):
        self.assertIsNone(art.transcode(HTML_401))
        self.assertIsNone(art.transcode(b""))


# ------------------------------------------------------------------ the URL
class ArtUrlTests(unittest.TestCase):
    def setUp(self):
        self.hass = _Hass()
        p = mock.patch.object(art, "get_url", lambda hass, **kw: BASE)
        p.start()
        self.addCleanup(p.stop)

    def test_the_url_is_ours_not_the_proxy(self):
        url = art.album_art_url_for(self.hass, ENTITY, PROXY)
        self.assertRegex(url, OUR_URL)
        self.assertNotIn("media_player_proxy", url)
        self.assertNotIn("token=", url)
        self.assertLessEqual(len(url), 256)

    def test_the_url_changes_when_the_picture_changes_and_only_then(self):
        a = art.album_art_url_for(self.hass, ENTITY, PROXY)
        again = art.album_art_url_for(self.hass, ENTITY, PROXY)
        b = art.album_art_url_for(self.hass, ENTITY, PROXY_NEXT_TRACK)
        self.assertEqual(a, again)
        self.assertNotEqual(a, b, "the dial hashes the whole URL; a new track must be a new URL")
        self.assertEqual(OUR_URL.match(a).group(1), OUR_URL.match(b).group(1), "same entity, same key")

    def test_the_key_names_the_entity_only_to_us(self):
        key = OUR_URL.match(art.album_art_url_for(self.hass, ENTITY, PROXY)).group(1)
        other = OUR_URL.match(art.album_art_url_for(self.hass, "media_player.kitchen", PROXY)).group(1)
        self.assertNotEqual(key, other)
        self.assertNotIn(ENTITY, key)
        self.assertNotEqual(key, hashlib.sha256(ENTITY.encode()).hexdigest()[:32], "an unkeyed hash is guessable")
        self.assertEqual(art.album_art_store(self.hass).entity_for(key), ENTITY)
        # Another runtime has another secret, so the key cannot be precomputed.
        self.assertNotEqual(OUR_URL.match(art.album_art_url_for(_Hass(), ENTITY, PROXY)).group(1), key)

    def test_no_picture_means_no_url(self):
        self.assertEqual(art.album_art_url_for(self.hass, ENTITY, ""), "")
        self.assertEqual(art.album_art_url_for(self.hass, ENTITY, "   "), "")

    def test_the_internal_url_is_preferred_and_external_is_the_fallback(self):
        calls = []

        def get_url(hass, **kw):
            calls.append(kw)
            if not kw.get("prefer_external"):
                raise _NoURL()
            return "https://example.duckdns.org/"

        with mock.patch.object(art, "get_url", get_url), mock.patch.object(art, "NoURLAvailableError", _NoURL):
            url = art.album_art_url_for(self.hass, ENTITY, PROXY)
        self.assertTrue(url.startswith("https://example.duckdns.org/api/deckhand/art/"), url)
        self.assertEqual(calls[0], {"allow_ip": True, "prefer_external": False})
        self.assertEqual(calls[1], {"allow_ip": True, "prefer_external": True})

    def test_no_ha_url_at_all_means_no_url_never_the_raw_picture(self):
        def get_url(hass, **kw):
            raise _NoURL()

        with mock.patch.object(art, "get_url", get_url), mock.patch.object(art, "NoURLAvailableError", _NoURL):
            self.assertEqual(art.album_art_url_for(self.hass, ENTITY, PROXY), "")


# ------------------------------------------------------------------ the view
@unittest.skipUnless(HAVE_PIL and HAVE_AIOHTTP, "Pillow + aiohttp not available")
class ArtViewTests(unittest.TestCase):
    def setUp(self):
        self.entity = _Entity(_png_with_alpha())
        self.state = _state(entity_picture=PROXY, friendly_name="Office")
        self.hass = _Hass(states={ENTITY: self.state}, entities={ENTITY: self.entity})
        p = mock.patch.object(art, "get_url", lambda hass, **kw: BASE)
        p.start()
        self.addCleanup(p.stop)
        self.view = art.DeckhandAlbumArtView(self.hass)

    def key(self, entity_id=ENTITY, picture=PROXY) -> str:
        return OUR_URL.match(art.album_art_url_for(self.hass, entity_id, picture)).group(1)

    def get(self, key, **headers):
        return asyncio.run(self.view.get(_request(**headers), key))

    def test_an_unknown_key_is_a_bare_404(self):
        for key in ("0" * 32, "../../etc/passwd", "", self.key()[:-1]):
            with self.subTest(key=key):
                resp = self.get(key)
                self.assertEqual(resp.status, 404)
                self.assertFalse(resp.body, "no detail")

    def test_a_known_key_serves_a_baseline_jpeg_with_cache_headers(self):
        resp = self.get(self.key())
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Type"], "image/jpeg")
        self.assertEqual(resp.headers["Content-Length"], str(len(resp.body)))
        self.assertEqual(resp.headers["ETag"], f'"{hashlib.sha256(resp.body).hexdigest()}"')
        self.assertEqual(resp.headers["Cache-Control"], "public, max-age=86400")
        self.assertIn(SOF0, resp.body)
        self.assertNotIn(SOF2, resp.body)
        self.assertNotEqual(resp.body, self.entity.raw, "positive control: the PNG was transcoded")

    def test_the_view_needs_no_auth_and_is_registered_once(self):
        self.assertIs(art.DeckhandAlbumArtView.requires_auth, False)
        self.assertEqual(art.DeckhandAlbumArtView.url, "/api/deckhand/art/{key}.jpg")
        art.register_album_art_view(self.hass)
        art.register_album_art_view(self.hass)  # a second config entry
        self.assertEqual(len(self.hass.views), 1)
        self.assertIsInstance(self.hass.views[0], art.DeckhandAlbumArtView)

    def test_if_none_match_gives_304(self):
        first = self.get(self.key())
        etag = first.headers["ETag"]
        for header in (etag, f"W/{etag}", f'"other", {etag}', "*"):
            with self.subTest(header=header):
                resp = self.get(self.key(), **{"If-None-Match": header})
                self.assertEqual(resp.status, 304)
                self.assertFalse(resp.body)
                self.assertEqual(resp.headers["ETag"], etag)
        stale = self.get(self.key(), **{"If-None-Match": '"stale"'})
        self.assertEqual(stale.status, 200, "positive control: a non-matching tag gets the body")

    def test_art_is_cached_per_picture_and_refetched_on_a_new_one(self):
        self.get(self.key())
        self.get(self.key())
        self.assertEqual(self.entity.calls, 1, "the second dial must not refetch and retranscode")
        self.state.attributes["entity_picture"] = PROXY_NEXT_TRACK
        self.get(self.key())
        self.assertEqual(self.entity.calls, 2)

    def test_the_cache_is_bounded(self):
        for i in range(art.ART_CACHE_SIZE + 5):
            self.state.attributes["entity_picture"] = f"{PROXY}&n={i}"
            self.get(self.key())
        self.assertEqual(len(art.album_art_store(self.hass).cache), art.ART_CACHE_SIZE)

    def test_an_unusable_upstream_serves_the_last_good_art(self):
        good = self.get(self.key())
        # The token rotated: HA now answers with an HTML body.
        self.entity.raw = HTML_401
        self.state.attributes["entity_picture"] = PROXY_NEXT_TRACK
        resp = self.get(self.key())
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, good.body)
        self.assertEqual(resp.headers["ETag"], good.headers["ETag"])

    def test_an_unusable_upstream_with_no_history_is_a_plain_text_502(self):
        self.entity.raw = HTML_401
        resp = self.get(self.key())
        self.assertEqual(resp.status, 502)
        self.assertTrue(resp.headers["Content-Type"].startswith("text/plain"))
        self.assertNotIn(b"<html", resp.body or b"")

    def test_no_picture_and_no_history_is_a_404(self):
        self.state.attributes["entity_picture"] = ""
        self.assertEqual(self.get(self.key(picture=PROXY_NEXT_TRACK)).status, 404)

    def test_without_the_entity_component_the_picture_is_fetched_through_the_session(self):
        hass = _Hass(states={ENTITY: self.state})  # no entity_components
        session = _FakeSession(_png_with_alpha())
        with mock.patch.object(art, "get_url", lambda h, **kw: BASE), \
                mock.patch.object(art, "async_get_clientsession", lambda h: session):
            key = OUR_URL.match(art.album_art_url_for(hass, ENTITY, PROXY)).group(1)
            resp = asyncio.run(art.DeckhandAlbumArtView(hass).get(_request(), key))
        self.assertEqual(resp.status, 200)
        self.assertIn(SOF0, resp.body)
        self.assertEqual(session.urls, [f"{BASE}{PROXY}"])


# ------------------------------------------------------------------ the payload
class NowPlayingPayloadArt(harness._ServiceTest):
    """The real ``_extract_now_playing`` through ``update_from_media_player``."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(INIT_ART, "get_url", lambda hass, **kw: BASE)
        p.start()
        self.addCleanup(p.stop)
        self.state = harness._state(
            "playing", media_title="Dreamscapes", media_artist="Eli & Fur",
            friendly_name="Speakers - Office", entity_picture=PROXY, supported_features=0,
        )
        self.hass.states._states[ENTITY] = self.state

    def published(self, picture):
        self.state.attributes["entity_picture"] = picture
        return self.one("update_from_media_player", "/cmd/now_playing", entity_id=ENTITY)

    def test_the_published_art_is_served_by_the_integration(self):
        p = self.published(PROXY)
        self.assertRegex(p["album_art_url"], OUR_URL)
        self.assertNotIn("media_player_proxy", p["album_art_url"])
        self.assertEqual(p["title"], "Dreamscapes", "positive control: the rest of the payload is intact")

    def test_the_published_art_changes_when_the_picture_changes(self):
        a = self.published(PROXY)["album_art_url"]
        b = self.published(PROXY_NEXT_TRACK)["album_art_url"]
        self.assertNotEqual(a, b)
        self.assertEqual(a, self.published(PROXY)["album_art_url"])

    def test_an_empty_picture_still_omits_the_key(self):
        self.assertNotIn("album_art_url", self.published(""))
        self.state.attributes.pop("entity_picture")
        self.assertNotIn("album_art_url", self.one("update_from_media_player", "/cmd/now_playing", entity_id=ENTITY))

    def test_the_dial_can_resolve_what_was_published(self):
        key = OUR_URL.match(self.published(PROXY)["album_art_url"]).group(1)
        self.assertEqual(INIT_ART.album_art_store(self.hass).entity_for(key), ENTITY)

    def test_setup_registers_the_view_and_the_manifest_depends_on_http(self):
        src = (standin.PKG_DIR / "__init__.py").read_text(encoding="utf-8")
        block = re.search(r"async def async_setup_entry\(.*?\n    return True", src, re.DOTALL)
        self.assertIn("register_album_art_view(hass)", block.group(0))
        self.assertNotIn("_resolve_entity_picture_url", src, "the proxy URL is never published any more")
        manifest = (standin.PKG_DIR / "manifest.json").read_text(encoding="utf-8")
        self.assertIn('"http"', manifest)


if __name__ == "__main__":
    unittest.main()
