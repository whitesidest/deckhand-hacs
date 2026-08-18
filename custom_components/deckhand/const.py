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
# cmd/sunset — anchors the Sundowner theme's clock-mode sun descent to a
# REAL sunset epoch (vs the theme's manual "HH:MM" fallback). Home
# Assistant is the natural source: sun.sun's next_setting attribute is an
# ISO timestamp, far easier to hand off than a lat/long. Firmware's
# INBOUND_SUNSET handler (deckhand-firmware/src/main.cpp) accepts
# {"epoch": N} (also {"sunset_at": N}); 0/absent clears to manual.
TOPIC_CMD_SUNSET = "deckhand/{team_id}/dial/{dial_id}/cmd/sunset"
TOPIC_CMD_NOW_PLAYING = "deckhand/{team_id}/dial/{dial_id}/cmd/now_playing"
# cmd/dnd — quiet-mode Do Not Disturb (helm#179 Phase 2). Wire contract
# mirrors Helm's apps/mqtt/tasks.py::push_dnd EXACTLY:
#   on  → retained {"on": true,  "source": "ha"}  (offline dials converge
#         on reconnect via broker retention)
#   off → retained {"on": false, "source": "ha"} publish so an online
#         dial hears the clear immediately, then an EMPTY retained
#         payload to the same topic (the invitation-terminal-state
#         hygiene rule) so the broker never replays a stale mode at a
#         reconnecting dial. The dial's NVS is the on-device truth and
#         treats the empty retained payload as a no-op.
TOPIC_CMD_DND = "deckhand/{team_id}/dial/{dial_id}/cmd/dnd"
TOPIC_CMD_SENSOR_VALUE = "deckhand/{team_id}/dial/{dial_id}/cmd/sensor_value"

# sensor_watches: retained per-dial list of HA entity_ids the dial's
# current sensor face is watching. Helm + Console publish this on every
# face change; HACS subscribes and turns each entity into an
# async_track_state_change_event listener so a state change pushes
# cmd/sensor_value within a second (vs ~60s polling fallback). Empty
# ``entities`` list means "this dial isn't on a sensor face right now"
# — drop any listeners we registered for it.
TOPIC_SENSOR_WATCHES = "deckhand/{team_id}/dial/+/sensor_watches"

# hacs/presence: retained per-team executor-arbitration beacon (R1). HACS
# publishes {online, ts, interval_s, version, entry_id, owns:[...]} here on
# startup and every PRESENCE_INTERVAL_S, and an empty retained payload on
# graceful unload. Helm + Console subscribe to deckhand/+/hacs/presence and
# DEFER the domains listed in ``owns`` while the beacon is fresh, so a dial
# action HACS owns (media_player today) isn't double-executed server-side.
# NOT per-dial — one beacon per team/entry.
TOPIC_HACS_PRESENCE = "deckhand/{team_id}/hacs/presence"

# Phase 6 dial-platform — face dispatch topics. {face_id} is the
# face identifier (e.g. "perimeter_pulse") chosen by the firmware
# face registry.
TOPIC_CMD_FACE_MOUNT  = "deckhand/{team_id}/dial/{dial_id}/cmd/face/{face_id}/mount"
TOPIC_CMD_FACE_STATE  = "deckhand/{team_id}/dial/{dial_id}/cmd/face/{face_id}/state"
TOPIC_CMD_FACE_CONFIG = "deckhand/{team_id}/dial/{dial_id}/cmd/face/{face_id}/config"
TOPIC_CMD_FACE_UNMOUNT = "deckhand/{team_id}/dial/{dial_id}/cmd/face/{face_id}/unmount"
# Temporary/contextual menu items — consumed by Helm's MQTT listener
# (handle_menu_request), which writes a real MenuItem on the dial's
# resolved profile and republishes the menu. SF-parity path.
TOPIC_MENU_REQUEST = "deckhand/{team_id}/dial/{dial_id}/menu_request"
# Alarm lifecycle (create/enable/disable/snooze/dismiss) — consumed by
# Helm's MQTT listener (handle_alarm_request). SF-invocable parity.
TOPIC_ALARM_REQUEST = "deckhand/{team_id}/dial/{dial_id}/alarm_request"
# Per-dial settings (clock face/format, label, timezone, haptics) — Helm
# handle_settings_request. SF SetDialSettings / REST parity.
#
# This MUST go through Helm rather than straight to cmd/config. Helm's
# Dial row is the source of truth for every one of these keys: it rebuilds
# the COMPLETE cmd/config from the row on boot, on "Push to dial", and on
# any settings save. A publish that skips the row therefore holds only
# until the next full push, then silently reverts — which is exactly what
# the old set_timezone did.
TOPIC_SETTINGS_REQUEST = "deckhand/{team_id}/dial/{dial_id}/settings_request"
# Credential lifecycle (enroll/revoke/restore) — Helm handle_credential_request.
TOPIC_CREDENTIAL_REQUEST = "deckhand/{team_id}/dial/{dial_id}/credential_request"
# Schedule lifecycle (create/enable/disable/fire) — Helm handle_schedule_request.
TOPIC_SCHEDULE_REQUEST = "deckhand/{team_id}/dial/{dial_id}/schedule_request"
# Invitation lifecycle routed through Helm (handle_invitation_request).
# Only quiet/menu invitations (helm#165) use this — Helm injects the
# MenuItem server-side and fans out per menu profile. Prompt invitations
# keep the direct cmd/face/invitation/mount publish (broker-only, no
# Helm required).
TOPIC_INVITATION_REQUEST = "deckhand/{team_id}/dial/{dial_id}/invitation_request"
# Theme selections that need SERVER-side resolution — today just the
# "random" sentinel (Helm picks a random activated theme, excluding the
# dial's current one, then pushes the concrete cmd/theme). Consumed by
# Helm's MQTT listener (handle_theme_request). Concrete slugs keep the
# direct cmd/theme publish — firmware resolves those locally.
TOPIC_THEME_REQUEST = "deckhand/{team_id}/dial/{dial_id}/theme_request"

# Theme selections the dial can't resolve locally — routed via
# TOPIC_THEME_REQUEST instead of the direct cmd/theme publish.
SERVER_RESOLVED_THEMES = ("random",)

# Perimeter Pulse treatment names recognised by the firmware. Kept here so
# the HACS service schema can validate without the user having to re-read
# the firmware source.
PERIMETER_TREATMENTS = ("state_color", "ripple", "gradient", "flash", "sweep")
# Firmware renders at most this many bindings (PP_MAX_BINDINGS in
# face_perimeter_pulse.h) — extras are silently dropped on-dial, so the
# integration truncates with a warning instead.
PERIMETER_MAX_BINDINGS = 16

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

# Per-send screen transition (helm#224). How an announcement or an
# invitation ARRIVES on the dial. Mirrors Helm's
# ``apps.utils.transitions.TRANSITION_CHOICES`` and the firmware parser
# in ``deckhand-firmware/src/screen_transition_policy.h``.
#
# "fade" is the DEFAULT and is the coalescing fade the dial has always
# used, so it is never put on the wire — an automation that does not opt
# in keeps producing byte-identical payloads. Same publisher rule the
# ``animation`` key already follows in _send_announcement.
TRANSITIONS = ("fade", "none", "iris", "dissolve")
DEFAULT_TRANSITION = "fade"

# Platforms to set up
PLATFORMS = [
    "sensor",
    "binary_sensor",
    "select",
    "button",
    "number",
]
