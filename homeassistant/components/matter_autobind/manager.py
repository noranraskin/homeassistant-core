"""Manager for Matter AutoBind integration."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chip.clusters import Objects as Clusters
from matter_server.client.models import device_types
from matter_server.common.models import EventType

from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
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

from .automation import (
    AutomationAnalyzer,
    is_physical_state_change,
    to_legacy_eligibility_result,
)
from .const import (
    CLUSTER_ID_BINDING,
    CLUSTER_ID_ON_OFF,
    DEBUG_OVERWRITE_ACLS,
    DOMAIN,
    LOGGER,
)
from .logic import GroupManager, NodeInfo, ResourceReconciler
from .matter import MatterAdapter
from .store import EligibilityStatus, MatterBindingStore

if TYPE_CHECKING:
    from homeassistant.components.matter.helpers import (  # pylint: disable=hass-component-root-import
        get_matter as _get_matter,
        get_node_from_device_entry as _get_node_from_device_entry,
    )
    from homeassistant.config_entries import ConfigEntry
else:
    from homeassistant.components.matter.helpers import (  # pylint: disable=hass-component-root-import
        get_matter as _get_matter,
        get_node_from_device_entry as _get_node_from_device_entry,
    )

from .discovery import async_discover_client_cluster_entities


def get_matter(hass: HomeAssistant) -> Any:
    """Wrapper for get_matter."""
    return _get_matter(hass)


def get_node_from_device_entry(hass: HomeAssistant, device_entry: Any) -> Any:
    """Wrapper for get_node_from_device_entry."""
    return _get_node_from_device_entry(hass, device_entry)


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
        # Matter adapter for ACL/binding operations
        self._adapter = MatterAdapter(
            hass, LOGGER, debug_overwrite_acls=DEBUG_OVERWRITE_ACLS
        )
        # Group manager for group operations
        self._group_manager = GroupManager(hass, store, LOGGER)
        # Resource reconciler for state management
        self._reconciler = ResourceReconciler(
            hass, config_entry, store, self._adapter, self._group_manager, LOGGER
        )
        # Automation analyzer for eligibility checking
        self._analyzer = AutomationAnalyzer(
            hass, er.async_get(hass), dr.async_get(hass), LOGGER
        )

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

        # Re-initialize analyzer with registries now that they are loaded
        self._analyzer = AutomationAnalyzer(
            self._hass, self._entity_registry, self._device_registry, LOGGER
        )

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
        await self._reconciler.release_all_resources(automation_id)

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
        await self._reconciler.release_all_resources(automation_id)

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
                await self._reconciler.release_all_resources(automation_id)

            LOGGER.info(
                "✗ INELIGIBLE: Automation %s (%s) is not eligible for Matter binding",
                automation_id,
                action,
            )
            LOGGER.info("  Reason: %s", reason)

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

        Delegates to AutomationAnalyzer for the heavy lifting.

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
        # Use the analyzer to check eligibility
        analysis = await self._analyzer.analyze(automation_id)

        # Separate basic eligibility check results
        is_eligible, status, triggers, actions, reason = to_legacy_eligibility_result(
            analysis
        )

        # If technically eligible based on structure, perform deeper check
        # for binding/client cluster capabilities on the actual Matter nodes
        if is_eligible:
            # Check if trigger device has binding cluster
            # This requires checking the actual Matter node
            if not triggers:
                return (
                    False,
                    EligibilityStatus.INELIGIBLE_NO_MATTER_TRIGGER,
                    [],
                    actions,
                    "No Matter devices found in automation triggers",
                )

            trigger_entity_id = triggers[0]
            trigger_has_binding = await self._check_entity_has_binding_cluster(
                trigger_entity_id
            )

            if not trigger_has_binding:
                return (
                    False,
                    EligibilityStatus.INELIGIBLE_NO_BINDING_CLUSTER,
                    triggers,
                    actions,
                    f"Trigger device {trigger_entity_id} does not have Binding cluster",
                )

            # Check if trigger device has appropriate client cluster (e.g., OnOff Client)
            trigger_has_client = await self._check_entity_has_client_cluster(
                trigger_entity_id
            )

            if not trigger_has_client:
                return (
                    False,
                    EligibilityStatus.INELIGIBLE_NO_CLIENT_CLUSTER,
                    triggers,
                    actions,
                    f"Trigger device {trigger_entity_id} does not have client cluster",
                )

        return is_eligible, status, triggers, actions, reason

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
        return entity_entry.platform in (MATTER_DOMAIN, DOMAIN)

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
            if entity.platform in (MATTER_DOMAIN, DOMAIN)
        ]

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

        if self._entity_registry is None:
            LOGGER.warning("Entity registry not available")
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

        if self._entity_registry is None:
            LOGGER.warning("Entity registry not available")
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
            node_id = self._get_node_id_from_device(device)

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

    def _get_node_id_from_device(self, device: dr.DeviceEntry) -> int | None:
        """Extract Matter node ID from device identifiers."""
        node_id: int | None = None
        for domain, identifier in device.identifiers:
            if domain == MATTER_DOMAIN:
                # Try to parse as simple integer first
                if identifier.isdigit():
                    return int(identifier)

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
                    return node_id
        return None

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
        current = self._reconciler.get_current_resource_state(automation_id)

        # Compute desired state
        existing_group_id = self._store.get_existing_group_id(automation_id)
        desired = self._reconciler.compute_desired_state(
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
        await self._reconciler.reconcile_groups(
            automation_id, current.groups, desired.groups, trigger_nodes
        )

        # --- Reconcile ACLs ---
        await self._reconciler.reconcile_acls(automation_id, current.acls, desired.acls)

        # --- Reconcile Bindings ---
        await self._reconciler.reconcile_bindings(
            automation_id, current.bindings, desired.bindings
        )

        await self._store.async_save()
        LOGGER.info("Reconciliation complete for automation %s", automation_id)

    async def _read_node_bindings(
        self, matter_client: Any, node_id: int
    ) -> tuple[list[dict], str] | None:
        """Read bindings from a node, trying endpoint 1 then 0.

        Returns:
            Tuple of (bindings, binding_path) or None on failure.
        """
        binding_path = "1/30/0"
        binding_read_success = False
        current_bindings: list[dict] = []

        try:
            current_bindings_resp = await matter_client.read_attribute(
                node_id, binding_path
            )
            raw_bindings = current_bindings_resp.get(binding_path, [])

            if isinstance(raw_bindings, dict):
                if raw_bindings.get("TLVValue") is None or raw_bindings.get("Reason"):
                    LOGGER.debug(
                        "Node %d binding cluster error on endpoint 1: %s",
                        node_id,
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
                node_id,
            )

        if not binding_read_success:
            binding_path = "0/30/0"
            try:
                current_bindings_resp = await matter_client.read_attribute(
                    node_id, binding_path
                )
                raw_bindings = current_bindings_resp.get(binding_path, [])

                if isinstance(raw_bindings, dict):
                    if raw_bindings.get("TLVValue") is None or raw_bindings.get(
                        "Reason"
                    ):
                        LOGGER.warning(
                            "Node %d does not support binding cluster: %s",
                            node_id,
                            raw_bindings.get("Reason", "Unknown error"),
                        )
                        return None
                    current_bindings = [raw_bindings]
                elif isinstance(raw_bindings, list):
                    current_bindings = raw_bindings
                    binding_read_success = True
            except (HomeAssistantError, OSError, ValueError) as err:
                LOGGER.warning("Failed to read bindings from node %d: %s", node_id, err)
                return None

        LOGGER.debug("Current bindings on node %d: %s", node_id, current_bindings)
        return current_bindings, binding_path
