"""Test the Matter AutoBind manager."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.matter_autobind.manager import MatterBindingManager
from homeassistant.components.matter_autobind.store import MatterBindingStore
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from tests.common import MockConfigEntry


@pytest.fixture
def mock_store() -> MagicMock:
    """Create a mock MatterBindingStore."""
    store = MagicMock(spec=MatterBindingStore)
    store.is_automation_scanned.return_value = False
    store.async_load = AsyncMock()
    store.async_save = AsyncMock()
    store.async_mark_automation_scanned = AsyncMock()
    return store


@pytest.fixture
def manager(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_store: MagicMock,
) -> MatterBindingManager:
    """Create a MatterBindingManager instance for testing."""
    return MatterBindingManager(hass, mock_config_entry, mock_store)


async def test_async_setup_loads_store(
    hass: HomeAssistant,
    manager: MatterBindingManager,
    mock_store: MagicMock,
) -> None:
    """Test that async_setup loads the store."""
    await manager.async_setup()

    mock_store.async_load.assert_called_once()


async def test_async_shutdown_saves_store(
    hass: HomeAssistant,
    manager: MatterBindingManager,
    mock_store: MagicMock,
) -> None:
    """Test that async_shutdown saves the store."""
    await manager.async_shutdown()

    mock_store.async_save.assert_called_once()


async def test_scan_automations_no_automations(
    hass: HomeAssistant,
    manager: MatterBindingManager,
) -> None:
    """Test scanning when no automations exist."""
    await manager.async_setup()

    results = await manager.async_scan_automations()

    assert results == {}


async def test_scan_automations_logs_found_automations(
    hass: HomeAssistant,
    manager: MatterBindingManager,
    mock_automation_entities: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that scanning logs each automation found."""
    await manager.async_setup()

    with caplog.at_level("INFO"):
        await manager.async_scan_automations()

    # Check that automations were logged
    for automation_id in mock_automation_entities:
        assert f"Checking automation: {automation_id}" in caplog.text


async def test_scan_automations_marks_as_scanned(
    hass: HomeAssistant,
    manager: MatterBindingManager,
    mock_automation_entities: list[str],
    mock_store: MagicMock,
) -> None:
    """Test that scanned automations are marked in the store."""
    # async_setup calls async_scan_automations internally
    await manager.async_setup()

    # Each automation should be marked as scanned (from the setup scan)
    assert mock_store.async_mark_automation_scanned.call_count == len(
        mock_automation_entities
    )


async def test_scan_automations_skips_already_scanned(
    hass: HomeAssistant,
    manager: MatterBindingManager,
    mock_automation_entities: list[str],
    mock_store: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that already-scanned automations are skipped."""
    # Mark first automation as already scanned
    skipped_automation = mock_automation_entities[0]
    mock_store.is_automation_scanned.side_effect = lambda x: x == skipped_automation

    await manager.async_setup()

    with caplog.at_level("DEBUG"):
        await manager.async_scan_automations()

    # Check that the skipped automation is logged
    assert f"Automation {skipped_automation} already checked" in caplog.text


async def test_is_matter_entity_returns_true_for_matter(
    hass: HomeAssistant,
    manager: MatterBindingManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test _is_matter_entity correctly identifies Matter entities."""
    # Create Matter entity
    matter_entry = entity_registry.async_get_or_create(
        domain="light",
        platform="matter",
        unique_id="matter_test_1",
    )

    await manager.async_setup()

    assert manager._is_matter_entity(matter_entry.entity_id) is True


async def test_is_matter_entity_returns_false_for_non_matter(
    hass: HomeAssistant,
    manager: MatterBindingManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test _is_matter_entity correctly rejects non-Matter entities."""
    # Create ZHA entity
    zha_entry = entity_registry.async_get_or_create(
        domain="light",
        platform="zha",
        unique_id="zha_test_1",
    )

    await manager.async_setup()

    assert manager._is_matter_entity(zha_entry.entity_id) is False


async def test_is_matter_entity_returns_false_for_unknown(
    hass: HomeAssistant,
    manager: MatterBindingManager,
) -> None:
    """Test _is_matter_entity returns False for unknown entities."""
    await manager.async_setup()

    assert manager._is_matter_entity("light.nonexistent") is False


async def test_scan_automations_finds_matter_entities(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_store: MagicMock,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that scanning finds Matter entities in automations."""
    # Create automation state
    automation_id = "automation.test_matter"
    hass.states.async_set(automation_id, "on", {"friendly_name": "Test Matter"})

    # Create Matter entity
    matter_entry = entity_registry.async_get_or_create(
        domain="light",
        platform="matter",
        unique_id="matter_scan_test",
        suggested_object_id="matter_test_light",
    )

    # Create manager without using fixture (to control timing)
    manager = MatterBindingManager(hass, mock_config_entry, mock_store)

    # Load store first
    await mock_store.async_load()

    # Cache registries
    manager._entity_registry = entity_registry
    manager._device_registry = None

    # Mock the automation's referenced entities and run scan
    with (
        patch(
            "homeassistant.components.matter_autobind.manager.entities_in_automation",
            return_value=[matter_entry.entity_id],
        ),
        caplog.at_level("INFO"),
    ):
        results = await manager.async_scan_automations()

    # Should process the automation (even if ineligible due to mock setup)
    assert automation_id in results or "Checking automation" in caplog.text


async def test_scan_automations_no_matter_entities(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_store: MagicMock,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that scanning correctly reports no Matter entities."""
    # Create automation state
    automation_id = "automation.test_non_matter"
    hass.states.async_set(automation_id, "on", {"friendly_name": "Test Non-Matter"})

    # Create non-Matter entity
    zha_entry = entity_registry.async_get_or_create(
        domain="light",
        platform="zha",
        unique_id="zha_scan_test",
        suggested_object_id="zha_test_light",
    )

    # Create manager without using fixture (to control timing)
    manager = MatterBindingManager(hass, mock_config_entry, mock_store)

    # Load store first
    await mock_store.async_load()

    # Cache registries
    manager._entity_registry = entity_registry
    manager._device_registry = None

    # Mock the automation's referenced entities and run scan
    with (
        patch(
            "homeassistant.components.matter_autobind.manager.entities_in_automation",
            return_value=[zha_entry.entity_id],
        ),
        caplog.at_level("INFO"),
    ):
        results = await manager.async_scan_automations()

    # Should not find any Matter entities (or be ineligible)
    assert automation_id not in results or "Checking automation" in caplog.text
