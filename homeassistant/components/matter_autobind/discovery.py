"""Discovery for Matter client cluster entities."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

from chip.clusters import Objects as Clusters
from matter_server.client.models import device_types

from homeassistant.const import Platform

from .const import CLUSTER_ID_LEVEL_CONTROL, CLUSTER_ID_ON_OFF, LOGGER
from .models import ClientClusterDiscoverySchema, ClientClusterEntityInfo

if TYPE_CHECKING:
    from matter_server.client.models.node import MatterEndpoint

    from homeassistant.helpers import entity_registry as er

from .light import ClientClusterLight, ClientLightEntityDescription
from .switch import ClientClusterSwitch, ClientSwitchEntityDescription

# Client device types - devices that send commands rather than receive them
# These have the Binding cluster and client-side clusters
CLIENT_DEVICE_TYPES: tuple[type, ...] = (
    device_types.OnOffLightSwitch,
    device_types.DimmerSwitch,
    device_types.ColorDimmerSwitch,
)


def _get_discovery_schemas() -> list[ClientClusterDiscoverySchema]:
    """Return discovery schemas for client cluster entities.

    Import here to avoid circular imports.
    """

    return [
        # OnOff Light Switch -> Switch entity
        ClientClusterDiscoverySchema(
            platform=Platform.SWITCH,
            entity_description=ClientSwitchEntityDescription(
                key="client_onoff",
                name=None,  # Use device name
            ),
            entity_class=ClientClusterSwitch,
            required_client_clusters=(CLUSTER_ID_ON_OFF,),
            device_type=(device_types.OnOffLightSwitch,),
        ),
        # Dimmer Switch -> Light entity
        ClientClusterDiscoverySchema(
            platform=Platform.LIGHT,
            entity_description=ClientLightEntityDescription(
                key="client_dimmable",
                name=None,  # Use device name
            ),
            entity_class=ClientClusterLight,
            required_client_clusters=(CLUSTER_ID_ON_OFF, CLUSTER_ID_LEVEL_CONTROL),
            device_type=(device_types.DimmerSwitch, device_types.ColorDimmerSwitch),
        ),
    ]


def endpoint_has_server_and_client_clusters(
    endpoint: MatterEndpoint,
    cluster_ids: tuple[int, ...],
) -> bool:
    """Check if endpoint has BOTH server and client for given cluster IDs.

    This identifies "stateful switches" - devices that have both server and client
    clusters for the same functionality (e.g., OnOff server + OnOff client).
    These devices already get entities from the official Matter integration
    via their server clusters, so we don't need to create additional entities.

    Args:
        endpoint: The Matter endpoint to check.
        cluster_ids: Tuple of cluster IDs to check (e.g., OnOff, LevelControl).

    Returns:
        True if ANY of the cluster_ids appear in BOTH serverList and clientList.
    """
    descriptor = endpoint.get_cluster(Clusters.Descriptor)
    if descriptor is None:
        return False

    server_list = set(descriptor.serverList or [])
    client_list = set(descriptor.clientList or [])

    # Check if any of the required clusters appear in BOTH lists
    for cluster_id in cluster_ids:
        if cluster_id in server_list and cluster_id in client_list:
            LOGGER.debug(
                "Endpoint %d has cluster 0x%04X in both server and client lists "
                "(stateful switch)",
                endpoint.endpoint_id,
                cluster_id,
            )
            return True

    return False


def endpoint_has_existing_entity(
    endpoint: MatterEndpoint,
    entity_registry: er.EntityRegistry,
) -> bool:
    """Check if an endpoint already has a suitable entity from the matter integration.

    Returns True if the endpoint already has a switch or light entity,
    meaning we don't need to create a client cluster entity for it.
    """
    # Look for existing entities for this device
    # The matter integration uses a specific unique_id format
    node_id = endpoint.node.node_id
    endpoint_id = endpoint.endpoint_id

    # Check all entities in registry for matter platform
    for entity in entity_registry.entities.values():
        if entity.platform != "matter":
            continue

        # Matter entities have unique_id format:
        # {fabric_id}-{node_id}-{endpoint_id}-{key}-{cluster_id}-{attribute_id}
        # We check if the unique_id contains our node and endpoint
        unique_id = entity.unique_id
        if unique_id and f"-{node_id}-" in unique_id:
            # Check if it's for our endpoint
            # The endpoint_id is the third part after splitting by "-"
            parts = unique_id.split("-")
            if len(parts) >= 3:
                try:
                    entity_endpoint_id = int(parts[2])
                    if entity_endpoint_id == endpoint_id:
                        # Check if it's a switch or light
                        if entity.domain in ("switch", "light"):
                            LOGGER.debug(
                                "Endpoint %d/%d already has %s entity: %s",
                                node_id,
                                endpoint_id,
                                entity.domain,
                                entity.entity_id,
                            )
                            return True
                except (ValueError, IndexError):
                    continue

    return False


def async_discover_client_cluster_entities(
    endpoint: MatterEndpoint,
    entity_registry: er.EntityRegistry,
) -> Generator[ClientClusterEntityInfo]:
    """Discover client cluster entities for a Matter endpoint.

    Yields ClientClusterEntityInfo for each matching discovery schema.
    Skips endpoints that:
    - Already have suitable entities from the matter integration
    - Have both server AND client clusters (stateful switches)
    """
    # Check if endpoint already has entities from matter integration
    if endpoint_has_existing_entity(endpoint, entity_registry):
        LOGGER.debug(
            "Skipping endpoint %d - already has entities",
            endpoint.endpoint_id,
        )
        return

    # Check if endpoint has BOTH server and client clusters (stateful switch)
    # These devices already have entities via their server clusters
    if endpoint_has_server_and_client_clusters(
        endpoint, (CLUSTER_ID_ON_OFF, CLUSTER_ID_LEVEL_CONTROL)
    ):
        LOGGER.debug(
            "Skipping endpoint %d - has both server and client clusters "
            "(stateful switch, already has entities via server clusters)",
            endpoint.endpoint_id,
        )
        return

    # Check if this endpoint has any client device types
    endpoint_device_types = set(endpoint.device_types)

    for schema in _get_discovery_schemas():
        # Check device type match
        if schema.device_type is not None:
            if not any(dt in schema.device_type for dt in endpoint_device_types):
                continue

        LOGGER.debug(
            "Matched schema %s for endpoint %d (device types: %s)",
            schema.entity_description.key,
            endpoint.endpoint_id,
            [dt.__name__ for dt in endpoint_device_types],
        )

        yield ClientClusterEntityInfo(
            endpoint=endpoint,
            platform=schema.platform,
            entity_description=schema.entity_description,
            entity_class=schema.entity_class,
            client_clusters=list(schema.required_client_clusters),
        )

        # Only create one entity per endpoint to avoid duplicates
        return
