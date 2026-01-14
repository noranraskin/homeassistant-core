"""Models for Matter AutoBind client cluster entities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers.entity import EntityDescription

if TYPE_CHECKING:
    from matter_server.client.models.device_types import DeviceType
    from matter_server.client.models.node import MatterEndpoint


@dataclass
class ClientClusterEntityInfo:
    """Info discovered from Matter endpoint to create client cluster entity."""

    # MatterEndpoint to which the client clusters belong
    endpoint: MatterEndpoint

    # The home assistant platform for which an entity should be created
    platform: Platform

    # The entity description to use
    entity_description: EntityDescription

    # Entity class to use to instantiate the entity
    entity_class: type

    # Client cluster IDs this device supports
    client_clusters: list[int]


@dataclass
class ClientClusterDiscoverySchema:
    """Schema for discovering client cluster entities.

    Similar to MatterDiscoverySchema but for client clusters.
    """

    # Specify the HA platform for this schema (e.g., switch, light)
    platform: Platform

    # Platform-specific entity description
    entity_description: EntityDescription

    # Entity class to use to instantiate the entity
    entity_class: type

    # Required client cluster IDs (all must be present)
    required_client_clusters: tuple[int, ...]

    # Optional client cluster IDs (checked but not required)
    optional_client_clusters: tuple[int, ...] | None = None

    # Optional: the endpoint must contain one of these device types
    device_type: tuple[type[DeviceType], ...] | None = None
