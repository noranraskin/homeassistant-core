"""Test the Matter AutoBind store."""

import pytest

from homeassistant.components.matter_autobind.store import (
    EligibilityStatus,
    MatterBindingStore,
    MatterBindingStoreData,
)
from homeassistant.core import HomeAssistant


async def test_store_load_empty(hass: HomeAssistant) -> None:
    """Test loading an empty store."""
    store = MatterBindingStore(hass)

    await store.async_load()

    assert store.data.scanned_automation_ids == set()
    assert store.data.acl_resources == {}
    assert store.data.binding_resources == {}
    assert store.data.group_resources == {}


async def test_store_load_twice_skips_second(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that loading twice skips the second load."""
    store = MatterBindingStore(hass)

    await store.async_load()

    with caplog.at_level("DEBUG"):
        await store.async_load()

    assert "already loaded" in caplog.text


async def test_store_mark_automation_scanned(hass: HomeAssistant) -> None:
    """Test marking an automation as scanned."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"

    assert not store.is_automation_scanned(automation_id)

    await store.async_mark_automation_scanned(automation_id)

    assert store.is_automation_scanned(automation_id)


async def test_store_mark_automation_scanned_idempotent(
    hass: HomeAssistant,
) -> None:
    """Test that marking the same automation twice is idempotent."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"

    await store.async_mark_automation_scanned(automation_id)
    await store.async_mark_automation_scanned(automation_id)

    # Should still only have one entry
    assert len(store.data.scanned_automation_ids) == 1


async def test_store_clear_scanned_automation(hass: HomeAssistant) -> None:
    """Test clearing the scanned status of an automation."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"

    await store.async_mark_automation_scanned(automation_id)
    assert store.is_automation_scanned(automation_id)

    await store.async_clear_scanned_automation(automation_id)
    assert not store.is_automation_scanned(automation_id)


async def test_store_eligibility_result(hass: HomeAssistant) -> None:
    """Test setting and getting eligibility results."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"
    trigger_entities = ["light.source"]
    action_entities = ["light.target"]
    reason = "Test reason"

    await store.async_set_eligibility_result(
        automation_id,
        EligibilityStatus.ELIGIBLE,
        trigger_entities,
        action_entities,
        reason,
    )

    result = store.get_eligibility_result(automation_id)
    assert result is not None
    assert result["status"] == EligibilityStatus.ELIGIBLE
    assert result["trigger_entities"] == trigger_entities
    assert result["action_entities"] == action_entities
    assert result["reason"] == reason


async def test_store_acl_resources(hass: HomeAssistant) -> None:
    """Test ACL resource management (acquire/release)."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"
    target_node = 1
    source_node = 2

    # Acquire ACL (should create new)
    key, is_new = store.acquire_acl(automation_id, target_node, source_node)
    assert is_new
    assert store.acl_exists(key)
    resource = store.get_acl_resource(key)
    assert resource["ref_count"] == 1
    assert automation_id in resource["automation_ids"]

    # Acquire same ACL again (should increment ref count)
    automation_id_2 = "automation.other"
    key2, is_new_2 = store.acquire_acl(automation_id_2, target_node, source_node)
    assert not is_new_2
    assert key2 == key
    assert resource["ref_count"] == 2
    assert automation_id_2 in resource["automation_ids"]

    # Release from first automation
    should_remove = store.release_acl(automation_id, key)
    assert not should_remove
    assert resource["ref_count"] == 1
    assert automation_id not in resource["automation_ids"]

    # Release from second automation (should remove)
    should_remove = store.release_acl(automation_id_2, key)
    assert should_remove
    assert not store.acl_exists(key)


async def test_store_binding_resources(hass: HomeAssistant) -> None:
    """Test Binding resource management (acquire/release)."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"
    source_node = 1
    source_ep = 1
    target_node = 2
    target_ep = 1

    # Acquire Binding (unicast)
    key, is_new = store.acquire_binding(
        automation_id, source_node, source_ep, target_node, target_ep
    )
    assert is_new
    assert store.binding_exists(key)
    resource = store.get_binding_resource(key)
    assert resource["ref_count"] == 1
    assert resource["target_node_id"] == target_node
    assert resource["target_group_id"] is None

    # Release Binding
    should_remove = store.release_binding(automation_id, key)
    assert should_remove
    assert not store.binding_exists(key)


async def test_store_group_resources(hass: HomeAssistant) -> None:
    """Test Group resource management (allocate/acquire/release)."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"

    # Allocate Group ID
    group_id = store.allocate_group_id()
    assert group_id >= 0

    # Acquire Group
    members = [(2, 1), (3, 1)]
    key, is_new = store.acquire_group(automation_id, group_id, "Test Group", members)
    assert is_new
    assert store.group_exists(key)
    resource = store.get_group_resource(key)
    assert resource["ref_count"] == 1
    assert len(resource["members"]) == 2

    # Update Group Epoch Key
    epoch_key = "00112233445566778899aabbccddeeff"
    store.update_group_epoch_key(key, epoch_key, 1)
    assert resource["epoch_key"] == epoch_key

    # Release Group
    should_remove = store.release_group(automation_id, key)
    assert should_remove
    assert not store.group_exists(key)


async def test_store_persistence(hass: HomeAssistant) -> None:
    """Test that data persists across store instances."""
    # First store instance
    store1 = MatterBindingStore(hass)
    await store1.async_load()

    automation_id = "automation.test"
    target_node = 1
    source_node = 2

    # Create ACL in first store
    key, _ = store1.acquire_acl(automation_id, target_node, source_node)
    await store1.async_save()

    # Second store instance (simulates restart)
    store2 = MatterBindingStore(hass)
    await store2.async_load()

    assert store2.acl_exists(key)
    resource = store2.get_acl_resource(key)
    assert resource["ref_count"] == 1
    assert automation_id in resource["automation_ids"]


def test_store_data_to_dict() -> None:
    """Test converting store data to dictionary."""
    data = MatterBindingStoreData()
    data.scanned_automation_ids.add("automation.test")
    # Add dummy resources if needed, but empty checks are fine too

    result = data.to_dict()
    assert "automation.test" in result["scanned_automation_ids"]
    assert isinstance(result["scanned_automation_ids"], list)
    assert result["acl_resources"] == {}


def test_store_data_from_dict() -> None:
    """Test creating store data from dictionary."""
    stored = {
        "scanned_automation_ids": ["automation.test"],
        "eligibility_results": {},
        "acl_resources": {},
        "binding_resources": {},
        "group_resources": {},
        "automation_resources": {},
    }

    data = MatterBindingStoreData.from_dict(stored)
    assert "automation.test" in data.scanned_automation_ids
    assert data.acl_resources == {}


async def test_store_binding_preference(hass: HomeAssistant) -> None:
    """Test setting and getting binding preferences."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"

    # Initially no preference
    assert store.get_binding_preference(automation_id) is None

    # Set to unicast
    await store.async_set_binding_preference(automation_id, "unicast")
    assert store.get_binding_preference(automation_id) == "unicast"

    # Set to group
    await store.async_set_binding_preference(automation_id, "group")
    assert store.get_binding_preference(automation_id) == "group"

    # Set to auto (removes preference)
    await store.async_set_binding_preference(automation_id, "auto")
    assert store.get_binding_preference(automation_id) is None

    # Set back and then clear with None
    await store.async_set_binding_preference(automation_id, "unicast")
    await store.async_set_binding_preference(automation_id, None)
    assert store.get_binding_preference(automation_id) is None


async def test_store_binding_preference_persistence(hass: HomeAssistant) -> None:
    """Test that binding preferences persist across store instances."""
    # First store instance
    store1 = MatterBindingStore(hass)
    await store1.async_load()

    automation_id = "automation.test"
    await store1.async_set_binding_preference(automation_id, "group")
    await store1.async_save()

    # Second store instance (simulates restart)
    store2 = MatterBindingStore(hass)
    await store2.async_load()

    assert store2.get_binding_preference(automation_id) == "group"


def test_store_data_binding_preferences_serialization() -> None:
    """Test that binding preferences are serialized correctly."""
    data = MatterBindingStoreData()
    data.binding_preferences["automation.test"] = "unicast"

    result = data.to_dict()
    assert result["binding_preferences"] == {"automation.test": "unicast"}

    # Test from_dict
    restored = MatterBindingStoreData.from_dict(result)
    assert restored.binding_preferences == {"automation.test": "unicast"}
