"""The Matter AutoBind integration.

This integration automatically converts Home Assistant automations into
direct Matter Bindings, reducing latency and enabling offline operation.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER
from .manager import MatterBindingManager
from .store import MatterBindingStore


@dataclass
class MatterAutoBindData:
    """Runtime data for the Matter AutoBind integration."""

    manager: MatterBindingManager
    store: MatterBindingStore


type MatterAutoBindConfigEntry = ConfigEntry[MatterAutoBindData]


async def async_setup_entry(
    hass: HomeAssistant, entry: MatterAutoBindConfigEntry
) -> bool:
    """Set up Matter AutoBind from a config entry."""
    LOGGER.info("Setting up Matter AutoBind integration")

    # Check that Matter integration is available
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # Initialize storage
    store = MatterBindingStore(hass)

    # Initialize manager
    manager = MatterBindingManager(hass, entry, store)

    # Store runtime data
    entry.runtime_data = MatterAutoBindData(manager=manager, store=store)

    # Set up the manager (loads store, scans automations)
    await manager.async_setup()

    LOGGER.info("Matter AutoBind integration setup complete")
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MatterAutoBindConfigEntry
) -> bool:
    """Unload a config entry."""
    LOGGER.info("Unloading Matter AutoBind integration")

    # Shutdown manager
    if entry.runtime_data:
        await entry.runtime_data.manager.async_shutdown()

    return True


async def async_reload_entry(
    hass: HomeAssistant, entry: MatterAutoBindConfigEntry
) -> None:
    """Reload the config entry."""
    LOGGER.info("Reloading Matter AutoBind integration")
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
