"""Test the MatterAdapter class."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.matter_autobind.matter.adapter import MatterAdapter
from homeassistant.core import HomeAssistant


@pytest.fixture
def mock_matter_client():
    """Mock the Matter client."""
    client = MagicMock()
    # Mock server info
    client.server_info = MagicMock()
    client.server_info.fabric_id = 1

    # Mock node devices
    client.node_devices = MagicMock()
    # Mock .get as a Method since it's called on the dict-like object if checking devices
    client.node_devices.get = MagicMock()

    # Async methods
    client.read_attribute = AsyncMock()
    client.write_attribute = AsyncMock()

    return client


@pytest.fixture
def adapter(hass: HomeAssistant, mock_matter_client):
    """Create an adapter instance."""
    with patch(
        "homeassistant.components.matter_autobind.matter.adapter.get_matter"
    ) as mock_get_matter:
        mock_get_matter.return_value.matter_client = mock_matter_client
        yield MatterAdapter(hass)


async def test_write_binding_success(
    adapter: MatterAdapter, mock_matter_client
) -> None:
    """Test successful binding write."""
    source_node = 1
    source_ep = 1
    target_node = 2
    target_ep = 1

    # Mock read sequence:
    # 1. Initial read (empty)
    # 2. Verification read (contains the new binding)
    mock_matter_client.read_attribute.side_effect = [
        {},
        {"1/30/0": [{"node": target_node}]},
    ]

    # Mock node endpoint having binding cluster
    mock_node = MagicMock()
    mock_ep = MagicMock()
    mock_ep.has_cluster.return_value = True

    # Properly mock endpoints dict
    mock_node.endpoints = {1: mock_ep}

    # Configure client.node_devices.get(node_id) to return mock_node
    mock_matter_client.node_devices.get.return_value = mock_node

    success = await adapter.write_binding(
        source_node, source_ep, target_node, target_ep
    )

    assert success
    mock_matter_client.write_attribute.assert_awaited_once()

    # Verify written value
    args = mock_matter_client.write_attribute.await_args[1]
    assert args["node_id"] == source_node
    # Should use verified endpoint 1
    assert args["attribute_path"] == "1/30/0"

    bindings = args["value"]
    assert len(bindings) == 1
    assert bindings[0]["1"] == target_node


async def test_write_binding_dynamic_endpoint_fallback(
    adapter: MatterAdapter, mock_matter_client
) -> None:
    """Test binding write uses fallback endpoint if source endpoint lacks binding cluster."""
    source_node = 1
    source_ep = 1  # Requested endpoint
    target_node = 2
    target_ep = 1

    # Mock node structure
    mock_node = MagicMock()

    # Requested Endpoint 1: No binding cluster
    ep1 = MagicMock()
    ep1.has_cluster.return_value = False

    # Fallback Endpoint 2: Has binding cluster (30 = 0x001E)
    ep2 = MagicMock()
    # Check for binding cluster ID (0x001E)
    ep2.has_cluster.side_effect = lambda cid: cid == 0x001E

    # Setup node endpoints
    mock_node.endpoints = {1: ep1, 2: ep2}

    mock_matter_client.node_devices.get.return_value = mock_node

    # Mock read sequence:
    # 1. Initial read (empty)
    # 2. Verification read (contains the new binding)
    mock_matter_client.read_attribute.side_effect = [
        {},
        {"2/30/0": [{"node": target_node}]},
    ]

    success = await adapter.write_binding(
        source_node, source_ep, target_node, target_ep
    )

    assert success

    # Verify written to fallback endpoint 2
    args = mock_matter_client.write_attribute.await_args[1]
    assert args["attribute_path"] == "2/30/0"
