"""Light platform for Matter AutoBind client cluster entities."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ClientClusterEntity, ClientClusterEntityDescription


@dataclass(frozen=True, kw_only=True)
class ClientLightEntityDescription(ClientClusterEntityDescription):
    """Describe a client cluster light entity."""


class ClientClusterLight(ClientClusterEntity, LightEntity):
    """Light entity representing a Matter OnOff+LevelControl client cluster.

    This entity is always unavailable because it represents a device that
    SENDS brightness commands rather than receiving them. It exists to allow
    users to reference the device in automations and for the binding system.
    """

    entity_description: ClientLightEntityDescription

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_is_on = None  # State unknown - this is a client
    _attr_brightness = None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up client cluster light entities from config entry."""
    # Entities are added by the manager after discovery
    # Store the callback for later use
    data = hass.data[DOMAIN].get(config_entry.entry_id)
    if data:
        data.light_add_entities = async_add_entities
