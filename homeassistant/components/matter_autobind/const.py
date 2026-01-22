"""Constants for the Matter AutoBind integration."""

from __future__ import annotations

import logging
from typing import Final

# Domain
DOMAIN: Final = "matter_autobind"

# Logger
LOGGER = logging.getLogger(__package__)

# Storage
STORAGE_KEY: Final = f"{DOMAIN}.storage"
STORAGE_VERSION: Final = 1

# Configuration options
CONF_ENABLE_GROUP_BINDINGS: Final = "enable_group_bindings"

# Matter Cluster IDs
# See: https://csa-iot.org/developer-resource/specifications-download-request/
CLUSTER_ID_BINDING: Final = 0x001E  # Binding Cluster (Client devices)
CLUSTER_ID_ON_OFF: Final = 0x0006  # On/Off Cluster
CLUSTER_ID_LEVEL_CONTROL: Final = 0x0008  # Level Control Cluster
CLUSTER_ID_COLOR_CONTROL: Final = 0x0300  # Color Control Cluster
CLUSTER_ID_DESCRIPTOR: Final = 0x001D  # Descriptor Cluster
CLUSTER_ID_GROUPS: Final = 0x0004  # Groups Cluster

# Additional Matter Cluster IDs for expanded bindings
CLUSTER_ID_DOOR_LOCK: Final = 0x0101  # Door Lock Cluster
CLUSTER_ID_WINDOW_COVERING: Final = 0x0102  # Window Covering Cluster
CLUSTER_ID_THERMOSTAT: Final = 0x0201  # Thermostat Cluster
CLUSTER_ID_FAN_CONTROL: Final = 0x0202  # Fan Control Cluster
CLUSTER_ID_VALVE_CONFIG: Final = 0x0081  # Valve Configuration and Control Cluster

# Group ID allocation range
# We start at 0x8000 to avoid conflicts with user-created groups
AUTOBIND_GROUP_ID_START: Final = 0x8000  # 32768
AUTOBIND_GROUP_ID_MAX: Final = 0xFFFE  # 65534 (max valid group ID)


# Service to Cluster mapping
# Maps Home Assistant services to Matter Cluster IDs for binding creation
# NOTE: Bindings are created at the cluster level, not command level
# So we map services to the cluster(s) they interact with
SERVICE_TO_CLUSTER_MAP: Final[dict[str, list[int]]] = {
    # Light services
    "light.turn_on": [CLUSTER_ID_ON_OFF, CLUSTER_ID_LEVEL_CONTROL],
    "light.turn_off": [CLUSTER_ID_ON_OFF],
    "light.toggle": [CLUSTER_ID_ON_OFF],
    # Switch services
    "switch.turn_on": [CLUSTER_ID_ON_OFF],
    "switch.turn_off": [CLUSTER_ID_ON_OFF],
    "switch.toggle": [CLUSTER_ID_ON_OFF],
    # Cover/Window Covering services
    "cover.open_cover": [CLUSTER_ID_WINDOW_COVERING],
    "cover.close_cover": [CLUSTER_ID_WINDOW_COVERING],
    "cover.stop_cover": [CLUSTER_ID_WINDOW_COVERING],
    "cover.toggle": [CLUSTER_ID_WINDOW_COVERING],
    "cover.set_cover_position": [CLUSTER_ID_WINDOW_COVERING],
    "cover.set_cover_tilt_position": [CLUSTER_ID_WINDOW_COVERING],
    # Lock services
    "lock.lock": [CLUSTER_ID_DOOR_LOCK],
    "lock.unlock": [CLUSTER_ID_DOOR_LOCK],
    "lock.open": [CLUSTER_ID_DOOR_LOCK],
    # Fan services
    "fan.turn_on": [CLUSTER_ID_FAN_CONTROL, CLUSTER_ID_ON_OFF],
    "fan.turn_off": [CLUSTER_ID_FAN_CONTROL, CLUSTER_ID_ON_OFF],
    "fan.toggle": [CLUSTER_ID_FAN_CONTROL, CLUSTER_ID_ON_OFF],
    "fan.set_percentage": [CLUSTER_ID_FAN_CONTROL],
    "fan.set_preset_mode": [CLUSTER_ID_FAN_CONTROL],
    "fan.oscillate": [CLUSTER_ID_FAN_CONTROL],
    "fan.set_direction": [CLUSTER_ID_FAN_CONTROL],
    # Climate/Thermostat services
    "climate.turn_on": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_ON_OFF],
    "climate.turn_off": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_ON_OFF],
    "climate.toggle": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_ON_OFF],
    "climate.set_temperature": [CLUSTER_ID_THERMOSTAT],
    "climate.set_hvac_mode": [CLUSTER_ID_THERMOSTAT],
    "climate.set_fan_mode": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_FAN_CONTROL],
    "climate.set_preset_mode": [CLUSTER_ID_THERMOSTAT],
    # Valve services
    "valve.open_valve": [CLUSTER_ID_VALVE_CONFIG],
    "valve.close_valve": [CLUSTER_ID_VALVE_CONFIG],
    "valve.set_valve_position": [CLUSTER_ID_VALVE_CONFIG],
}

# Events
EVENT_AUTOMATION_RELOADED: Final = "automation_reloaded"

# =============================================================================
# Debug Flags (MANUAL USE ONLY - set back to False before production use)
# =============================================================================

# Set to True to wipe ALL store data on next startup
# This is useful for recovering from a corrupted store state
# The integration will start fresh as if never configured
DEBUG_RESET_STORE: Final = False

# Set to True to overwrite ALL ACL entries on devices
# This removes stale/duplicate ACLs and only keeps:
# 1. The controller's admin ACL (privilege 5, subject from server_info)
# 2. ACLs managed by this integration (from store)
# Use when ACLs are corrupted or hitting per-fabric entry limits
DEBUG_OVERWRITE_ACLS: Final = True
