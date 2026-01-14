"""Discovery for Matter client cluster entities."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

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
    Skips endpoints that already have suitable entities from the matter integration.
    """
    # Check if endpoint already has entities
    if endpoint_has_existing_entity(endpoint, entity_registry):
        LOGGER.debug(
            "Skipping endpoint %d - already has entities",
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
