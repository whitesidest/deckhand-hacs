"""Emoji are stripped from every service that sends text the dial draws (helm#399).

1.14.0 stripped emoji from send_announcement only. The same Home Assistant
text reaches the same dial fonts through many other services, and each one
drew a tofu box for an emoji: countdowns, overlay subtitles and home messages,
invitations (prompt and quiet), generic face mounts, perimeter rings,
now-playing, sensor values, menu items, alarm labels, the dial's own label and
scheduled announcements.

These tests run the REAL ``__init__.py`` against the Home Assistant stand-in
from test_entities_follow_heartbeats.py: the module is imported for real,
``_register_services`` registers the real handlers into a recording registry,
and only the MQTT client, target resolution and the error class are swapped.
Nested helpers (``_publish_face_mount``, ``_build_perimeter_binding``, the
``*_request`` fan-outs) therefore run as they ship.

Each test also pins the other direction: identifiers (entity ids, alarm and
schedule names, media ``sources``, ``hvac_modes``, ``on_accept`` action data)
reach the wire byte-exact, and text the dial CAN draw — arrows (firmware
0.4.122), Latin, Cyrillic, kana — is left alone.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest

import test_entities_follow_heartbeats as standin  # installs the HA stand-in

PKG = "deckhand_emoji_under_test"
TARGETS = [("DECK-AAAA", "team-1"), ("DECK-BBBB", "team-1")]
RENDERABLE = "← Back · Next → Café Привет こんにちは"


def _load_component():
    spec = importlib.util.spec_from_file_location(
        PKG, standin.PKG_DIR / "__init__.py",
        submodule_search_locations=[str(standin.PKG_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load_component()


class _SVE(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args or (kwargs.get("translation_key"),))


class _Mqtt:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def async_publish(self, hass, topic, payload, qos=0, retain=False):
        self.sent.append((topic, json.loads(payload) if payload else None))


class _Services:
    def __init__(self):
        self.handlers = {}

    def has_service(self, domain, name):
        return name in self.handlers

    def async_register(self, domain, name, handler, *args, **kwargs):
        self.handlers[name] = handler


class _States:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)


def _state(value, **attributes):
    return types.SimpleNamespace(state=value, attributes=attributes)


class _ServiceTest(unittest.TestCase):
    def setUp(self):
        self.mqtt = _Mqtt()
        self.hass = types.SimpleNamespace(
            services=_Services(),
            data={},
            states=_States({
                "sensor.pool": _state("27.5", unit_of_measurement="°C", friendly_name="Pool 🏊"),
                "sensor.wind": _state("12", unit_of_measurement="kn", friendly_name="Wind"),
                "media_player.salon": _state(
                    "playing", media_title="Summer 🌞 Song", media_artist="Band 🎸",
                    friendly_name="Salon", source_list=["TV", "Radio"],
                    supported_features=0,
                ),
            }),
        )
        patches = {
            "mqtt": self.mqtt,
            "ServiceValidationError": _SVE,
            "_resolve_targets": lambda hass, device_id: list(TARGETS),
        }
        self._saved = {k: getattr(MOD, k) for k in patches}
        for k, v in patches.items():
            setattr(MOD, k, v)
        MOD._register_services(self.hass, types.SimpleNamespace(entry_id="entry-1"))

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(MOD, k, v)

    def call(self, service, **data):
        data.setdefault("device_id", ["d1"])
        self.mqtt.sent.clear()
        asyncio.run(self.hass.services.handlers[service](types.SimpleNamespace(data=data)))
        return self.mqtt.sent

    def one(self, service, suffix, **data):
        sent = [p for t, p in self.call(service, **data) if t.endswith(suffix)]
        self.assertEqual(len(sent), len(TARGETS), f"expected one {suffix} per dial, got {sent}")
        return sent[0]


class DirectToTheDial(_ServiceTest):
    def test_countdown_strips_all_three_text_fields(self):
        p = self.one(
            "send_countdown", "/cmd/announce",
            target_datetime="2027-01-01T00:00:00+00:00",
            message="Almost 🎆 there → midnight",
            celebration_message="Happy New Year 🎉",
            from_name="Captain ⚓",
        )
        self.assertEqual(p["message"], "Almost there → midnight")
        self.assertEqual(p["celebration_message"], "Happy New Year")
        self.assertEqual(p["from"], "Captain")

    def test_an_emoji_only_celebration_is_refused_not_sent_blank(self):
        with self.assertRaises(_SVE):
            self.call("send_countdown", target_datetime="2027-01-01T00:00:00+00:00",
                      celebration_message="🎉🎉")
        self.assertEqual(self.mqtt.sent, [])

    def test_overlay_strips_subtitle_message_and_every_sensor_label(self):
        sent = self.call(
            "apply_overlay",
            home_face="message", home_message="Dinner 🍽️ at 8",
            subtitle_text="Привет 👋 →",
            sensor_entity_id="sensor.pool", sensor_label="Pool 🏊",
            sensor_marquee=[{"entity_id": "sensor.wind", "label": "Wind 💨", "unit": "kn"}],
        )
        overlay = [p for t, p in sent if t.endswith("/cmd/overlay")][0]
        self.assertEqual(overlay["home_message"], "Dinner at 8")
        self.assertEqual(overlay["subtitle_text"], "Привет →")
        self.assertEqual(overlay["subtitle_mode"], "custom")
        self.assertEqual(overlay["sensors"]["quad"],
                         [{"slot": 1, "entity_id": "sensor.pool", "label": "Pool"}])
        self.assertEqual(overlay["sensors"]["marquee"],
                         [{"entity_id": "sensor.wind", "label": "Wind", "unit": "kn"}])
        # The sensor-value warm-up pushes reuse the stripped labels.
        labels = {p["entity_id"]: p["label"] for t, p in sent if t.endswith("/cmd/sensor_value")}
        self.assertEqual(labels, {"sensor.pool": "Pool", "sensor.wind": "Wind"})

    def test_an_emoji_only_subtitle_does_not_force_custom_mode(self):
        sent = self.call("apply_overlay", subtitle_text="🌅", brightness=50)
        overlay = [p for t, p in sent if t.endswith("/cmd/overlay")][0]
        self.assertNotIn("subtitle_mode", overlay)  # the dial keeps its subtitle
        self.assertEqual(overlay["brightness"], 50)

    def test_invitation_prompt_strips_text_and_keeps_on_accept_exact(self):
        p = self.one(
            "send_invitation", "/cmd/face/invitation/mount",
            text="Sunset cruise 🛥️ → 6pm?", subtitle="さくら 🌸",
            accept_label="Hold ✋ to join", decline_label="Spin 🔄 to pass",
            from_name="Zoë 😊",
            # Opaque action data the dial echoes back — and it uses a key
            # ("text") that IS a prompt key, so only the never-walk rule
            # protects it.
            on_accept={"type": "push_message", "data": {"text": "Guest joined 🎉"}},
        )
        self.assertEqual(p["text"], "Sunset cruise → 6pm?")
        self.assertEqual(p["subtitle"], "さくら")
        self.assertEqual(p["accept_label"], "Hold to join")
        self.assertEqual(p["decline_label"], "Spin to pass")
        self.assertEqual(p["from_name"], "Zoë")
        self.assertEqual(p["on_accept"], {"type": "push_message", "data": {"text": "Guest joined 🎉"}})

    def test_an_emoji_only_invitation_is_refused(self):
        with self.assertRaises(_SVE):
            self.call("send_invitation", text="🛥️")
        self.assertEqual(self.mqtt.sent, [])

    def test_mount_face_strips_display_keys_and_leaves_identifiers(self):
        p = self.one(
            "mount_face", "/cmd/face/climate/mount",
            face_id="climate",
            payload={"entity_id": "climate.salon", "title": "Salon 🌡️",
                     "subtitle_text": "Cozy 🔥", "hvac_modes": ["heat 🔥", "cool"]},
        )
        self.assertEqual((p["title"], p["subtitle_text"]), ("Salon", "Cozy"))
        self.assertEqual(p["entity_id"], "climate.salon")
        self.assertEqual(p["hvac_modes"], ["heat 🔥", "cool"])

    def test_mount_face_never_rewrites_names_ha_selects_by(self):
        """A media source or art entry is selected in HA BY its label, so it
        must reach the dial (and come back) byte-exact."""
        p = self.one(
            "mount_face", "/cmd/face/volume/mount", face_id="volume",
            payload={"label": "Salon 🔊",
                     "sources": [{"id": "📺 Netflix", "label": "📺 Netflix"}],
                     "scenes": [{"entity_id": "scene.movie", "label": "Movie 🍿"}]},
        )
        self.assertEqual(p["label"], "Salon")  # positive control
        self.assertEqual(p["sources"], [{"id": "📺 Netflix", "label": "📺 Netflix"}])
        self.assertEqual(p["scenes"], [{"entity_id": "scene.movie", "label": "Movie 🍿"}])

    def test_mount_face_charge_and_message(self):
        charge = self.one("mount_face", "/cmd/face/charge/mount", face_id="charge",
                          payload={"mode": "ev", "label": "Rivian 🚙", "range": "212 mi 🔋",
                                   "battery_pct": 62})
        self.assertEqual((charge["label"], charge["range"], charge["battery_pct"]),
                         ("Rivian", "212 mi", 62))
        msg = self.one("mount_face", "/cmd/face/message/mount", face_id="message",
                       payload={"text": "Back soon 🙂 →"})
        self.assertEqual(msg["text"], "Back soon →")

    def test_perimeter_friendly_names_are_stripped_and_states_are_not(self):
        p = self.one(
            "mount_perimeter_pulse", "/cmd/face/perimeter_pulse/mount",
            bindings=[{"id": "gate", "friendly_name": "Gate 🚪", "state": "open",
                       "active_state": "open"}],
        )
        [b] = p["bindings"]
        self.assertEqual((b["id"], b["friendly_name"], b["state"], b["active_state"]),
                         ("gate", "Gate", "open", "open"))

    def test_update_now_playing_strips_title_and_artist(self):
        p = self.one("update_now_playing", "/cmd/now_playing",
                     title="Summer 🌞 Song", artist="Band 🎸", source="Salon")
        self.assertEqual((p["title"], p["artist"], p["source"]), ("Summer Song", "Band", "Salon"))

    def test_update_from_media_player_strips_title_and_artist(self):
        p = self.one("update_from_media_player", "/cmd/now_playing",
                     entity_id="media_player.salon")
        self.assertNotIn("🌞", p["title"])
        self.assertIn("Summer", p["title"])  # positive control: it IS the track
        self.assertEqual(p["entity_id"], "media_player.salon")

    def test_update_sensor_value_strips_label_and_value(self):
        p = self.one("update_sensor_value", "/cmd/sensor_value",
                     entity_id="sensor.sky", label="Sky ☁️", value="Sunny ☀️")
        self.assertEqual((p["entity_id"], p["label"], p["value"]), ("sensor.sky", "Sky", "Sunny"))


class ThroughHelmsRequestPlane(_ServiceTest):
    """These go to Helm, which stores the text and pushes it to the dial."""

    def test_menu_item_label_is_stripped(self):
        p = self.one("add_menu_item", "/menu_request", key="towels", label="Fresh towels 🧺 →")
        self.assertEqual((p["key"], p["label"]), ("towels", "Fresh towels →"))

    def test_an_emoji_only_menu_label_is_refused(self):
        with self.assertRaises(_SVE):
            self.call("add_menu_item", key="coffee", label="☕")
        self.assertEqual(self.mqtt.sent, [])

    def test_quiet_invitation_text_is_stripped(self):
        p = self.one("send_invitation", "/invitation_request", presentation="menu",
                     text="Cruise at 6 🛥️", subtitle="Aft deck 🌅", from_name="Crew 🧑‍✈️")
        self.assertEqual((p["text"], p["subtitle"], p["from_name"]),
                         ("Cruise at 6", "Aft deck", "Crew"))

    def test_alarm_label_is_stripped_and_its_name_is_not(self):
        p = self.one("create_alarm", "/alarm_request",
                     name="Wake 🌞 Tyler", time="06:45", label="Rise ☀️ and shine")
        self.assertEqual(p["label"], "Rise and shine")
        self.assertEqual(p["name"], "Wake 🌞 Tyler", "the name is the enable/disable handle")

    def test_schedule_announcement_is_stripped_and_its_name_is_not(self):
        p = self.one("create_schedule", "/schedule_request", name="Dinner 🍽️",
                     announcement_message="Dinner 🍽️ is served", announcement_from="Chef 👨‍🍳")
        self.assertEqual((p["announcement_message"], p["announcement_from"]),
                         ("Dinner is served", "Chef"))
        self.assertEqual(p["name"], "Dinner 🍽️")

    def test_dial_label_is_stripped(self):
        p = self.one("set_dial_settings", "/settings_request", label="Salon ⚓")
        self.assertEqual(p["label"], "Salon")


class NothingRenderableIsTouched(_ServiceTest):
    def test_renderable_text_survives_every_service(self):
        cases = [
            ("send_countdown", "/cmd/announce", "celebration_message",
             {"target_datetime": "2027-01-01T00:00:00+00:00", "celebration_message": RENDERABLE}),
            ("apply_overlay", "/cmd/overlay", "subtitle_text", {"subtitle_text": RENDERABLE}),
            ("send_invitation", "/cmd/face/invitation/mount", "text", {"text": RENDERABLE}),
            ("update_now_playing", "/cmd/now_playing", "title", {"title": RENDERABLE}),
            ("add_menu_item", "/menu_request", "label", {"key": "k", "label": RENDERABLE}),
        ]
        for service, suffix, key, data in cases:
            with self.subTest(service=service):
                self.assertEqual(self.one(service, suffix, **data)[key], RENDERABLE)


if __name__ == "__main__":
    unittest.main()
