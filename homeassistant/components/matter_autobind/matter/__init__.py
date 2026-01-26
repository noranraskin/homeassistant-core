"""Matter protocol interaction module for Matter AutoBind.

This module contains low-level Matter client wrappers and protocol-specific
logic for interacting with Matter devices.

Submodules:
- adapter: Wrapper around Matter client for read/write/command operations
- cluster_map: Service-to-cluster mapping lookup functions
- models: Matter-specific dataclasses
"""

from .adapter import MatterAdapter
from .cluster_map import get_clusters_for_service, get_primary_cluster
from .models import (
    AclEntry,
    BindingTarget,
    GroupInfo,
    GroupMember,
    GroupTarget,
    NodeTarget,
    StatefulSwitchInfo,
)

__all__ = [
    "AclEntry",
    "BindingTarget",
    "GroupInfo",
    "GroupMember",
    "GroupTarget",
    "MatterAdapter",
    "NodeTarget",
    "StatefulSwitchInfo",
    "get_clusters_for_service",
    "get_primary_cluster",
]
