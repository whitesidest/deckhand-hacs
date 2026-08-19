# Deckhand Smart Dial - Home Assistant Integration

HACS-compatible custom integration that makes Deckhand smart dials first-class Home Assistant citizens. Dials are auto-discovered via MQTT and appear as full HA devices with sensors, controls, and services.

## Requirements

- Home Assistant 2024.1.0+
- HA MQTT integration configured and connected to the same broker as your Deckhand dials
- Deckhand dials running firmware v0.3.0+

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** > three-dot menu > **Custom repositories**
3. Add `https://github.com/neomind/deckhand-hacs` with category **Integration**
4. Search for "Deckhand" and install
5. Restart Home Assistant

### Manual

Copy the `custom_components/deckhand/` folder into your HA `config/custom_components/` directory and restart.

## Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for "Deckhand Smart Dial"
3. Enter your **Team ID**:
   - **Helm (cloud / multi-tenant):** the team id shown in Helm settings
   - **Console (self-hosted, single-tenant):** enter `local` — Console mirrors
     Helm's topic shape under the synthetic team id `local` so HACS can
     discover Console dials. Already-deployed Console dials on the legacy
     flat topic shape (`deckhand/<id>/...`) are not discovered; re-register
     them against Console (factory reset + auto-register) to promote them
     onto the team-prefixed topics.
4. Dials are auto-discovered as they publish heartbeats via MQTT

## Entities

Each Deckhand dial creates the following entities:

### Sensors
| Entity | Description |
|--------|-------------|
| Battery | Battery percentage (if battery-powered) |
| WiFi Signal | RSSI in dBm |
| Temperature | Ambient temperature in Celsius (if SHT40 sensor present) |
| Humidity | Relative humidity (if SHT40 sensor present) |
| Ambient Light | Illuminance in lux (if APDS-9960 sensor present) |
| Theme | Current active theme name |

### Binary Sensors
| Entity | Description |
|--------|-------------|
| Connectivity | Online/offline based on MQTT heartbeat |

### Controls
| Entity | Description |
|--------|-------------|
| Theme (select) | Dropdown to switch the dial's visual theme |
| Brightness (number) | Display brightness slider (0-255) |
| Reboot (button) | Restart the dial |

## Services

### `deckhand.push_theme`
Push a theme to a specific dial.

```yaml
service: deckhand.push_theme
data:
  device_id: <ha_device_id>
  theme: "cosmos"
```

### `deckhand.send_announcement`
Send an announcement message to a dial's display.

```yaml
service: deckhand.send_announcement
data:
  device_id: <ha_device_id>
  message: "Dinner is ready!"
  from_name: "Kitchen"
  duration: 30
```

### `deckhand.reboot`
Reboot a dial.

```yaml
service: deckhand.reboot
data:
  device_id: <ha_device_id>
```

## Events

The integration fires `deckhand_dial_event` on the HA event bus when a dial button is pressed, encoder is rotated, or a menu item is selected. Use these in automations:

```yaml
automation:
  - alias: "Deckhand button press"
    trigger:
      - platform: event
        event_type: deckhand_dial_event
        event_data:
          dial_id: "DECK-3AC0"
          type: "button_press"
    action:
      - service: light.toggle
        target:
          entity_id: light.living_room
```

## Waking a cold speaker from the Audio face

Opening the Audio face while nothing is playing used to hand the operator
a screen they could not use. Every control on it targeted a media_player
that silently no-ops while the speaker is off — Home Assistant answers the
service call, the amp stays dark, and there is no way to tell that from a
broken integration.

So a **cold** player — one with no `media_title` loaded, as opposed to one
merely paused mid-track — draws a single **Turn On** control instead of the
transport row. Pressing it publishes an ordinary dial event carrying an
action string, and an automation you own decides what "on" means for that
room. The default action is `media_wake`.

Like an NFC tap, this arrives on the HA bus by up to two paths with
**different shapes**, so pick the one your setup actually has:

**Helm-relayed (flat).** If the dials are managed by Helm, it relays the
event with the fields at the top level — filterable directly in
`event_data`:

```yaml
automation:
  - alias: "Deckhand: wake the office speakers"
    trigger:
      - platform: event
        event_type: deckhand_dial_event
        event_data:
          event_type: "button_press"
          action: "media_wake"
          dial_id: "DECK-3140"
    action:
      - service: switch.turn_on
        target: {entity_id: switch.office_amp}
      - service: media_player.select_source
        target: {entity_id: media_player.office}
        data: {source: "Spotify"}
      - service: media_player.media_play
        target: {entity_id: media_player.office}
```

**This integration's raw re-fire (nested).** Fired straight off MQTT, so
the dial's own envelope is preserved under `payload` and `type` replaces
`event_type`. Trigger on the outer keys and branch in a condition:

```yaml
automation:
  - alias: "Deckhand: wake the speakers (MQTT-direct)"
    trigger:
      - platform: event
        event_type: deckhand_dial_event
        event_data:
          type: "button_press"
    condition:
      - "{{ trigger.event.data.payload.action == 'media_wake' }}"
    action:
      - service: media_player.turn_on
        target: {entity_id: "{{ trigger.event.data.payload.item_id }}" }
```

Both carry `item_id` — the media_player entity the face was showing — so an
automation can key off the player instead of the dial where that reads
better. Routing by `dial_id` lets one automation serve every room.

`media_wake` is the default. Helm can override it per menu item
("Turn-On Action" on an `ha_media` item) when one room needs its own
automation; dials driven from this integration alone always send the
default, so wiring that single action covers them.

The dial deliberately does **not** claim the music started. It publishes
the action, buzzes, and waits for the next `cmd/now_playing` to say so — a
wake that fails looks like a wake that failed, rather than a face that
flickers into "now playing" and back.

Where the player also exposes selectable inputs, the cold face keeps the
source picker on a swipe up, so the operator can jump straight to an input
instead of firing the automation.

## NFC taps in automations

When a card is tapped on a dial's NFC reader, a `deckhand_dial_event`
lands on the HA bus **twice**, via two independent paths:

1. **Helm-enriched relay (canonical).** Helm resolves the card against
   its credential store, then relays the enriched event to HA. This is
   the event to use for anything identity-aware — it is the only one
   that knows *who* tapped.
2. **Raw MQTT re-fire.** This integration also re-fires the dial's raw
   MQTT event directly. It carries no identity — fine for "any tap
   happened" automations, but it cannot branch on identity.

**How to tell them apart:** the enriched payload carries an `action`
key; the raw one does not. Blueprints and automations that match
`event_data: { event_type: nfc_tap, action: ... }` will therefore only
ever fire on the enriched event — no double-trigger.

### Enriched event contract

`event_type: deckhand_dial_event`, with `data`:

| Key | Value |
|---|---|
| `dial_id` | which dial was tapped |
| `team_slug` | your team |
| `event_type` | `"nfc_tap"` — branch on this |
| `action` | `"known"` \| `"unknown"` \| `"revoked"` — branch on this |
| `item_id` | identity id, or `""` if unknown |
| `item_label` | identity display name, or `"unknown"` |
| `role` | role name, or `""` |
| `credential_id` | stable credential id, or `""` |

`uid_hash` is **never** present on the HA relay — it is a stable
pseudo-identifier (even for unknown cards) and stays in Helm for
enrollment and audit only.

**Rate limit:** the enriched relay is limited to **12 taps/min per
dial** and **60 taps/min per team**. Taps beyond the limit are not
relayed to HA (a spoofed-card storm can't hammer your instance), but
Helm's audit log records every tap regardless.

### Shipped blueprints

Ready-made automations live in
[`blueprints/automation/deckhand/`](blueprints/automation/deckhand/):

- `nfc_known_tap_scene.yaml` — known tap (optionally filtered by role) runs a scene
- `nfc_unknown_tap_alert.yaml` — unknown tap during a time window sends a notification
- `nfc_revoked_tap_alert.yaml` — revoked-card tap always sends a notification

### Example: known tap by a Housekeeper runs a scene

```yaml
automation:
  - alias: "Housekeeper tap starts service mode"
    trigger:
      - platform: event
        event_type: deckhand_dial_event
        event_data:
          event_type: "nfc_tap"
          action: "known"
          role: "Housekeeper"
    action:
      - service: scene.turn_on
        target:
          entity_id: scene.in_service
```

A tap is *identification*, not authentication of intent — wire taps to
signals and scenes, and keep anything irreversible behind an HA-side
confirmation you own.

## Example Automation

Push a "night mode" theme to all dials at sunset:

```yaml
automation:
  - alias: "Sunset theme push"
    trigger:
      - platform: sun
        event: sunset
    action:
      - service: deckhand.push_theme
        data:
          device_id: <ha_device_id>
          theme: "ghost"
```

## Streaming updates: Now Playing + Sensor Value

Two lightweight streaming services let you push data to the dial's home
face without re-sending the full theme. Both are **ephemeral** — the dial
forgets them on reboot.

### Now Playing

Swap the home face to a track-now-playing view whenever your media player
state changes:

```yaml
automation:
  - alias: "Mirror Spotify on kitchen dial"
    trigger:
      - platform: state
        entity_id: media_player.spotify
        attribute: media_title
    action:
      - service: deckhand.update_now_playing
        data:
          device_id: <ha_device_id>
          title: "{{ state_attr('media_player.spotify', 'media_title') }}"
          artist: "{{ state_attr('media_player.spotify', 'media_artist') }}"
          album_art_url: "{{ state_attr('media_player.spotify', 'entity_picture') }}"
          source: "Spotify"
          is_playing: "{{ is_state('media_player.spotify', 'playing') }}"
```

Leave `title` blank to revert the dial to its theme-default home face
(e.g. when the track ends / player goes idle).

### Automatic now-playing push

Skip the template glue: call `deckhand.update_from_media_player` with a
dial `device_id` + `media_player` `entity_id` to mirror that player's
current track / episode / movie poster to the dial in one shot. For
hands-off auto-push, open the Deckhand integration's **Configure**
dialog and add one or more dial &harr; media_player bindings — state
changes are streamed to the dial automatically (debounced so volume /
seek chatter doesn't spam the bus).

### Sensor Value

Feed a live reading into the dial's sensor face. Refreshes in place
without flicker, so it's safe to fire many times per minute:

```yaml
automation:
  - alias: "Temperature on dial"
    trigger:
      - platform: state
        entity_id: sensor.living_room_temp
    action:
      - service: deckhand.update_sensor_value
        data:
          device_id: <ha_device_id>
          entity_id: sensor.living_room_temp
          label: "Living Room"
          value: "{{ states('sensor.living_room_temp') }}"
          unit: "°F"
          color: "#FFD700"
```

Tip: wrap the service call behind a trigger that only fires on real
change (`platform: state` + throttling) rather than on every sensor
update — the dial will render either way, but network traffic adds up
if you poll rapidly.
