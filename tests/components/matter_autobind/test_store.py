"""Test the Matter AutoBind store."""

import pytest

from homeassistant.components.matter_autobind.store import (
    BindingEntryDict,
    MatterBindingStore,
    MatterBindingStoreData,
)
from homeassistant.core import HomeAssistant


async def test_store_load_empty(hass: HomeAssistant) -> None:
    """Test loading an empty store."""
    store = MatterBindingStore(hass)

    await store.async_load()

    assert store.data.scanned_automation_ids == set()
    assert store.data.supported_devices == []
    assert store.data.managed_bindings == {}
    assert store.data.retry_queue == []


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


async def test_store_add_binding(hass: HomeAssistant) -> None:
    """Test adding a binding entry."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"
    binding: BindingEntryDict = {
        "client_node_id": 1,
        "client_endpoint": 1,
        "target_node_id": 2,
        "target_endpoint": 1,
        "clusters": [0x0006],
    }

    await store.async_add_binding(automation_id, binding)

    assert automation_id in store.data.managed_bindings
    assert len(store.data.managed_bindings[automation_id]) == 1
    assert store.data.managed_bindings[automation_id][0] == binding


async def test_store_add_multiple_bindings(hass: HomeAssistant) -> None:
    """Test adding multiple bindings to the same automation."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"
    binding1: BindingEntryDict = {
        "client_node_id": 1,
        "client_endpoint": 1,
        "target_node_id": 2,
        "target_endpoint": 1,
        "clusters": [0x0006],
    }
    binding2: BindingEntryDict = {
        "client_node_id": 1,
        "client_endpoint": 1,
        "target_node_id": 3,
        "target_endpoint": 1,
        "clusters": [0x0006, 0x0008],
    }

    await store.async_add_binding(automation_id, binding1)
    await store.async_add_binding(automation_id, binding2)

    assert len(store.data.managed_bindings[automation_id]) == 2


async def test_store_remove_bindings_for_automation(hass: HomeAssistant) -> None:
    """Test removing all bindings for an automation."""
    store = MatterBindingStore(hass)
    await store.async_load()

    automation_id = "automation.test"
    binding: BindingEntryDict = {
        "client_node_id": 1,
        "client_endpoint": 1,
        "target_node_id": 2,
        "target_endpoint": 1,
        "clusters": [0x0006],
    }

    await store.async_add_binding(automation_id, binding)
    removed = await store.async_remove_bindings_for_automation(automation_id)

    assert len(removed) == 1
    assert removed[0] == binding
    assert automation_id not in store.data.managed_bindings


async def test_store_remove_bindings_for_unknown_automation(
    hass: HomeAssistant,
) -> None:
    """Test removing bindings for an automation that doesn't exist."""
    store = MatterBindingStore(hass)
    await store.async_load()

    removed = await store.async_remove_bindings_for_automation("automation.unknown")

    assert removed == []


async def test_store_add_to_retry_queue(hass: HomeAssistant) -> None:
    """Test adding a failed binding to the retry queue."""
    store = MatterBindingStore(hass)
    await store.async_load()

    binding: BindingEntryDict = {
        "client_node_id": 1,
        "client_endpoint": 1,
        "target_node_id": 2,
        "target_endpoint": 1,
        "clusters": [0x0006],
    }

    await store.async_add_to_retry_queue(binding)

    assert len(store.data.retry_queue) == 1
    assert store.data.retry_queue[0] == binding


async def test_store_clear_retry_queue(hass: HomeAssistant) -> None:
    """Test clearing the retry queue."""
    store = MatterBindingStore(hass)
    await store.async_load()

    binding: BindingEntryDict = {
        "client_node_id": 1,
        "client_endpoint": 1,
        "target_node_id": 2,
        "target_endpoint": 1,
        "clusters": [0x0006],
    }

    await store.async_add_to_retry_queue(binding)
    cleared = await store.async_clear_retry_queue()

    assert len(cleared) == 1
    assert cleared[0] == binding
    assert store.data.retry_queue == []


async def test_store_persistence(hass: HomeAssistant) -> None:
    """Test that data persists across store instances."""
    # First store instance
    store1 = MatterBindingStore(hass)
    await store1.async_load()

    automation_id = "automation.test"
    await store1.async_mark_automation_scanned(automation_id)

    # Second store instance (simulates restart)
    store2 = MatterBindingStore(hass)
    await store2.async_load()

    assert store2.is_automation_scanned(automation_id)


def test_store_data_to_dict() -> None:
    """Test converting store data to dictionary."""
    data = MatterBindingStoreData(
        scanned_automation_ids={"automation.test1", "automation.test2"},
        supported_devices=[1, 2, 3],
        managed_bindings={
            "automation.test1": [
                {
                    "client_node_id": 1,
                    "client_endpoint": 1,
                    "target_node_id": 2,
                    "target_endpoint": 1,
                    "clusters": [0x0006],
                }
            ]
        },
        retry_queue=[],
    )

    result = data.to_dict()

    # Sets are converted to lists
    assert set(result["scanned_automation_ids"]) == {
        "automation.test1",
        "automation.test2",
    }
    assert result["supported_devices"] == [1, 2, 3]
    assert "automation.test1" in result["managed_bindings"]
    assert result["retry_queue"] == []


def test_store_data_from_dict() -> None:
    """Test creating store data from dictionary."""
    stored = {
        "scanned_automation_ids": ["automation.test1", "automation.test2"],
        "supported_devices": [1, 2, 3],
        "managed_bindings": {},
        "retry_queue": [],
    }

    data = MatterBindingStoreData.from_dict(stored)

    assert data.scanned_automation_ids == {"automation.test1", "automation.test2"}
    assert data.supported_devices == [1, 2, 3]
    assert data.managed_bindings == {}
    assert data.retry_queue == []


def test_store_data_from_dict_none() -> None:
    """Test creating store data from None."""
    data = MatterBindingStoreData.from_dict(None)

    assert data.scanned_automation_ids == set()
    assert data.supported_devices == []
    assert data.managed_bindings == {}
    assert data.retry_queue == []
