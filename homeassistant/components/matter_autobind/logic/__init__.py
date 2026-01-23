"""Core business logic module for Matter AutoBind.

This module contains the high-level orchestration and reconciliation logic
for managing Matter bindings, ACLs, and groups.

Submodules:
- manager: Main orchestrator (slimmed down from original manager.py)
- reconciler: Logic to diff Desired vs Current state
- groups: Group key generation and propagation
"""

from .groups import GroupManager

__all__ = ["GroupManager"]
