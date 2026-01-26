"""Shared utility functions for Matter AutoBind.

This module provides common helper functions used across the integration,
eliminating code duplication in manager.py, analyzer.py, and other modules.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.matter.helpers import (  # pylint: disable=hass-component-root-import
        get_matter as _get_matter,
        get_node_from_device_entry as _get_node_from_device_entry,
    )
else:
    from homeassistant.components.matter.helpers import (  # pylint: disable=hass-component-root-import
        get_matter as _get_matter,
        get_node_from_device_entry as _get_node_from_device_entry,
    )


# Matter domain constant
MATTER_DOMAIN = "matter"


# =============================================================================
# Matter Client Access
# =============================================================================


def get_matter(hass: HomeAssistant) -> Any:
    """Get the Matter integration entry point.

    Args:
        hass: Home Assistant instance.

    Returns:
        The Matter integration object.

    Raises:
        KeyError or StopIteration: If Matter integration is not available.
    """
    return _get_matter(hass)


def get_matter_client_safe(hass: HomeAssistant) -> Any | None:
    """Get the Matter client safely, returning None if unavailable.

    This is a convenience wrapper that catches common exceptions
    when the Matter integration is not available.

    Args:
        hass: Home Assistant instance.

    Returns:
        The Matter client or None if unavailable.
    """
    try:
        matter = get_matter(hass)
    except (KeyError, StopIteration, RuntimeError, AttributeError):
        return None
    else:
        return matter.matter_client


def get_node_from_device_entry(hass: HomeAssistant, device_entry: Any) -> Any:
    """Get the Matter node from a device entry.

    Args:
        hass: Home Assistant instance.
        device_entry: The device registry entry.

    Returns:
        The MatterNode for the device, or None if not found.
    """
    return _get_node_from_device_entry(hass, device_entry)


# =============================================================================
# Node Context - Unified Entity/Device/Node Information
# =============================================================================


@dataclass
class NodeContext:
    """Unified context information for a Matter entity.

    This dataclass provides all relevant information about an entity's
    relationship to the Matter node, device registry, and entity registry.
    """

    entity_id: str
    """The entity_id being looked up."""

    entity_entry: er.RegistryEntry | None
    """The entity registry entry, or None if not found."""

    device_entry: dr.DeviceEntry | None
    """The device registry entry, or None if not found."""

    node: Any | None
    """The MatterNode object, or None if not found."""

    node_id: int | None
    """The Matter node ID, or None if not found."""

    endpoint_id: int
    """The endpoint ID (defaults to 1 for most devices)."""

    @property
    def is_valid(self) -> bool:
        """Check if this context has valid node information."""
        return self.node is not None and self.node_id is not None


def get_node_context(
    hass: HomeAssistant,
    entity_id: str,
    entity_registry: er.EntityRegistry | None = None,
    device_registry: dr.DeviceRegistry | None = None,
) -> NodeContext:
    """Get unified context information for a Matter entity.

    This is the primary helper for looking up entity/device/node relationships.
    It consolidates the logic previously spread across multiple methods.

    Args:
        hass: Home Assistant instance.
        entity_id: The entity_id to look up.
        entity_registry: Optional entity registry (will be fetched if not provided).
        device_registry: Optional device registry (will be fetched if not provided).

    Returns:
        NodeContext with all available information.
    """
    # Get registries if not provided
    if entity_registry is None:
        entity_registry = er.async_get(hass)
    if device_registry is None:
        device_registry = dr.async_get(hass)

    # Start with empty context
    context = NodeContext(
        entity_id=entity_id,
        entity_entry=None,
        device_entry=None,
        node=None,
        node_id=None,
        endpoint_id=1,  # Default endpoint
    )

    # Look up entity entry
    entity_entry = entity_registry.async_get(entity_id)
    if entity_entry is None:
        return context
    context.entity_entry = entity_entry

    # Look up device entry
    if entity_entry.device_id is None:
        return context
    device_entry = device_registry.async_get(entity_entry.device_id)
    if device_entry is None:
        return context
    context.device_entry = device_entry

    # Check Matter is available
    try:
        get_matter(hass)
    except (KeyError, StopIteration):
        # Try to at least get node_id from device identifiers
        context.node_id = get_node_id_from_device(device_entry)
        return context

    # Get Matter node
    node = get_node_from_device_entry(hass, device_entry)
    if node is not None:
        context.node = node
        context.node_id = node.node_id
    else:
        # Fallback: try to get node_id from device identifiers
        context.node_id = get_node_id_from_device(device_entry)

    return context


def get_node_id_from_device(device: dr.DeviceEntry) -> int | None:
    """Extract Matter node ID from device identifiers.

    Args:
        device: The device registry entry.

    Returns:
        The node ID or None if not found.
    """
    for domain, identifier in device.identifiers:
        if domain == MATTER_DOMAIN:
            # Try to parse as simple integer first
            if identifier.isdigit():
                return int(identifier)

            # Try direct integer conversion
            with contextlib.suppress(ValueError):
                return int(identifier)

            # Try to parse "deviceid_FABRIC-NODEID-MatterNodeDevice" format
            if identifier.startswith("deviceid_"):
                try:
                    parts = identifier.split("-")
                    if len(parts) >= 2:
                        return int(parts[1], 16)
                except (ValueError, IndexError):
                    pass
    return None


# =============================================================================
# Entity Helpers
# =============================================================================


@callback
def is_matter_entity(
    entity_id: str,
    entity_registry: er.EntityRegistry,
) -> bool:
    """Check if an entity belongs to the Matter or Matter AutoBind integration.

    Args:
        entity_id: The entity_id to check.
        entity_registry: The entity registry instance.

    Returns:
        True if the entity is from the Matter or Matter AutoBind integration.
    """
    entity_entry = entity_registry.async_get(entity_id)
    if entity_entry is None:
        return False

    # Accept entities from both the official matter integration
    # and our matter_autobind integration (for client cluster entities)
    return entity_entry.platform in (MATTER_DOMAIN, DOMAIN)


@callback
def get_matter_entities_for_device(
    device_id: str,
    entity_registry: er.EntityRegistry,
) -> list[str]:
    """Get all Matter or Matter AutoBind entities for a device.

    Args:
        device_id: The device_id to look up.
        entity_registry: The entity registry instance.

    Returns:
        List of Matter/Matter AutoBind entity_ids for the device.
    """
    return [
        entity.entity_id
        for entity in er.async_entries_for_device(
            entity_registry, device_id, include_disabled_entities=False
        )
        if entity.platform in (MATTER_DOMAIN, DOMAIN)
    ]


# =============================================================================
# Serialization Helpers
# =============================================================================


def serialize_cluster_data(
    data: Any,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any:
    """Serialize cluster data to JSON-safe format.

    This function handles Matter cluster data structures which may contain
    circular references, custom types (Nullable, enums), and bytes.

    Args:
        data: The data to serialize.
        depth: Current recursion depth (to prevent infinite recursion).
        seen: Set of object IDs already visited (to detect cycles).

    Returns:
        JSON-serializable representation of the data.
    """
    # Prevent infinite recursion
    if depth > 10:
        return f"<max depth exceeded: {type(data).__name__}>"

    if seen is None:
        seen = set()

    if data is None:
        return None

    # Handle chip.clusters.Types.Nullable (Matter's null type)
    type_name = type(data).__name__
    if type_name == "Nullable" or "Null" in type_name:
        return None

    # Handle NullValue singleton
    if str(data) == "Null" or repr(data).startswith("Null"):
        return None

    # Check for circular references using object id
    obj_id = id(data)
    if obj_id in seen:
        return f"<circular ref: {type_name}>"
    seen.add(obj_id)

    try:
        if isinstance(data, list):
            return [serialize_cluster_data(item, depth + 1, seen) for item in data]
        if isinstance(data, dict):
            return {
                str(k): serialize_cluster_data(v, depth + 1, seen)
                for k, v in data.items()
            }
        if isinstance(data, (int, float, str, bool)):
            return data
        if isinstance(data, bytes):
            return data.hex()
        # Handle enums
        if hasattr(data, "value") and hasattr(data, "name"):
            return data.value
        if hasattr(data, "__dict__"):
            # Convert chip cluster objects to dicts
            # Skip private attributes and known problematic ones
            result = {}
            for k, v in vars(data).items():
                # Skip private attributes and known circular reference fields
                if k.startswith("_"):
                    continue
                if k in ("endpoint", "node", "parent", "cluster"):
                    continue
                result[k] = serialize_cluster_data(v, depth + 1, seen)
            return result
        # For other types, convert to string
        return str(data)
    finally:
        # Remove from seen set when done with this branch
        seen.discard(obj_id)
