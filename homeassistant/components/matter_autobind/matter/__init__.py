"""Matter protocol interaction module for Matter AutoBind.

This module contains low-level Matter client wrappers and protocol-specific
logic for interacting with Matter devices.

Submodules:
- adapter: Wrapper around Matter client for read/write/command operations
- cluster_map: Service-to-cluster mapping lookup table
- models: Matter-specific dataclasses
"""

from .cluster_map import ClusterMap, get_clusters_for_service, get_primary_cluster
from .models import BindingTarget, GroupTarget, NodeTarget

__all__ = [
    "BindingTarget",
    "ClusterMap",
    "GroupTarget",
    "NodeTarget",
    "get_clusters_for_service",
    "get_primary_cluster",
]
