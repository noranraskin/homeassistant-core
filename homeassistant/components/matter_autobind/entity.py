"""Base entity for Matter AutoBind client cluster entities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from matter_server.common.models import ServerInfoMessage

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import LOGGER

if TYPE_CHECKING:
    from matter_server.client import MatterClient
    from matter_server.client.models.node import MatterEndpoint

    from .models import ClientClusterEntityInfo


# Import device ID helpers from the matter integration
# These are used to match the exact device identifier format
MATTER_DOMAIN = "matter"
ID_TYPE_DEVICE_ID = "deviceid"


def get_operational_instance_id(
    server_info: ServerInfoMessage,
    node_id: int,
) -> str:
    """Return `Operational Instance Name` for given node.

    This matches the format used by the matter integration.
    """
    fabric_id_hex = f"{server_info.compressed_fabric_id:016X}"
    node_id_hex = f"{node_id:016X}"
    return f"{fabric_id_hex}-{node_id_hex}"


def get_device_id(server_info: ServerInfoMessage, endpoint: MatterEndpoint) -> str:
    """Return HA device_id for the given MatterEndpoint.

    This matches the format used by the matter integration exactly.
    """
    operational_instance_id = get_operational_instance_id(
        server_info, endpoint.node.node_id
    )
    # Check if this is a composed device and get the compose parent
    if compose_parent := endpoint.node.get_compose_parent(endpoint.endpoint_id):
        endpoint = compose_parent
    if endpoint.is_bridged_device:
        # Append endpoint ID if this endpoint is a bridged device
        postfix = str(endpoint.endpoint_id)
    else:
        # This matches the matter integration's format for non-bridged devices
        postfix = "MatterNodeDevice"
    return f"{operational_instance_id}-{postfix}"


@dataclass(frozen=True, kw_only=True)
class ClientClusterEntityDescription(EntityDescription):
    """Describe a client cluster entity."""


class ClientClusterEntity(Entity):
    """Base entity for Matter client cluster devices.

    These entities represent the "client side" of Matter devices - devices that
    send commands rather than receive them. They are marked as unavailable
    because they cannot be directly controlled through Home Assistant.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_available = False  # Always unavailable - represents client capability

    entity_description: ClientClusterEntityDescription

    def __init__(
        self,
        matter_client: MatterClient,
        endpoint: MatterEndpoint,
        entity_info: ClientClusterEntityInfo,
    ) -> None:
        """Initialize the client cluster entity."""
        self._matter_client = matter_client
        self._endpoint = endpoint
        self._entity_info = entity_info
        self.entity_description = entity_info.entity_description

        # Get server info for unique ID generation
        server_info = cast(ServerInfoMessage, matter_client.server_info)

        # Use the exact same device ID format as the matter integration
        node_device_id = get_device_id(server_info, endpoint)

        # Create unique ID including "client" to distinguish from server entities
        cluster_ids = "_".join(str(c) for c in entity_info.client_clusters)
        self._attr_unique_id = (
            f"{node_device_id}-"
            f"client-"
            f"{entity_info.entity_description.key}-"
            f"{cluster_ids}"
        )

        # Link to the existing Matter device using the exact same identifier format
        self._attr_device_info = DeviceInfo(
            identifiers={(MATTER_DOMAIN, f"{ID_TYPE_DEVICE_ID}_{node_device_id}")}
        )

        LOGGER.debug(
            "Created client cluster entity %s for endpoint %d (device_id: %s)",
            self._attr_unique_id,
            endpoint.endpoint_id,
            f"{ID_TYPE_DEVICE_ID}_{node_device_id}",
        )

    @property
    def endpoint(self) -> MatterEndpoint:
        """Return the Matter endpoint."""
        return self._endpoint
