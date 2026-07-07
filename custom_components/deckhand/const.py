"""Constants for the Deckhand integration."""

DOMAIN = "deckhand"
MANUFACTURER = "Deckhand"

# Config keys
CONF_TEAM_ID = "team_id"

# Options keys
CONF_MEDIA_PLAYER_BINDINGS = "media_player_bindings"
CONF_BINDING_DIAL = "dial_device_id"
CONF_BINDING_ENTITY = "entity_id"

# Debounce window (seconds) for auto-push media_player state changes. If
# the same entity fires again within this window with identical core
# fields (title+artist+is_playing) we skip the publish so volume/seek
# chatter doesn't spam the dial.
MEDIA_PLAYER_DEBOUNCE_S = 0.5

# MQTT topic templates — {team_id} and {dial_id} are substituted at runtime
TOPIC_STATUS = "deckhand/{team_id}/dial/+/status"
TOPIC_EVENT = "deckhand/{team_id}/dial/+/event"

# Command topic templates — {team_id} and {dial_id} are substituted
TOPIC_CMD_THEME = "deckhand/{team_id}/dial/{dial_id}/cmd/theme"
TOPIC_CMD_REBOOT = "deckhand/{team_id}/dial/{dial_id}/cmd/reboot"
TOPIC_CMD_CONFIG = "deckhand/{team_id}/dial/{dial_id}/cmd/config"
TOPIC_CMD_ANNOUNCE = "deckhand/{team_id}/dial/{dial_id}/cmd/announce"
# cmd/image — chunked RGB565 image backdrop stream. HACS publishes to
# this DIRECTLY (LAN → broker), it does NOT go through Helm's REST API,
# so the image conversion + chunking happens here in ``image_push.py``.
# Firmware parses the fast-path in deckhand-firmware/src/network.h.
TOPIC_CMD_IMAGE = "deckhand/{team_id}/dial/{dial_id}/cmd/image"
TOPIC_CMD_OVERLAY = "deckhand/{team_id}/dial/{dial_id}/cmd/overlay"
TOPIC_CMD_NOW_PLAYING = "deckhand/{team_id}/dial/{dial_id}/cmd/now_playing"
TOPIC_CMD_SENSOR_VALUE = "deckhand/{team_id}/dial/{dial_id}/cmd/sensor_value"

# sensor_watches: retained per-dial list of HA entity_ids the dial's
# current sensor face is watching. Helm + Console publish this on every
# face change; HACS subscribes and turns each entity into an
# async_track_state_change_event listener so a state change pushes
# cmd/sensor_value within a second (vs ~60s polling fallback). Empty
# ``entities`` list means "this dial isn't on a sensor face right now"
# — drop any listeners we registered for it.
TOPIC_SENSOR_WATCHES = "deckhand/{team_id}/dial/+/sensor_watches"

# Phase 6 dial-platform — face dispatch topics. {face_id} is the
# face identifier (e.g. "perimeter_pulse") chosen by the firmware
# face registry.
TOPIC_CMD_FACE_MOUNT  = "deckhand/{team_id}/dial/{dial_id}/cmd/face/{face_id}/mount"
TOPIC_CMD_FACE_STATE  = "deckhand/{team_id}/dial/{dial_id}/cmd/face/{face_id}/state"
TOPIC_CMD_FACE_CONFIG = "deckhand/{team_id}/dial/{dial_id}/cmd/face/{face_id}/config"

# Perimeter Pulse treatment names recognised by the firmware. Kept here so
# the HACS service schema can validate without the user having to re-read
# the firmware source.
PERIMETER_TREATMENTS = ("state_color", "ripple", "gradient", "flash", "sweep")

# ── Doorbell / snapshot image push (cmd/image) ──────────────────────
# Default dial resolution for image backdrops pushed from HACS. HACS
# does not reliably know each dial's hardware type, so we default to
# 360x360 — the universal FB_CAP size that renders on every board (the
# AMOLED upscales). Mirrors Helm's ``process_theme_image`` sizing and
# ``rgb565_to_chunks`` chunking (apps/themes/image_utils.py). Total
# RGB565 bytes = W*H*2 (360x360 → 259200 → 51 chunks at 5120 bytes).
IMAGE_DEFAULT_SIZE = 360
# ~5 KB raw per chunk → ~6.8 KB base64 + JSON overhead stays under the
# firmware's 8 KB inbound cap (network.h: ``if (length >= 8192) return``).
IMAGE_CHUNK_SIZE_BYTES = 5120

# Default heartbeat timeout (seconds) — mark offline/unavailable if no
# heartbeat received within this window.
HEARTBEAT_TIMEOUT = 120

# First-boot fallback theme list. Used ONLY when the team's retained
# ``deckhand/{team_id}/themes/list`` topic hasn't arrived yet (fresh
# broker, Helm/Console not connected, etc.). These slugs MUST match the
# canonical Helm seed at
# ``helm/apps/themes/management/commands/seed_system_themes.py`` —
# inventing labels here means the dial silently no-ops on theme push
# because firmware can't match the slug. Keep this list tiny and add
# new themes by relying on the dynamic ``themes/list`` topic instead.
FALLBACK_THEMES = [
    "elysian",
    "concierge",
    "nordic",
    "aegis",
    "vault",
    "quarterdeck",
    "ember",
    "ghost",
]

# Hardware type to friendly model name
HARDWARE_MODELS = {
    "crowpanel128": "CrowPanel 1.28in",
    "waveshare18": "Waveshare 1.8in Knob",
    "matouch21": "MaTouch 2.1in Rotary",
    "crowpanel21": "CrowPanel 2.1in",
    "unknown": "Deckhand Dial",
}

# Platforms to set up
PLATFORMS = [
    "sensor",
    "binary_sensor",
    "select",
    "button",
    "number",
]
