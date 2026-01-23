"""The Matter AutoBind integration.

This integration automatically converts Home Assistant automations into
direct Matter Bindings, reducing latency and enabling offline operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER
from .logic.manager import MatterBindingManager
from .store import MatterBindingStore

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

# Platforms to set up
PLATFORMS = [Platform.LIGHT, Platform.SWITCH]


@dataclass
class MatterAutoBindData:
    """Runtime data for the Matter AutoBind integration."""

    manager: MatterBindingManager
    store: MatterBindingStore

    # Platform entity callbacks (set during platform setup)
    light_add_entities: AddConfigEntryEntitiesCallback | None = None
    switch_add_entities: AddConfigEntryEntitiesCallback | None = None


type MatterAutoBindConfigEntry = ConfigEntry[MatterAutoBindData]


async def async_setup_entry(
    hass: HomeAssistant, entry: MatterAutoBindConfigEntry
) -> bool:
    """Set up Matter AutoBind from a config entry."""
    LOGGER.info("Setting up Matter AutoBind integration")

    # Ensure domain data dict exists
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # Initialize storage
    store = MatterBindingStore(hass)

    # Initialize manager
    manager = MatterBindingManager(hass, entry, store)

    # Create runtime data
    runtime_data = MatterAutoBindData(manager=manager, store=store)
    entry.runtime_data = runtime_data

    # Store in hass.data for platform access
    hass.data[DOMAIN][entry.entry_id] = runtime_data

    # Forward platform setup first (so callbacks are registered)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up the manager (loads store, scans automations, discovers client clusters)
    await manager.async_setup()

    LOGGER.info("Matter AutoBind integration setup complete")
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MatterAutoBindConfigEntry
) -> bool:
    """Unload a config entry."""
    LOGGER.info("Unloading Matter AutoBind integration")

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Shutdown manager
        if entry.runtime_data:
            await entry.runtime_data.manager.async_shutdown()

        # Remove from hass.data
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_reload_entry(
    hass: HomeAssistant, entry: MatterAutoBindConfigEntry
) -> None:
    """Reload the config entry."""
    LOGGER.info("Reloading Matter AutoBind integration")
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
