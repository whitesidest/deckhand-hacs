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
TOPIC_CMD_OVERLAY = "deckhand/{team_id}/dial/{dial_id}/cmd/overlay"
TOPIC_CMD_NOW_PLAYING = "deckhand/{team_id}/dial/{dial_id}/cmd/now_playing"
TOPIC_CMD_SENSOR_VALUE = "deckhand/{team_id}/dial/{dial_id}/cmd/sensor_value"

# Default heartbeat timeout (seconds) — mark offline/unavailable if no
# heartbeat received within this window.
HEARTBEAT_TIMEOUT = 120

# Default theme list (built-in themes)
DEFAULT_THEMES = [
    "elysian",
    "concierge",
    "meridian",
    "shogun",
    "royale",
    "cosmos",
    "terroir",
    "nordic_lodge",
    "aegis",
    "vault_tec",
    "quarterdeck",
    "polar",
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
