"""Core business logic module for Matter AutoBind.

This module contains the high-level orchestration and reconciliation logic
for managing Matter bindings, ACLs, and groups.

Submodules:
- reconciler: Logic to diff Desired vs Current state
- groups: Group key generation and propagation
"""

from .groups import GroupManager
from .reconciler import DesiredResourceState, NodeInfo, ResourceReconciler

__all__ = [
    "DesiredResourceState",
    "GroupManager",
    "NodeInfo",
    "ResourceReconciler",
]
