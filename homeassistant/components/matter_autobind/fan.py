"""Fan platform for Matter AutoBind client cluster entities."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.fan import (
    FanEntity,
    FanEntityDescription,
    FanEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ClientClusterEntity


@dataclass(frozen=True, kw_only=True)
class ClientFanEntityDescription(FanEntityDescription):
    """Describe a client cluster fan entity."""


class ClientClusterFan(ClientClusterEntity, FanEntity):
    """Fan entity representing a Matter FanControl client cluster.

    This entity is always unavailable because it represents a device that
    SENDS fan commands rather than receiving them. It exists to allow
    users to reference the device in automations and for the binding system.
    """

    entity_description: ClientFanEntityDescription

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_is_on = None  # State unknown - this is a client
    _attr_percentage = None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up client cluster fan entities from config entry."""
    # Entities are added by the manager after discovery
    # Store the callback for later use
    data = hass.data[DOMAIN].get(config_entry.entry_id)
    if data:
        data.fan_add_entities = async_add_entities
