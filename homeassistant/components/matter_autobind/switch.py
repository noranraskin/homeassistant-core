"""Switch platform for Matter AutoBind client cluster entities."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ClientClusterEntity


@dataclass(frozen=True, kw_only=True)
class ClientSwitchEntityDescription(SwitchEntityDescription):
    """Describe a client cluster switch entity."""


class ClientClusterSwitch(ClientClusterEntity, SwitchEntity):
    """Switch entity representing a Matter OnOff client cluster.

    This entity is always unavailable because it represents a device that
    SENDS On/Off commands rather than receiving them. It exists to allow
    users to reference the device in automations and for the binding system.
    """

    entity_description: ClientSwitchEntityDescription

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_is_on = None  # State unknown - this is a client


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up client cluster switch entities from config entry."""
    # Entities are added by the manager after discovery
    # Store the callback for later use
    data = hass.data[DOMAIN].get(config_entry.entry_id)
    if data:
        data.switch_add_entities = async_add_entities
