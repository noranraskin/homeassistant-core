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

# Matter Cluster IDs
# See: https://csa-iot.org/developer-resource/specifications-download-request/
CLUSTER_ID_BINDING: Final = 0x001E  # Binding Cluster (Client devices)
CLUSTER_ID_ON_OFF: Final = 0x0006  # On/Off Cluster
CLUSTER_ID_LEVEL_CONTROL: Final = 0x0008  # Level Control Cluster
CLUSTER_ID_COLOR_CONTROL: Final = 0x0300  # Color Control Cluster
CLUSTER_ID_DESCRIPTOR: Final = 0x001D  # Descriptor Cluster
CLUSTER_ID_GROUPS: Final = 0x0004  # Groups Cluster

# Service to Cluster mapping
# Maps Home Assistant services to Matter Cluster IDs
SERVICE_TO_CLUSTER_MAP: Final[dict[str, list[int]]] = {
    "light.turn_on": [CLUSTER_ID_ON_OFF, CLUSTER_ID_LEVEL_CONTROL],
    "light.turn_off": [CLUSTER_ID_ON_OFF],
    "light.toggle": [CLUSTER_ID_ON_OFF],
    "switch.turn_on": [CLUSTER_ID_ON_OFF],
    "switch.turn_off": [CLUSTER_ID_ON_OFF],
    "switch.toggle": [CLUSTER_ID_ON_OFF],
}

# Events
EVENT_AUTOMATION_RELOADED: Final = "automation_reloaded"
