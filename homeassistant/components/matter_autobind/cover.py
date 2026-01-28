"""Cover platform for Matter AutoBind client cluster entities."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityDescription,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ClientClusterEntity


@dataclass(frozen=True, kw_only=True)
class ClientCoverEntityDescription(CoverEntityDescription):
    """Describe a client cluster cover entity."""


class ClientClusterCover(ClientClusterEntity, CoverEntity):
    """Cover entity representing a Matter WindowCovering client cluster.

    This entity is always unavailable because it represents a device that
    SENDS window covering commands rather than receiving them. It exists to
    allow users to reference the device in automations and for the binding system.
    """

    entity_description: ClientCoverEntityDescription

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )
    _attr_is_closed = None  # State unknown - this is a client
    _attr_current_cover_position = None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up client cluster cover entities from config entry."""
    # Entities are added by the manager after discovery
    # Store the callback for later use
    data = hass.data[DOMAIN].get(config_entry.entry_id)
    if data:
        data.cover_add_entities = async_add_entities
