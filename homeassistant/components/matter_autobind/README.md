# Matter AutoBind

The **Matter AutoBind** integration automatically creates direct Matter device bindings based on your Home Assistant automations, enabling faster device-to-device communication and offline operation.

## Overview

When you create an automation in Home Assistant that uses a Matter device (like a switch) to control another Matter device (like a light), this integration:

1. **Detects** the automation and analyzes the trigger/action relationship
2. **Configures** a direct Matter binding on the devices
3. **Manages** Access Control Lists (ACLs) to allow the communication

This means your devices can communicate directly without going through Home Assistant, reducing latency and ensuring they work even when Home Assistant is offline.

## Prerequisites

- **Home Assistant** 2025.1 or later
- **Matter integration** must be set up and working
- **Matter devices** that support bindings (switches, dimmers, and remotes)

## Installation

1. Copy the `matter_autobind` folder to your `custom_components` directory
2. Add a `version` field in `manifest.json` 
3. Restart Home Assistant
4. Go to **Settings** → **Devices & Services**
5. Click **+ Add Integration**
6. Search for "Matter AutoBind"
7. Click to add it

## Configuration

After installation, the integration works automatically. You can configure options via:

1. Go to **Settings** → **Devices & Services**
2. Find **Matter AutoBind** and click **Configure**

### Options

| Option | Default | Description |
|--------|---------|-------------|
| **Create group bindings** | Off | When enabled, automations with multiple targets use Matter Groups for efficient multicast. When disabled, individual unicast bindings are created to each target. |
| **Enable debug inspector** | Off | Shows an advanced Debug Inspector panel for viewing and managing raw ACLs, bindings, groups, and keys on devices. Use with caution. |

## How It Works

### Supported Automations

The integration supports automations with:

- **Trigger**: A Matter device state change (e.g., switch pressed)
- **Action**: Controlling other Matter device(s) (e.g., turn on light)

#### Example: Simple Switch to Light

```yaml
automation:
  - alias: "Living Room Switch Controls Light"
    trigger:
      - platform: state
        entity_id: event.matter_switch_press
    action:
      - service: light.turn_on
        target:
          entity_id: light.matter_bulb
```


### Binding Types

**Unicast Bindings** (1:1)
- One switch controls one light
- Direct point-to-point communication

**Group Bindings** (1:N) - Requires "group bindings" option enabled
- One switch controls multiple lights
- Uses Matter multicast for efficient communication
- Automatically manages group keys and membership

## Viewing Bindings

### Dashboard Panel

The integration adds a **Matter AutoBind** panel to your sidebar where you can:

- See all analyzed automations and their eligibility status
- View which devices are bound together
- See ACLs, bindings, and groups created for each automation
- Change binding preferences (unicast/group/disabled) per automation

### Debug Inspector (Advanced)

When enabled in options, the Debug Inspector lets you:

- View raw ACL entries on any Matter device
- View binding table entries
- View group memberships and keys
- Manually delete entries (use with extreme caution!)

## Known Limitations

1. **Battery-powered devices**: Sleepy devices may be slow to accept binding writes. The integration will retry automatically.

2. **Complex automations**: Only simple trigger→action automations are supported. Automations with conditions, delays, or multiple triggers are not eligible.

3. **Non-Matter triggers/actions**: The trigger AND action must both be Matter devices. Mixed automations (e.g., Zigbee switch to Matter light) are not supported.

4. **Supported device types**: Currently supports switches, dimmers, lights, locks, covers, fans, and climate devices. Sensors are not supported as they don't have controllable endpoints.

## Troubleshooting

### Automation shows as "Not Eligible"

Check the automation panel for the specific reason. Common causes:
- Trigger or action entity is not a Matter device
- Automation has conditions (not supported)
- Automation has delays or complex actions

### Bindings not working

1. **Check device availability**: Ensure both devices are online in the Matter integration
2. **Check logs**: Enable debug logging for `custom_components.matter_autobind`
3. **Verify ACLs**: Use the Debug Inspector to check if ACLs were written correctly
4. **Re-create automation**: Try disabling and re-enabling the automation

### Debug Logging

Add to your `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.matter_autobind: debug
```

## Removal

1. Go to **Settings** → **Devices & Services**
2. Find **Matter AutoBind**
3. Click the three dots menu (⋮)
4. Select **Delete**

**Note**: Removing the integration does NOT remove ACLs and bindings from devices. Use the Debug Inspector to manually clean up if needed before removal.

## Support

- **Issues**: Report bugs on the GitHub repository

