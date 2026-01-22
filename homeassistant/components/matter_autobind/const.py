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

# =============================================================================
# Matter Cluster IDs
# =============================================================================
# Re-export commonly used cluster IDs from cluster_map for convenience.
# The full mapping and additional cluster IDs are in matter/cluster_map.py
from .matter.cluster_map import (  # noqa: E402
    CLUSTER_ID_BINDING,
    CLUSTER_ID_COLOR_CONTROL,
    CLUSTER_ID_DESCRIPTOR,
    CLUSTER_ID_DOOR_LOCK,
    CLUSTER_ID_FAN_CONTROL,
    CLUSTER_ID_GROUPS,
    CLUSTER_ID_IDENTIFY,
    CLUSTER_ID_LEVEL_CONTROL,
    CLUSTER_ID_ON_OFF,
    CLUSTER_ID_THERMOSTAT,
    CLUSTER_ID_WINDOW_COVERING,
)

# Explicit re-exports for public API
__all__ = [
    "AUTOBIND_GROUP_ID_MAX",
    "AUTOBIND_GROUP_ID_START",
    "CLUSTER_ID_BINDING",
    "CLUSTER_ID_COLOR_CONTROL",
    "CLUSTER_ID_DESCRIPTOR",
    "CLUSTER_ID_DOOR_LOCK",
    "CLUSTER_ID_FAN_CONTROL",
    "CLUSTER_ID_GROUPS",
    "CLUSTER_ID_IDENTIFY",
    "CLUSTER_ID_LEVEL_CONTROL",
    "CLUSTER_ID_ON_OFF",
    "CLUSTER_ID_THERMOSTAT",
    "CLUSTER_ID_WINDOW_COVERING",
    "CONF_ENABLE_GROUP_BINDINGS",
    "DEBUG_OVERWRITE_ACLS",
    "DEBUG_RESET_STORE",
    "DOMAIN",
    "EVENT_AUTOMATION_RELOADED",
    "LOGGER",
    "STORAGE_KEY",
    "STORAGE_VERSION",
]

# Group ID allocation range
# We start at 0x8000 to avoid conflicts with user-created groups
AUTOBIND_GROUP_ID_START: Final = 0x8000  # 32768
AUTOBIND_GROUP_ID_MAX: Final = 0xFFFE  # 65534 (max valid group ID)

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
