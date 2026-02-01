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


async def test_compute_desired_state_unicast_preference(
    hass: HomeAssistant, store: MatterBindingStore
) -> None:
    """Test that unicast preference forces unicast even with multiple targets."""
    await store.async_load()

    # Set unicast preference
    await store.async_set_binding_preference("automation.test", "unicast")

    # Create reconciler with groups enabled
    config_entry = MagicMock()
    config_entry.options = {"enable_group_bindings": True}

    adapter = MagicMock()
    group_manager = MagicMock()
    logger = MagicMock()

    reconciler = ResourceReconciler(
        hass, config_entry, store, adapter, group_manager, logger
    )

    source_nodes = [NodeInfo(entity_id="switch.source", node_id=1, endpoint_id=1)]
    target_nodes = [
        NodeInfo(entity_id="light.target1", node_id=2, endpoint_id=1),
        NodeInfo(entity_id="light.target2", node_id=3, endpoint_id=1),
    ]

    # With unicast preference, should create unicast bindings even with 2 targets
    desired = reconciler.compute_desired_state(
        source_nodes, target_nodes, automation_id="automation.test"
    )

    # Should have 2 ACLs (one for each target, CASE mode)
    assert len(desired.acls) == 2
    assert (2, 1, 2) in desired.acls  # target 2 grants source 1
    assert (3, 1, 2) in desired.acls  # target 3 grants source 1

    # Should have 2 unicast bindings (not group)
    assert len(desired.bindings) == 2
    assert (1, 1, 2, 1) in desired.bindings  # source -> target 2
    assert (1, 1, 3, 1) in desired.bindings  # source -> target 3

    # No groups
    assert not desired.groups


async def test_compute_desired_state_group_preference(
    hass: HomeAssistant, store: MatterBindingStore
) -> None:
    """Test that group preference creates groups when enabled."""
    await store.async_load()

    # Set group preference
    await store.async_set_binding_preference("automation.test", "group")

    # Create reconciler with groups enabled
    config_entry = MagicMock()
    config_entry.options = {"enable_group_bindings": True}

    adapter = MagicMock()
    group_manager = MagicMock()
    logger = MagicMock()

    reconciler = ResourceReconciler(
        hass, config_entry, store, adapter, group_manager, logger
    )

    source_nodes = [NodeInfo(entity_id="switch.source", node_id=1, endpoint_id=1)]
    target_nodes = [
        NodeInfo(entity_id="light.target1", node_id=2, endpoint_id=1),
        NodeInfo(entity_id="light.target2", node_id=3, endpoint_id=1),
    ]

    desired = reconciler.compute_desired_state(
        source_nodes, target_nodes, automation_id="automation.test"
    )

    # Should have groups (one binding to group)
    assert len(desired.groups) == 1

    # Check the group binding exists
    group_bindings = [b for b in desired.bindings if isinstance(b[2], str)]
    assert len(group_bindings) == 1

    # ACLs should be GROUP only (auth_mode=3), NOT CASE (auth_mode=2)
    # For group bindings, we only need GROUP ACLs on targets
    assert len(desired.acls) == 2  # One GROUP ACL per target node

    # All ACLs should be GROUP type (auth_mode=3)
    for acl in desired.acls:
        _, subject, auth_mode = acl
        assert auth_mode == 3, f"Expected GROUP ACL (auth_mode=3), got {auth_mode}"
        # Subject should be the group ID, not a source node ID
        assert subject >= 32768, f"Expected group ID, got {subject}"


async def test_compute_desired_state_auto_detection(
    hass: HomeAssistant, store: MatterBindingStore
) -> None:
    """Test auto-detection without preference set."""
    await store.async_load()

    # No preference set, auto mode
    config_entry = MagicMock()
    config_entry.options = {"enable_group_bindings": True}

    adapter = MagicMock()
    group_manager = MagicMock()
    logger = MagicMock()

    reconciler = ResourceReconciler(
        hass, config_entry, store, adapter, group_manager, logger
    )

    source_nodes = [NodeInfo(entity_id="switch.source", node_id=1, endpoint_id=1)]
    target_nodes = [
        NodeInfo(entity_id="light.target1", node_id=2, endpoint_id=1),
        NodeInfo(entity_id="light.target2", node_id=3, endpoint_id=1),
    ]

    # With auto mode and multiple targets + groups enabled, should use groups
    desired = reconciler.compute_desired_state(
        source_nodes, target_nodes, automation_id="automation.test"
    )

    # Should have groups
    assert len(desired.groups) == 1


async def test_compute_desired_state_none_preference(
    hass: HomeAssistant, store: MatterBindingStore
) -> None:
    """Test that none preference disables bindings entirely."""
    await store.async_load()

    # Set none preference
    await store.async_set_binding_preference("automation.test", "none")

    config_entry = MagicMock()
    config_entry.options = {"enable_group_bindings": True}

    adapter = MagicMock()
    group_manager = MagicMock()
    logger = MagicMock()

    reconciler = ResourceReconciler(
        hass, config_entry, store, adapter, group_manager, logger
    )

    source_nodes = [NodeInfo(entity_id="switch.source", node_id=1, endpoint_id=1)]
    target_nodes = [
        NodeInfo(entity_id="light.target1", node_id=2, endpoint_id=1),
        NodeInfo(entity_id="light.target2", node_id=3, endpoint_id=1),
    ]

    # With none preference, should return empty state
    desired = reconciler.compute_desired_state(
        source_nodes, target_nodes, automation_id="automation.test"
    )

    # All should be empty - bindings are disabled
    assert len(desired.acls) == 0
    assert len(desired.bindings) == 0
    assert len(desired.groups) == 0
    assert desired.group_id is None
