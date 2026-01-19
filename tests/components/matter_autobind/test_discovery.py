"""Test the Matter AutoBind discovery module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from homeassistant.components.matter_autobind.discovery import (
    endpoint_has_server_and_client_clusters,
)


class MockDescriptor:
    """Mock Descriptor cluster."""

    def __init__(self, server_list: list[int], client_list: list[int]) -> None:
        """Initialize mock descriptor."""
        self.serverList = server_list
        self.clientList = client_list


def _create_mock_endpoint_with_descriptor(
    endpoint_id: int,
    server_list: list[int] | None,
    client_list: list[int] | None,
) -> MagicMock:
    """Create a mock endpoint with a Descriptor cluster."""
    from chip.clusters import Objects as Clusters

    endpoint = MagicMock()
    endpoint.endpoint_id = endpoint_id

    if server_list is not None and client_list is not None:
        descriptor = MockDescriptor(server_list, client_list)
        endpoint.get_cluster.side_effect = (
            lambda c: descriptor if c == Clusters.Descriptor else None
        )
    else:
        endpoint.get_cluster.return_value = None

    return endpoint


def test_endpoint_has_server_and_client_clusters_stateful_switch() -> None:
    """Test detection of stateful switch (OnOff in both server and client lists)."""
    # Stateful switch: OnOff (0x0006) in both server and client lists
    endpoint = _create_mock_endpoint_with_descriptor(
        endpoint_id=1,
        server_list=[29, 3, 4, 6, 8, 98, 30],  # OnOff (6) in server
        client_list=[3, 6, 8],  # OnOff (6) in client
    )

    result = endpoint_has_server_and_client_clusters(endpoint, (0x0006, 0x0008))

    assert result is True


def test_endpoint_has_server_and_client_clusters_client_only() -> None:
    """Test detection of client-only device (OnOff only in client list)."""
    # Simple light switch: OnOff only in client list (no server)
    endpoint = _create_mock_endpoint_with_descriptor(
        endpoint_id=1,
        server_list=[29, 3, 30, 4],  # No OnOff in server
        client_list=[3, 6, 4],  # OnOff (6) in client
    )

    result = endpoint_has_server_and_client_clusters(endpoint, (0x0006, 0x0008))

    assert result is False


def test_endpoint_has_server_and_client_clusters_server_only() -> None:
    """Test detection of server-only device (OnOff only in server list)."""
    # Light bulb: OnOff only in server list (no client)
    endpoint = _create_mock_endpoint_with_descriptor(
        endpoint_id=1,
        server_list=[29, 3, 6, 8],  # OnOff (6) in server
        client_list=[],  # No client clusters
    )

    result = endpoint_has_server_and_client_clusters(endpoint, (0x0006, 0x0008))

    assert result is False


def test_endpoint_has_server_and_client_clusters_no_descriptor() -> None:
    """Test handling of endpoint with no Descriptor cluster."""
    endpoint = _create_mock_endpoint_with_descriptor(
        endpoint_id=1,
        server_list=None,
        client_list=None,
    )

    result = endpoint_has_server_and_client_clusters(endpoint, (0x0006, 0x0008))

    assert result is False
