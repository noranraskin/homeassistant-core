"""Service to Matter Cluster mapping for Matter AutoBind.

This module provides lookup functions for mapping Home Assistant
services to Matter Cluster IDs. This is essential for determining which
clusters need to be bound when creating Matter bindings.

Example:
    from .matter.cluster_map import get_clusters_for_service

    clusters = get_clusters_for_service("light.turn_on")
    # Returns: [6, 8, 768] (OnOff, LevelControl, ColorControl)
"""

from __future__ import annotations

from typing import Final

# =============================================================================
# Matter Cluster ID Constants
# =============================================================================
# See: https://csa-iot.org/developer-resource/specifications-download-request/

# Base clusters
CLUSTER_ID_IDENTIFY: Final = 0x0003  # Identify Cluster
CLUSTER_ID_GROUPS: Final = 0x0004  # Groups Cluster
CLUSTER_ID_ON_OFF: Final = 0x0006  # On/Off Cluster
CLUSTER_ID_LEVEL_CONTROL: Final = 0x0008  # Level Control Cluster
CLUSTER_ID_DESCRIPTOR: Final = 0x001D  # Descriptor Cluster
CLUSTER_ID_BINDING: Final = 0x001E  # Binding Cluster (Client devices)

# Lighting clusters
CLUSTER_ID_COLOR_CONTROL: Final = 0x0300  # Color Control Cluster

# Entry/Access control clusters
CLUSTER_ID_DOOR_LOCK: Final = 0x0101  # Door Lock Cluster
CLUSTER_ID_WINDOW_COVERING: Final = 0x0102  # Window Covering Cluster

# HVAC clusters
CLUSTER_ID_THERMOSTAT: Final = 0x0201  # Thermostat Cluster
CLUSTER_ID_FAN_CONTROL: Final = 0x0202  # Fan Control Cluster

# Pump clusters
CLUSTER_ID_PUMP_CONFIG: Final = 0x0200  # Pump Configuration and Control Cluster

# Valve clusters
CLUSTER_ID_VALVE_CONFIG: Final = 0x0081  # Valve Configuration and Control Cluster

# Media clusters
CLUSTER_ID_MEDIA_PLAYBACK: Final = 0x0506  # Media Playback Cluster
CLUSTER_ID_KEYPAD_INPUT: Final = 0x0509  # Keypad Input Cluster
CLUSTER_ID_CONTENT_LAUNCHER: Final = 0x050A  # Content Launcher Cluster
CLUSTER_ID_AUDIO_OUTPUT: Final = 0x050B  # Audio Output Cluster

# Operational State clusters
CLUSTER_ID_OPERATIONAL_STATE: Final = 0x0060  # Operational State Cluster
CLUSTER_ID_RVC_RUN_MODE: Final = 0x0054  # RVC Run Mode Cluster
CLUSTER_ID_RVC_OPERATIONAL_STATE: Final = 0x0061  # RVC Operational State Cluster


# =============================================================================
# Service to Cluster Mapping
# =============================================================================
# Maps Home Assistant services to Matter Cluster IDs for binding creation.
# NOTE: Bindings are created at the CLUSTER level, not command level.
# A binding allows a client device to send commands to a server device's cluster.
#
# For example:
# - A Dimmer Switch (client) binding to a Dimmable Light (server) on OnOff cluster
#   allows the switch to send On/Off commands to the light.
# - Adding LevelControl to that binding allows brightness control.
#
# The first cluster in each list is typically the PRIMARY cluster for that service.
# =============================================================================

_SERVICE_TO_CLUSTER_MAP: Final[dict[str, list[int]]] = {
    # -------------------------------------------------------------------------
    # Light services (targets: OnOffLight, DimmableLight, ColorLight, etc.)
    # -------------------------------------------------------------------------
    "light.turn_on": [
        CLUSTER_ID_ON_OFF,
        CLUSTER_ID_LEVEL_CONTROL,
        CLUSTER_ID_COLOR_CONTROL,
    ],
    "light.turn_off": [CLUSTER_ID_ON_OFF],
    "light.toggle": [CLUSTER_ID_ON_OFF],
    # -------------------------------------------------------------------------
    # Switch services (targets: OnOffPlugInUnit, Pump, appliances with OnOff)
    # Covers: plugs, pumps, cook surfaces, cooktops, dishwashers, etc.
    # -------------------------------------------------------------------------
    "switch.turn_on": [CLUSTER_ID_ON_OFF],
    "switch.turn_off": [CLUSTER_ID_ON_OFF],
    "switch.toggle": [CLUSTER_ID_ON_OFF],
    # -------------------------------------------------------------------------
    # Cover/Window Covering services (targets: WindowCovering device type)
    # -------------------------------------------------------------------------
    "cover.open_cover": [CLUSTER_ID_WINDOW_COVERING],
    "cover.close_cover": [CLUSTER_ID_WINDOW_COVERING],
    "cover.stop_cover": [CLUSTER_ID_WINDOW_COVERING],
    "cover.toggle": [CLUSTER_ID_WINDOW_COVERING],
    "cover.set_cover_position": [CLUSTER_ID_WINDOW_COVERING],
    "cover.set_cover_tilt_position": [CLUSTER_ID_WINDOW_COVERING],
    "cover.open_cover_tilt": [CLUSTER_ID_WINDOW_COVERING],
    "cover.close_cover_tilt": [CLUSTER_ID_WINDOW_COVERING],
    # -------------------------------------------------------------------------
    # Lock services (targets: DoorLock device type)
    # -------------------------------------------------------------------------
    "lock.lock": [CLUSTER_ID_DOOR_LOCK],
    "lock.unlock": [CLUSTER_ID_DOOR_LOCK],
    "lock.open": [CLUSTER_ID_DOOR_LOCK],
    # -------------------------------------------------------------------------
    # Fan services (targets: Fan, AirPurifier, ExtractorHood)
    # Many fan devices also support OnOff for power control
    # -------------------------------------------------------------------------
    "fan.turn_on": [CLUSTER_ID_FAN_CONTROL, CLUSTER_ID_ON_OFF],
    "fan.turn_off": [CLUSTER_ID_FAN_CONTROL, CLUSTER_ID_ON_OFF],
    "fan.toggle": [CLUSTER_ID_FAN_CONTROL, CLUSTER_ID_ON_OFF],
    "fan.set_percentage": [CLUSTER_ID_FAN_CONTROL],
    "fan.set_preset_mode": [CLUSTER_ID_FAN_CONTROL],
    "fan.oscillate": [CLUSTER_ID_FAN_CONTROL],
    "fan.set_direction": [CLUSTER_ID_FAN_CONTROL],
    "fan.increase_speed": [CLUSTER_ID_FAN_CONTROL],
    "fan.decrease_speed": [CLUSTER_ID_FAN_CONTROL],
    # -------------------------------------------------------------------------
    # Climate/Thermostat services (targets: Thermostat, RoomAirConditioner)
    # -------------------------------------------------------------------------
    "climate.turn_on": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_ON_OFF],
    "climate.turn_off": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_ON_OFF],
    "climate.toggle": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_ON_OFF],
    "climate.set_temperature": [CLUSTER_ID_THERMOSTAT],
    "climate.set_hvac_mode": [CLUSTER_ID_THERMOSTAT],
    "climate.set_fan_mode": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_FAN_CONTROL],
    "climate.set_preset_mode": [CLUSTER_ID_THERMOSTAT],
    "climate.set_humidity": [CLUSTER_ID_THERMOSTAT],
    "climate.set_swing_mode": [CLUSTER_ID_THERMOSTAT],
    # -------------------------------------------------------------------------
    # Valve services (targets: WaterValve device type)
    # -------------------------------------------------------------------------
    "valve.open_valve": [CLUSTER_ID_VALVE_CONFIG],
    "valve.close_valve": [CLUSTER_ID_VALVE_CONFIG],
    "valve.set_valve_position": [CLUSTER_ID_VALVE_CONFIG],
    # -------------------------------------------------------------------------
    # Media Player services (targets: Speaker, BasicVideoPlayer, CastingVideoPlayer)
    # Speaker uses OnOff (mute) + LevelControl (volume)
    # Video players use OnOff + MediaPlayback + KeypadInput
    # -------------------------------------------------------------------------
    "media_player.turn_on": [CLUSTER_ID_ON_OFF],
    "media_player.turn_off": [CLUSTER_ID_ON_OFF],
    "media_player.toggle": [CLUSTER_ID_ON_OFF],
    "media_player.volume_up": [CLUSTER_ID_LEVEL_CONTROL],
    "media_player.volume_down": [CLUSTER_ID_LEVEL_CONTROL],
    "media_player.volume_set": [CLUSTER_ID_LEVEL_CONTROL],
    "media_player.volume_mute": [CLUSTER_ID_ON_OFF],  # Speaker uses OnOff for mute
    "media_player.media_play": [CLUSTER_ID_MEDIA_PLAYBACK],
    "media_player.media_pause": [CLUSTER_ID_MEDIA_PLAYBACK],
    "media_player.media_stop": [CLUSTER_ID_MEDIA_PLAYBACK],
    "media_player.media_play_pause": [CLUSTER_ID_MEDIA_PLAYBACK],
    "media_player.media_next_track": [CLUSTER_ID_MEDIA_PLAYBACK],
    "media_player.media_previous_track": [CLUSTER_ID_MEDIA_PLAYBACK],
    # -------------------------------------------------------------------------
    # Humidifier services (uses OnOff for power, similar to Fan)
    # -------------------------------------------------------------------------
    "humidifier.turn_on": [CLUSTER_ID_ON_OFF],
    "humidifier.turn_off": [CLUSTER_ID_ON_OFF],
    "humidifier.toggle": [CLUSTER_ID_ON_OFF],
    # -------------------------------------------------------------------------
    # Siren services (uses OnOff for activation)
    # -------------------------------------------------------------------------
    "siren.turn_on": [CLUSTER_ID_ON_OFF],
    "siren.turn_off": [CLUSTER_ID_ON_OFF],
    "siren.toggle": [CLUSTER_ID_ON_OFF],
    # -------------------------------------------------------------------------
    # Button services (triggers via Identify cluster can be bound)
    # -------------------------------------------------------------------------
    "button.press": [CLUSTER_ID_IDENTIFY],
    # -------------------------------------------------------------------------
    # Water Heater services (targets: WaterHeater device type)
    # Uses Thermostat + OnOff
    # -------------------------------------------------------------------------
    "water_heater.turn_on": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_ON_OFF],
    "water_heater.turn_off": [CLUSTER_ID_THERMOSTAT, CLUSTER_ID_ON_OFF],
    "water_heater.set_temperature": [CLUSTER_ID_THERMOSTAT],
    "water_heater.set_operation_mode": [CLUSTER_ID_THERMOSTAT],
    # -------------------------------------------------------------------------
    # Vacuum services (targets: RoboticVacuumCleaner)
    # Uses RVC clusters for mode/state control
    # -------------------------------------------------------------------------
    "vacuum.start": [CLUSTER_ID_RVC_RUN_MODE, CLUSTER_ID_RVC_OPERATIONAL_STATE],
    "vacuum.stop": [CLUSTER_ID_RVC_RUN_MODE, CLUSTER_ID_RVC_OPERATIONAL_STATE],
    "vacuum.pause": [CLUSTER_ID_RVC_OPERATIONAL_STATE],
    "vacuum.return_to_base": [CLUSTER_ID_RVC_OPERATIONAL_STATE],
    "vacuum.locate": [CLUSTER_ID_IDENTIFY],
}


# =============================================================================
# Module-level functions
# =============================================================================


def get_clusters_for_service(service: str) -> list[int] | None:
    """Get all Matter cluster IDs required for a service call.

    Args:
        service: The service call in 'domain.service' format
                 (e.g., 'light.turn_on', 'switch.toggle')

    Returns:
        List of cluster IDs required for this service, or None if
        the service is not mapped. The first cluster in the list
        is typically the PRIMARY cluster for the service.

    Example:
        clusters = get_clusters_for_service("light.turn_on")
        # Returns [6, 8, 768]  (OnOff, LevelControl, ColorControl)
    """
    return _SERVICE_TO_CLUSTER_MAP.get(service)


def get_primary_cluster(service: str) -> int | None:
    """Get the primary Matter cluster ID for a service call.

    The primary cluster is the main cluster used for the service.
    For example, 'light.turn_on' primarily uses OnOff (0x0006).

    Args:
        service: The service call in 'domain.service' format.

    Returns:
        The primary cluster ID, or None if the service is not mapped.

    Example:
        primary = get_primary_cluster("light.turn_on")
        # Returns 6  (OnOff)
    """
    clusters = _SERVICE_TO_CLUSTER_MAP.get(service)
    return clusters[0] if clusters else None


def is_service_supported(service: str) -> bool:
    """Check if a service call is supported for Matter binding.

    Args:
        service: The service call in 'domain.service' format.

    Returns:
        True if the service can be mapped to Matter clusters.
    """
    return service in _SERVICE_TO_CLUSTER_MAP


def get_all_supported_services() -> list[str]:
    """Get all service calls that can be mapped to Matter clusters.

    Returns:
        List of all supported service names in 'domain.service' format.
    """
    return list(_SERVICE_TO_CLUSTER_MAP.keys())


def get_services_for_cluster(cluster_id: int) -> list[str]:
    """Get all services that use a specific cluster.

    This is useful for determining what services a device with
    a particular cluster can support.

    Args:
        cluster_id: The Matter cluster ID to look up.

    Returns:
        List of service names that use this cluster.
    """
    return [
        service
        for service, clusters in _SERVICE_TO_CLUSTER_MAP.items()
        if cluster_id in clusters
    ]


def get_clusters_for_device_capabilities(
    service: str,
    available_clusters: set[int],
) -> list[int]:
    """Get clusters for a service, filtered by device capabilities.

    This handles edge cases where a device might not have all
    clusters that a service could use. For example, 'light.turn_on'
    maps to OnOff, LevelControl, and ColorControl, but a simple
    OnOff light only has the OnOff cluster.

    Args:
        service: The service call in 'domain.service' format.
        available_clusters: Set of cluster IDs the device supports.

    Returns:
        List of cluster IDs the device has that are relevant to
        the service. Returns empty list if service not supported
        or device has none of the required clusters.
    """
    required = _SERVICE_TO_CLUSTER_MAP.get(service)
    if not required:
        return []

    # Return clusters that both the service needs AND the device has
    return [c for c in required if c in available_clusters]
