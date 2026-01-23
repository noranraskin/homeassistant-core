"""Test the Matter AutoBind integration initialization."""

from unittest.mock import AsyncMock, patch

from homeassistant.components.matter_autobind.const import DOMAIN
from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry


async def test_setup_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test setting up the integration creates manager and store."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "homeassistant.components.matter_autobind.logic.manager.MatterBindingManager.async_setup",
        new_callable=AsyncMock,
    ) as mock_setup:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Verify manager setup was called
        mock_setup.assert_called_once()

        # Verify runtime_data is set
        assert mock_config_entry.runtime_data is not None
        assert mock_config_entry.runtime_data.manager is not None
        assert mock_config_entry.runtime_data.store is not None


async def test_unload_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test unloading the integration calls manager shutdown."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.matter_autobind.logic.manager.MatterBindingManager.async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "homeassistant.components.matter_autobind.logic.manager.MatterBindingManager.async_shutdown",
            new_callable=AsyncMock,
        ) as mock_shutdown,
    ):
        # Setup
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Unload
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Verify shutdown was called
        mock_shutdown.assert_called_once()


async def test_setup_entry_initializes_domain_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that setup initializes hass.data[DOMAIN] if not present."""
    mock_config_entry.add_to_hass(hass)

    # Ensure domain data doesn't exist
    assert DOMAIN not in hass.data

    with patch(
        "homeassistant.components.matter_autobind.logic.manager.MatterBindingManager.async_setup",
        new_callable=AsyncMock,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Domain data should now exist
        assert DOMAIN in hass.data
