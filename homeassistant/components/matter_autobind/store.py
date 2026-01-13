"""Storage for Matter AutoBind integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import LOGGER, STORAGE_KEY, STORAGE_VERSION


class BindingEntryDict(TypedDict):
    """TypedDict for a binding entry."""

    client_node_id: int
    client_endpoint: int
    target_node_id: int
    target_endpoint: int
    clusters: list[int]


class StoredDataDict(TypedDict):
    """TypedDict for the stored data structure."""

    scanned_automation_ids: list[str]
    supported_devices: list[int]
    managed_bindings: dict[str, list[BindingEntryDict]]
    retry_queue: list[BindingEntryDict]


@dataclass
class MatterBindingStoreData:
    """Data structure for matter binding store."""

    scanned_automation_ids: set[str] = field(default_factory=set)
    """Set of automation IDs that have been scanned."""

    supported_devices: list[int] = field(default_factory=list)
    """List of Matter Node IDs that support binding (have Binding Cluster)."""

    managed_bindings: dict[str, list[BindingEntryDict]] = field(default_factory=dict)
    """Map of automation_id -> list of binding entries created for that automation."""

    retry_queue: list[BindingEntryDict] = field(default_factory=list)
    """List of binding entries that failed and should be retried."""

    def to_dict(self) -> StoredDataDict:
        """Convert dataclass to a dictionary for storage."""
        return StoredDataDict(
            scanned_automation_ids=list(self.scanned_automation_ids),
            supported_devices=self.supported_devices,
            managed_bindings=self.managed_bindings,
            retry_queue=self.retry_queue,
        )

    @classmethod
    def from_dict(cls, data: StoredDataDict | None) -> MatterBindingStoreData:
        """Create dataclass from stored dictionary."""
        if data is None:
            return cls()
        return cls(
            scanned_automation_ids=set(data.get("scanned_automation_ids", [])),
            supported_devices=data.get("supported_devices", []),
            managed_bindings=data.get("managed_bindings", {}),
            retry_queue=data.get("retry_queue", []),
        )


class MatterBindingStore:
    """Store for Matter AutoBind data.

    This class handles persistence of binding state using Home Assistant's
    storage helper. It tracks:
    - Which automations have been scanned
    - Which devices support Matter binding
    - Active bindings created from automations
    - Failed binding attempts for retry
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store."""
        self._hass = hass
        self._store: Store[StoredDataDict] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: MatterBindingStoreData = MatterBindingStoreData()
        self._loaded: bool = False

    @property
    def data(self) -> MatterBindingStoreData:
        """Return the current store data."""
        return self._data

    async def async_load(self) -> None:
        """Load data from storage."""
        if self._loaded:
            LOGGER.debug("Store already loaded, skipping")
            return

        LOGGER.debug("Loading Matter AutoBind store from disk")
        stored: StoredDataDict | None = await self._store.async_load()
        self._data = MatterBindingStoreData.from_dict(stored)
        self._loaded = True
        LOGGER.info(
            "Loaded Matter AutoBind store: %d scanned automations, %d managed bindings",
            len(self._data.scanned_automation_ids),
            len(self._data.managed_bindings),
        )

    async def async_save(self) -> None:
        """Save data to storage."""
        LOGGER.debug("Saving Matter AutoBind store to disk")
        await self._store.async_save(self._data.to_dict())

    def is_automation_scanned(self, automation_id: str) -> bool:
        """Check if an automation has already been scanned.

        Args:
            automation_id: The entity_id of the automation.

        Returns:
            True if the automation has been scanned, False otherwise.
        """
        return automation_id in self._data.scanned_automation_ids

    async def async_mark_automation_scanned(self, automation_id: str) -> None:
        """Mark an automation as scanned and persist.

        Args:
            automation_id: The entity_id of the automation.
        """
        if automation_id not in self._data.scanned_automation_ids:
            self._data.scanned_automation_ids.add(automation_id)
            LOGGER.debug("Marked automation %s as scanned", automation_id)
            await self.async_save()

    async def async_clear_scanned_automation(self, automation_id: str) -> None:
        """Remove an automation from the scanned set.

        Useful when an automation is updated and needs re-scanning.

        Args:
            automation_id: The entity_id of the automation.
        """
        if automation_id in self._data.scanned_automation_ids:
            self._data.scanned_automation_ids.discard(automation_id)
            LOGGER.debug("Cleared scanned status for automation %s", automation_id)
            await self.async_save()

    async def async_add_binding(
        self, automation_id: str, binding: BindingEntryDict
    ) -> None:
        """Add a binding entry for an automation.

        Args:
            automation_id: The entity_id of the automation.
            binding: The binding entry details.
        """
        if automation_id not in self._data.managed_bindings:
            self._data.managed_bindings[automation_id] = []
        self._data.managed_bindings[automation_id].append(binding)
        LOGGER.debug(
            "Added binding for automation %s: client=%d -> target=%d",
            automation_id,
            binding["client_node_id"],
            binding["target_node_id"],
        )
        await self.async_save()

    async def async_remove_bindings_for_automation(
        self, automation_id: str
    ) -> list[BindingEntryDict]:
        """Remove all bindings for an automation.

        Args:
            automation_id: The entity_id of the automation.

        Returns:
            List of removed binding entries.
        """
        bindings = self._data.managed_bindings.pop(automation_id, [])
        if bindings:
            LOGGER.debug(
                "Removed %d bindings for automation %s", len(bindings), automation_id
            )
            await self.async_save()
        return bindings

    async def async_add_to_retry_queue(self, binding: BindingEntryDict) -> None:
        """Add a failed binding to the retry queue.

        Args:
            binding: The binding entry that failed.
        """
        self._data.retry_queue.append(binding)
        LOGGER.debug(
            "Added binding to retry queue: client=%d -> target=%d",
            binding["client_node_id"],
            binding["target_node_id"],
        )
        await self.async_save()

    async def async_clear_retry_queue(self) -> list[BindingEntryDict]:
        """Clear and return the retry queue.

        Returns:
            List of binding entries from the retry queue.
        """
        queue = self._data.retry_queue.copy()
        self._data.retry_queue.clear()
        if queue:
            LOGGER.debug("Cleared retry queue of %d entries", len(queue))
            await self.async_save()
        return queue
