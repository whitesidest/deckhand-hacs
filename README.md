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
