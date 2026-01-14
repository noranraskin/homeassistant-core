"""Manager for Matter AutoBind integration."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from matter_server.client.models import device_types
from matter_server.common.models import APICommand

from homeassistant.components.automation import (
    DOMAIN as AUTOMATION_DOMAIN,
    devices_in_automation,
    entities_in_automation,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_added_domain,
    async_track_state_change_event,
)

from .const import CLUSTER_ID_BINDING, DOMAIN as AUTOBIND_DOMAIN, LOGGER
from .store import AclEntryDict, BindingEntryDict, EligibilityStatus, MatterBindingStore

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
        self._unsub_automation_listeners: list[CALLBACK_TYPE] = []

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

        # Subscribe to automation events (creation and updates)
        self._subscribe_to_automation_events()

        LOGGER.info("Matter AutoBind Manager setup complete")

    async def async_shutdown(self) -> None:
        """Shut down the manager.

        Called during config entry unload.
        """
        LOGGER.info("Shutting down Matter AutoBind Manager")

        # Unsubscribe from automation events
        for unsub in self._unsub_automation_listeners:
            unsub()
        self._unsub_automation_listeners.clear()

        # Save any pending state
        await self._store.async_save()

    @callback
    def _subscribe_to_automation_events(self) -> None:
        """Subscribe to automation creation and update events.

        This tracks:
        1. New automations being created (state_added_domain)
        2. Existing automations being updated (state_change_event for all automation entities)
        """
        LOGGER.debug("Subscribing to automation events")

        # Track new automations added to the automation domain
        unsub_added = async_track_state_added_domain(
            self._hass,
            AUTOMATION_DOMAIN,
            self._handle_automation_added,
        )
        self._unsub_automation_listeners.append(unsub_added)

        # Track updates to existing automations
        # Get all current automation entity IDs and track their changes
        automation_entity_ids = self._hass.states.async_entity_ids(AUTOMATION_DOMAIN)
        if automation_entity_ids:
            unsub_changed = async_track_state_change_event(
                self._hass,
                list(automation_entity_ids),
                self._handle_automation_updated,
            )
            self._unsub_automation_listeners.append(unsub_changed)

        LOGGER.info(
            "Subscribed to automation events (tracking %d existing automations)",
            len(automation_entity_ids),
        )

    @callback
    def _handle_automation_added(self, event: Event[EventStateChangedData]) -> None:
        """Handle a new automation being created.

        Args:
            event: The state changed event for the new automation.
        """
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]

        if new_state is None:
            return

        LOGGER.info("=" * 60)
        LOGGER.info("MATTER AUTOBIND: New automation detected: %s", entity_id)
        LOGGER.info("=" * 60)

        # Schedule the eligibility check (needs to be async)
        self._hass.async_create_task(
            self._async_check_and_log_automation(entity_id, is_new=True)
        )

        # Also subscribe to future changes for this new automation
        unsub = async_track_state_change_event(
            self._hass,
            entity_id,
            self._handle_automation_updated,
        )
        self._unsub_automation_listeners.append(unsub)

    @callback
    def _handle_automation_updated(self, event: Event[EventStateChangedData]) -> None:
        """Handle an automation being updated.

        Args:
            event: The state changed event for the updated automation.
        """
        entity_id = event.data["entity_id"]
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]

        # Handle deletion - clean up ACLs
        if new_state is None:
            LOGGER.info("Automation %s was deleted, cleaning up ACLs", entity_id)
            self._hass.async_create_task(
                self._async_handle_automation_deleted(entity_id)
            )
            return

        # Skip if only the state (on/off) changed, we care about config changes
        # which would show up in attribute changes
        if old_state is not None and old_state.attributes == new_state.attributes:
            LOGGER.debug(
                "Automation %s: only state changed (not config), skipping recheck",
                entity_id,
            )
            return

        LOGGER.info("=" * 60)
        LOGGER.info("MATTER AUTOBIND: Automation updated: %s", entity_id)
        LOGGER.info("=" * 60)

        # Schedule the eligibility check (needs to be async)
        self._hass.async_create_task(
            self._async_check_and_log_automation(entity_id, is_new=False)
        )

    async def _async_handle_automation_deleted(self, automation_id: str) -> None:
        """Handle an automation being deleted.

        Cleans up any ACL entries created for this automation.

        Args:
            automation_id: The entity_id of the deleted automation.
        """
        LOGGER.info("Cleaning up ACL entries for deleted automation: %s", automation_id)

        # Get and remove ACL entries from store
        acl_entries = await self._store.async_remove_acls_for_automation(automation_id)

        # Remove ACL entries from devices
        if acl_entries:
            await self._async_remove_acl_entries_from_devices(
                automation_id, acl_entries
            )
            LOGGER.info(
                "Cleaned up %d ACL entries for automation %s",
                len(acl_entries),
                automation_id,
            )
        else:
            LOGGER.debug("No ACL entries to clean up for automation %s", automation_id)

        # Also clear scanned status and eligibility result
        await self._store.async_clear_scanned_automation(automation_id)

    async def _async_check_and_log_automation(
        self, automation_id: str, is_new: bool
    ) -> None:
        """Check automation eligibility and log the result.

        Args:
            automation_id: The entity_id of the automation to check.
            is_new: True if this is a newly created automation.
        """
        action = "created" if is_new else "updated"

        # If this is an update, clear the previous scanned status and ACLs so we recheck
        if not is_new:
            # Mark as not scanned so we re-evaluate
            await self._store.async_clear_scanned_automation(automation_id)
            LOGGER.debug("Cleared previous scan status for %s", automation_id)

            # Remove any existing ACLs for this automation
            old_acls = await self._store.async_remove_acls_for_automation(automation_id)
            if old_acls:
                await self._async_remove_acl_entries_from_devices(
                    automation_id, old_acls
                )

        # Check eligibility
        (
            is_eligible,
            status,
            trigger_entities,
            action_entities,
            reason,
        ) = await self.check_automation_eligibility(automation_id)

        # Store the result
        await self._store.async_set_eligibility_result(
            automation_id,
            status,
            trigger_entities,
            action_entities,
            reason,
        )

        # Mark as scanned
        await self._store.async_mark_automation_scanned(automation_id)

        # Log the result and create ACLs if eligible
        if is_eligible:
            LOGGER.info(
                "✓ ELIGIBLE: Automation %s (%s) is eligible for Matter binding",
                automation_id,
                action,
            )
            LOGGER.info("  Trigger entities: %s", trigger_entities)
            LOGGER.info("  Action entities: %s", action_entities)

            # Create ACL entries for this automation
            await self._async_create_acl_for_automation(
                automation_id, trigger_entities, action_entities
            )
        else:
            LOGGER.info(
                "✗ INELIGIBLE: Automation %s (%s) is not eligible for Matter binding",
                automation_id,
                action,
            )
            LOGGER.info("  Reason: %s", reason)

    async def _async_create_acl_for_automation(
        self,
        automation_id: str,
        trigger_entities: list[str],
        action_entities: list[str],
    ) -> None:
        """Create ACL entries for an eligible automation.

        This grants the trigger device(s) access to control the action device(s).

        Args:
            automation_id: The entity_id of the automation.
            trigger_entities: List of trigger entity IDs (source devices).
            action_entities: List of action entity IDs (target devices).
        """
        LOGGER.info("Creating ACL entries for automation %s", automation_id)

        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available, cannot create ACLs")
            return

        # Get node IDs for trigger and action entities
        trigger_node_ids = await self._get_node_ids_for_entities(trigger_entities)
        action_node_ids = await self._get_node_ids_for_entities(action_entities)

        if not trigger_node_ids:
            LOGGER.warning(
                "Could not find node IDs for trigger entities: %s", trigger_entities
            )
            return

        if not action_node_ids:
            LOGGER.warning(
                "Could not find node IDs for action entities: %s", action_entities
            )
            return

        LOGGER.debug("Trigger node IDs: %s", trigger_node_ids)
        LOGGER.debug("Action node IDs: %s", action_node_ids)

        # For each target (action) device, add ACL entry granting access to source (trigger) devices
        for target_node_id in action_node_ids:
            for source_node_id in trigger_node_ids:
                try:
                    # Read current ACL from target device
                    acl_path = "0/31/0"  # Endpoint 0, AccessControl cluster (31), Acl attribute (0)
                    current_acls = await matter_client.read_attribute(
                        target_node_id, acl_path
                    )
                    current_acl_list = current_acls.get(acl_path, [])

                    LOGGER.debug(
                        "Current ACL for node %d: %s",
                        target_node_id,
                        current_acl_list,
                    )

                    # Check if we already have an ACL entry for this source node
                    # ACL entries have 'subjects' field containing node IDs
                    already_exists = False
                    for acl_entry in current_acl_list:
                        subjects = acl_entry.get("subjects", [])
                        if source_node_id in subjects:
                            LOGGER.debug(
                                "ACL entry already exists for source node %d on target %d",
                                source_node_id,
                                target_node_id,
                            )
                            already_exists = True
                            break

                    if already_exists:
                        continue

                    # Create new ACL entry granting Operate privilege (3) to the source node
                    # ACL entry structure:
                    # - privilege: 3 (Operate - can invoke commands)
                    # - authMode: 2 (CASE - standard auth)
                    # - subjects: list of node IDs
                    # - targets: None (all endpoints/clusters)
                    new_acl_entry = {
                        "privilege": 3,  # Operate
                        "authMode": 2,  # CASE
                        "subjects": [source_node_id],
                        "targets": None,  # All endpoints/clusters
                    }

                    # Append to existing ACL list
                    updated_acl_list = [*current_acl_list, new_acl_entry]
                    acl_index = len(current_acl_list)  # Index of new entry

                    # Write updated ACL using the Matter server API
                    await matter_client.send_command(
                        APICommand.SET_ACL_ENTRY,
                        node_id=target_node_id,
                        entry=updated_acl_list,
                    )

                    LOGGER.info(
                        "Created ACL entry on node %d granting access to node %d",
                        target_node_id,
                        source_node_id,
                    )

                    # Store ACL reference for cleanup
                    acl_entry_dict: AclEntryDict = {
                        "target_node_id": target_node_id,
                        "source_node_id": source_node_id,
                        "endpoint_id": 0,
                        "acl_index": acl_index,
                    }
                    await self._store.async_add_acl(automation_id, acl_entry_dict)

                except (HomeAssistantError, OSError, ValueError) as err:
                    LOGGER.error(
                        "Failed to create ACL entry on node %d for source %d: %s",
                        target_node_id,
                        source_node_id,
                        err,
                    )

    async def _async_remove_acl_entries_from_devices(
        self,
        automation_id: str,
        acl_entries: list[AclEntryDict],
    ) -> None:
        """Remove ACL entries from Matter devices.

        Args:
            automation_id: The entity_id of the automation (for logging).
            acl_entries: List of ACL entries to remove.
        """
        LOGGER.info(
            "Removing %d ACL entries for automation %s",
            len(acl_entries),
            automation_id,
        )

        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available, cannot remove ACLs")
            return

        for acl_entry in acl_entries:
            target_node_id = acl_entry["target_node_id"]
            source_node_id = acl_entry["source_node_id"]

            try:
                # Read current ACL from target device
                acl_path = "0/31/0"
                current_acls = await matter_client.read_attribute(
                    target_node_id, acl_path
                )
                current_acl_list = current_acls.get(acl_path, [])

                # Find and remove ACL entries that grant access to the source node
                updated_acl_list = []
                removed_count = 0
                for existing_acl in current_acl_list:
                    subjects = existing_acl.get("subjects", [])
                    if source_node_id in subjects:
                        # Remove entries that grant access to this source
                        LOGGER.debug(
                            "Removing ACL entry on node %d that grants access to node %d",
                            target_node_id,
                            source_node_id,
                        )
                        removed_count += 1
                    else:
                        updated_acl_list.append(existing_acl)

                if removed_count > 0:
                    # Write updated ACL
                    await matter_client.send_command(
                        APICommand.SET_ACL_ENTRY,
                        node_id=target_node_id,
                        entry=updated_acl_list,
                    )
                    LOGGER.info(
                        "Removed %d ACL entries from node %d",
                        removed_count,
                        target_node_id,
                    )

            except (HomeAssistantError, OSError, ValueError) as err:
                LOGGER.error(
                    "Failed to remove ACL entry from node %d: %s",
                    target_node_id,
                    err,
                )

    async def _get_node_ids_for_entities(self, entity_ids: list[str]) -> list[int]:
        """Get Matter node IDs for a list of entity IDs.

        Args:
            entity_ids: List of entity IDs.

        Returns:
            List of Matter node IDs.
        """
        node_ids: list[int] = []

        try:
            get_matter(self._hass)
        except (KeyError, StopIteration):
            return node_ids

        if self._entity_registry is None:
            return node_ids

        for entity_id in entity_ids:
            entity_entry = self._entity_registry.async_get(entity_id)
            if entity_entry is None or entity_entry.device_id is None:
                continue

            # Get the device
            if self._device_registry is None:
                continue

            device = self._device_registry.async_get(entity_entry.device_id)
            if device is None:
                continue

            # Find Matter node ID from device identifiers
            for domain, identifier in device.identifiers:
                if domain == MATTER_DOMAIN:
                    # Matter device identifiers can be in different formats:
                    # 1. Simple node ID (integer)
                    # 2. "deviceid_FABRIC-NODEID-MatterNodeDevice" format
                    # 3. "serial_XXXXX" format (not useful for node ID)

                    node_id: int | None = None

                    # Try to parse as simple integer first
                    with contextlib.suppress(ValueError):
                        node_id = int(identifier)

                    # Try to parse "deviceid_FABRIC-NODEID-MatterNodeDevice" format
                    if node_id is None and identifier.startswith("deviceid_"):
                        try:
                            # Format: deviceid_FABRIC-NODEID-MatterNodeDevice
                            # Example: deviceid_7AF1B06B5DA7FC6B-0000000000000004-MatterNodeDevice
                            parts = identifier.split("-")
                            if len(parts) >= 2:
                                # The second part is the node ID as a hex string
                                node_id_hex = parts[1]
                                node_id = int(node_id_hex, 16)
                                LOGGER.debug(
                                    "Parsed node ID %d from deviceid identifier: %s",
                                    node_id,
                                    identifier,
                                )
                        except (ValueError, IndexError) as parse_err:
                            LOGGER.debug(
                                "Could not parse deviceid format: %s - %s",
                                identifier,
                                parse_err,
                            )

                    if node_id is not None and node_id not in node_ids:
                        node_ids.append(node_id)
                        LOGGER.debug(
                            "Found node ID %d for entity %s",
                            node_id,
                            entity_id,
                        )
                    elif node_id is None:
                        LOGGER.debug(
                            "Could not extract node ID from identifier: %s",
                            identifier,
                        )

        return node_ids

    async def async_scan_automations(self) -> dict[str, list[str]]:
        """Scan all automations and identify Matter-eligible ones.

        Returns:
            Dictionary mapping automation_id to list of Matter entity_ids found.
        """
        LOGGER.info("=" * 60)
        LOGGER.info("MATTER AUTOBIND: Starting automation scan")
        LOGGER.info("=" * 60)

        results: dict[str, list[str]] = {}

        # Get all automation entity IDs
        automation_entity_ids = self._hass.states.async_entity_ids(AUTOMATION_DOMAIN)
        LOGGER.info("Found %d automations to scan", len(automation_entity_ids))

        eligible_count = 0
        ineligible_count = 0

        for automation_id in automation_entity_ids:
            # Check if already scanned
            if self._store.is_automation_scanned(automation_id):
                existing_result = self._store.get_eligibility_result(automation_id)
                if existing_result:
                    LOGGER.debug(
                        "Automation %s already checked: %s",
                        automation_id,
                        existing_result["status"],
                    )
                continue

            LOGGER.info("-" * 40)
            LOGGER.info("Checking automation: %s", automation_id)

            # Check eligibility
            (
                is_eligible,
                status,
                trigger_entities,
                action_entities,
                reason,
            ) = await self.check_automation_eligibility(automation_id)

            # Store the result
            await self._store.async_set_eligibility_result(
                automation_id,
                status,
                trigger_entities,
                action_entities,
                reason,
            )

            if is_eligible:
                eligible_count += 1
                LOGGER.info("✓ ELIGIBLE: %s", automation_id)
                LOGGER.info("  Trigger entities: %s", trigger_entities)
                LOGGER.info("  Action entities: %s", action_entities)
                results[automation_id] = trigger_entities + action_entities
            else:
                ineligible_count += 1
                LOGGER.info("✗ INELIGIBLE: %s", automation_id)
                LOGGER.info("  Reason: %s", reason)

            # Mark as scanned
            await self._store.async_mark_automation_scanned(automation_id)

        LOGGER.info("=" * 60)
        LOGGER.info("MATTER AUTOBIND: Automation scan complete")
        LOGGER.info("  Eligible: %d", eligible_count)
        LOGGER.info("  Ineligible: %d", ineligible_count)
        LOGGER.info("=" * 60)
        return results

    async def check_automation_eligibility(
        self, automation_id: str
    ) -> tuple[bool, EligibilityStatus, list[str], list[str], str]:
        """Check if an automation is eligible for Matter binding.

        An automation is eligible if:
        1. Trigger devices are Matter devices with client clusters + binding cluster
        2. Action devices are Matter devices
        3. No conditions are present

        Args:
            automation_id: The entity_id of the automation.

        Returns:
            Tuple of:
            - is_eligible: True if the automation can be bound
            - status: EligibilityStatus enum value
            - trigger_entities: List of trigger Matter entity IDs
            - action_entities: List of action Matter entity IDs
            - reason: Human-readable reason for the status
        """
        trigger_entities: list[str] = []
        action_entities: list[str] = []

        # Get the automation entity
        state = self._hass.states.get(automation_id)
        if not state:
            return (
                False,
                EligibilityStatus.NOT_CHECKED,
                [],
                [],
                f"Automation {automation_id} not found",
            )

        # Get the automation's config to check for conditions
        # We need to access the automation component to get the raw config
        automations = self._hass.data.get(AUTOMATION_DOMAIN, {})
        # Find the automation entity in the component (reserved for future use)
        # Currently we check conditions via referenced entities/devices
        _ = automations  # Silence unused variable warning

        # Check for conditions - we can infer from the state attributes
        # If the automation has complex logic, it's not suitable for binding
        # For now, we'll check the referenced entities/devices

        # Get referenced entities (actions)
        referenced_entities = await self._get_automation_referenced_entities(
            automation_id
        )

        # Get referenced devices
        referenced_devices = await self._get_automation_referenced_devices(
            automation_id
        )

        LOGGER.debug("Referenced entities: %s", referenced_entities)
        LOGGER.debug("Referenced devices: %s", referenced_devices)

        # Check if we have both trigger and action devices
        # For simplicity, the first Matter device found is the trigger,
        # and the second is the action target

        all_matter_entities: list[str] = []

        # Check entities
        for entity_id in referenced_entities:
            if self._is_matter_entity(entity_id):
                all_matter_entities.append(entity_id)
                LOGGER.debug("  Matter entity: %s", entity_id)

        # Check devices
        for device_id in referenced_devices:
            device_matter_entities = self._get_matter_entities_for_device(device_id)
            for entity_id in device_matter_entities:
                if entity_id not in all_matter_entities:
                    all_matter_entities.append(entity_id)
                    LOGGER.debug("  Matter entity (via device): %s", entity_id)

        # We need at least 2 Matter entities (one for trigger, one for action)
        if len(all_matter_entities) < 2:
            if len(all_matter_entities) == 0:
                return (
                    False,
                    EligibilityStatus.INELIGIBLE_NO_MATTER_TRIGGER,
                    [],
                    [],
                    "No Matter devices found in automation",
                )
            return (
                False,
                EligibilityStatus.INELIGIBLE_NO_MATTER_ACTION,
                all_matter_entities,
                [],
                f"Only {len(all_matter_entities)} Matter device found, need at least 2 (trigger + action)",
            )

        # For now, assume first entity is trigger, rest are actions
        # TODO: Analyze the actual trigger/action structure for accuracy
        trigger_entities = [all_matter_entities[0]]
        action_entities = all_matter_entities[1:]

        # Check if trigger device has binding cluster
        # This requires checking the actual Matter node
        trigger_has_binding = await self._check_entity_has_binding_cluster(
            trigger_entities[0]
        )

        if not trigger_has_binding:
            return (
                False,
                EligibilityStatus.INELIGIBLE_NO_BINDING_CLUSTER,
                trigger_entities,
                action_entities,
                f"Trigger device {trigger_entities[0]} does not have Binding cluster",
            )

        # Check if trigger device has appropriate client cluster (e.g., OnOff Client)
        trigger_has_client = await self._check_entity_has_client_cluster(
            trigger_entities[0]
        )

        if not trigger_has_client:
            return (
                False,
                EligibilityStatus.INELIGIBLE_NO_CLIENT_CLUSTER,
                trigger_entities,
                action_entities,
                f"Trigger device {trigger_entities[0]} does not have client cluster",
            )

        # All checks passed!
        return (
            True,
            EligibilityStatus.ELIGIBLE,
            trigger_entities,
            action_entities,
            "Automation is eligible for Matter binding",
        )

    async def _check_entity_has_binding_cluster(self, entity_id: str) -> bool:
        """Check if an entity's Matter device has the Binding cluster.

        Args:
            entity_id: The entity_id to check.

        Returns:
            True if the device has the Binding cluster.
        """
        # Get the Matter node for this entity
        try:
            matter = get_matter(self._hass)
        except (KeyError, StopIteration):
            LOGGER.debug("Matter integration not available")
            return False

        # Get device from entity registry
        if self._entity_registry is None:
            return False

        entity_entry = self._entity_registry.async_get(entity_id)
        if entity_entry is None or entity_entry.device_id is None:
            return False

        # Get the node from device
        device = self._device_registry.async_get(entity_entry.device_id)
        if device is None:
            return False

        # Check if any endpoint has the Binding cluster
        for node in matter.matter_client.get_nodes():
            for endpoint in node.endpoints.values():
                if endpoint.has_cluster(CLUSTER_ID_BINDING):
                    LOGGER.debug(
                        "Node %d endpoint %d has Binding cluster",
                        node.node_id,
                        endpoint.endpoint_id,
                    )
                    return True

        return False

    async def _check_entity_has_client_cluster(self, entity_id: str) -> bool:
        """Check if an entity's Matter device has a client cluster.

        Client clusters are identified by checking device types like
        OnOffLightSwitch, DimmerSwitch, etc.

        Args:
            entity_id: The entity_id to check.

        Returns:
            True if the device has an appropriate client cluster.
        """

        try:
            matter = get_matter(self._hass)
        except (KeyError, StopIteration):
            LOGGER.debug("Matter integration not available")
            return False

        # For now, check if any node has client device types
        client_device_types = (
            device_types.OnOffLightSwitch,
            device_types.DimmerSwitch,
            device_types.ColorDimmerSwitch,
        )

        for node in matter.matter_client.get_nodes():
            for endpoint in node.endpoints.values():
                for dt in endpoint.device_types:
                    if dt in client_device_types:
                        LOGGER.debug(
                            "Node %d endpoint %d has client device type: %s",
                            node.node_id,
                            endpoint.endpoint_id,
                            dt.__name__,
                        )
                        return True

        return False

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
        """Check if an entity belongs to the Matter or Matter AutoBind integration.

        Args:
            entity_id: The entity_id to check.

        Returns:
        True if the entity is from the Matter or Matter AutoBind integration.
        """

        if self._entity_registry is None:
            return False

        entity_entry = self._entity_registry.async_get(entity_id)
        if entity_entry is None:
            return False

        # Accept entities from both the official matter integration
        # and our matter_autobind integration (for client cluster entities)
        return entity_entry.platform in (MATTER_DOMAIN, AUTOBIND_DOMAIN)

    @callback
    def _get_matter_entities_for_device(self, device_id: str) -> list[str]:
        """Get all Matter or Matter AutoBind entities for a device.

        Args:
            device_id: The device_id to check.

        Returns:
        List of Matter/Matter AutoBind entity_ids for the device.
        """

        if self._entity_registry is None:
            return []

        # Accept entities from both the official matter integration
        # and our matter_autobind integration (for client cluster entities)
        return [
            entity.entity_id
            for entity in er.async_entries_for_device(
                self._entity_registry, device_id, include_disabled_entities=False
            )
            if entity.platform in (MATTER_DOMAIN, AUTOBIND_DOMAIN)
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
