"""Manager for Matter AutoBind integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.automation import (
    DOMAIN as AUTOMATION_DOMAIN,
    devices_in_automation,
    entities_in_automation,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CLUSTER_ID_BINDING, LOGGER
from .store import BindingEntryDict, MatterBindingStore

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

from homeassistant.components.matter.helpers import get_matter

from .discovery import async_discover_client_cluster_entities


# Matter domain constant
MATTER_DOMAIN = "matter"


class MatterBindingManager:
    """Manager for Matter AutoBind operations.

    This class coordinates automation scanning and Matter binding creation.
    It is designed to be mockable for testing without physical Matter devices.

    The manager:
    1. Scans all automations on startup
    2. Identifies automations that link Matter devices
    3. Logs findings for now (binding creation in future phases)
    4. Persists state to avoid re-scanning
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        store: MatterBindingStore,
    ) -> None:
        """Initialize the manager.

        Args:
            hass: Home Assistant instance.
            config_entry: The config entry for this integration.
            store: The storage instance for persistence.
        """
        self._hass = hass
        self._config_entry = config_entry
        self._store = store
        self._entity_registry: er.EntityRegistry | None = None
        self._device_registry: dr.DeviceRegistry | None = None

    @property
    def store(self) -> MatterBindingStore:
        """Return the store instance."""
        return self._store

    async def async_setup(self) -> None:
        """Set up the manager.

        Called during config entry setup. Loads the store and
        performs initial automation scan.
        """
        LOGGER.info("Setting up Matter AutoBind Manager")

        # Load persisted data
        await self._store.async_load()

        # Cache registries for faster lookups
        self._entity_registry = er.async_get(self._hass)
        self._device_registry = dr.async_get(self._hass)

        # Perform initial scan
        await self.async_scan_automations()

        # Discover and create client cluster entities
        await self.async_discover_client_clusters()

        LOGGER.info("Matter AutoBind Manager setup complete")

    async def async_shutdown(self) -> None:
        """Shut down the manager.

        Called during config entry unload.
        """
        LOGGER.info("Shutting down Matter AutoBind Manager")
        # Save any pending state
        await self._store.async_save()

    async def async_scan_automations(self) -> dict[str, list[str]]:
        """Scan all automations and identify Matter-eligible ones.

        Returns:
            Dictionary mapping automation_id to list of Matter entity_ids found.
        """
        LOGGER.info("Starting automation scan")

        results: dict[str, list[str]] = {}

        # Get all automation entity IDs
        automation_entity_ids = self._hass.states.async_entity_ids(AUTOMATION_DOMAIN)
        LOGGER.info("Found %d automations to scan", len(automation_entity_ids))

        for automation_id in automation_entity_ids:
            # Check if already scanned
            if self._store.is_automation_scanned(automation_id):
                LOGGER.debug("Automation %s already scanned, skipping", automation_id)
                continue

            LOGGER.info("Scanning automation: %s", automation_id)

            # Get Matter entities referenced by this automation
            matter_entities = await self._analyze_automation(automation_id)

            if matter_entities:
                LOGGER.info(
                    "Automation %s contains %d Matter device(s): %s",
                    automation_id,
                    len(matter_entities),
                    matter_entities,
                )
                results[automation_id] = matter_entities
            else:
                LOGGER.info(
                    "Automation %s does not contain Matter devices",
                    automation_id,
                )

            # Mark as scanned
            await self._store.async_mark_automation_scanned(automation_id)

        LOGGER.info(
            "Automation scan complete. Found %d automations with Matter devices",
            len(results),
        )
        return results

    async def _analyze_automation(self, automation_id: str) -> list[str]:
        """Analyze an automation to find Matter entities.

        Args:
            automation_id: The entity_id of the automation.

        Returns:
            List of Matter entity_ids referenced by the automation.
        """
        matter_entities: list[str] = []

        # Get the automation entity
        state = self._hass.states.get(automation_id)
        if not state:
            LOGGER.warning("Automation %s not found", automation_id)
            return matter_entities

        # Get referenced entities from the automation's attributes
        # Automations store their config which includes triggers and actions
        # We need to extract entity references from there

        # First, try to get referenced entities via the entity component
        referenced_entities = await self._get_automation_referenced_entities(
            automation_id
        )

        LOGGER.debug(
            "Automation %s references entities: %s",
            automation_id,
            referenced_entities,
        )

        # Check each referenced entity
        for entity_id in referenced_entities:
            if self._is_matter_entity(entity_id):
                matter_entities.append(entity_id)
                LOGGER.debug("Entity %s is a Matter device", entity_id)

        # Also check referenced devices (devices can have Matter entities)
        referenced_devices = await self._get_automation_referenced_devices(
            automation_id
        )

        LOGGER.debug(
            "Automation %s references devices: %s",
            automation_id,
            referenced_devices,
        )

        for device_id in referenced_devices:
            device_matter_entities = self._get_matter_entities_for_device(device_id)
            for entity_id in device_matter_entities:
                if entity_id not in matter_entities:
                    matter_entities.append(entity_id)
                    LOGGER.debug(
                        "Found Matter entity %s via device %s",
                        entity_id,
                        device_id,
                    )

        return matter_entities

    async def _get_automation_referenced_entities(
        self, automation_id: str
    ) -> list[str]:
        """Get entities referenced by an automation.

        This uses the automation component's built-in tracking.

        Args:
            automation_id: The entity_id of the automation.

        Returns:
            List of entity_ids referenced by the automation.
        """
        try:
            return entities_in_automation(self._hass, automation_id)
        except HomeAssistantError as err:
            LOGGER.warning(
                "Failed to get entities for automation %s: %s",
                automation_id,
                err,
            )
            return []

    async def _get_automation_referenced_devices(self, automation_id: str) -> list[str]:
        """Get devices referenced by an automation.

        This uses the automation component's built-in tracking.

        Args:
            automation_id: The entity_id of the automation.

        Returns:
            List of device_ids referenced by the automation.
        """
        try:
            return devices_in_automation(self._hass, automation_id)
        except HomeAssistantError as err:
            LOGGER.warning(
                "Failed to get devices for automation %s: %s",
                automation_id,
                err,
            )
            return []

    @callback
    def _is_matter_entity(self, entity_id: str) -> bool:
        """Check if an entity belongs to the Matter integration.

        Args:
            entity_id: The entity_id to check.

        Returns:
            True if the entity is from the Matter integration.
        """
        if self._entity_registry is None:
            return False

        entity_entry = self._entity_registry.async_get(entity_id)
        if entity_entry is None:
            return False

        return entity_entry.platform == MATTER_DOMAIN

    @callback
    def _get_matter_entities_for_device(self, device_id: str) -> list[str]:
        """Get all Matter entities for a device.

        Args:
            device_id: The device_id to check.

        Returns:
            List of Matter entity_ids for the device.
        """
        if self._entity_registry is None:
            return []

        return [
            entity.entity_id
            for entity in er.async_entries_for_device(
                self._entity_registry, device_id, include_disabled_entities=False
            )
            if entity.platform == MATTER_DOMAIN
        ]

    async def async_create_binding(
        self,
        automation_id: str,
        client_node_id: int,
        client_endpoint: int,
        target_node_id: int,
        target_endpoint: int,
        clusters: list[int],
    ) -> bool:
        """Create a Matter binding between two devices.

        NOTE: This is a stub for future implementation.

        Args:
            automation_id: The automation this binding is for.
            client_node_id: The Matter Node ID of the client (trigger device).
            client_endpoint: The endpoint on the client device.
            target_node_id: The Matter Node ID of the target (action device).
            target_endpoint: The endpoint on the target device.
            clusters: List of cluster IDs to bind.

        Returns:
            True if binding was created successfully.
        """
        LOGGER.info(
            "Creating binding: automation=%s, client=%d/%d -> target=%d/%d, clusters=%s",
            automation_id,
            client_node_id,
            client_endpoint,
            target_node_id,
            target_endpoint,
            clusters,
        )

        # TODO: Implement actual binding logic in future phase
        # 1. Check client has Binding Cluster (CLUSTER_ID_BINDING)
        # 2. Check client has open slot in Binding Table
        # 3. Write ACL entry to target device
        # 4. Write Binding entry to client device

        # For now, just store the binding
        binding: BindingEntryDict = {
            "client_node_id": client_node_id,
            "client_endpoint": client_endpoint,
            "target_node_id": target_node_id,
            "target_endpoint": target_endpoint,
            "clusters": clusters,
        }

        await self._store.async_add_binding(automation_id, binding)

        LOGGER.info("Binding created and stored (stub implementation)")
        return True

    def _has_binding_cluster(self, node_id: int) -> bool:
        """Check if a Matter node has the Binding cluster.

        NOTE: This is a stub for future implementation.

        Args:
            node_id: The Matter Node ID to check.

        Returns:
            True if the node has the Binding cluster.
        """
        # TODO: Query Matter server for node clusters
        _ = CLUSTER_ID_BINDING  # Reference to avoid unused import
        LOGGER.debug("Checking binding cluster support for node %d (stub)", node_id)
        return False

    async def async_discover_client_clusters(self) -> None:
        """Discover Matter nodes with client clusters and create entities.

        This scans all Matter nodes for endpoints that have client cluster
        device types (like OnOffLightSwitch, DimmerSwitch) but don't already
        have entities from the official matter integration.
        """
        LOGGER.info("Starting client cluster discovery")

        # Check if matter integration is loaded
        if MATTER_DOMAIN not in self._hass.data:
            LOGGER.warning(
                "Matter integration not loaded, skipping client cluster discovery"
            )
            return

        # Get the matter adapter
        try:
            matter = get_matter(self._hass)
        except KeyError:
            LOGGER.warning("Matter integration not available")
            return

        # Get runtime data for entity callbacks
        runtime_data = self._config_entry.runtime_data

        # Collect entities to add by platform
        switch_entities: list = []
        light_entities: list = []

        # Get all nodes from the matter client
        for node in matter.matter_client.get_nodes():
            LOGGER.debug(
                "Checking node %d for client clusters (endpoints: %s)",
                node.node_id,
                list(node.endpoints.keys()),
            )

            for endpoint in node.endpoints.values():
                # Skip root endpoint (0)
                if endpoint.endpoint_id == 0:
                    continue

                # Discover client cluster entities for this endpoint
                for entity_info in async_discover_client_cluster_entities(
                    endpoint, self._entity_registry
                ):
                    LOGGER.info(
                        "Creating %s entity for node %d endpoint %d",
                        entity_info.platform,
                        node.node_id,
                        endpoint.endpoint_id,
                    )

                    # Create the entity
                    entity = entity_info.entity_class(
                        matter.matter_client,
                        endpoint,
                        entity_info,
                    )

                    # Add to appropriate list
                    if entity_info.platform.value == "switch":
                        switch_entities.append(entity)
                    elif entity_info.platform.value == "light":
                        light_entities.append(entity)

        # Add entities via platform callbacks
        if switch_entities and runtime_data.switch_add_entities:
            LOGGER.info("Adding %d switch entities", len(switch_entities))
            runtime_data.switch_add_entities(switch_entities)

        if light_entities and runtime_data.light_add_entities:
            LOGGER.info("Adding %d light entities", len(light_entities))
            runtime_data.light_add_entities(light_entities)

        LOGGER.info(
            "Client cluster discovery complete: %d switch, %d light entities",
            len(switch_entities),
            len(light_entities),
        )
