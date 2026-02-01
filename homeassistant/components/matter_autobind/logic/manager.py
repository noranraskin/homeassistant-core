"""Manager for Matter AutoBind integration."""

from __future__ import annotations

import asyncio
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

from ..automation import (
    AutomationAnalyzer,
    is_physical_state_change,
    to_legacy_eligibility_result,
)
from ..const import (
    CLUSTER_ID_BINDING,
    CLUSTER_ID_DOOR_LOCK,
    CLUSTER_ID_FAN_CONTROL,
    CLUSTER_ID_LEVEL_CONTROL,
    CLUSTER_ID_ON_OFF,
    CLUSTER_ID_THERMOSTAT,
    CLUSTER_ID_WINDOW_COVERING,
    DEBUG_OVERWRITE_ACLS,
    DOMAIN,
    LOGGER,
)
from ..discovery import async_discover_client_cluster_entities
from ..matter import MatterAdapter, StatefulSwitchInfo
from ..store import EligibilityStatus, MatterBindingStore
from ..utils import (
    MATTER_DOMAIN,
    get_matter,
    get_node_from_device_entry,
    get_node_id_from_device,
    serialize_cluster_data,
)
from . import GroupManager, NodeInfo, ResourceReconciler

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


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

            # Wait a bit more to ensure the enable event is fully processed
            # before we remove from _suppressing_automations. This prevents
            # the event handler from triggering unnecessary reconciliation.
            await asyncio.sleep(0.1)

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

        # Subscribe to entity registry events for device/entity removal
        self._subscribe_to_entity_registry_events()

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
    def _subscribe_to_entity_registry_events(self) -> None:
        """Subscribe to entity registry events to detect device/entity removal.

        When a Matter entity is removed, we need to clean up any ACLs, bindings,
        or groups that reference that entity.
        """
        LOGGER.debug("Subscribing to entity registry events")

        @callback
        def handle_entity_registry_updated(
            event: Event[er.EventEntityRegistryUpdatedData],
        ) -> None:
            """Handle entity registry updates."""
            if event.data["action"] != "remove":
                return

            entity_id = event.data["entity_id"]

            # Only process Matter entities
            if not entity_id.startswith(("light.", "switch.", "fan.", "cover.")):
                return

            # Check if this entity was part of any automation we track
            # We need to re-reconcile any automations that used this entity
            self._hass.async_create_task(self._async_handle_entity_removed(entity_id))

        unsub = self._hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, handle_entity_registry_updated
        )
        self._unsub_automation_listeners.append(unsub)
        LOGGER.info("Subscribed to entity registry events for cleanup on removal")

    async def _async_handle_entity_removed(self, removed_entity_id: str) -> None:
        """Handle a Matter entity being removed.

        Finds all automations that reference this entity and re-reconciles them.
        Since the entity no longer exists, the automation will likely become
        ineligible and its resources will be released.

        Args:
            removed_entity_id: The entity_id that was removed.
        """
        LOGGER.info(
            "Matter entity removed: %s, checking for affected automations",
            removed_entity_id,
        )

        # Find automations that reference this entity by checking eligibility results
        affected_automations: list[str] = []

        # Iterate over all tracked automations
        for automation_id in self._store.get_all_tracked_automations():
            result = self._store.get_eligibility_result(automation_id)
            if result is None:
                continue

            # Check trigger entities
            trigger_entities = result.get("trigger_entities", [])
            action_entities = result.get("action_entities", [])

            if (
                removed_entity_id in trigger_entities
                or removed_entity_id in action_entities
            ):
                affected_automations.append(automation_id)
                LOGGER.info(
                    "Automation %s uses removed entity %s",
                    automation_id,
                    removed_entity_id,
                )

        # Re-analyze and reconcile affected automations
        for automation_id in affected_automations:
            LOGGER.info(
                "Re-analyzing automation %s after entity removal", automation_id
            )

            # Re-analyze the automation (it may now be ineligible)
            analysis = await self._analyzer.analyze(automation_id)

            if analysis is None or not analysis.eligible:
                # Automation is no longer eligible - release its resources
                reason = analysis.reason.value if analysis else "unknown"
                LOGGER.info(
                    "Automation %s is no longer eligible after entity %s removed "
                    "(reason: %s), releasing resources",
                    automation_id,
                    removed_entity_id,
                    reason,
                )
                await self._reconciler.release_all_resources(automation_id)
                await self._store.async_clear_scanned_automation(automation_id)

                # Unsubscribe from trigger entities
                for trigger_entity_id, auto_id in list(
                    self._trigger_to_automation.items()
                ):
                    if auto_id == automation_id:
                        self._unsubscribe_from_trigger_entity(trigger_entity_id)
            else:
                # Automation is still eligible but with different entities
                # Re-reconcile with the new entity set
                LOGGER.info(
                    "Automation %s still eligible, re-reconciling with updated entities",
                    automation_id,
                )
                await self._store.async_set_eligibility_result(
                    automation_id,
                    EligibilityStatus.ELIGIBLE,
                    analysis.trigger_entities,
                    analysis.action_entities,
                    analysis.reason.value,
                )
                await self._reconcile_automation_resources(
                    automation_id,
                    analysis.trigger_entities,
                    analysis.action_entities,
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

        # Skip ALL processing if we're suppressing this automation
        # This prevents unnecessary reconciliation during physical interaction handling
        if entity_id in self._suppressing_automations:
            LOGGER.debug(
                "Automation %s is being suppressed by us, ignoring state change event",
                entity_id,
            )
            return

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

        # Skip if only last_triggered changed - this happens when the automation
        # runs, not when its configuration is modified
        if old_state is not None:
            old_attrs = dict(old_state.attributes)
            new_attrs = dict(new_state.attributes)
            # Remove last_triggered from comparison
            old_attrs.pop("last_triggered", None)
            new_attrs.pop("last_triggered", None)
            if old_attrs == new_attrs:
                # LOGGER.debug(
                #     "Automation %s: only last_triggered changed (automation ran), skipping recheck",
                #     entity_id,
                # )
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

        # Client device types - devices that send commands rather than receive them
        client_device_types = (
            # Light switches
            device_types.OnOffLightSwitch,
            device_types.DimmerSwitch,
            device_types.ColorDimmerSwitch,
            # Lock controller
            device_types.DoorLockController,
            # Window covering controller
            device_types.WindowCoveringController,
        )

        # Client clusters that indicate binding capability
        client_cluster_ids = (
            CLUSTER_ID_ON_OFF,
            CLUSTER_ID_LEVEL_CONTROL,
            CLUSTER_ID_DOOR_LOCK,
            CLUSTER_ID_WINDOW_COVERING,
            CLUSTER_ID_THERMOSTAT,
            CLUSTER_ID_FAN_CONTROL,
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

            # Check 2: Devices with client clusters and binding capability
            # These are devices that have client clusters (for controlling other devices)
            # and the binding cluster for actual binding capability
            descriptor = endpoint.get_cluster(Clusters.Descriptor)
            if descriptor is not None:
                client_list = set(descriptor.clientList or [])
                # Check if endpoint has any supported client cluster
                if any(cluster_id in client_list for cluster_id in client_cluster_ids):
                    # Also verify it has the binding cluster for actual binding capability
                    if endpoint.has_cluster(CLUSTER_ID_BINDING):
                        LOGGER.debug(
                            "Node %d endpoint %d has client cluster(s) %s + binding cluster",
                            node.node_id,
                            endpoint.endpoint_id,
                            [
                                f"0x{c:04X}"
                                for c in client_list
                                if c in client_cluster_ids
                            ],
                        )
                        return True

        LOGGER.debug(
            "Node %d does not have client cluster capability on any endpoint",
            node.node_id,
        )
        return False

    async def async_discover_client_clusters(self) -> None:
        """Discover Matter nodes with client clusters and create entities.

        This scans all Matter nodes for endpoints that have client cluster
        device types (like OnOffLightSwitch, DimmerSwitch, DoorLockController,
        WindowCoveringController) but don't already have entities from the
        official matter integration.
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
        climate_entities: list = []
        cover_entities: list = []
        fan_entities: list = []
        light_entities: list = []
        lock_entities: list = []
        switch_entities: list = []

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

                    # Add to appropriate list based on platform
                    match entity_info.platform.value:
                        case "climate":
                            climate_entities.append(entity)
                        case "cover":
                            cover_entities.append(entity)
                        case "fan":
                            fan_entities.append(entity)
                        case "light":
                            light_entities.append(entity)
                        case "lock":
                            lock_entities.append(entity)
                        case "switch":
                            switch_entities.append(entity)

        # Add entities via platform callbacks
        if climate_entities and runtime_data.climate_add_entities:
            LOGGER.info("Adding %d climate entities", len(climate_entities))
            runtime_data.climate_add_entities(climate_entities)

        if cover_entities and runtime_data.cover_add_entities:
            LOGGER.info("Adding %d cover entities", len(cover_entities))
            runtime_data.cover_add_entities(cover_entities)

        if fan_entities and runtime_data.fan_add_entities:
            LOGGER.info("Adding %d fan entities", len(fan_entities))
            runtime_data.fan_add_entities(fan_entities)

        if light_entities and runtime_data.light_add_entities:
            LOGGER.info("Adding %d light entities", len(light_entities))
            runtime_data.light_add_entities(light_entities)

        if lock_entities and runtime_data.lock_add_entities:
            LOGGER.info("Adding %d lock entities", len(lock_entities))
            runtime_data.lock_add_entities(lock_entities)

        if switch_entities and runtime_data.switch_add_entities:
            LOGGER.info("Adding %d switch entities", len(switch_entities))
            runtime_data.switch_add_entities(switch_entities)
        elif switch_entities:
            LOGGER.warning(
                "Found %d switch entities but switch_add_entities callback not set",
                len(switch_entities),
            )

        LOGGER.info(
            "Client cluster discovery complete: "
            "%d climate, %d cover, %d fan, %d light, %d lock, %d switch entities",
            len(climate_entities),
            len(cover_entities),
            len(fan_entities),
            len(light_entities),
            len(lock_entities),
            len(switch_entities),
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
        climate_entities: list = []
        cover_entities: list = []
        fan_entities: list = []
        light_entities: list = []
        lock_entities: list = []
        switch_entities: list = []

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

                # Add to appropriate list based on platform
                match entity_info.platform.value:
                    case "climate":
                        climate_entities.append(entity)
                    case "cover":
                        cover_entities.append(entity)
                    case "fan":
                        fan_entities.append(entity)
                    case "light":
                        light_entities.append(entity)
                    case "lock":
                        lock_entities.append(entity)
                    case "switch":
                        switch_entities.append(entity)

        # Add entities via platform callbacks
        if climate_entities and runtime_data.climate_add_entities:
            LOGGER.info(
                "Adding %d climate entities for node %d",
                len(climate_entities),
                node.node_id,
            )
            runtime_data.climate_add_entities(climate_entities)

        if cover_entities and runtime_data.cover_add_entities:
            LOGGER.info(
                "Adding %d cover entities for node %d",
                len(cover_entities),
                node.node_id,
            )
            runtime_data.cover_add_entities(cover_entities)

        if fan_entities and runtime_data.fan_add_entities:
            LOGGER.info(
                "Adding %d fan entities for node %d",
                len(fan_entities),
                node.node_id,
            )
            runtime_data.fan_add_entities(fan_entities)

        if light_entities and runtime_data.light_add_entities:
            LOGGER.info(
                "Adding %d light entities for node %d",
                len(light_entities),
                node.node_id,
            )
            runtime_data.light_add_entities(light_entities)

        if lock_entities and runtime_data.lock_add_entities:
            LOGGER.info(
                "Adding %d lock entities for node %d",
                len(lock_entities),
                node.node_id,
            )
            runtime_data.lock_add_entities(lock_entities)

        if switch_entities and runtime_data.switch_add_entities:
            LOGGER.info(
                "Adding %d switch entities for node %d",
                len(switch_entities),
                node.node_id,
            )
            runtime_data.switch_add_entities(switch_entities)

        LOGGER.info(
            "Client cluster discovery for node %d complete: "
            "%d climate, %d cover, %d fan, %d light, %d lock, %d switch entities",
            node.node_id,
            len(climate_entities),
            len(cover_entities),
            len(fan_entities),
            len(light_entities),
            len(lock_entities),
            len(switch_entities),
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
            node_id = get_node_id_from_device(device)

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

        # Compute desired state (passing automation_id for preference lookup)
        existing_group_id = self._store.get_existing_group_id(automation_id)
        desired = self._reconciler.compute_desired_state(
            trigger_nodes, action_nodes, existing_group_id, automation_id
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

    # =========================================================================
    # Dashboard API Methods
    # =========================================================================

    async def get_dashboard_data(self) -> list[dict[str, Any]]:
        """Get dashboard data for all automations.

        Returns a list of automation summaries for the panel UI.
        """
        automations: list[dict[str, Any]] = []
        automation_states = self._hass.states.async_entity_ids(AUTOMATION_DOMAIN)

        for automation_id in automation_states:
            # Get eligibility result from store
            eligibility = self._store.get_eligibility_result(automation_id)
            resources = self._store.get_automation_resources(automation_id)

            # Get the friendly name from state
            state = self._hass.states.get(automation_id)
            friendly_name = (
                state.attributes.get("friendly_name", automation_id)
                if state
                else automation_id
            )

            # Determine binding status
            if eligibility is None:
                binding_status = "not_scanned"
                status_reason = "Automation has not been analyzed yet"
            elif eligibility["status"] != EligibilityStatus.ELIGIBLE:
                binding_status = "ineligible"
                status_reason = eligibility["reason"]
            elif resources and (resources["binding_keys"] or resources["group_keys"]):
                binding_status = "bound"
                status_reason = "Bindings active"
            else:
                binding_status = "eligible"
                status_reason = "Ready to bind"

            # Determine binding mode
            binding_mode = "none"
            if resources:
                if resources["group_keys"]:
                    binding_mode = "group"
                elif resources["binding_keys"]:
                    binding_mode = "unicast"

            # Get binding preference
            preference = self._store.get_binding_preference(automation_id) or "auto"

            automations.append(
                {
                    "automation_id": automation_id,
                    "friendly_name": friendly_name,
                    "is_enabled": state.state == "on" if state else False,
                    "binding_status": binding_status,
                    "status_reason": status_reason,
                    "binding_mode": binding_mode,
                    "binding_preference": preference,
                    "trigger_count": len(eligibility["trigger_entities"])
                    if eligibility
                    else 0,
                    "action_count": len(eligibility["action_entities"])
                    if eligibility
                    else 0,
                }
            )

        return automations

    async def get_automation_detail(self, automation_id: str) -> dict[str, Any]:
        """Get detailed information about a specific automation.

        Args:
            automation_id: The automation entity_id.

        Returns:
            Detailed automation info including devices and resources.
        """
        eligibility = self._store.get_eligibility_result(automation_id)
        resources = self._store.get_automation_resources(automation_id)
        state = self._hass.states.get(automation_id)
        preference = self._store.get_binding_preference(automation_id) or "auto"

        # Determine which preference options are available
        available_preferences = await self._get_available_preferences(
            eligibility, automation_id
        )

        result: dict[str, Any] = {
            "automation_id": automation_id,
            "friendly_name": (
                state.attributes.get("friendly_name", automation_id)
                if state
                else automation_id
            ),
            "is_enabled": state.state == "on" if state else False,
            "eligibility": eligibility,
            "binding_preference": preference,
            "available_preferences": available_preferences,
            "trigger_devices": [],
            "action_devices": [],
            "acls": [],
            "bindings": [],
            "groups": [],
        }

        # Get trigger device details
        if eligibility:
            for entity_id in eligibility["trigger_entities"]:
                device_info = await self._get_device_info_for_entity(entity_id)
                if device_info:
                    result["trigger_devices"].append(device_info)

            for entity_id in eligibility["action_entities"]:
                device_info = await self._get_device_info_for_entity(entity_id)
                if device_info:
                    result["action_devices"].append(device_info)

        # Get resource details
        if resources:
            for acl_key in resources["acl_keys"]:
                acl_resource = self._store.get_acl_resource(acl_key)
                if acl_resource:
                    result["acls"].append(
                        {
                            "key": acl_key,
                            "target_node_id": acl_resource["target_node_id"],
                            "source_node_id": acl_resource["source_node_id"],
                            "ref_count": acl_resource["ref_count"],
                        }
                    )

            for binding_key in resources["binding_keys"]:
                binding_resource = self._store.get_binding_resource(binding_key)
                if binding_resource:
                    result["bindings"].append(
                        {
                            "key": binding_key,
                            "source_node_id": binding_resource["source_node_id"],
                            "source_endpoint": binding_resource["source_endpoint"],
                            "target_node_id": binding_resource["target_node_id"],
                            "target_group_id": binding_resource["target_group_id"],
                            "target_endpoint": binding_resource["target_endpoint"],
                            "ref_count": binding_resource["ref_count"],
                        }
                    )

            for group_key in resources["group_keys"]:
                group_resource = self._store.get_group_resource(group_key)
                if group_resource:
                    result["groups"].append(
                        {
                            "key": group_key,
                            "group_id": group_resource["group_id"],
                            "group_name": group_resource["group_name"],
                            "member_count": len(group_resource["members"]),
                            "source_count": len(group_resource["source_nodes"]),
                            "ref_count": group_resource["ref_count"],
                        }
                    )

        return result

    async def _get_device_info_for_entity(
        self, entity_id: str
    ) -> dict[str, Any] | None:
        """Get device information for an entity."""
        if self._entity_registry is None or self._device_registry is None:
            return None

        entity_entry = self._entity_registry.async_get(entity_id)
        if entity_entry is None or entity_entry.device_id is None:
            return None

        device = self._device_registry.async_get(entity_entry.device_id)
        if device is None:
            return None

        node = get_node_from_device_entry(self._hass, device)
        node_id = node.node_id if node else None

        return {
            "entity_id": entity_id,
            "device_id": entity_entry.device_id,
            "device_name": device.name_by_user or device.name or "Unknown",
            "node_id": node_id,
        }

    async def _get_available_preferences(
        self,
        eligibility: Any | None,
        automation_id: str,
    ) -> list[dict[str, str]]:
        """Get the list of available binding preference options for an automation.

        The available options depend on:
        - Whether groups are enabled globally
        - Number of action targets (group requires 2+)
        - Whether the trigger entity is a virtual client-cluster-only entity

        Args:
            eligibility: The eligibility result for the automation.
            automation_id: The automation entity_id.

        Returns:
            List of dicts with 'value' and 'label' for each available option.
        """
        options: list[dict[str, str]] = []

        # Check if groups are enabled globally
        enable_groups = self._config_entry.options.get("enable_group_bindings", False)

        # Check number of action targets
        action_count = len(eligibility["action_entities"]) if eligibility else 0

        # Check if trigger entity is a virtual entity (created by this integration)
        is_virtual_trigger = False
        if eligibility and eligibility["trigger_entities"]:
            for trigger_entity_id in eligibility["trigger_entities"]:
                if self._is_virtual_entity(trigger_entity_id):
                    is_virtual_trigger = True
                    break

        # Always add "auto" option
        options.append({"value": "auto", "label": "Auto"})

        # Always add "unicast" option
        options.append({"value": "unicast", "label": "Unicast"})

        # Only add "group" if groups are enabled AND there are 2+ targets
        if enable_groups and action_count >= 2:
            options.append({"value": "group", "label": "Group"})

        # Only add "none" if the trigger is NOT a virtual entity
        # Virtual entities (client-cluster-only) can't really trigger automations
        # independently, so disabling bindings doesn't make sense for them
        if not is_virtual_trigger:
            options.append({"value": "none", "label": "None (disabled)"})

        return options

    def _is_virtual_entity(self, entity_id: str) -> bool:
        """Check if an entity is a virtual entity created by this integration.

        Virtual entities are client-cluster-only entities created by matter_autobind.
        They represent the client side of Matter switches and don't have server
        clusters, so they cannot be controlled directly.

        Args:
            entity_id: The entity_id to check.

        Returns:
            True if the entity belongs to the matter_autobind domain.
        """
        if self._entity_registry is None:
            return False

        entity_entry = self._entity_registry.async_get(entity_id)
        if entity_entry is None:
            return False

        # Check if the entity belongs to this integration's domain
        return entity_entry.platform == DOMAIN

    async def set_binding_preference(self, automation_id: str, preference: str) -> None:
        """Set the binding strategy preference for an automation.

        Args:
            automation_id: The automation entity_id.
            preference: One of "group", "unicast", "auto", or "none".
        """
        LOGGER.info(
            "Setting binding preference for %s to %s", automation_id, preference
        )

        # Store the preference
        await self._store.async_set_binding_preference(automation_id, preference)

        # Trigger a reconciliation to apply the new strategy
        await self.force_reconcile_automation(automation_id)

    async def get_node_raw_data(self, node_id: int) -> dict[str, Any]:
        """Get raw Matter cluster data from a node.

        Args:
            node_id: The Matter node ID.

        Returns:
            Dict with acls, bindings, groups, and group_key_map data.
        """
        result: dict[str, Any] = {
            "node_id": node_id,
            "acls": None,
            "bindings": None,
            "groups": None,
            "group_key_map": None,
            "errors": [],
        }

        # Read ACL cluster (endpoint 0, cluster 31, attribute 0)
        acl_data = await self._adapter.read_attribute(node_id, "0/31/0")
        if acl_data is not None:
            result["acls"] = serialize_cluster_data(acl_data)
        else:
            result["errors"].append("Failed to read ACL cluster")

        # Read Binding cluster - try endpoint 1 first, then 0
        for endpoint in (1, 0):
            binding_data = await self._adapter.read_attribute(
                node_id, f"{endpoint}/30/0"
            )
            if binding_data is not None:
                result["bindings"] = {
                    "endpoint": endpoint,
                    "data": serialize_cluster_data(binding_data),
                }
                break
        else:
            result["errors"].append("Failed to read Binding cluster")

        # Read Groups cluster (endpoint 1, cluster 4)
        groups_data = await self._adapter.read_attribute(node_id, "1/4/0")
        if groups_data is not None:
            result["groups"] = serialize_cluster_data(groups_data)

        # Read GroupKeyManagement cluster (endpoint 0, cluster 63, attribute 1)
        gkm_data = await self._adapter.read_attribute(node_id, "0/63/1")
        if gkm_data is not None:
            result["group_key_map"] = serialize_cluster_data(gkm_data)

        return result

    async def delete_resource(
        self,
        node_id: int,
        resource_type: str,
        resource_data: dict[str, Any],
    ) -> bool:
        """Delete a specific resource from a device.

        Args:
            node_id: The Matter node ID.
            resource_type: One of "acl", "binding", "group", "group_key_map".
            resource_data: Resource-specific data identifying what to delete.

        Returns:
            True if deletion succeeded.
        """
        LOGGER.warning(
            "Manual resource deletion requested: node=%d, type=%s, data=%s",
            node_id,
            resource_type,
            resource_data,
        )

        try:
            if resource_type == "acl":
                # Need to implement ACL removal by index
                return await self._adapter.remove_acl(
                    node_id, resource_data.get("index", 0)
                )
            if resource_type == "binding":
                endpoint = resource_data.get("endpoint", 1)
                target_node_id = resource_data.get("target_node_id")
                if target_node_id is None:
                    LOGGER.error("Target_node_id is required for binding deletion")
                    return False
                return await self._adapter.remove_binding(
                    node_id,
                    endpoint,
                    target_node_id,
                    resource_data.get("target_endpoint", 1),
                )
            if resource_type == "group":
                # Would need to implement group removal
                LOGGER.warning("Group deletion not yet implemented")
                return False
            if resource_type == "group_key_map":
                # Would need to implement group key map removal
                LOGGER.warning("GroupKeyMap deletion not yet implemented")
                return False
            LOGGER.error("Unknown resource type: %s", resource_type)
        except Exception as err:  # noqa: BLE001
            LOGGER.error("Failed to delete resource: %s", err)
            return False
        else:
            return False

    async def force_reconcile_automation(self, automation_id: str) -> None:
        """Force re-analysis and reconciliation of a single automation.

        Args:
            automation_id: The automation entity_id.
        """
        LOGGER.info("Force reconciling automation: %s", automation_id)

        # Clear the scanned status to force re-analysis
        await self._store.async_clear_scanned_automation(automation_id)

        # Re-check and process
        await self._async_check_and_log_automation(automation_id, is_new=False)

    async def force_reconcile_all(self) -> None:
        """Force re-analysis and reconciliation of all automations."""
        LOGGER.info("Force reconciling all automations")

        # Clear all scanned statuses
        automation_states = self._hass.states.async_entity_ids(AUTOMATION_DOMAIN)
        for automation_id in automation_states:
            await self._store.async_clear_scanned_automation(automation_id)

        # Re-scan all
        await self.async_scan_automations()

    # =========================================================================
    # Debug Panel Methods
    # =========================================================================

    async def get_matter_devices(self) -> list[dict[str, Any]]:
        """Get list of all Matter devices with their node IDs.

        Returns:
            List of device info dictionaries.
        """
        devices: list[dict[str, Any]] = []
        device_registry = dr.async_get(self._hass)
        entity_registry = er.async_get(self._hass)

        # Find all Matter devices
        for device_entry in device_registry.devices.values():
            # Check if this is a Matter device
            is_matter = any(
                ident[0] == MATTER_DOMAIN for ident in device_entry.identifiers
            )
            if not is_matter:
                continue

            # Get the node ID by looking up the MatterNode from the device entry
            node_id: int | None = None
            try:
                matter_node = get_node_from_device_entry(self._hass, device_entry)
                if matter_node is not None:
                    node_id = matter_node.node_id
            except Exception:  # noqa: BLE001
                # If we can't get the node, skip this device
                pass

            # Get entities for this device
            entity_count = len(
                er.async_entries_for_device(entity_registry, device_entry.id)
            )

            devices.append(
                {
                    "device_id": device_entry.id,
                    "device_name": device_entry.name
                    or device_entry.name_by_user
                    or "Unknown",
                    "node_id": node_id,
                    "manufacturer": device_entry.manufacturer,
                    "model": device_entry.model,
                    "entity_count": entity_count,
                }
            )

        # Sort by node_id, putting devices without node_id at the end
        devices.sort(key=lambda d: (d["node_id"] is None, d["node_id"] or 0))
        return devices

    def _get_matter_node(self, node_id: int) -> Any | None:
        """Get a MatterNode by ID from the Matter client cache.

        Args:
            node_id: The Matter node ID.

        Returns:
            The MatterNode object or None if not found.
        """
        try:
            matter_client = self._adapter._get_matter_client()  # noqa: SLF001
            for node in matter_client.get_nodes():
                if node.node_id == node_id:
                    return node
        except (RuntimeError, AttributeError, KeyError, StopIteration):
            pass
        return None

    def _get_cluster_attribute_by_name(
        self, cluster: Any, cluster_id: int
    ) -> Any | None:
        """Get cluster attribute value by trying common attribute names.

        Args:
            cluster: The cluster object.
            cluster_id: The cluster ID to determine which attributes to try.

        Returns:
            The attribute value or None if not found.
        """
        attr_names: dict[int, str | tuple[str, ...]] = {
            31: "acl",  # AccessControl
            30: "binding",  # Binding
            63: ("groupKeyMap", "groupTable"),  # GroupKeyManagement
            4: "nameSupport",  # Groups
        }
        names_to_try = attr_names.get(cluster_id, ())
        if isinstance(names_to_try, str):
            names_to_try = (names_to_try,)
        for name in names_to_try:
            if hasattr(cluster, name):
                return getattr(cluster, name)
        return None

    def _read_cluster_from_cache(
        self, node: Any, endpoint_id: int, cluster_id: int, attribute_id: int = 0
    ) -> Any | None:
        """Read cluster attribute data from the cached node model.

        This does NOT make network requests - it reads from the Matter client's
        cached node state which is kept even when nodes go offline.

        Args:
            node: The MatterNode object.
            endpoint_id: The endpoint ID.
            cluster_id: The cluster ID.
            attribute_id: The attribute ID (default 0).

        Returns:
            The attribute value or None if not found.
        """
        try:
            endpoints = getattr(node, "endpoints", None)
            if not endpoints:
                return None

            endpoint = endpoints.get(endpoint_id)
            if not endpoint:
                return None

            # Method 1: Try endpoint.get_attribute_value() which is the cleanest API
            if hasattr(endpoint, "get_attribute_value"):
                try:
                    value = endpoint.get_attribute_value(cluster_id, attribute_id)
                    if value is not None:
                        return value
                except Exception:  # noqa: BLE001
                    pass

            # Method 2: Try endpoint.get_cluster() then access attribute
            if hasattr(endpoint, "get_cluster"):
                try:
                    cluster = endpoint.get_cluster(cluster_id)
                    if cluster is None:
                        pass
                    elif value := self._get_cluster_attribute_by_name(
                        cluster, cluster_id
                    ):
                        return value
                except Exception:  # noqa: BLE001
                    pass

            # Method 3: Try accessing clusters dict directly
            if hasattr(endpoint, "clusters"):
                clusters = endpoint.clusters
                if isinstance(clusters, dict) and (cluster := clusters.get(cluster_id)):
                    if hasattr(cluster, "get"):
                        value = cluster.get(attribute_id)
                        if value is not None:
                            return value

        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Error reading cluster from cache: %s", err)

        return None

    async def get_device_raw_data(
        self, node_id: int, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Get comprehensive raw Matter cluster data from a device.

        By default, reads from the Matter client's cached node model, which is
        available even when nodes are offline.

        When force_refresh=True, reads live data from the device and updates
        the cache. This is useful after making changes to verify they took effect.

        Reads these clusters:
        - ACL (AccessControl cluster, endpoint 0, cluster 31)
        - Bindings (Binding cluster, various endpoints, cluster 30)
        - Groups (GroupKeyManagement cluster, endpoint 0, cluster 63)

        Args:
            node_id: The Matter node ID.
            force_refresh: If True, read live from device instead of cache.

        Returns:
            Dict with all cluster data and parsed entries.
        """
        result: dict[str, Any] = {
            "node_id": node_id,
            "acls": [],
            "bindings": [],
            "groups": [],
            "group_key_sets": [],
            "group_key_map": [],
            "errors": [],
            "from_cache": not force_refresh,
        }

        if force_refresh:
            LOGGER.debug("Reading live data from node %d", node_id)
            return await self._get_device_raw_data_live(node_id, result)

        LOGGER.debug("Reading cached data for node %d", node_id)
        return self._get_device_raw_data_cached(node_id, result)

    def _get_device_raw_data_cached(
        self, node_id: int, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Get device raw data from cache."""
        # Get the node from cache
        node = self._get_matter_node(node_id)
        if node is None:
            result["errors"].append(f"Node {node_id} not found in Matter client cache")
            return result

        # Check if node is available
        is_available = getattr(node, "available", True)
        if not is_available:
            result["errors"].append(f"Node {node_id} is offline - showing cached data")

        # Read ACL cluster (endpoint 0, cluster 31)
        acl_data = self._read_cluster_from_cache(node, 0, 31, 0)
        if acl_data is not None:
            result["acls"] = self._parse_acl_list(acl_data)
        else:
            result["errors"].append("ACL data not in cache")

        # Try to read Bindings from multiple endpoints (cluster 30)
        endpoints_to_check = list(getattr(node, "endpoints", {}).keys())
        for endpoint_id in endpoints_to_check:
            binding_data = self._read_cluster_from_cache(node, endpoint_id, 30, 0)
            if binding_data is not None and binding_data:
                parsed = self._parse_binding_list(binding_data, endpoint_id)
                if parsed:
                    result["bindings"].extend(parsed)

        if not result["bindings"]:
            result["errors"].append("No bindings found in cache")

        # Read GroupKeyManagement cluster (endpoint 0, cluster 63)
        # Attribute 0 = GroupKeyMap
        gkm_map = self._read_cluster_from_cache(node, 0, 63, 0)
        if gkm_map is not None:
            result["group_key_map"] = self._parse_group_key_map(gkm_map)

        # GroupTable (attribute 1)
        group_table = self._read_cluster_from_cache(node, 0, 63, 1)
        if group_table is not None:
            result["groups"] = self._parse_group_table(group_table)

        return result

    async def _get_device_raw_data_live(
        self, node_id: int, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Get device raw data by triggering a node interview/refresh.

        IMPORTANT: We do NOT use read_attribute here because it triggers
        subscription updates in the matter server that can crash the HA
        matter integration when there are phantom/invalid endpoints or
        None values in the server's cache.

        Instead, we:
        1. Request a fresh interview of the node (which updates the cache safely)
        2. Then return the cached data

        This gives us fresh data without the subscription crash issues.
        """
        try:
            matter_client = self._adapter._get_matter_client()  # noqa: SLF001
        except RuntimeError:
            result["errors"].append("Matter integration not available")
            result["from_cache"] = True
            return self._get_device_raw_data_cached(node_id, result)

        # Check if node is available first
        node = self._get_matter_node(node_id)
        if node is None:
            result["errors"].append(f"Node {node_id} not found")
            result["from_cache"] = True
            return result

        is_available = getattr(node, "available", False)
        if not is_available:
            result["errors"].append(f"Node {node_id} is offline - showing cached data")
            result["from_cache"] = True
            return self._get_device_raw_data_cached(node_id, result)

        # Try to interview the node to get fresh data
        # This updates the cache without the subscription crash issues
        try:
            LOGGER.debug("Requesting interview for node %d to refresh data", node_id)
            await matter_client.interview_node(node_id)
            # Small delay to let the cache update

            await asyncio.sleep(0.5)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug(
                "Failed to interview node %d: %s - using cached data", node_id, err
            )
            result["errors"].append(f"Could not refresh node: {err}")

        # Now return cached data (which should be fresh after interview)
        result["from_cache"] = False  # Data was refreshed via interview
        return self._get_device_raw_data_cached(node_id, result)

    def _parse_key_set_response(self, key_set_id: int, response: Any) -> dict[str, Any]:
        """Parse a KeySetRead response."""
        result: dict[str, Any] = {
            "key_set_id": key_set_id,
        }

        if hasattr(response, "groupKeySet"):
            key_set = response.groupKeySet
            result["security_policy"] = getattr(key_set, "groupKeySecurityPolicy", None)
            result["epoch_key0_present"] = (
                getattr(key_set, "epochKey0", None) is not None
            )
            result["epoch_start_time0"] = getattr(key_set, "epochStartTime0", None)
        elif isinstance(response, dict):
            result["raw"] = response

        return result

    def _parse_acl_list(self, acl_data: Any) -> list[dict[str, Any]]:
        """Parse ACL list into readable format."""
        parsed: list[dict[str, Any]] = []

        if not isinstance(acl_data, list):
            return parsed

        for idx, acl_entry in enumerate(acl_data):
            entry: dict[str, Any] = {
                "index": idx,
                "raw": serialize_cluster_data(acl_entry),
            }

            # Parse common ACL fields
            if isinstance(acl_entry, dict):
                entry["privilege"] = self._get_privilege_name(acl_entry.get("1", 0))
                entry["auth_mode"] = self._get_auth_mode_name(acl_entry.get("2", 0))
                subjects = acl_entry.get("3", [])
                entry["subjects"] = serialize_cluster_data(subjects) or []
                targets = acl_entry.get("4", [])
                entry["targets"] = serialize_cluster_data(targets) or []
            else:
                # Chip cluster object
                entry["privilege"] = self._get_privilege_name(
                    getattr(acl_entry, "privilege", 0)
                )
                entry["auth_mode"] = self._get_auth_mode_name(
                    getattr(acl_entry, "authMode", 0)
                )
                subjects = getattr(acl_entry, "subjects", [])
                entry["subjects"] = serialize_cluster_data(subjects) or []
                targets = getattr(acl_entry, "targets", [])
                entry["targets"] = serialize_cluster_data(targets) or []

            parsed.append(entry)

        return parsed

    def _parse_binding_list(
        self, binding_data: Any, endpoint: int
    ) -> list[dict[str, Any]]:
        """Parse binding list into readable format."""
        parsed: list[dict[str, Any]] = []

        if not isinstance(binding_data, list):
            return parsed

        for idx, binding_entry in enumerate(binding_data):
            entry: dict[str, Any] = {
                "index": idx,
                "endpoint": endpoint,
                "raw": serialize_cluster_data(binding_entry),
            }

            if isinstance(binding_entry, dict):
                entry["node_id"] = binding_entry.get("1")
                entry["group_id"] = binding_entry.get("2")
                entry["target_endpoint"] = binding_entry.get("3")
                entry["cluster_id"] = binding_entry.get("4")
                entry["fabric_index"] = binding_entry.get("254")
            else:
                entry["node_id"] = getattr(binding_entry, "node", None)
                entry["group_id"] = getattr(binding_entry, "group", None)
                entry["target_endpoint"] = getattr(binding_entry, "endpoint", None)
                entry["cluster_id"] = getattr(binding_entry, "cluster", None)
                entry["fabric_index"] = getattr(binding_entry, "fabricIndex", None)

            # Determine binding type
            if entry.get("group_id"):
                entry["type"] = "group"
            elif entry.get("node_id"):
                entry["type"] = "unicast"
            else:
                entry["type"] = "unknown"

            parsed.append(entry)

        return parsed

    def _parse_group_key_map(self, gkm_data: Any) -> list[dict[str, Any]]:
        """Parse GroupKeyMap into readable format."""
        parsed: list[dict[str, Any]] = []

        if not isinstance(gkm_data, list):
            return parsed

        for idx, entry in enumerate(gkm_data):
            item: dict[str, Any] = {
                "index": idx,
                "raw": serialize_cluster_data(entry),
            }

            if isinstance(entry, dict):
                item["group_id"] = entry.get("1")
                item["group_key_set_id"] = entry.get("2")
                item["fabric_index"] = entry.get("254")
            else:
                item["group_id"] = getattr(entry, "groupId", None)
                item["group_key_set_id"] = getattr(entry, "groupKeySetID", None)
                item["fabric_index"] = getattr(entry, "fabricIndex", None)

            parsed.append(item)

        return parsed

    def _parse_group_table(self, group_table: Any) -> list[dict[str, Any]]:
        """Parse GroupTable into readable format."""
        parsed: list[dict[str, Any]] = []

        if not isinstance(group_table, list):
            return parsed

        for idx, entry in enumerate(group_table):
            item: dict[str, Any] = {
                "index": idx,
                "raw": serialize_cluster_data(entry),
            }

            if isinstance(entry, dict):
                item["group_id"] = entry.get("1")
                item["endpoints"] = entry.get("2", [])
                item["group_name"] = entry.get("3", "")
                item["fabric_index"] = entry.get("254")
            else:
                item["group_id"] = getattr(entry, "groupId", None)
                item["endpoints"] = getattr(entry, "endpoints", [])
                item["group_name"] = getattr(entry, "groupName", "")
                item["fabric_index"] = getattr(entry, "fabricIndex", None)

            parsed.append(item)

        return parsed

    def _get_privilege_name(self, privilege: int) -> str:
        """Get human-readable privilege name."""
        privileges = {
            1: "View",
            2: "ProxyView",
            3: "Operate",
            4: "Manage",
            5: "Administer",
        }
        return privileges.get(privilege, f"Unknown({privilege})")

    def _get_auth_mode_name(self, auth_mode: int) -> str:
        """Get human-readable authentication mode name."""
        modes = {
            1: "PASE",  # codespell:ignore
            2: "CASE",
            3: "Group",
        }
        return modes.get(auth_mode, f"Unknown({auth_mode})")

    def _acl_entry_to_dict(self, acl_entry: Any) -> dict[str, Any]:
        """Convert an ACL entry (SDK object or dict) to dict format for writing.

        The Matter SDK expects ACL entries in dict format with specific keys:
        - privilege: int (1=View, 2=ProxyView, 3=Operate, 4=Manage, 5=Administer)
        - authMode: int (1=PASE, 2=CASE, 3=Group) # codespell:ignore
        - subjects: list[int] or None
        - targets: list[dict] or None
        - fabricIndex: int (0 = device sets automatically)
        """
        if isinstance(acl_entry, dict):
            # Already a dict, normalize keys
            # SDK uses both numeric keys ("1", "2") and camelCase keys
            privilege = acl_entry.get("privilege") or acl_entry.get("1", 3)
            auth_mode = acl_entry.get("authMode") or acl_entry.get("2", 2)
            subjects = acl_entry.get("subjects") or acl_entry.get("3")
            targets = acl_entry.get("targets") or acl_entry.get("4")
        else:
            # SDK cluster object
            privilege = getattr(acl_entry, "privilege", 3)
            auth_mode = getattr(acl_entry, "authMode", 2)
            subjects = getattr(acl_entry, "subjects", None)
            targets = getattr(acl_entry, "targets", None)

        # Convert targets to proper format
        parsed_targets = None
        if targets:
            parsed_targets = []
            for target in targets:
                if isinstance(target, dict):
                    parsed_targets.append(
                        {
                            "cluster": target.get("cluster") or target.get("0"),
                            "endpoint": target.get("endpoint") or target.get("1"),
                            "deviceType": target.get("deviceType") or target.get("2"),
                        }
                    )
                else:
                    parsed_targets.append(
                        {
                            "cluster": getattr(target, "cluster", None),
                            "endpoint": getattr(target, "endpoint", None),
                            "deviceType": getattr(target, "deviceType", None),
                        }
                    )

        # Convert subjects to plain Python ints
        parsed_subjects = None
        if subjects:
            parsed_subjects = [int(s) for s in subjects if s is not None]

        return {
            "privilege": int(privilege) if privilege is not None else 3,
            "authMode": int(auth_mode) if auth_mode is not None else 2,
            "subjects": parsed_subjects,
            "targets": parsed_targets,
            "fabricIndex": 0,  # Device sets this automatically
        }

    async def delete_acl_entry(self, node_id: int, acl_index: int) -> bool:
        """Delete an ACL entry by index.

        Args:
            node_id: The Matter node ID.
            acl_index: The index of the ACL entry to delete.

        Returns:
            True if deletion succeeded.
        """
        LOGGER.warning(
            "Deleting ACL entry: node=%d, index=%d",
            node_id,
            acl_index,
        )

        try:
            matter_client = self._adapter._get_matter_client()  # noqa: SLF001
            acl_path = "0/31/0"
            current_acls = await matter_client.read_attribute(node_id, acl_path)
            acl_list = current_acls.get(acl_path, [])

            if not isinstance(acl_list, list):
                LOGGER.error("Invalid ACL data format")
                return False

            if acl_index < 0 or acl_index >= len(acl_list):
                LOGGER.error(
                    "ACL index %d out of range (0-%d)", acl_index, len(acl_list) - 1
                )
                return False

            # Determine our fabric index by looking at entries with full data
            # Entries from other fabrics only show fabricIndex, not full data
            our_fabric_index: int | None = None
            for entry in acl_list:
                # Check if this entry has privilege (key "1" or "privilege")
                # If it does, it's from our fabric
                if isinstance(entry, dict):
                    priv = entry.get("privilege") or entry.get("1")
                else:
                    priv = getattr(entry, "privilege", None)

                if priv is not None:
                    # This entry is from our fabric, get its fabricIndex
                    if isinstance(entry, dict):
                        our_fabric_index = entry.get("fabricIndex") or entry.get("254")
                    else:
                        our_fabric_index = getattr(entry, "fabricIndex", None)
                    break

            LOGGER.debug("Our fabric index: %s", our_fabric_index)

            # Build list of entries to keep:
            # - Skip the entry at acl_index
            # - Only include entries from OUR fabric (have privilege field)
            updated_list: list[dict[str, Any]] = []
            for idx, entry in enumerate(acl_list):
                # Check if this entry is from our fabric
                if isinstance(entry, dict):
                    priv = entry.get("privilege") or entry.get("1")
                else:
                    priv = getattr(entry, "privilege", None)

                # Skip entries from other fabrics (they only have fabricIndex)
                if priv is None:
                    LOGGER.debug("Skipping entry %d - different fabric", idx)
                    continue

                # Skip the entry we want to delete
                if idx == acl_index:
                    LOGGER.debug("Skipping entry %d - deleting this one", idx)
                    continue

                converted = self._acl_entry_to_dict(entry)
                LOGGER.debug("Keeping entry %d: %s", idx, converted)
                updated_list.append(converted)

            LOGGER.info(
                "Writing %d ACL entries to node %d (was %d)",
                len(updated_list),
                node_id,
                len(acl_list),
            )

            # Use the dedicated set_acl_entry command for reliable ACL writes
            await matter_client.send_command(
                "set_acl_entry",
                node_id=node_id,
                entry=updated_list,
            )
            LOGGER.info("Deleted ACL entry %d from node %d", acl_index, node_id)

        except Exception as err:  # noqa: BLE001
            LOGGER.error("Failed to delete ACL entry: %s", err)
            return False
        else:
            return True

    def _binding_entry_to_dict(self, binding_entry: Any) -> dict[str, Any]:
        """Convert a binding entry (SDK object or dict) to dict format for writing.

        The Matter SDK expects binding entries in dict format with specific keys:
        - node: int (target node ID for unicast binding)
        - group: int (target group ID for group binding)
        - endpoint: int (target endpoint)
        - cluster: int (target cluster)
        - fabricIndex: int (0 = device sets automatically)
        """
        if isinstance(binding_entry, dict):
            # Already a dict, normalize keys
            # SDK uses both numeric keys and lowercase keys
            node = binding_entry.get("node") or binding_entry.get("1")
            group = binding_entry.get("group") or binding_entry.get("2")
            endpoint_val = binding_entry.get("endpoint") or binding_entry.get("3")
            cluster = binding_entry.get("cluster") or binding_entry.get("4")
        else:
            # SDK cluster object
            node = getattr(binding_entry, "node", None)
            group = getattr(binding_entry, "group", None)
            endpoint_val = getattr(binding_entry, "endpoint", None)
            cluster = getattr(binding_entry, "cluster", None)

        result: dict[str, Any] = {
            "fabricIndex": 0,  # Device sets this automatically
        }
        if node is not None:
            result["node"] = int(node)
        if group is not None:
            result["group"] = int(group)
        if endpoint_val is not None:
            result["endpoint"] = int(endpoint_val)
        if cluster is not None:
            result["cluster"] = int(cluster)

        return result

    async def delete_binding_entry(
        self, node_id: int, endpoint: int, binding_index: int
    ) -> bool:
        """Delete a binding entry by index.

        Args:
            node_id: The Matter node ID.
            endpoint: The endpoint containing the binding.
            binding_index: The index of the binding to delete.

        Returns:
            True if deletion succeeded.
        """
        LOGGER.warning(
            "Deleting binding entry: node=%d, endpoint=%d, index=%d",
            node_id,
            endpoint,
            binding_index,
        )

        try:
            matter_client = self._adapter._get_matter_client()  # noqa: SLF001
            binding_path = f"{endpoint}/30/0"
            current_bindings = await matter_client.read_attribute(node_id, binding_path)
            binding_list = current_bindings.get(binding_path, [])

            if not isinstance(binding_list, list):
                LOGGER.error("Invalid binding data format")
                return False

            if binding_index < 0 or binding_index >= len(binding_list):
                LOGGER.error(
                    "Binding index %d out of range (0-%d)",
                    binding_index,
                    len(binding_list) - 1,
                )
                return False

            # Convert all entries to dict format, excluding the one to delete
            updated_list: list[dict[str, Any]] = []
            for idx, entry in enumerate(binding_list):
                if idx != binding_index:
                    updated_list.append(self._binding_entry_to_dict(entry))

            # Use the dedicated set_node_binding command for reliable binding writes
            await matter_client.send_command(
                "set_node_binding",
                node_id=node_id,
                endpoint=endpoint,
                bindings=updated_list,
            )
            LOGGER.info(
                "Deleted binding entry %d from node %d endpoint %d",
                binding_index,
                node_id,
                endpoint,
            )

        except Exception as err:  # noqa: BLE001
            LOGGER.error("Failed to delete binding entry: %s", err)
            return False
        else:
            return True

    async def delete_group_entry(
        self, node_id: int, endpoint: int, group_id: int
    ) -> bool:
        """Delete a group membership entry.

        Args:
            node_id: The Matter node ID.
            endpoint: The endpoint to remove from group.
            group_id: The group ID to leave.

        Returns:
            True if deletion succeeded.
        """
        LOGGER.warning(
            "Deleting group entry: node=%d, endpoint=%d, group_id=%d",
            node_id,
            endpoint,
            group_id,
        )

        try:
            matter_client = self._adapter._get_matter_client()  # noqa: SLF001

            # Use RemoveGroup command (Groups cluster, endpoint 1)
            # Command 3 = RemoveGroup
            await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=endpoint,
                command=Clusters.Groups.Commands.RemoveGroup(
                    groupID=group_id,
                ),
            )
            LOGGER.info(
                "Removed node %d endpoint %d from group %d",
                node_id,
                endpoint,
                group_id,
            )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error("Failed to delete group entry: %s", err)
            return False
        else:
            return True

    async def delete_group_key_map_entry(self, node_id: int, entry_index: int) -> bool:
        """Delete a GroupKeyMap entry by index.

        Args:
            node_id: The Matter node ID.
            entry_index: The index of the entry to delete.

        Returns:
            True if deletion succeeded.
        """
        LOGGER.warning(
            "Deleting GroupKeyMap entry: node=%d, index=%d",
            node_id,
            entry_index,
        )

        try:
            matter_client = self._adapter._get_matter_client()  # noqa: SLF001
            gkm_path = "0/63/0"
            current_map = await matter_client.read_attribute(node_id, gkm_path)
            map_list = current_map.get(gkm_path, [])

            if not isinstance(map_list, list):
                LOGGER.error("Invalid GroupKeyMap data format")
                return False

            if entry_index < 0 or entry_index >= len(map_list):
                LOGGER.error(
                    "GroupKeyMap index %d out of range (0-%d)",
                    entry_index,
                    len(map_list) - 1,
                )
                return False

            # Remove the entry at the specified index
            updated_list = map_list[:entry_index] + map_list[entry_index + 1 :]

            await matter_client.write_attribute(
                node_id=node_id,
                attribute_path=gkm_path,
                value=updated_list,
            )
            LOGGER.info(
                "Deleted GroupKeyMap entry %d from node %d", entry_index, node_id
            )

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error("Failed to delete GroupKeyMap entry: %s", err)
            return False
        else:
            return True

    async def delete_group_key_set(self, node_id: int, key_set_id: int) -> bool:
        """Delete a GroupKeySet by ID using the KeySetRemove command.

        Note: KeySet 0 (IPK) cannot be removed.

        Args:
            node_id: The Matter node ID.
            key_set_id: The KeySet ID to remove.

        Returns:
            True if deletion succeeded.
        """
        if key_set_id == 0:
            LOGGER.error("Cannot remove KeySet 0 (IPK)")
            return False

        LOGGER.warning(
            "Deleting GroupKeySet: node=%d, key_set_id=%d",
            node_id,
            key_set_id,
        )

        try:
            matter_client = self._adapter._get_matter_client()  # noqa: SLF001
            await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=0,
                command=Clusters.GroupKeyManagement.Commands.KeySetRemove(
                    groupKeySetID=key_set_id
                ),
            )
            LOGGER.info("Deleted GroupKeySet %d from node %d", key_set_id, node_id)

        except (HomeAssistantError, OSError, ValueError) as err:
            LOGGER.error("Failed to delete GroupKeySet: %s", err)
            return False
        else:
            return True
