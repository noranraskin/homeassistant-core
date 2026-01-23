"""Test the ResourceReconciler class."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.components.matter_autobind.logic.reconciler import (
    NodeInfo,
    ResourceReconciler,
)
from homeassistant.components.matter_autobind.store import MatterBindingStore
from homeassistant.core import HomeAssistant


@pytest.fixture
def mock_registries():
    """Mock entity and device registries."""
    return MagicMock(), MagicMock()


@pytest.fixture
def store(hass: HomeAssistant):
    """Create a store instance."""
    return MatterBindingStore(hass)


@pytest.fixture
def reconciler(hass: HomeAssistant, store, mock_registries):
    """Create a reconciler instance."""
    _ = mock_registries
    # Mock missing dependencies
    config_entry = MagicMock()
    config_entry.options = {}

    adapter = MagicMock()
    adapter.write_acl = AsyncMock()
    adapter.remove_acl = AsyncMock()

    group_manager = MagicMock()
    logger = MagicMock()

    return ResourceReconciler(hass, config_entry, store, adapter, group_manager, logger)


async def test_compute_desired_state(
    reconciler: ResourceReconciler, store: MatterBindingStore
) -> None:
    """Test computing desired state for an automation."""

    source_nodes = [NodeInfo(entity_id="switch.source", node_id=1, endpoint_id=1)]
    target_nodes = [NodeInfo(entity_id="light.target", node_id=2, endpoint_id=1)]

    desired = reconciler.compute_desired_state(source_nodes, target_nodes)

    # Check ACLs (target, source, auth_mode=2 CASE)
    assert len(desired.acls) == 1
    assert (2, 1, 2) in desired.acls

    # Check Bindings (source, source_ep, target, target_ep)
    assert len(desired.bindings) == 1
    assert (1, 1, 2, 1) in desired.bindings

    assert not desired.groups


async def test_reconcile_acls_add_new(
    reconciler: ResourceReconciler, store: MatterBindingStore
) -> None:
    """Test creating new ACLs during reconciliation."""
    automation_id = "automation.test"

    # Desired state: 1 ACL (target, source, mode)
    # Using sets directly
    desired_acls = {(2, 1, 2)}
    current_acls = set()

    await reconciler.reconcile_acls(automation_id, current_acls, desired_acls)

    # Verify adapter write was called matches (target=2, subject=1, mode=2)
    # Access adapter mock from reconciler instance
    reconciler._adapter.write_acl.assert_awaited_with(2, 1, 2)

    # Verify store has the ACL (ref count 1)
    assert len(store.data.acl_resources) == 1


async def test_reconcile_acls_remove_old(
    reconciler: ResourceReconciler, store: MatterBindingStore
) -> None:
    """Test removing old ACLs during reconciliation."""
    automation_id = "automation.test"
    await store.async_load()

    # Setup existing ACL in store
    key, _ = store.acquire_acl(automation_id, 2, 1, 2)

    desired_acls = set()
    current_acls = {(2, 1, 2)}

    await reconciler.reconcile_acls(automation_id, current_acls, desired_acls)

    # Verify adapter remove was called
    reconciler._adapter.remove_acl.assert_awaited_with(2, 1, 2)

    # Verify store no longer has the ACL
    assert not store.acl_exists(key)
