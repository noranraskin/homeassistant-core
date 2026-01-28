"""Climate platform for Matter AutoBind client cluster entities."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ClientClusterEntity


@dataclass(frozen=True, kw_only=True)
class ClientClimateEntityDescription(ClimateEntityDescription):
    """Describe a client cluster climate entity."""


class ClientClusterClimate(ClientClusterEntity, ClimateEntity):
    """Climate entity representing a Matter Thermostat client cluster.

    This entity is always unavailable because it represents a device that
    SENDS thermostat commands rather than receiving them. It exists to allow
    users to reference the device in automations and for the binding system.
    """

    entity_description: ClientClimateEntityDescription

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.AUTO]
    _attr_hvac_mode = None  # State unknown - this is a client
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    )
    _attr_current_temperature = None
    _attr_target_temperature = None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up client cluster climate entities from config entry."""
    # Entities are added by the manager after discovery
    # Store the callback for later use
    data = hass.data[DOMAIN].get(config_entry.entry_id)
    if data:
        data.climate_add_entities = async_add_entities
