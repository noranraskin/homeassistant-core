"""Common fixtures for the Matter AutoBind tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.components.matter_autobind.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from tests.common import MockConfigEntry


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Create a mock config entry for Matter AutoBind."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Matter AutoBind",
        data={},
        options={},
    )


@pytest.fixture
def mock_matter_client() -> Generator[MagicMock]:
    """Create a mock Matter client.

    This fixture provides a MagicMock that simulates the python-matter-server client.
    It includes sample nodes with various cluster configurations for testing.
    """
    with patch("homeassistant.components.matter.helpers.get_matter") as mock_get_matter:
        # Create mock Matter adapter
        mock_adapter = MagicMock()
        mock_client = MagicMock()
        mock_adapter.matter_client = mock_client

        # Create mock server info
        mock_server_info = MagicMock()
        mock_server_info.compressed_fabric_id = 0x1234567890ABCDEF
        mock_client.server_info = mock_server_info

        # Create mock nodes with different configurations
        mock_nodes = _create_mock_nodes()
        mock_client.get_nodes.return_value = mock_nodes

        mock_get_matter.return_value = mock_adapter

        yield mock_client


def _create_mock_nodes() -> list[MagicMock]:
    """Create a list of mock Matter nodes with various cluster configurations.

    Returns nodes with:
    - Node 1: Light with On/Off + LevelControl (no binding cluster - server only)
    - Node 2: Switch with Binding cluster (client device)
    - Node 3: Socket with On/Off only (no binding cluster - server only)
    - Node 4: Multi-endpoint device with Binding cluster
    """
    nodes = []

    # Node 1: Light with On/Off + LevelControl clusters (server device)
    node1 = _create_mock_node(
        node_id=1,
        endpoints={
            1: {
                "clusters": {
                    0x0006: {"name": "On/Off", "is_client": False},  # Server
                    0x0008: {"name": "LevelControl", "is_client": False},  # Server
                }
            }
        },
    )
    nodes.append(node1)

    # Node 2: Switch with Binding cluster (client device - can bind)
    node2 = _create_mock_node(
        node_id=2,
        endpoints={
            1: {
                "clusters": {
                    0x001E: {"name": "Binding", "is_client": True},  # Client cluster
                    0x0006: {"name": "On/Off", "is_client": True},  # Client cluster
                }
            }
        },
    )
    nodes.append(node2)

    # Node 3: Socket with On/Off only (server device - no binding)
    node3 = _create_mock_node(
        node_id=3,
        endpoints={
            1: {
                "clusters": {
                    0x0006: {"name": "On/Off", "is_client": False},  # Server
                }
            }
        },
    )
    nodes.append(node3)

    # Node 4: Multi-endpoint device with Binding support
    node4 = _create_mock_node(
        node_id=4,
        endpoints={
            1: {
                "clusters": {
                    0x001E: {"name": "Binding", "is_client": True},
                }
            },
            2: {
                "clusters": {
                    0x0006: {"name": "On/Off", "is_client": False},
                }
            },
        },
    )
    nodes.append(node4)

    return nodes


def _create_mock_node(node_id: int, endpoints: dict[int, dict[str, Any]]) -> MagicMock:
    """Create a single mock Matter node.

    Args:
        node_id: The node ID.
        endpoints: Dictionary of endpoint_id -> endpoint config.

    Returns:
        A MagicMock representing a Matter node.
    """
    node = MagicMock()
    node.node_id = node_id

    mock_endpoints: dict[int, MagicMock] = {}
    for ep_id, ep_config in endpoints.items():
        endpoint = MagicMock()
        endpoint.endpoint_id = ep_id
        endpoint.node = node

        # Create mock clusters
        mock_clusters: dict[int, MagicMock] = {}
        for cluster_id, cluster_config in ep_config.get("clusters", {}).items():
            cluster = MagicMock()
            cluster.id = cluster_id
            cluster.name = cluster_config.get("name", f"Cluster_{cluster_id}")
            cluster.is_client = cluster_config.get("is_client", False)
            mock_clusters[cluster_id] = cluster

        endpoint.clusters = mock_clusters

        def _has_cluster(cid: int, ep: MagicMock = endpoint) -> bool:
            return cid in ep.clusters

        endpoint.has_cluster.side_effect = _has_cluster

        mock_endpoints[ep_id] = endpoint

    node.endpoints = mock_endpoints
    node.get_endpoint.side_effect = mock_endpoints.get

    return node


@pytest.fixture
def mock_automation_entities(hass: HomeAssistant) -> list[str]:
    """Create mock automation entities in Home Assistant.

    Creates automation entities that reference different types of devices
    for testing the matter-eligibility scanning.
    """
    automations = [
        "automation.light_on_at_sunset",
        "automation.switch_controls_light",
        "automation.motion_sensor_trigger",
    ]

    for automation_id in automations:
        hass.states.async_set(
            automation_id,
            "on",
            {
                "friendly_name": automation_id.replace("automation.", "")
                .replace("_", " ")
                .title()
            },
        )

    return automations


@pytest.fixture
def mock_matter_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> dict[str, str]:
    """Create mock Matter entities in the entity registry.

    Returns:
        Dictionary mapping entity_id to device_id.
    """
    entities = {}

    # Create Matter light entity
    light_entry = entity_registry.async_get_or_create(
        domain="light",
        platform="matter",
        unique_id="matter_light_1",
        suggested_object_id="matter_living_room_light",
    )
    entities[light_entry.entity_id] = "device_matter_light_1"

    # Create Matter switch entity
    switch_entry = entity_registry.async_get_or_create(
        domain="switch",
        platform="matter",
        unique_id="matter_switch_1",
        suggested_object_id="matter_wall_switch",
    )
    entities[switch_entry.entity_id] = "device_matter_switch_1"

    # Create non-Matter (zigbee) entity
    zigbee_entry = entity_registry.async_get_or_create(
        domain="light",
        platform="zha",
        unique_id="zigbee_light_1",
        suggested_object_id="zigbee_kitchen_light",
    )
    entities[zigbee_entry.entity_id] = "device_zigbee_light_1"

    return entities
