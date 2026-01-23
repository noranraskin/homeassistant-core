"""Manager for Matter AutoBind integration."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import secrets
import time
from typing import TYPE_CHECKING, Any

from chip.clusters import Objects as Clusters
from matter_server.client.models import device_types
from matter_server.common.models import EventType

from homeassistant.components.automation import (
    DATA_COMPONENT,
    DOMAIN as AUTOMATION_DOMAIN,
    devices_in_automation,
    entities_in_automation,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_ENTITY_ID, CONF_PLATFORM
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

from .automation import is_physical_state_change
from .const import (
    CLUSTER_ID_BINDING,
    CLUSTER_ID_ON_OFF,
    CONF_ENABLE_GROUP_BINDINGS,
    DEBUG_OVERWRITE_ACLS,
    DOMAIN as AUTOBIND_DOMAIN,
    LOGGER,
)
from .store import (
    AclEntryDict,
    BindingEntryDict,
    EligibilityStatus,
    MatterBindingStore,
    acl_key,
    binding_key,
    group_key,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

from homeassistant.components.matter.helpers import (
    get_matter,
    get_node_from_device_entry,
)

from .discovery import async_discover_client_cluster_entities

# Matter domain constant
MATTER_DOMAIN = "matter"


@dataclass
class StatefulSwitchInfo:
    """Information about a stateful switch entity.

    Stateful switches have both server and client clusters on the same endpoint.
    They need special handling to track whether state changes are from UI or
    physical device interactions.
    """

    entity_id: str
    """The entity_id of the stateful switch."""

    node_id: int
    """The Matter node ID."""

    endpoint_id: int
    """The endpoint ID with both server and client clusters."""

    client_clusters: list[int]
    """List of client cluster IDs available for binding."""


@dataclass
class NodeInfo:
    """Information about a Matter node derived from an entity."""

    entity_id: str
    """The entity_id."""

    node_id: int
    """The Matter node ID."""

    endpoint_id: int
    """The endpoint ID (typically 1 for most devices)."""


@dataclass
class DesiredResourceState:
    """Represents the desired resource state for an automation.

    This is computed from the automation's trigger and action entities,
    then compared against the current state to determine what operations
    are needed to reconcile.
    """

    # Set of (target_node_id, source_node_id, endpoint_id) tuples
    acls: set[tuple[int, int, int]]

    # Set of (source_node, source_ep, target (int or "gXXXX"), target_ep) tuples
    bindings: set[tuple[int, int, int | str, int]]

    # Set of (group_id, frozenset of (node_id, endpoint_id)) tuples
    groups: set[tuple[int, frozenset[tuple[int, int]]]]

    # The group ID to use if needed (None if single target)
    group_id: int | None = None


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
        # Track stateful switches (entities with both server + client clusters)
        # Key: entity_id, Value: StatefulSwitchInfo
        self._stateful_switches: dict[str, StatefulSwitchInfo] = {}
        # Track trigger entities for automation suppression
        # Key: trigger entity_id, Value: automation_id
        self._trigger_to_automation: dict[str, str] = {}
        # Track unsubscribe callbacks for trigger entity state changes
        # Key: trigger entity_id, Value: unsubscribe callback
        self._unsub_trigger_listeners: dict[str, CALLBACK_TYPE] = {}
        # Track automations currently being suppressed (temporary disable)
        # Used to prevent binding removal when we temporarily disable automation
        self._suppressing_automations: set[str] = set()

    @property
    def store(self) -> MatterBindingStore:
        """Return the store instance."""
        return self._store

    @callback
    def _subscribe_to_trigger_entity(
        self, trigger_entity_id: str, automation_id: str
    ) -> None:
        """Subscribe to state changes on a trigger entity for automation suppression.

        When a physical state change is detected (no user_id in context),
        the associated automation will be temporarily disabled to prevent
        duplicate execution (since the binding handles the action directly).

        Args:
            trigger_entity_id: The entity_id of the trigger device.
            automation_id: The automation entity_id to suppress.
        """
        # Skip if already subscribed
        if trigger_entity_id in self._unsub_trigger_listeners:
            LOGGER.debug(
                "Already subscribed to %s for automation suppression",
                trigger_entity_id,
            )
            return

        # Store the mapping
        self._trigger_to_automation[trigger_entity_id] = automation_id

        # Subscribe to state changes
        unsub = async_track_state_change_event(
            self._hass,
            [trigger_entity_id],
            self._handle_trigger_state_change,
        )
        self._unsub_trigger_listeners[trigger_entity_id] = unsub

        LOGGER.info(
            "Subscribed to %s for physical interaction detection (automation: %s)",
            trigger_entity_id,
            automation_id,
        )

    @callback
    def _unsubscribe_from_trigger_entity(self, trigger_entity_id: str) -> None:
        """Unsubscribe from state changes on a trigger entity.

        Args:
            trigger_entity_id: The entity_id to unsubscribe from.
        """
        if trigger_entity_id in self._unsub_trigger_listeners:
            self._unsub_trigger_listeners[trigger_entity_id]()
            del self._unsub_trigger_listeners[trigger_entity_id]
            LOGGER.debug("Unsubscribed from %s", trigger_entity_id)

        if trigger_entity_id in self._trigger_to_automation:
            del self._trigger_to_automation[trigger_entity_id]

    @callback
    def _handle_trigger_state_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle state change on a bound trigger entity.

        If the state change is from a physical interaction (no user_id),
        temporarily disable the associated automation to prevent it from
        running (the binding already handles the action).

        Args:
            event: The state changed event.
        """
        entity_id = event.data["entity_id"]
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]

        # Skip if states are the same (attribute-only change)
        if old_state is not None and new_state is not None:
            if old_state.state == new_state.state:
                return

        # Check if this is a physical state change
        if not is_physical_state_change(event, LOGGER):
            LOGGER.debug(
                "State change on %s was from UI/automation - letting automation run",
                entity_id,
            )
            return

        # Get the associated automation
        automation_id = self._trigger_to_automation.get(entity_id)
        if automation_id is None:
            LOGGER.debug(
                "No automation associated with trigger %s, skipping suppression",
                entity_id,
            )
            return

        # Temporarily disable the automation to prevent it from running
        LOGGER.info(
            "Physical interaction detected on %s - temporarily disabling automation %s",
            entity_id,
            automation_id,
        )
        self._hass.async_create_task(self._async_suppress_automation(automation_id))

    async def _async_suppress_automation(self, automation_id: str) -> None:
        """Temporarily disable an automation to prevent it from running.

        The automation is disabled, waits briefly, then re-enabled.
        This prevents the automation from triggering when a physical
        button press occurs (since the binding handles the action).

        Args:
            automation_id: The automation entity_id to suppress.
        """
        try:
            # Mark automation as being suppressed BEFORE disabling
            # This prevents _handle_automation_updated from removing bindings
            self._suppressing_automations.add(automation_id)

            # Disable the automation
            await self._hass.services.async_call(
                "automation",
                "turn_off",
                {"entity_id": automation_id},
                blocking=True,
            )
            LOGGER.debug("Disabled automation %s", automation_id)

            # Wait briefly to ensure automation doesn't trigger
            # This is a short window since the state change event propagates quickly
            await asyncio.sleep(0.5)

            # Re-enable the automation
            await self._hass.services.async_call(
                "automation",
                "turn_on",
                {"entity_id": automation_id},
                blocking=True,
            )
            LOGGER.info(
                "Re-enabled automation %s after physical interaction suppression",
                automation_id,
            )

        except Exception as err:  # noqa: BLE001
            LOGGER.error(
                "Failed to suppress automation %s: %s",
                automation_id,
                err,
            )
        finally:
            # Always remove from suppression set when done
            self._suppressing_automations.discard(automation_id)

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

        # Subscribe to Matter node events for new device discovery
        self._subscribe_to_matter_node_events()

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

        # Unsubscribe from trigger entity state changes
        for unsub in self._unsub_trigger_listeners.values():
            unsub()
        self._unsub_trigger_listeners.clear()
        self._trigger_to_automation.clear()

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
    def _subscribe_to_matter_node_events(self) -> None:
        """Subscribe to Matter node events for new device discovery.

        When a new Matter device is added, we discover any client cluster
        entities that may need to be created.
        """
        LOGGER.debug("Subscribing to Matter node events")

        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.debug(
                "Matter integration not available, skipping node event subscription"
            )
            return

        def handle_node_added(event: EventType, node) -> None:
            """Handle a new Matter node being added."""
            LOGGER.info(
                "Matter node %d added, discovering client cluster entities",
                node.node_id,
            )
            # Schedule discovery for the new node
            self._hass.async_create_task(
                self._async_discover_client_clusters_for_node(node)
            )

        unsub = matter_client.subscribe_events(
            callback=handle_node_added, event_filter=EventType.NODE_ADDED
        )
        self._unsub_automation_listeners.append(unsub)
        LOGGER.info("Subscribed to Matter node events")

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

        # Handle deletion - clean up ACLs and bindings
        if new_state is None:
            LOGGER.info(
                "Automation %s was deleted, cleaning up ACLs and bindings", entity_id
            )
            self._hass.async_create_task(
                self._async_handle_automation_deleted(entity_id)
            )
            return

        # Skip new automations - they're handled by _handle_automation_added
        if old_state is None:
            LOGGER.debug(
                "Automation %s is new, skipping update handler (handled by add handler)",
                entity_id,
            )
            return

        # Handle disabled - remove bindings only (keep ACLs for re-enabling)
        if (
            old_state is not None
            and old_state.state == "on"
            and new_state.state == "off"
        ):
            # Check if this is our own temporary suppression - skip binding removal
            if entity_id in self._suppressing_automations:
                LOGGER.debug(
                    "Automation %s is being suppressed by us, skipping binding removal",
                    entity_id,
                )
                return

            LOGGER.info(
                "Automation %s was disabled, removing bindings (keeping ACLs)",
                entity_id,
            )
            self._hass.async_create_task(
                self._async_handle_automation_disabled(entity_id)
            )
            return

        # Handle enabled - recreate bindings
        if (
            old_state is not None
            and old_state.state == "off"
            and new_state.state == "on"
        ):
            # Check if this is our own re-enabling after suppression - skip binding recreation
            if entity_id in self._suppressing_automations:
                LOGGER.debug(
                    "Automation %s is being re-enabled after suppression, skipping binding recreation",
                    entity_id,
                )
                return

            LOGGER.info("Automation %s was enabled, recreating bindings", entity_id)
            self._hass.async_create_task(
                self._async_handle_automation_enabled(entity_id)
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

        Uses reference-counted resource release which only removes ACLs,
        bindings, and groups from devices when no other automation still
        needs them.

        Args:
            automation_id: The entity_id of the deleted automation.
        """
        LOGGER.info("Cleaning up resources for deleted automation: %s", automation_id)

        # Release all resources with reference counting
        await self._release_all_resources(automation_id)

        # Also clear scanned status and eligibility result
        await self._store.async_clear_scanned_automation(automation_id)

        # Unsubscribe from trigger entities for this automation
        for trigger_entity_id, auto_id in list(self._trigger_to_automation.items()):
            if auto_id == automation_id:
                self._unsubscribe_from_trigger_entity(trigger_entity_id)

    async def _async_handle_automation_disabled(self, automation_id: str) -> None:
        """Handle an automation being disabled.

        Releases all resources since the automation shouldn't affect
        devices while disabled.

        Args:
            automation_id: The entity_id of the disabled automation.
        """
        LOGGER.info("Releasing resources for disabled automation: %s", automation_id)

        # Release all resources
        await self._release_all_resources(automation_id)

    async def _async_handle_automation_enabled(self, automation_id: str) -> None:
        """Handle an automation being re-enabled.

        Recreates all resources using reconciliation.

        Args:
            automation_id: The entity_id of the enabled automation.
        """
        LOGGER.info("Recreating resources for enabled automation: %s", automation_id)

        # Get eligibility result to find trigger/action entities
        result = self._store.get_eligibility_result(automation_id)
        if result is None:
            LOGGER.warning(
                "No eligibility result found for automation %s, cannot recreate resources",
                automation_id,
            )
            return

        if result["status"] != EligibilityStatus.ELIGIBLE:
            LOGGER.debug(
                "Automation %s is not eligible, skipping resource recreation",
                automation_id,
            )
            return

        trigger_entities = result["trigger_entities"]
        action_entities = result["action_entities"]

        # Use reconciliation to recreate all resources
        await self._reconcile_automation_resources(
            automation_id, trigger_entities, action_entities
        )

    async def _async_check_and_log_automation(
        self, automation_id: str, is_new: bool
    ) -> None:
        """Check automation eligibility and set up resources using reconciliation.

        Uses the declarative reconciliation approach that handles both new and
        updated automations uniformly by computing desired vs current state.

        Args:
            automation_id: The entity_id of the automation to check.
            is_new: True if this is a newly created automation.
        """
        action = "created" if is_new else "updated"

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

        if is_eligible:
            LOGGER.info(
                "✓ ELIGIBLE: Automation %s (%s) is eligible for Matter binding",
                automation_id,
                action,
            )
            LOGGER.info("  Trigger entities: %s", trigger_entities)
            LOGGER.info("  Action entities: %s", action_entities)

            # Use reconciliation to set up resources
            # This handles both new automations and updates uniformly
            await self._reconcile_automation_resources(
                automation_id, trigger_entities, action_entities
            )

            # Subscribe to trigger entity state changes for automation suppression
            for trigger_entity_id in trigger_entities:
                self._subscribe_to_trigger_entity(trigger_entity_id, automation_id)
        else:
            # If previously eligible, release resources
            if not is_new:
                await self._release_all_resources(automation_id)

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
                    # ACL entries may be dicts with numeric keys or dataclass objects
                    already_exists = False
                    for acl_entry in current_acl_list:
                        # Handle both dict format ("3" key) and dataclass format (subjects attr)
                        if isinstance(acl_entry, dict):
                            subjects = acl_entry.get("3", []) or []
                        else:
                            subjects = getattr(acl_entry, "subjects", []) or []
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

                    # Get fabric_id from server_info (required for ACL entries)
                    server_info = matter_client.server_info
                    if server_info is None:
                        LOGGER.error("Matter server info not available")
                        continue
                    fabric_id = server_info.fabric_id

                    # Create new ACL entry using raw TLV dict format
                    # (matching the format used by the working binding script)
                    # TLV keys: "1"=privilege, "2"=authMode, "3"=subjects, "4"=targets, "254"=fabricIndex
                    new_acl_entry = {
                        "254": fabric_id,  # fabricIndex
                        "1": 3,  # Operate privilege
                        "2": 2,  # CASE authMode
                        "3": [source_node_id],  # subjects
                        "4": None,  # targets (None = all endpoints/clusters)
                    }

                    LOGGER.debug(
                        "New ACL entry to add: %s",
                        new_acl_entry,
                    )

                    # Filter out invalid/incomplete ACL entries before writing
                    # Valid ACL entries must have privilege ('1') and authMode ('2')
                    def is_valid_acl_entry(entry):
                        if isinstance(entry, dict):
                            has_privilege = "1" in entry
                            has_auth_mode = "2" in entry
                            return has_privilege and has_auth_mode
                        # For dataclass entries, check for privilege attribute
                        return hasattr(entry, "privilege") and hasattr(
                            entry, "authMode"
                        )

                    valid_acl_entries = [
                        e for e in current_acl_list if is_valid_acl_entry(e)
                    ]

                    if len(valid_acl_entries) < len(current_acl_list):
                        LOGGER.warning(
                            "Filtered out %d invalid ACL entries from node %d",
                            len(current_acl_list) - len(valid_acl_entries),
                            target_node_id,
                        )

                    # Append new entry to valid entries
                    updated_acl_list = [*valid_acl_entries, new_acl_entry]

                    LOGGER.debug(
                        "Full ACL list to write (before): %s",
                        current_acl_list,
                    )
                    LOGGER.debug(
                        "Full ACL list to write (after): %s",
                        updated_acl_list,
                    )

                    # Write ACL using write_attribute method
                    write_response = await matter_client.write_attribute(
                        node_id=target_node_id,
                        attribute_path="0/31/0",  # Endpoint 0, AccessControl cluster (31), Acl attr (0)
                        value=updated_acl_list,
                    )

                    LOGGER.debug(
                        "ACL write response for node %d: %s",
                        target_node_id,
                        write_response,
                    )

                    # Verify by re-reading ACL
                    verify_acls = await matter_client.read_attribute(
                        target_node_id, acl_path
                    )
                    verify_acl_list = verify_acls.get(acl_path, [])
                    LOGGER.info(
                        "ACL VERIFICATION - Node %d now has %d ACL entries (was %d)",
                        target_node_id,
                        len(verify_acl_list),
                        len(current_acl_list),
                    )
                    LOGGER.debug(
                        "Verified ACL on node %d: %s",
                        target_node_id,
                        verify_acl_list,
                    )

                    if len(verify_acl_list) <= len(current_acl_list):
                        LOGGER.error(
                            "ACL WRITE FAILED! Entry was not added to node %d",
                            target_node_id,
                        )
                    else:
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
                        "acl_index": len(updated_acl_list)
                        - 1,  # Index of the new entry
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
                    # Handle both dict format (TLV keys) and dataclass format
                    if isinstance(existing_acl, dict):
                        subjects = existing_acl.get("3", []) or []
                    else:
                        subjects = getattr(existing_acl, "subjects", []) or []
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
                    await matter_client.write_attribute(
                        node_id=target_node_id,
                        attribute_path=acl_path,
                        value=updated_acl_list,
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

    async def _async_create_bindings_for_automation(
        self,
        automation_id: str,
        trigger_entities: list[str],
        action_entities: list[str],
    ) -> None:
        """Create bindings on trigger devices pointing to target devices.

        For each trigger device, creates bindings to ALL target devices.

        Args:
            automation_id: The entity_id of the automation.
            trigger_entities: List of trigger entity IDs (source devices).
            action_entities: List of action entity IDs (target devices).
        """
        LOGGER.info("Creating bindings for automation %s", automation_id)

        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available, cannot create bindings")
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

        LOGGER.debug("Creating bindings - Trigger node IDs: %s", trigger_node_ids)
        LOGGER.debug("Creating bindings - Action node IDs: %s", action_node_ids)

        # For each trigger device, add bindings to ALL target devices
        for source_node_id in trigger_node_ids:
            try:
                # Read current bindings from the trigger device
                # Binding cluster (30) is typically on endpoint 1 for switches
                binding_path = (
                    "1/30/0"  # Endpoint 1, Binding cluster (30), Binding attribute (0)
                )

                current_bindings: list[dict] = []
                binding_read_success = False

                # Try endpoint 1 first
                try:
                    current_bindings_resp = await matter_client.read_attribute(
                        source_node_id, binding_path
                    )
                    raw_bindings = current_bindings_resp.get(binding_path, [])

                    # Check if response is an error dict (device doesn't support binding)
                    if isinstance(raw_bindings, dict):
                        if raw_bindings.get("TLVValue") is None or raw_bindings.get(
                            "Reason"
                        ):
                            LOGGER.debug(
                                "Node %d binding cluster error on endpoint 1: %s",
                                source_node_id,
                                raw_bindings.get("Reason", "Unknown error"),
                            )
                        else:
                            binding_read_success = True
                    elif isinstance(raw_bindings, list):
                        current_bindings = raw_bindings
                        binding_read_success = True
                except (HomeAssistantError, OSError, ValueError):
                    LOGGER.debug(
                        "Failed to read bindings from endpoint 1 on node %d",
                        source_node_id,
                    )

                # Try endpoint 0 if endpoint 1 failed
                if not binding_read_success:
                    binding_path = "0/30/0"
                    try:
                        current_bindings_resp = await matter_client.read_attribute(
                            source_node_id, binding_path
                        )
                        raw_bindings = current_bindings_resp.get(binding_path, [])

                        # Check if response is an error dict
                        if isinstance(raw_bindings, dict):
                            if raw_bindings.get("TLVValue") is None or raw_bindings.get(
                                "Reason"
                            ):
                                LOGGER.warning(
                                    "Node %d does not support binding cluster: %s",
                                    source_node_id,
                                    raw_bindings.get("Reason", "Unknown error"),
                                )
                                continue
                        elif isinstance(raw_bindings, list):
                            current_bindings = raw_bindings
                            binding_read_success = True
                    except (HomeAssistantError, OSError, ValueError):
                        LOGGER.warning(
                            "Could not read bindings from node %d, device may not support binding",
                            source_node_id,
                        )
                        continue

                if not binding_read_success:
                    LOGGER.warning(
                        "Could not read bindings from node %d - no valid binding cluster found",
                        source_node_id,
                    )
                    continue

                LOGGER.debug(
                    "Current bindings on node %d: %s",
                    source_node_id,
                    current_bindings,
                )

                # Build new bindings list - add bindings to all target devices
                new_bindings = list(current_bindings)
                endpoint_id = int(binding_path.split("/")[0])

                for target_node_id in action_node_ids:
                    # Check if binding already exists
                    # Handle both dict format (TLV keys) and dataclass format
                    binding_exists = False
                    for existing_binding in current_bindings:
                        if isinstance(existing_binding, dict):
                            # Key "1" is node in TLV encoding
                            existing_node = existing_binding.get("1")
                        else:
                            existing_node = getattr(existing_binding, "node", None)
                        if existing_node == target_node_id:
                            binding_exists = True
                            break

                    if binding_exists:
                        LOGGER.debug(
                            "Binding already exists on node %d to target %d",
                            source_node_id,
                            target_node_id,
                        )
                        continue

                    # Get fabric_id from server_info (required for binding entries)
                    server_info = matter_client.server_info
                    if server_info is None:
                        LOGGER.error("Matter server info not available")
                        continue
                    fabric_id = server_info.fabric_id

                    # Create new binding entry using raw TLV dict format
                    # (matching the format used by the working binding script)
                    # TLV keys: "1"=node, "3"=endpoint, "4"=cluster, "254"=fabricIndex
                    new_binding = {
                        "254": fabric_id,  # fabricIndex
                        "1": target_node_id,  # node
                        "3": 1,  # endpoint (typically 1 for lights)
                        "4": None,  # cluster (None = all clusters, like the working script)
                    }
                    new_bindings.append(new_binding)

                    # Store binding reference for cleanup
                    binding_entry: BindingEntryDict = {
                        "client_node_id": source_node_id,
                        "client_endpoint": endpoint_id,
                        "target_node_id": target_node_id,
                        "target_endpoint": 1,
                        "clusters": [],
                    }
                    await self._store.async_add_binding(automation_id, binding_entry)

                    LOGGER.info(
                        "Adding binding on node %d to target node %d",
                        source_node_id,
                        target_node_id,
                    )

                # Write updated bindings back to device using WRITE_ATTRIBUTE
                if len(new_bindings) > len(current_bindings):
                    LOGGER.debug(
                        "Binding list to write (before): %s",
                        current_bindings,
                    )
                    LOGGER.debug(
                        "Binding list to write (after): %s",
                        new_bindings,
                    )

                    write_response = await matter_client.write_attribute(
                        node_id=source_node_id,
                        attribute_path=binding_path,  # e.g., "1/30/0"
                        value=new_bindings,
                    )

                    LOGGER.debug(
                        "Binding write response for node %d: %s",
                        source_node_id,
                        write_response,
                    )

                    # Verify by re-reading bindings
                    verify_bindings_resp = await matter_client.read_attribute(
                        source_node_id, binding_path
                    )
                    verify_bindings = verify_bindings_resp.get(binding_path, [])
                    LOGGER.info(
                        "BINDING VERIFICATION - Node %d now has %d bindings (was %d)",
                        source_node_id,
                        len(verify_bindings),
                        len(current_bindings),
                    )
                    LOGGER.debug(
                        "Verified bindings on node %d: %s",
                        source_node_id,
                        verify_bindings,
                    )

                    if len(verify_bindings) <= len(current_bindings):
                        LOGGER.error(
                            "BINDING WRITE FAILED! Entry was not added to node %d",
                            source_node_id,
                        )
                    else:
                        LOGGER.info(
                            "Created %d new bindings on node %d",
                            len(new_bindings) - len(current_bindings),
                            source_node_id,
                        )

            except (HomeAssistantError, OSError, ValueError) as err:
                LOGGER.error(
                    "Failed to create bindings on node %d: %s",
                    source_node_id,
                    err,
                )

    async def _async_remove_bindings_from_devices(
        self,
        automation_id: str,
        bindings: list[BindingEntryDict],
    ) -> None:
        """Remove bindings from Matter devices.

        Args:
            automation_id: The entity_id of the automation (for logging).
            bindings: List of binding entries to remove.
        """
        LOGGER.info(
            "Removing %d bindings for automation %s",
            len(bindings),
            automation_id,
        )

        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available, cannot remove bindings")
            return

        # Group bindings by source node for efficiency
        bindings_by_node: dict[int, list[BindingEntryDict]] = {}
        for binding in bindings:
            source_node_id = binding["client_node_id"]
            if source_node_id not in bindings_by_node:
                bindings_by_node[source_node_id] = []
            bindings_by_node[source_node_id].append(binding)

        for source_node_id, node_bindings in bindings_by_node.items():
            endpoint_id = node_bindings[0]["client_endpoint"]
            target_node_ids_to_remove = {b["target_node_id"] for b in node_bindings}

            try:
                # Read current bindings
                binding_path = f"{endpoint_id}/30/0"
                current_bindings_resp = await matter_client.read_attribute(
                    source_node_id, binding_path
                )
                current_bindings = current_bindings_resp.get(binding_path, [])

                # Filter out bindings to the target nodes
                # Handle both dict format (TLV keys) and dataclass format
                def get_binding_node(b):
                    if isinstance(b, dict):
                        return b.get("1")
                    return getattr(b, "node", None)

                updated_bindings = [
                    b
                    for b in current_bindings
                    if get_binding_node(b) not in target_node_ids_to_remove
                ]

                removed_count = len(current_bindings) - len(updated_bindings)
                if removed_count > 0:
                    # Write updated bindings
                    await matter_client.write_attribute(
                        node_id=source_node_id,
                        attribute_path=binding_path,
                        value=updated_bindings,
                    )
                    LOGGER.info(
                        "Removed %d bindings from node %d",
                        removed_count,
                        source_node_id,
                    )

            except (HomeAssistantError, OSError, ValueError) as err:
                LOGGER.error(
                    "Failed to remove bindings from node %d: %s",
                    source_node_id,
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

        # Access the automation entity directly to get separate trigger/action references

        automation_entity = None
        if DATA_COMPONENT in self._hass.data:
            automation_entity = self._hass.data[DATA_COMPONENT].get_entity(
                automation_id
            )

        if automation_entity is None:
            return (
                False,
                EligibilityStatus.NOT_CHECKED,
                [],
                [],
                f"Could not access automation entity {automation_id}",
            )

        # Get action entities from action_script
        # These are the entities that the automation ACTS ON (targets)
        action_referenced_entities = list(
            automation_entity.action_script.referenced_entities
        )
        action_referenced_devices = list(
            automation_entity.action_script.referenced_devices
        )

        # Get trigger entities from trigger config
        # These are the entities that TRIGGER the automation (sources)
        trigger_referenced_entities: list[str] = []
        trigger_referenced_devices: list[str] = []

        # Check if automation has _trigger_config (AutomationEntity has it, UnavailableAutomationEntity doesn't)
        if hasattr(automation_entity, "_trigger_config"):
            for trigger_conf in automation_entity._trigger_config:
                # Extract entities from trigger
                platform = trigger_conf.get(CONF_PLATFORM)
                if platform in ("state", "numeric_state"):
                    entity_ids = trigger_conf.get(CONF_ENTITY_ID, [])
                    if isinstance(entity_ids, str):
                        entity_ids = [entity_ids]
                    trigger_referenced_entities.extend(entity_ids)
                elif platform == "device":
                    device_id = trigger_conf.get(CONF_DEVICE_ID)
                    if device_id:
                        if isinstance(device_id, list):
                            trigger_referenced_devices.extend(device_id)
                        else:
                            trigger_referenced_devices.append(device_id)
                    # Also check for entity_id in device triggers
                    entity_id = trigger_conf.get(CONF_ENTITY_ID)
                    if entity_id:
                        if isinstance(entity_id, list):
                            trigger_referenced_entities.extend(entity_id)
                        else:
                            trigger_referenced_entities.append(entity_id)

        LOGGER.debug("Action entities: %s", action_referenced_entities)
        LOGGER.debug("Action devices: %s", action_referenced_devices)
        LOGGER.debug("Trigger entities: %s", trigger_referenced_entities)
        LOGGER.debug("Trigger devices: %s", trigger_referenced_devices)

        # Convert trigger devices to entities
        trigger_matter_entities: list[str] = []
        for entity_id in trigger_referenced_entities:
            if self._is_matter_entity(entity_id):
                if entity_id not in trigger_matter_entities:
                    trigger_matter_entities.append(entity_id)
                    LOGGER.debug("  Trigger Matter entity: %s", entity_id)

        for device_id in trigger_referenced_devices:
            device_matter_entities = self._get_matter_entities_for_device(device_id)
            for entity_id in device_matter_entities:
                if entity_id not in trigger_matter_entities:
                    trigger_matter_entities.append(entity_id)
                    LOGGER.debug("  Trigger Matter entity (via device): %s", entity_id)

        # Convert action devices to entities
        action_matter_entities: list[str] = []
        for entity_id in action_referenced_entities:
            if self._is_matter_entity(entity_id):
                if entity_id not in action_matter_entities:
                    action_matter_entities.append(entity_id)
                    LOGGER.debug("  Action Matter entity: %s", entity_id)

        for device_id in action_referenced_devices:
            device_matter_entities = self._get_matter_entities_for_device(device_id)
            for entity_id in device_matter_entities:
                if entity_id not in action_matter_entities:
                    action_matter_entities.append(entity_id)
                    LOGGER.debug("  Action Matter entity (via device): %s", entity_id)

        # Validate we have both trigger and action Matter entities
        if not trigger_matter_entities:
            if not action_matter_entities:
                return (
                    False,
                    EligibilityStatus.INELIGIBLE_NO_MATTER_TRIGGER,
                    [],
                    [],
                    "No Matter devices found in automation",
                )
            return (
                False,
                EligibilityStatus.INELIGIBLE_NO_MATTER_TRIGGER,
                [],
                action_matter_entities,
                "No Matter devices found in automation triggers",
            )

        if not action_matter_entities:
            return (
                False,
                EligibilityStatus.INELIGIBLE_NO_MATTER_ACTION,
                trigger_matter_entities,
                [],
                "No Matter devices found in automation actions",
            )

        # Set properly extracted trigger and action entities
        trigger_entities = trigger_matter_entities
        action_entities = action_matter_entities

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
        # Get the specific Matter node for this entity
        node = self._get_node_for_entity(entity_id)
        if node is None:
            LOGGER.debug(
                "Could not find Matter node for entity %s",
                entity_id,
            )
            return False

        # Check if any endpoint on THIS node has the Binding cluster
        for endpoint in node.endpoints.values():
            if endpoint.has_cluster(CLUSTER_ID_BINDING):
                LOGGER.debug(
                    "Node %d endpoint %d has Binding cluster",
                    node.node_id,
                    endpoint.endpoint_id,
                )
                return True

        LOGGER.debug(
            "Node %d does not have Binding cluster on any endpoint",
            node.node_id,
        )
        return False

    def _get_node_for_entity(self, entity_id: str):
        """Get the Matter node for an entity.

        Args:
            entity_id: The entity_id to look up.

        Returns:
            The MatterNode for the entity, or None if not found.
        """
        try:
            get_matter(self._hass)
        except (KeyError, StopIteration):
            return None

        if self._entity_registry is None or self._device_registry is None:
            return None

        entity_entry = self._entity_registry.async_get(entity_id)
        if entity_entry is None or entity_entry.device_id is None:
            return None

        device = self._device_registry.async_get(entity_entry.device_id)
        if device is None:
            return None

        return get_node_from_device_entry(self._hass, device)

    async def _check_entity_has_client_cluster(self, entity_id: str) -> bool:
        """Check if an entity's Matter device has a client cluster.

        Client clusters are identified by:
        1. Checking device types like OnOffLightSwitch, DimmerSwitch, etc.
        2. For stateful switches (server entities), checking the Descriptor cluster's
           clientList for matching clusters (e.g., OnOff in both server and client)

        Args:
            entity_id: The entity_id to check.

        Returns:
            True if the device has an appropriate client cluster.
        """
        # Get the specific Matter node for this entity
        node = self._get_node_for_entity(entity_id)
        if node is None:
            LOGGER.debug(
                "Could not find Matter node for entity %s",
                entity_id,
            )
            return False

        client_device_types = (
            device_types.OnOffLightSwitch,
            device_types.DimmerSwitch,
            device_types.ColorDimmerSwitch,
        )

        for endpoint in node.endpoints.values():
            # Check 1: Traditional client device types
            for dt in endpoint.device_types:
                if dt in client_device_types:
                    LOGGER.debug(
                        "Node %d endpoint %d has client device type: %s",
                        node.node_id,
                        endpoint.endpoint_id,
                        dt.__name__,
                    )
                    return True

            # Check 2: Stateful switches - have BOTH server and client clusters
            # These are devices like DimmableLightSwitch that have OnOff server
            # (for local state) AND OnOff client (for controlling other devices)
            descriptor = endpoint.get_cluster(Clusters.Descriptor)
            if descriptor is not None:
                client_list = set(descriptor.clientList or [])
                # Check if endpoint has OnOff or LevelControl in client list
                if CLUSTER_ID_ON_OFF in client_list:
                    # Also verify it has the binding cluster for actual binding capability
                    if endpoint.has_cluster(CLUSTER_ID_BINDING):
                        LOGGER.debug(
                            "Node %d endpoint %d is a stateful switch "
                            "(has OnOff client cluster + binding cluster)",
                            node.node_id,
                            endpoint.endpoint_id,
                        )
                        return True

        LOGGER.debug(
            "Node %d does not have client cluster capability on any endpoint",
            node.node_id,
        )
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

    async def _async_discover_client_clusters_for_node(self, node) -> None:
        """Discover client cluster entities for a specific Matter node.

        This is called when a new node is added to discover any client clusters.

        Args:
            node: The MatterNode to discover entities for.
        """
        LOGGER.info("Discovering client cluster entities for node %d", node.node_id)

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
            LOGGER.info(
                "Adding %d switch entities for node %d",
                len(switch_entities),
                node.node_id,
            )
            runtime_data.switch_add_entities(switch_entities)

        if light_entities and runtime_data.light_add_entities:
            LOGGER.info(
                "Adding %d light entities for node %d",
                len(light_entities),
                node.node_id,
            )
            runtime_data.light_add_entities(light_entities)

        LOGGER.info(
            "Client cluster discovery for node %d complete: %d switch, %d light entities",
            node.node_id,
            len(switch_entities),
            len(light_entities),
        )

    # =========================================================================
    # State Reconciliation Methods (New Reference-Counted Approach)
    # =========================================================================

    async def _get_node_info_for_entities(
        self, entity_ids: list[str]
    ) -> list[NodeInfo]:
        """Get NodeInfo for a list of entity IDs.

        Args:
            entity_ids: List of entity IDs.

        Returns:
            List of NodeInfo objects with node and endpoint information.
        """
        result: list[NodeInfo] = []

        try:
            get_matter(self._hass)
        except (KeyError, StopIteration):
            return result

        if self._entity_registry is None:
            return result

        for entity_id in entity_ids:
            entity_entry = self._entity_registry.async_get(entity_id)
            if entity_entry is None or entity_entry.device_id is None:
                continue

            if self._device_registry is None:
                continue

            device = self._device_registry.async_get(entity_entry.device_id)
            if device is None:
                continue

            # Find Matter node ID from device identifiers
            node_id: int | None = None
            for domain, identifier in device.identifiers:
                if domain == MATTER_DOMAIN:
                    # Try to parse as simple integer first
                    with contextlib.suppress(ValueError):
                        node_id = int(identifier)

                    # Try to parse "deviceid_FABRIC-NODEID-MatterNodeDevice" format
                    if node_id is None and identifier.startswith("deviceid_"):
                        try:
                            parts = identifier.split("-")
                            if len(parts) >= 2:
                                node_id = int(parts[1], 16)
                        except (ValueError, IndexError):
                            pass

                    if node_id is not None:
                        break

            if node_id is not None:
                # Default to endpoint 1 for most devices
                result.append(
                    NodeInfo(
                        entity_id=entity_id,
                        node_id=node_id,
                        endpoint_id=1,
                    )
                )

        return result

    def _compute_desired_state(
        self,
        trigger_nodes: list[NodeInfo],
        action_nodes: list[NodeInfo],
        existing_group_id: int | None = None,
    ) -> DesiredResourceState:
        """Compute what resources SHOULD exist for this automation.

        Args:
            trigger_nodes: List of trigger node info.
            action_nodes: List of action node info.
            existing_group_id: Reuse existing group ID if available.

        Returns:
            DesiredResourceState with all required resources.
        """
        acls: set[tuple[int, int, int]] = set()
        bindings: set[tuple[int, int, int | str, int]] = set()
        groups: set[tuple[int, frozenset[tuple[int, int]]]] = set()
        group_id: int | None = None

        # Check if group bindings are enabled
        enable_groups = self._config_entry.options.get(
            CONF_ENABLE_GROUP_BINDINGS, False
        )

        # Decide binding strategy based on target count and group setting
        use_groups = len(action_nodes) > 1 and enable_groups

        if not use_groups:
            # Unicast bindings: each source -> each target
            # Used for single target OR when groups are disabled
            for target in action_nodes:
                for source in trigger_nodes:
                    # ACL tuple: (target_node_id, subject, auth_mode)
                    # authMode 2 = CASE (node-to-node)
                    acls.add((target.node_id, source.node_id, 2))
                    bindings.add(
                        (
                            source.node_id,
                            source.endpoint_id,
                            target.node_id,  # Direct node reference (int)
                            target.endpoint_id,
                        )
                    )

            if len(action_nodes) > 1:
                LOGGER.info(
                    "Group bindings disabled - creating %d unicast bindings "
                    "(%d sources × %d targets)",
                    len(trigger_nodes) * len(action_nodes),
                    len(trigger_nodes),
                    len(action_nodes),
                )
        else:
            # Group bindings: sources -> group, targets in group
            group_id = existing_group_id or self._store.allocate_group_id()

            # Group membership
            members = frozenset((n.node_id, n.endpoint_id) for n in action_nodes)
            groups.add((group_id, members))

            # ACLs: Each target needs BOTH:
            # 1. GROUP ACL (authMode=3) for receiving group multicast messages
            # 2. CASE ACLs (authMode=2) for each source (for unicast fallback/setup)
            for target in action_nodes:
                # GROUP ACL for group messages
                acls.add((target.node_id, group_id, 3))
                # CASE ACLs for each source node
                for source in trigger_nodes:
                    acls.add((target.node_id, source.node_id, 2))

            # Group bindings for each trigger (source -> group)
            for source in trigger_nodes:
                bindings.add(
                    (
                        source.node_id,
                        source.endpoint_id,
                        f"g{group_id}",  # Group reference (string)
                        0,  # Endpoint not used for group bindings
                    )
                )

        return DesiredResourceState(
            acls=acls,
            bindings=bindings,
            groups=groups,
            group_id=group_id,
        )

    def _get_current_resource_state(self, automation_id: str) -> DesiredResourceState:
        """Get current resource state for an automation from the store.

        Args:
            automation_id: The automation to get state for.

        Returns:
            DesiredResourceState representing current resources.
        """
        acls: set[tuple[int, int, int]] = set()
        bindings: set[tuple[int, int, int | str, int]] = set()
        groups: set[tuple[int, frozenset[tuple[int, int]]]] = set()

        resources = self._store.get_automation_resources(automation_id)
        if resources is None:
            return DesiredResourceState(acls=acls, bindings=bindings, groups=groups)

        # Extract ACLs
        for key in resources["acl_keys"]:
            acl = self._store.get_acl_resource(key)
            if acl:
                acls.add(
                    (
                        acl["target_node_id"],
                        acl["source_node_id"],
                        acl["endpoint_id"],
                    )
                )

        # Extract bindings
        for key in resources["binding_keys"]:
            binding = self._store.get_binding_resource(key)
            if binding:
                target: int | str
                if binding["target_group_id"] is not None:
                    target = f"g{binding['target_group_id']}"
                else:
                    target = binding["target_node_id"] or 0
                bindings.add(
                    (
                        binding["source_node_id"],
                        binding["source_endpoint"],
                        target,
                        binding["target_endpoint"],
                    )
                )

        # Extract groups
        for key in resources["group_keys"]:
            group = self._store.get_group_resource(key)
            if group:
                members = frozenset(
                    (m["node_id"], m["endpoint_id"]) for m in group["members"]
                )
                groups.add((group["group_id"], members))

        return DesiredResourceState(acls=acls, bindings=bindings, groups=groups)

    async def _reconcile_automation_resources(
        self,
        automation_id: str,
        trigger_entities: list[str],
        action_entities: list[str],
    ) -> None:
        """Reconcile current resources to match desired state.

        This is the main entry point for setting up or updating automation resources.
        It computes the diff between current and desired state, then applies
        the minimal operations needed.

        Args:
            automation_id: The automation to reconcile.
            trigger_entities: List of trigger entity IDs.
            action_entities: List of action entity IDs.
        """
        LOGGER.info("Reconciling resources for automation %s", automation_id)

        # Get node info for entities
        trigger_nodes = await self._get_node_info_for_entities(trigger_entities)
        action_nodes = await self._get_node_info_for_entities(action_entities)

        if not trigger_nodes or not action_nodes:
            LOGGER.warning(
                "Could not get node info for automation %s entities",
                automation_id,
            )
            return

        # Get current state
        current = self._get_current_resource_state(automation_id)

        # Compute desired state
        existing_group_id = self._store.get_existing_group_id(automation_id)
        desired = self._compute_desired_state(
            trigger_nodes, action_nodes, existing_group_id
        )

        LOGGER.debug(
            "Reconciliation for %s: current=%s, desired=%s",
            automation_id,
            current,
            desired,
        )

        # --- Reconcile Groups First (create before bindings need them) ---
        # Pass source nodes so they get the group key for encryption
        await self._reconcile_groups(
            automation_id, current.groups, desired.groups, trigger_nodes
        )

        # --- Reconcile ACLs ---
        await self._reconcile_acls(automation_id, current.acls, desired.acls)

        # --- Reconcile Bindings ---
        await self._reconcile_bindings(
            automation_id, current.bindings, desired.bindings
        )

        await self._store.async_save()
        LOGGER.info("Reconciliation complete for automation %s", automation_id)

    async def _reconcile_acls(
        self,
        automation_id: str,
        current: set[tuple[int, int, int]],
        desired: set[tuple[int, int, int]],
    ) -> None:
        """Reconcile ACL resources.

        ACL tuple format: (target_node_id, subject, auth_mode)
        - For CASE auth (mode=2): subject is source_node_id
        - For GROUP auth (mode=3): subject is group_id
        """
        to_add = desired - current
        to_remove = current - desired

        for target_node, subject, auth_mode in to_add:
            key, is_new = self._store.acquire_acl(
                automation_id, target_node, subject, auth_mode
            )
            if is_new:
                # Actually write ACL to device with correct auth_mode
                await self._write_acl_to_device(target_node, subject, auth_mode)
            LOGGER.debug(
                "Acquired ACL %s (new=%s, auth_mode=%d)", key, is_new, auth_mode
            )

        for target_node, subject, auth_mode in to_remove:
            key = acl_key(target_node, subject, auth_mode)
            should_remove = self._store.release_acl(automation_id, key)
            if should_remove:
                # Actually remove ACL from device
                await self._remove_acl_from_device(target_node, subject, auth_mode)
            LOGGER.debug("Released ACL %s (removed=%s)", key, should_remove)

    async def _reconcile_bindings(
        self,
        automation_id: str,
        current: set[tuple[int, int, int | str, int]],
        desired: set[tuple[int, int, int | str, int]],
    ) -> None:
        """Reconcile binding resources.

        ALWAYS writes bindings to device regardless of store state to fix
        store/device sync issues from previous failed writes.
        """
        to_add = desired - current
        to_remove = current - desired

        for source_node, source_ep, target, target_ep in to_add:
            key, is_new = self._store.acquire_binding(
                automation_id, source_node, source_ep, target, target_ep
            )
            # ALWAYS write to device and verify - don't trust is_new flag
            # The store may think it exists but the device may not have it
            LOGGER.info(
                "Ensuring binding on device: node %d ep %d -> %s (store new=%s)",
                source_node,
                source_ep,
                target,
                is_new,
            )
            await self._write_binding_to_device(
                source_node, source_ep, target, target_ep
            )
            LOGGER.debug("Acquired binding %s (new=%s)", key, is_new)

        for source_node, source_ep, target, target_ep in to_remove:
            key = binding_key(source_node, source_ep, target, target_ep)
            should_remove = self._store.release_binding(automation_id, key)
            if should_remove:
                # Actually remove binding from device
                await self._remove_binding_from_device(
                    source_node, source_ep, target, target_ep
                )
            LOGGER.debug("Released binding %s (removed=%s)", key, should_remove)

    async def _reconcile_groups(
        self,
        automation_id: str,
        current: set[tuple[int, frozenset[tuple[int, int]]]],
        desired: set[tuple[int, frozenset[tuple[int, int]]]],
        source_nodes: list[NodeInfo] | None = None,
    ) -> None:
        """Reconcile group resources.

        Handles:
        - Creating new groups with sources and targets
        - Adding/removing targets from existing groups
        - Adding GroupKeyMap to new sources
        - Removing GroupKeyMap from old sources
        - Removing groups when targets drop below 2

        Args:
            automation_id: The automation ID.
            current: Current group state (group_id, members frozenset).
            desired: Desired group state (group_id, members frozenset).
            source_nodes: Source nodes that need the group key for encryption.
        """
        current_by_id = dict(current)
        desired_by_id = dict(desired)
        desired_source_ids = (
            {n.node_id for n in source_nodes} if source_nodes else set()
        )

        # Groups to add (new group IDs)
        for gid in desired_by_id.keys() - current_by_id.keys():
            members = desired_by_id[gid]

            # Only create group if there are 2+ targets
            if len(members) < 2:
                LOGGER.debug(
                    "Skipping group %d creation: only %d target(s), need 2+",
                    gid,
                    len(members),
                )
                continue

            key, is_new = self._store.acquire_group(
                automation_id,
                gid,
                f"AutoBind-{automation_id[:16]}",
                list(members),
            )
            if is_new:
                # Actually create group on devices - pass source nodes for key distribution
                await self._create_group_on_devices(gid, members, source_nodes)
            LOGGER.debug("Acquired group %s (new=%s)", key, is_new)

        # Groups to update (same ID, different members or sources)
        for gid in current_by_id.keys() & desired_by_id.keys():
            current_members = current_by_id[gid]
            desired_members = desired_by_id[gid]
            key = group_key(gid)

            # Check if group should be removed (less than 2 targets remaining)
            if len(desired_members) < 2:
                LOGGER.info(
                    "Group %d reduced to %d target(s), removing group",
                    gid,
                    len(desired_members),
                )
                should_remove = self._store.release_group(automation_id, key)
                if should_remove:
                    # Remove from all devices (targets AND sources)
                    await self._remove_group_completely(gid, current_members)
                continue

            # Handle member changes
            if current_members != desired_members:
                members_to_add = desired_members - current_members
                members_to_remove = current_members - desired_members

                # For new target members, set up GroupKeyMap first
                if members_to_add:
                    await self._setup_group_key_for_new_members(
                        gid, members_to_add, source_nodes
                    )

                # Update target group membership
                for node_id, endpoint_id in members_to_add:
                    await self._add_device_to_group(gid, node_id, endpoint_id)
                for node_id, endpoint_id in members_to_remove:
                    await self._remove_device_from_group(gid, node_id, endpoint_id)

                # Update store with new members
                self._store.update_group_members(key, list(desired_members))

            # Handle source node changes
            group_resource = self._store.get_group_resource(key)
            if group_resource:
                current_source_ids = set(group_resource.get("source_nodes", []))
                sources_to_add = desired_source_ids - current_source_ids
                sources_to_remove = current_source_ids - desired_source_ids

                # Add GroupKeyMap to new sources
                if sources_to_add:
                    await self._setup_group_key_for_sources(gid, sources_to_add)

                # Remove GroupKeyMap from old sources
                if sources_to_remove:
                    await self._remove_group_key_from_sources(gid, sources_to_remove)

                # Update store with new source list
                if sources_to_add or sources_to_remove:
                    self._store.update_group_source_nodes(key, list(desired_source_ids))

        # Groups to remove (no longer needed)
        for gid in current_by_id.keys() - desired_by_id.keys():
            key = group_key(gid)
            should_remove = self._store.release_group(automation_id, key)
            if should_remove:
                # Remove from all devices (targets AND sources)
                members = current_by_id[gid]
                await self._remove_group_completely(gid, members)
            LOGGER.debug("Released group %s (removed=%s)", key, should_remove)

    async def _release_all_resources(self, automation_id: str) -> None:
        """Release all resources for an automation.

        Args:
            automation_id: The automation to clean up.
        """
        LOGGER.info("Releasing all resources for automation %s", automation_id)

        resources = self._store.get_automation_resources(automation_id)
        if resources is None:
            return

        # Release bindings first (they may reference groups)
        for key in list(resources["binding_keys"]):
            binding = self._store.get_binding_resource(key)
            should_remove = self._store.release_binding(automation_id, key)
            if should_remove and binding:
                target: int | str
                if binding["target_group_id"] is not None:
                    target = f"g{binding['target_group_id']}"
                else:
                    target = binding["target_node_id"] or 0
                await self._remove_binding_from_device(
                    binding["source_node_id"],
                    binding["source_endpoint"],
                    target,
                    binding["target_endpoint"],
                )

        # Release ACLs
        for key in list(resources["acl_keys"]):
            acl = self._store.get_acl_resource(key)
            should_remove = self._store.release_acl(automation_id, key)
            if should_remove and acl:
                await self._remove_acl_from_device(
                    acl["target_node_id"],
                    acl["source_node_id"],
                )

        # Release groups last
        for key in list(resources["group_keys"]):
            group = self._store.get_group_resource(key)
            should_remove = self._store.release_group(automation_id, key)
            if should_remove and group:
                members = frozenset(
                    (m["node_id"], m["endpoint_id"]) for m in group["members"]
                )
                await self._remove_group_from_devices(group["group_id"], members)

        # Clear automation resources mapping
        await self._store.async_clear_automation_resources(automation_id)

    # =========================================================================
    # Device Operations (Write/Remove to Matter devices)
    # =========================================================================

    async def _write_acl_to_device(
        self, target_node_id: int, subject: int, auth_mode: int = 2
    ) -> None:
        """Write ACL entry to a Matter device.

        Args:
            target_node_id: The node to write the ACL to.
            subject: The subject to grant access (node_id for CASE, group_id for GROUP).
            auth_mode: The authentication mode:
                2 = CASE (node-to-node unicast, subject is source node ID)
                3 = GROUP (group multicast, subject is group ID)
        """
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available for ACL write")
            return

        auth_mode_name = "GROUP" if auth_mode == 3 else "CASE"

        try:
            acl_path = "0/31/0"
            LOGGER.debug(
                "Reading current ACLs from node %d path %s", target_node_id, acl_path
            )
            current_acls = await matter_client.read_attribute(target_node_id, acl_path)
            current_acl_list = current_acls.get(acl_path, [])
            LOGGER.debug(
                "Current ACLs on node %d: %d entries",
                target_node_id,
                len(current_acl_list),
            )

            # Get fabric ID and controller subject
            server_info = matter_client.server_info
            if server_info is None:
                LOGGER.error("Matter server info not available")
                return
            fabric_id = server_info.fabric_id
            # Controller subject is typically in compressed_fabric_id or we use a known value
            # The admin ACL usually has privilege=5 and authMode=2 (CASE)

            # In DEBUG_OVERWRITE_ACLS mode, we clean up duplicate/invalid entries
            if DEBUG_OVERWRITE_ACLS:
                LOGGER.warning(
                    "⚠️ DEBUG_OVERWRITE_ACLS: Cleaning up ACLs on node %d",
                    target_node_id,
                )
                # Keep only the controller's admin ACL (privilege 5, our fabric)
                # and remove duplicates
                cleaned_entries: list[dict[str, Any]] = []
                seen_acls: set[tuple[int, int, tuple[int, ...]]] = set()

                for acl_entry in current_acl_list:
                    if not isinstance(acl_entry, dict):
                        continue

                    # Check for required fields
                    if "1" not in acl_entry or "2" not in acl_entry:
                        LOGGER.warning(
                            "Removing invalid ACL entry (missing fields): %s", acl_entry
                        )
                        continue

                    privilege = acl_entry.get("1", 0)
                    entry_auth_mode = acl_entry.get("2", 0)
                    subjects = tuple(acl_entry.get("3", []) or [])
                    entry_fabric = acl_entry.get("254", 0)

                    # Skip entries from other fabrics
                    if entry_fabric != fabric_id:
                        cleaned_entries.append(acl_entry)
                        continue

                    # Create a signature for deduplication
                    sig = (privilege, entry_auth_mode, subjects)
                    if sig in seen_acls:
                        LOGGER.warning(
                            "Removing duplicate ACL entry: privilege=%d, authMode=%d, subjects=%s",
                            privilege,
                            entry_auth_mode,
                            subjects,
                        )
                        continue

                    # Keep controller admin ACL (privilege 5)
                    if privilege == 5:
                        seen_acls.add(sig)
                        cleaned_entries.append(acl_entry)
                        LOGGER.debug("Keeping admin ACL: subjects=%s", subjects)
                        continue

                    # Remove all other Operate (privilege 3) ACLs - we'll add ours fresh
                    LOGGER.info(
                        "Removing stale ACL: privilege=%d, authMode=%d, subjects=%s",
                        privilege,
                        entry_auth_mode,
                        subjects,
                    )

                current_acl_list = cleaned_entries
                LOGGER.info(
                    "After cleanup, node %d has %d ACL entries",
                    target_node_id,
                    len(cleaned_entries),
                )
            else:
                # Normal mode: check if already exists
                for acl_entry in current_acl_list:
                    if isinstance(acl_entry, dict):
                        subjects = acl_entry.get("3", []) or []
                        entry_auth_mode = acl_entry.get("2", 0)
                    else:
                        subjects = getattr(acl_entry, "subjects", []) or []
                        entry_auth_mode = getattr(acl_entry, "authMode", 0)
                    if subject in subjects and entry_auth_mode == auth_mode:
                        LOGGER.debug(
                            "ACL already exists: subject %d, authMode %s on node %d",
                            subject,
                            auth_mode_name,
                            target_node_id,
                        )
                        return

                # Filter invalid entries
                current_acl_list = [
                    e
                    for e in current_acl_list
                    if (isinstance(e, dict) and "1" in e and "2" in e)
                    or (hasattr(e, "privilege") and hasattr(e, "authMode"))
                ]

            # Create new ACL entry with correct authMode
            # For GROUP auth (3), we also add endpoint targets
            if auth_mode == 3:
                # GROUP ACL - restrict to endpoint 1 (typical application endpoint)
                new_acl_entry = {
                    "254": fabric_id,
                    "1": 3,  # Operate privilege
                    "2": 3,  # GROUP authMode
                    "3": [subject],  # group_id
                    "4": [{"cluster": None, "endpoint": 1, "deviceType": None}],
                }
                LOGGER.info(
                    "Creating GROUP ACL: fabric=%d, group_id=%d, authMode=GROUP (3)",
                    fabric_id,
                    subject,
                )
            else:
                # CASE ACL - node-to-node (no target restriction)
                new_acl_entry = {
                    "254": fabric_id,
                    "1": 3,  # Operate privilege
                    "2": 2,  # CASE authMode
                    "3": [subject],  # source_node_id
                    "4": None,
                }
                LOGGER.info(
                    "Creating CASE ACL: fabric=%d, node_id=%d, authMode=CASE (2)",
                    fabric_id,
                    subject,
                )

            updated_list = [*current_acl_list, new_acl_entry]
            LOGGER.debug(
                "Writing %d ACL entries to node %d", len(updated_list), target_node_id
            )

            await matter_client.write_attribute(
                node_id=target_node_id,
                attribute_path=acl_path,
                value=updated_list,
            )

            # Verify write succeeded
            verify_resp = await matter_client.read_attribute(target_node_id, acl_path)
            verify_acl_list = verify_resp.get(acl_path, [])

            # Check if our subject is now in the ACL with correct authMode
            found = False
            for acl_entry in verify_acl_list:
                if isinstance(acl_entry, dict):
                    subjects = acl_entry.get("3", []) or []
                    entry_auth_mode = acl_entry.get("2", 0)
                else:
                    subjects = getattr(acl_entry, "subjects", []) or []
                    entry_auth_mode = getattr(acl_entry, "authMode", 0)
                if subject in subjects and entry_auth_mode == auth_mode:
                    found = True
                    break

            if found:
                LOGGER.info(
                    "✓ ACL WRITE VERIFIED: node %d has %s ACL for subject %d",
                    target_node_id,
                    auth_mode_name,
                    subject,
                )
            else:
                LOGGER.error(
                    "✗ ACL WRITE FAILED: node %d missing %s ACL for subject %d (total=%d)",
                    target_node_id,
                    auth_mode_name,
                    subject,
                    len(verify_acl_list),
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error("Failed to write ACL: %s", err)

    async def _remove_acl_from_device(
        self, target_node_id: int, subject: int, auth_mode: int = 2
    ) -> None:
        """Remove ACL entry from a Matter device.

        Args:
            target_node_id: The node to remove ACL from.
            subject: The subject to remove (node_id for CASE, group_id for GROUP).
            auth_mode: The authentication mode (2=CASE, 3=GROUP).
        """
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            return

        auth_mode_name = "GROUP" if auth_mode == 3 else "CASE"

        try:
            acl_path = "0/31/0"
            current_acls = await matter_client.read_attribute(target_node_id, acl_path)
            current_acl_list = current_acls.get(acl_path, [])

            # Filter out entries matching subject AND auth_mode
            updated_list = []
            for acl_entry in current_acl_list:
                if isinstance(acl_entry, dict):
                    subjects = acl_entry.get("3", []) or []
                    entry_auth_mode = acl_entry.get("2", 0)
                else:
                    subjects = getattr(acl_entry, "subjects", []) or []
                    entry_auth_mode = getattr(acl_entry, "authMode", 0)

                # Keep entry if subject not in subjects OR auth_mode differs
                if subject not in subjects or entry_auth_mode != auth_mode:
                    updated_list.append(acl_entry)

            if len(updated_list) < len(current_acl_list):
                await matter_client.write_attribute(
                    node_id=target_node_id,
                    attribute_path=acl_path,
                    value=updated_list,
                )
                LOGGER.info(
                    "Removed %s ACL from node %d for subject %d",
                    auth_mode_name,
                    target_node_id,
                    subject,
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error("Failed to remove ACL: %s", err)

    async def _write_binding_to_device(
        self,
        source_node_id: int,
        source_endpoint: int,
        target: int | str,
        target_endpoint: int,
    ) -> None:
        """Write binding entry to a Matter device."""
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available for binding write")
            return

        try:
            binding_path = f"{source_endpoint}/30/0"
            LOGGER.info(
                "Writing binding on node %d: ep %d -> target %s (ep %d)",
                source_node_id,
                source_endpoint,
                target,
                target_endpoint,
            )
            current_bindings_resp = await matter_client.read_attribute(
                source_node_id, binding_path
            )
            current_bindings = current_bindings_resp.get(binding_path, [])
            LOGGER.debug(
                "Current bindings on node %d: %s", source_node_id, current_bindings
            )

            if not isinstance(current_bindings, list):
                current_bindings = []

            # Filter out invalid binding entries
            valid_bindings = []
            for binding in current_bindings:
                if isinstance(binding, dict):
                    # Valid binding must have fabric index (254) and either
                    # node (1) for unicast or group (2) for group binding
                    if "254" in binding and ("1" in binding or "2" in binding):
                        valid_bindings.append(binding)
                    else:
                        LOGGER.warning(
                            "Filtering invalid binding entry on node %d: %s",
                            source_node_id,
                            binding,
                        )
                elif hasattr(binding, "fabricIndex"):
                    # Object format - check for node or group
                    if hasattr(binding, "node") or hasattr(binding, "group"):
                        valid_bindings.append(binding)
                    else:
                        LOGGER.warning(
                            "Filtering invalid binding entry on node %d: %s",
                            source_node_id,
                            binding,
                        )
                else:
                    LOGGER.warning(
                        "Filtering invalid binding entry on node %d: %s",
                        source_node_id,
                        binding,
                    )

            # Get fabric ID
            server_info = matter_client.server_info
            if server_info is None:
                LOGGER.error("Matter server info not available")
                return
            fabric_id = server_info.fabric_id

            # Create binding entry
            if isinstance(target, str) and target.startswith("g"):
                # Group binding - only needs group ID and fabric
                group_id = int(target[1:])
                new_binding = {
                    "254": fabric_id,
                    "2": group_id,  # group field (index 2 in TargetStruct)
                }
                LOGGER.info(
                    "Creating GROUP binding: node %d -> group %d",
                    source_node_id,
                    group_id,
                )

                # Check if this group binding already exists in valid bindings
                for existing in valid_bindings:
                    if isinstance(existing, dict) and existing.get("2") == group_id:
                        LOGGER.info(
                            "✓ Group binding to %d already exists on node %d, skipping",
                            group_id,
                            source_node_id,
                        )
                        return
            else:
                # Unicast binding
                new_binding = {
                    "254": fabric_id,
                    "1": int(target),  # node field
                    "3": target_endpoint,  # endpoint field
                    "4": None,  # cluster field (None = all clusters)
                }
                LOGGER.info(
                    "Creating UNICAST binding: node %d -> node %d ep %d",
                    source_node_id,
                    target,
                    target_endpoint,
                )

                # Check if this unicast binding already exists in valid bindings
                for existing in valid_bindings:
                    if isinstance(existing, dict):
                        if (
                            existing.get("1") == int(target)
                            and existing.get("3") == target_endpoint
                        ):
                            LOGGER.info(
                                "✓ Unicast binding to node %d already exists on node %d, skipping",
                                target,
                                source_node_id,
                            )
                            return

            # Use valid_bindings instead of current_bindings
            updated_bindings = [*valid_bindings, new_binding]
            LOGGER.debug(
                "Writing %d bindings to node %d (was %d, filtered %d invalid): %s",
                len(updated_bindings),
                source_node_id,
                len(current_bindings),
                len(current_bindings) - len(valid_bindings),
                updated_bindings,
            )

            await matter_client.write_attribute(
                node_id=source_node_id,
                attribute_path=binding_path,
                value=updated_bindings,
            )

            # Verify write succeeded by reading back
            verify_resp = await matter_client.read_attribute(
                source_node_id, binding_path
            )
            verify_bindings = verify_resp.get(binding_path, [])
            LOGGER.debug(
                "After write, bindings on node %d: %s", source_node_id, verify_bindings
            )

            if len(verify_bindings) > len(current_bindings):
                LOGGER.info(
                    "✓ BINDING WRITE VERIFIED: node %d now has %d bindings (was %d)",
                    source_node_id,
                    len(verify_bindings),
                    len(current_bindings),
                )
            else:
                LOGGER.error(
                    "✗ BINDING WRITE FAILED: node %d still has %d bindings (expected %d)",
                    source_node_id,
                    len(verify_bindings),
                    len(updated_bindings),
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error("Failed to write binding: %s", err)

    async def _ensure_binding_on_device(
        self,
        source_node_id: int,
        source_endpoint: int,
        target: int | str,
        target_endpoint: int,
    ) -> None:
        """Ensure a binding exists on a Matter device.

        Checks if binding already exists on device before writing.
        This fixes store/device sync issues from previous failed writes.
        """
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available for binding check")
            return

        try:
            binding_path = f"{source_endpoint}/30/0"
            current_bindings_resp = await matter_client.read_attribute(
                source_node_id, binding_path
            )
            current_bindings = current_bindings_resp.get(binding_path, [])

            if not isinstance(current_bindings, list):
                current_bindings = []

            # Check if binding already exists on device
            is_group = isinstance(target, str) and target.startswith("g")
            target_value = int(target[1:]) if is_group else int(target)

            binding_exists = False
            for binding in current_bindings:
                if isinstance(binding, dict):
                    if is_group:
                        if binding.get("2") == target_value:
                            binding_exists = True
                            break
                    elif binding.get("1") == target_value:
                        binding_exists = True
                        break

            if binding_exists:
                LOGGER.debug(
                    "Binding to %s already exists on node %d, skipping write",
                    target,
                    source_node_id,
                )
                return

            # Binding doesn't exist, write it
            LOGGER.info(
                "Binding to %s not found on node %d, writing it now",
                target,
                source_node_id,
            )
            await self._write_binding_to_device(
                source_node_id, source_endpoint, target, target_endpoint
            )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error("Failed to ensure binding: %s", err)

    async def _remove_binding_from_device(
        self,
        source_node_id: int,
        source_endpoint: int,
        target: int | str,
        target_endpoint: int,
    ) -> None:
        """Remove binding entry from a Matter device."""
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            return

        try:
            binding_path = f"{source_endpoint}/30/0"
            current_bindings_resp = await matter_client.read_attribute(
                source_node_id, binding_path
            )
            current_bindings = current_bindings_resp.get(binding_path, [])

            if not isinstance(current_bindings, list):
                return

            # Filter out the target binding
            updated_bindings = []
            is_group = isinstance(target, str) and target.startswith("g")
            target_value = int(target[1:]) if is_group else int(target)

            for binding in current_bindings:
                if isinstance(binding, dict):
                    if is_group:
                        if binding.get("2") != target_value:
                            updated_bindings.append(binding)
                    elif binding.get("1") != target_value:
                        updated_bindings.append(binding)
                else:
                    updated_bindings.append(binding)

            if len(updated_bindings) < len(current_bindings):
                await matter_client.write_attribute(
                    node_id=source_node_id,
                    attribute_path=binding_path,
                    value=updated_bindings,
                )
                LOGGER.info(
                    "Removed binding from node %d to %s", source_node_id, target
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error("Failed to remove binding: %s", err)

    async def _create_group_on_devices(
        self,
        group_id: int,
        members: frozenset[tuple[int, int]],
        source_nodes: list[NodeInfo] | None = None,
    ) -> None:
        """Create a Matter group on all member devices.

        This involves:
        1. Generating ONE shared Group Key
        2. Writing the key to ALL nodes (sources AND targets)
        3. Mapping the group ID to that key set via GroupKeyMap on ALL nodes
        4. Calling AddGroup only on TARGET nodes (they join the group)
        5. Saving the epoch key to the store for future member additions

        Args:
            group_id: The group ID.
            members: Target nodes (node_id, endpoint_id) that join the group.
            source_nodes: Source nodes that need the key to encrypt outgoing messages.
        """
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available for group creation")
            return

        group_name = f"AutoBind-{group_id}"
        key_set_index = (group_id % 3) + 1

        # Generate ONE shared key for all nodes
        epoch_key = secrets.token_bytes(16)
        epoch_start_time = int(time.time() * 1_000_000)

        LOGGER.info(
            "Creating group %d with shared key (key=%s...) on %d targets + %d sources",
            group_id,
            epoch_key.hex()[:8],
            len(members),
            len(source_nodes) if source_nodes else 0,
        )

        # Save the epoch key to the store for future member additions
        key = group_key(group_id)
        self._store.update_group_epoch_key(key, epoch_key.hex(), key_set_index)

        # Track source node IDs in the store
        source_node_ids = [n.node_id for n in source_nodes] if source_nodes else []
        self._store.update_group_source_nodes(key, source_node_ids)

        # Collect all nodes that need the key
        all_nodes_for_key: set[int] = set()

        # Add source nodes (they need key to ENCRYPT outgoing messages)
        if source_nodes:
            for node_info in source_nodes:
                all_nodes_for_key.add(node_info.node_id)

        # Add target nodes (they need key to DECRYPT incoming messages)
        for node_id, _ in members:
            all_nodes_for_key.add(node_id)

        # Step 1 & 2: Write KeySet and GroupKeyMap to ALL nodes (sources + targets)
        for node_id in all_nodes_for_key:
            await self._setup_group_key_on_node(
                matter_client,
                node_id,
                group_id,
                key_set_index,
                epoch_key,
                epoch_start_time,
            )

        # Step 3: Call AddGroup ONLY on TARGET nodes (they join the group)
        # Source nodes do NOT join the group, they just have the key
        for node_id, endpoint_id in members:
            await self._add_node_to_group(
                matter_client, node_id, endpoint_id, group_id, group_name
            )

    async def _setup_group_key_for_new_members(
        self,
        group_id: int,
        new_members: frozenset[tuple[int, int]],
        source_nodes: list[NodeInfo] | None = None,
    ) -> None:
        """Set up GroupKeyMap on new members using stored epoch key.

        When a group already exists and new members are added, we need to
        write the KeySet and GroupKeyMap to the new devices before calling AddGroup.

        Args:
            group_id: The group ID.
            new_members: New members (node_id, endpoint_id) being added.
            source_nodes: Source nodes that also need the key (if new).
        """
        # Get the stored epoch key for this group
        key = group_key(group_id)
        group_resource = self._store.get_group_resource(key)
        if not group_resource:
            LOGGER.warning("Group %s not found in store, cannot propagate key", key)
            return

        epoch_key_hex = group_resource.get("epoch_key")
        key_set_index = group_resource.get("key_set_index")

        if not epoch_key_hex or not key_set_index:
            LOGGER.warning(
                "Group %s missing epoch_key or key_set_index, regenerating",
                key,
            )
            # If missing, we need to regenerate - this shouldn't happen normally
            # but handles edge case of upgraded stores without these fields
            return

        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available for group key setup")
            return

        epoch_key = bytes.fromhex(epoch_key_hex)
        epoch_start_time = int(time.time() * 1_000_000)

        LOGGER.info(
            "Propagating group %d key (key=%s...) to %d new members",
            group_id,
            epoch_key_hex[:8],
            len(new_members),
        )

        # Collect all nodes that need the key
        nodes_needing_key: set[int] = set()

        # Add new member nodes
        for node_id, _ in new_members:
            nodes_needing_key.add(node_id)

        # Add source nodes if they're new (check if they already have the key)
        if source_nodes:
            for node_info in source_nodes:
                nodes_needing_key.add(node_info.node_id)

        # Set up key on each node
        for node_id in nodes_needing_key:
            await self._setup_group_key_on_node(
                matter_client,
                node_id,
                group_id,
                key_set_index,
                epoch_key,
                epoch_start_time,
            )

    async def _setup_group_key_for_sources(
        self,
        group_id: int,
        source_node_ids: set[int],
    ) -> None:
        """Set up GroupKeyMap on new source nodes.

        Source nodes need the GroupKeyMap to ENCRYPT messages to the group,
        but they do NOT join the group (no AddGroup call).

        Args:
            group_id: The group ID.
            source_node_ids: Node IDs of new sources needing the key.
        """
        key = group_key(group_id)
        group_resource = self._store.get_group_resource(key)
        if not group_resource:
            LOGGER.warning("Group %s not found, cannot setup source keys", key)
            return

        epoch_key_hex = group_resource.get("epoch_key")
        key_set_index = group_resource.get("key_set_index")

        if not epoch_key_hex or not key_set_index:
            LOGGER.warning("Group %s missing epoch_key, cannot setup source keys", key)
            return

        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available")
            return

        epoch_key = bytes.fromhex(epoch_key_hex)
        epoch_start_time = int(time.time() * 1_000_000)

        LOGGER.info(
            "Adding GroupKeyMap for group %d to %d new source(s): %s",
            group_id,
            len(source_node_ids),
            source_node_ids,
        )

        for node_id in source_node_ids:
            await self._setup_group_key_on_node(
                matter_client,
                node_id,
                group_id,
                key_set_index,
                epoch_key,
                epoch_start_time,
            )

    async def _remove_group_key_from_sources(
        self,
        group_id: int,
        source_node_ids: set[int],
    ) -> None:
        """Remove GroupKeyMap from old source nodes.

        When sources are no longer controlling the group, remove the key.

        Args:
            group_id: The group ID.
            source_node_ids: Node IDs of old sources to remove key from.
        """
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available")
            return

        LOGGER.info(
            "Removing GroupKeyMap for group %d from %d old source(s): %s",
            group_id,
            len(source_node_ids),
            source_node_ids,
        )

        for node_id in source_node_ids:
            await self._remove_group_key_from_node(matter_client, node_id, group_id)

    async def _remove_group_key_from_node(
        self,
        matter_client: Any,
        node_id: int,
        group_id: int,
    ) -> None:
        """Remove a group's KeyMap entry from a node.

        Args:
            matter_client: The Matter client.
            node_id: The node to remove key from.
            group_id: The group ID to remove.
        """
        gkm_path = "0/63/0"

        try:
            # Read current GroupKeyMap
            current_map_resp = await matter_client.read_attribute(node_id, gkm_path)
            current_map = current_map_resp.get(gkm_path, [])

            if not current_map:
                return

            # Filter out the entry for this group
            updated_map = []
            removed = False
            for entry in current_map:
                entry_group_id = None
                if isinstance(entry, dict):
                    entry_group_id = entry.get("1")
                elif hasattr(entry, "groupId"):
                    entry_group_id = entry.groupId

                if entry_group_id == group_id:
                    removed = True
                    LOGGER.debug(
                        "Removing GroupKeyMap entry for group %d from node %d",
                        group_id,
                        node_id,
                    )
                else:
                    updated_map.append(entry)

            if removed:
                await matter_client.write_attribute(
                    node_id=node_id,
                    attribute_path=gkm_path,
                    value=updated_map,
                )
                LOGGER.info(
                    "✓ Removed GroupKeyMap for group %d from node %d",
                    group_id,
                    node_id,
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.warning(
                "Failed to remove GroupKeyMap for group %d from node %d: %s",
                group_id,
                node_id,
                err,
            )

    async def _remove_group_completely(
        self,
        group_id: int,
        members: frozenset[tuple[int, int]],
    ) -> None:
        """Remove a group completely from all devices (targets AND sources).

        Args:
            group_id: The group ID to remove.
            members: The target members (node_id, endpoint_id).
        """
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client
        except (KeyError, StopIteration):
            LOGGER.warning("Matter integration not available for group removal")
            return

        # Get source nodes from store
        key = group_key(group_id)
        group_resource = self._store.get_group_resource(key)
        source_node_ids = (
            set(group_resource.get("source_nodes", [])) if group_resource else set()
        )

        LOGGER.info(
            "Removing group %d completely: %d targets, %d sources",
            group_id,
            len(members),
            len(source_node_ids),
        )

        # Remove from target members (RemoveGroup + remove GroupKeyMap)
        for node_id, endpoint_id in members:
            await self._remove_device_from_group(group_id, node_id, endpoint_id)
            await self._remove_group_key_from_node(matter_client, node_id, group_id)

        # Remove GroupKeyMap from sources (they were never in the group)
        for node_id in source_node_ids:
            await self._remove_group_key_from_node(matter_client, node_id, group_id)

    async def _setup_group_key_on_node(
        self,
        matter_client: Any,
        node_id: int,
        group_id: int,
        key_set_index: int,
        epoch_key: bytes,
        epoch_start_time: int,
    ) -> None:
        """Set up group key on a single node (KeySetWrite + GroupKeyMap)."""
        gkm_path = "0/63/0"

        try:
            # Read current GroupKeyMap
            try:
                current_map_resp = await matter_client.read_attribute(node_id, gkm_path)
                current_map = current_map_resp.get(gkm_path, [])
                LOGGER.debug("Node %d current GroupKeyMap: %s", node_id, current_map)
            except (HomeAssistantError, OSError, ValueError):
                current_map = []

            # Filter out invalid entries from current map
            valid_entries = []
            for entry in current_map:
                if isinstance(entry, dict):
                    # Dict format: must have groupId (key "1") and groupKeySetID (key "2")
                    if "1" in entry and "2" in entry:
                        valid_entries.append(entry)
                    else:
                        LOGGER.warning(
                            "Filtering invalid GroupKeyMap entry on node %d: %s",
                            node_id,
                            entry,
                        )
                elif hasattr(entry, "groupId") and hasattr(entry, "groupKeySetID"):
                    # Object format
                    valid_entries.append(entry)
                else:
                    LOGGER.warning(
                        "Filtering invalid GroupKeyMap entry on node %d: %s",
                        node_id,
                        entry,
                    )

            # Check if group is already mapped
            group_already_mapped = False
            for entry in valid_entries:
                if isinstance(entry, dict):
                    if entry.get("1") == group_id:
                        group_already_mapped = True
                        break
                elif hasattr(entry, "groupId") and entry.groupId == group_id:
                    group_already_mapped = True
                    break

            if group_already_mapped:
                LOGGER.debug(
                    "Group %d already mapped on node %d, skipping", group_id, node_id
                )
                return

            # Write KeySet
            try:
                key_set = Clusters.GroupKeyManagement.Structs.GroupKeySetStruct(
                    groupKeySetID=key_set_index,
                    groupKeySecurityPolicy=Clusters.GroupKeyManagement.Enums.GroupKeySecurityPolicyEnum.kTrustFirst,
                    epochKey0=epoch_key,
                    epochStartTime0=epoch_start_time,
                    epochKey1=None,
                    epochStartTime1=None,
                    epochKey2=None,
                    epochStartTime2=None,
                )
                LOGGER.debug(
                    "Writing KeySet %d to node %d (key=%s...)",
                    key_set_index,
                    node_id,
                    epoch_key.hex()[:8],
                )
                await matter_client.send_device_command(
                    node_id=node_id,
                    endpoint_id=0,
                    command=Clusters.GroupKeyManagement.Commands.KeySetWrite(
                        groupKeySet=key_set
                    ),
                )
                LOGGER.info("✓ KeySetWrite succeeded on node %d", node_id)
            except (HomeAssistantError, OSError, ValueError) as err:
                LOGGER.warning("KeySetWrite on node %d failed: %s", node_id, err)

            # Write GroupKeyMap (using filtered valid entries)
            try:
                new_map_entry = Clusters.GroupKeyManagement.Structs.GroupKeyMapStruct(
                    groupId=group_id,
                    groupKeySetID=key_set_index,
                    fabricIndex=0,
                )
                updated_map = [*valid_entries, new_map_entry]
                LOGGER.debug(
                    "Writing GroupKeyMap on node %d: %d entries",
                    node_id,
                    len(updated_map),
                )
                await matter_client.write_attribute(
                    node_id=node_id,
                    attribute_path=gkm_path,
                    value=updated_map,
                )

                # Verify write by reading back
                verify_resp = await matter_client.read_attribute(node_id, gkm_path)
                verify_map = verify_resp.get(gkm_path, [])

                # Check if our group is now in the map
                found = False
                for entry in verify_map:
                    if isinstance(entry, dict):
                        if entry.get("1") == group_id:
                            found = True
                            break
                    elif hasattr(entry, "groupId") and entry.groupId == group_id:
                        found = True
                        break

                if found:
                    LOGGER.info(
                        "✓ GroupKeyMap VERIFIED: node %d has group %d mapped",
                        node_id,
                        group_id,
                    )
                else:
                    LOGGER.error(
                        "✗ GroupKeyMap WRITE FAILED: node %d does not have group %d after write",
                        node_id,
                        group_id,
                    )

            except (HomeAssistantError, OSError, ValueError) as err:
                LOGGER.warning(
                    "Failed to write GroupKeyMap on node %d: %s", node_id, err
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error("Failed to setup group key on node %d: %s", node_id, err)

    async def _add_node_to_group(
        self,
        matter_client: Any,
        node_id: int,
        endpoint_id: int,
        group_id: int,
        group_name: str,
    ) -> None:
        """Add a node to a group (AddGroup command)."""
        try:
            LOGGER.debug(
                "Sending AddGroup(groupID=%d) to node %d endpoint %d",
                group_id,
                node_id,
                endpoint_id,
            )
            response = await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=endpoint_id,
                command=Clusters.Groups.Commands.AddGroup(
                    groupID=group_id,
                    groupName=group_name,
                ),
            )
            LOGGER.debug("AddGroup response from node %d: %s", node_id, response)

            # Check status
            add_status = None
            if isinstance(response, dict):
                add_status = response.get("status")
            elif hasattr(response, "status"):
                add_status = response.status

            if add_status == 0:
                LOGGER.info(
                    "✓ AddGroup SUCCESS: node %d endpoint %d added to group %d",
                    node_id,
                    endpoint_id,
                    group_id,
                )
            else:
                LOGGER.warning(
                    "✗ AddGroup FAILED: node %d endpoint %d status=%s",
                    node_id,
                    endpoint_id,
                    add_status,
                )

            # Verify with GetGroupMembership
            verify_response = await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=endpoint_id,
                command=Clusters.Groups.Commands.GetGroupMembership(
                    groupList=[group_id]
                ),
            )

            group_list = []
            if isinstance(verify_response, dict):
                group_list = verify_response.get("groupList", [])
            elif hasattr(verify_response, "groupList"):
                group_list = verify_response.groupList or []

            if group_id in group_list:
                LOGGER.info(
                    "✓ GROUP VERIFIED: node %d endpoint %d is member of group %d",
                    node_id,
                    endpoint_id,
                    group_id,
                )
            else:
                LOGGER.warning(
                    "✗ GROUP UNVERIFIED: node %d endpoint %d not in group %d",
                    node_id,
                    endpoint_id,
                    group_id,
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error(
                "Failed to add node %d to group %d: %s", node_id, group_id, err
            )

    async def _add_device_to_group(
        self,
        group_id: int,
        node_id: int,
        endpoint_id: int,
    ) -> None:
        """Add a single device to a Matter group."""
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client

            await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=endpoint_id,
                command=Clusters.Groups.Commands.AddGroup(
                    groupID=group_id,
                    groupName=f"AutoBind-{group_id}",
                ),
            )
            LOGGER.info("Added node %d to group %d", node_id, group_id)
        except (
            HomeAssistantError,
            OSError,
            ValueError,
            KeyError,
            StopIteration,
        ) as err:
            LOGGER.error("Failed to add node %d to group: %s", node_id, err)

    async def _remove_device_from_group(
        self,
        group_id: int,
        node_id: int,
        endpoint_id: int,
    ) -> None:
        """Remove a single device from a Matter group."""
        try:
            matter = get_matter(self._hass)
            matter_client = matter.matter_client

            await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=endpoint_id,
                command=Clusters.Groups.Commands.RemoveGroup(groupID=group_id),
            )
            LOGGER.info("Removed node %d from group %d", node_id, group_id)
        except (
            HomeAssistantError,
            OSError,
            ValueError,
            KeyError,
            StopIteration,
        ) as err:
            LOGGER.error("Failed to remove node %d from group: %s", node_id, err)

    async def _remove_group_from_devices(
        self,
        group_id: int,
        members: frozenset[tuple[int, int]],
    ) -> None:
        """Remove a Matter group from all member devices."""
        for node_id, endpoint_id in members:
            await self._remove_device_from_group(group_id, node_id, endpoint_id)
