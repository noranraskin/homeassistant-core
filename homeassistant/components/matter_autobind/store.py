"""Storage for Matter AutoBind integration.

This module provides reference-counted resource management for ACLs, bindings,
and Matter groups. Resources are shared across automations with safe cleanup
when reference counts reach zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    AUTOBIND_GROUP_ID_MAX,
    AUTOBIND_GROUP_ID_START,
    DEBUG_RESET_STORE,
    LOGGER,
    STORAGE_KEY,
    STORAGE_VERSION,
)

# =============================================================================
# Resource Type Enum
# =============================================================================


class ResourceType(StrEnum):
    """Type of managed resource."""

    ACL = "acl"
    BINDING = "binding"
    GROUP = "group"


# =============================================================================
# Eligibility Status
# =============================================================================


class EligibilityStatus(StrEnum):
    """Status of automation eligibility for Matter binding."""

    ELIGIBLE = "eligible"
    """Automation is eligible for Matter binding."""

    INELIGIBLE_NO_MATTER_TRIGGER = "ineligible_no_matter_trigger"
    """Trigger device is not a Matter device."""

    INELIGIBLE_NO_CLIENT_CLUSTER = "ineligible_no_client_cluster"
    """Trigger device does not have required client cluster."""

    INELIGIBLE_NO_BINDING_CLUSTER = "ineligible_no_binding_cluster"
    """Trigger device does not have binding cluster."""

    INELIGIBLE_NO_MATTER_ACTION = "ineligible_no_matter_action"
    """Action device is not a Matter device."""

    INELIGIBLE_HAS_CONDITIONS = "ineligible_has_conditions"
    """Automation has conditions which cannot be replicated in Matter binding."""

    INELIGIBLE_COMPLEX_TRIGGER = "ineligible_complex_trigger"
    """Trigger is too complex (multiple triggers, non-device triggers)."""

    INELIGIBLE_COMPLEX_ACTION = "ineligible_complex_action"
    """Action is too complex (multiple actions, non-device actions)."""

    NOT_CHECKED = "not_checked"
    """Automation has not been checked yet."""


# =============================================================================
# Resource Key Generation Helpers
# =============================================================================


def acl_key(target_node_id: int, source_node_id: int, auth_mode: int = 2) -> str:
    """Generate a unique key for an ACL resource.

    Args:
        target_node_id: Node where ACL exists.
        source_node_id: Node/group granted access (subject).
        auth_mode: Authentication mode (2=CASE, 3=GROUP).
    """
    return f"acl:{target_node_id}:{source_node_id}:{auth_mode}"


def binding_key(
    source_node_id: int,
    source_endpoint: int,
    target: int | str,  # node_id (int) or group ref like "g12345" (str)
    target_endpoint: int,
) -> str:
    """Generate a unique key for a binding resource."""
    return f"bind:{source_node_id}:{source_endpoint}:{target}:{target_endpoint}"


def group_key(group_id: int) -> str:
    """Generate a unique key for a group resource."""
    return f"group:{group_id}"


def parse_binding_target(target: int | str) -> tuple[int | None, int | None]:
    """Parse binding target into (node_id, group_id).

    Returns:
        Tuple of (node_id, group_id) where one is always None.
    """
    if isinstance(target, str) and target.startswith("g"):
        return None, int(target[1:])
    return int(target), None


# =============================================================================
# TypedDicts for Storage
# =============================================================================


class EligibilityResultDict(TypedDict):
    """TypedDict for eligibility check result."""

    status: str
    trigger_entities: list[str]
    action_entities: list[str]
    reason: str


# Backwards-compatible TypedDicts for legacy manager.py code
class BindingEntryDict(TypedDict):
    """TypedDict for a binding entry (legacy compatibility)."""

    client_node_id: int
    client_endpoint: int
    target_node_id: int
    target_endpoint: int
    clusters: list[int]


class AclEntryDict(TypedDict):
    """TypedDict for tracking an ACL entry (legacy compatibility)."""

    target_node_id: int
    source_node_id: int
    auth_mode: int
    acl_index: int | None


class AclResourceDict(TypedDict):
    """A normalized ACL resource with reference counting."""

    target_node_id: int
    """Node where ACL exists."""

    source_node_id: int
    """Node/group granted access (subject)."""

    auth_mode: int
    """Authentication mode (2=CASE, 3=GROUP)."""

    ref_count: int
    """Number of automations using this ACL."""

    automation_ids: list[str]
    """Which automations reference this."""


class BindingResourceDict(TypedDict):
    """A normalized binding resource with reference counting."""

    source_node_id: int
    """Node where binding is written."""

    source_endpoint: int
    """Endpoint with binding cluster."""

    target_node_id: int | None
    """Target node (unicast) - None for group."""

    target_group_id: int | None
    """Target group (multicast) - None for unicast."""

    target_endpoint: int
    """Target endpoint."""

    cluster_ids: list[int]
    """Specific clusters or empty for all."""

    ref_count: int
    """Number of automations using this binding."""

    automation_ids: list[str]
    """Which automations reference this."""


class GroupMemberDict(TypedDict):
    """A group member (node + endpoint)."""

    node_id: int
    endpoint_id: int


class GroupResourceDict(TypedDict):
    """A Matter group managed by this integration."""

    group_id: int
    """The Matter group ID."""

    group_name: str
    """Name for the group (stored on devices)."""

    members: list[GroupMemberDict]
    """Target nodes that are members of this group (receive group messages)."""

    source_nodes: list[int]
    """Source node IDs that have GroupKeyMap for this group (send group messages)."""

    epoch_key: str | None
    """Hex-encoded 16-byte epoch key for this group (for key distribution)."""

    key_set_index: int | None
    """Key set index used for this group (1-3)."""

    ref_count: int
    """Number of automations using this group."""

    automation_ids: list[str]
    """Which automations reference this."""


class AutomationResourcesDict(TypedDict):
    """Track which resources an automation uses."""

    acl_keys: list[str]
    """Keys into acl_resources."""

    binding_keys: list[str]
    """Keys into binding_resources."""

    group_keys: list[str]
    """Keys into group_resources."""


class StoredDataDict(TypedDict, total=False):
    """TypedDict for the stored data structure.

    Uses total=False to allow optional fields for backwards compatibility.
    """

    # Core tracking
    scanned_automation_ids: list[str]
    eligibility_results: dict[str, EligibilityResultDict]

    # New: Normalized resource storage with reference counting
    acl_resources: dict[str, AclResourceDict]
    binding_resources: dict[str, BindingResourceDict]
    group_resources: dict[str, GroupResourceDict]

    # New: Automation → Resources mapping
    automation_resources: dict[str, AutomationResourcesDict]

    # Per-automation binding preferences
    binding_preferences: dict[str, str]


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class MatterBindingStoreData:
    """Data structure for matter binding store."""

    scanned_automation_ids: set[str] = field(default_factory=set)
    """Set of automation IDs that have been scanned."""

    eligibility_results: dict[str, EligibilityResultDict] = field(default_factory=dict)
    """Map of automation_id -> eligibility check result."""

    # Normalized resource storage with reference counting
    acl_resources: dict[str, AclResourceDict] = field(default_factory=dict)
    """Map of acl_key -> ACL resource with ref counting."""

    binding_resources: dict[str, BindingResourceDict] = field(default_factory=dict)
    """Map of binding_key -> binding resource with ref counting."""

    group_resources: dict[str, GroupResourceDict] = field(default_factory=dict)
    """Map of group_key -> group resource with ref counting."""

    automation_resources: dict[str, AutomationResourcesDict] = field(
        default_factory=dict
    )
    """Map of automation_id -> resources used by that automation."""

    binding_preferences: dict[str, str] = field(default_factory=dict)
    """Map of automation_id -> binding preference ('group', 'unicast', or 'auto')."""

    def to_dict(self) -> StoredDataDict:
        """Convert dataclass to a dictionary for storage."""
        return StoredDataDict(
            scanned_automation_ids=list(self.scanned_automation_ids),
            eligibility_results=self.eligibility_results,
            acl_resources=self.acl_resources,
            binding_resources=self.binding_resources,
            group_resources=self.group_resources,
            automation_resources=self.automation_resources,
            binding_preferences=self.binding_preferences,
        )

    @classmethod
    def from_dict(cls, data: StoredDataDict | None) -> MatterBindingStoreData:
        """Create dataclass from stored dictionary."""
        if data is None:
            return cls()

        return cls(
            scanned_automation_ids=set(data.get("scanned_automation_ids", [])),
            eligibility_results=data.get("eligibility_results", {}),
            acl_resources=data.get("acl_resources", {}),
            binding_resources=data.get("binding_resources", {}),
            group_resources=data.get("group_resources", {}),
            automation_resources=data.get("automation_resources", {}),
            binding_preferences=data.get("binding_preferences", {}),
        )


# =============================================================================
# Store Class
# =============================================================================


class MatterBindingStore:
    """Store for Matter AutoBind data with reference-counted resources.

    This class handles persistence of binding state using Home Assistant's
    storage helper. It tracks:
    - Which automations have been scanned
    - Eligibility check results
    - ACL resources with reference counting
    - Binding resources with reference counting
    - Group resources with reference counting
    - Automation -> resource mappings for cleanup
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

    # =========================================================================
    # Load / Save
    # =========================================================================

    async def async_load(self) -> None:
        """Load data from storage."""
        if self._loaded:
            LOGGER.debug("Store already loaded, skipping")
            return

        # Check for debug reset flag BEFORE loading
        if DEBUG_RESET_STORE:
            LOGGER.warning("DEBUG_RESET_STORE is enabled! Wiping all store data")
            self._data = MatterBindingStoreData()
            self._loaded = True
            await self.async_save()
            LOGGER.warning(
                "Store has been reset. Set DEBUG_RESET_STORE=False and restart"
            )
            return

        LOGGER.debug("Loading Matter AutoBind store from disk")
        stored: StoredDataDict | None = await self._store.async_load()
        self._data = MatterBindingStoreData.from_dict(stored)
        self._loaded = True
        LOGGER.info(
            "Loaded Matter AutoBind store: %d scanned automations, "
            "%d ACL resources, %d binding resources, %d group resources",
            len(self._data.scanned_automation_ids),
            len(self._data.acl_resources),
            len(self._data.binding_resources),
            len(self._data.group_resources),
        )

    async def async_save(self) -> None:
        """Save data to storage."""
        LOGGER.debug("Saving Matter AutoBind store to disk")
        await self._store.async_save(self._data.to_dict())

    # =========================================================================
    # Eligibility Tracking
    # =========================================================================

    def is_automation_scanned(self, automation_id: str) -> bool:
        """Check if an automation has already been scanned."""
        return automation_id in self._data.scanned_automation_ids

    def get_all_tracked_automations(self) -> list[str]:
        """Get all automation IDs that have eligibility results.

        Returns:
            List of automation IDs that have been analyzed and have results.
        """
        return list(self._data.eligibility_results.keys())

    def get_eligibility_result(
        self, automation_id: str
    ) -> EligibilityResultDict | None:
        """Get the eligibility result for an automation."""
        return self._data.eligibility_results.get(automation_id)

    async def async_set_eligibility_result(
        self,
        automation_id: str,
        status: EligibilityStatus,
        trigger_entities: list[str],
        action_entities: list[str],
        reason: str,
    ) -> None:
        """Set the eligibility result for an automation."""
        self._data.eligibility_results[automation_id] = EligibilityResultDict(
            status=status.value,
            trigger_entities=trigger_entities,
            action_entities=action_entities,
            reason=reason,
        )
        LOGGER.debug(
            "Set eligibility for %s: %s - %s",
            automation_id,
            status.value,
            reason,
        )
        await self.async_save()

    async def async_mark_automation_scanned(self, automation_id: str) -> None:
        """Mark an automation as scanned and persist."""
        if automation_id not in self._data.scanned_automation_ids:
            self._data.scanned_automation_ids.add(automation_id)
            LOGGER.debug("Marked automation %s as scanned", automation_id)
            await self.async_save()

    async def async_clear_scanned_automation(self, automation_id: str) -> None:
        """Remove an automation from the scanned set."""
        if automation_id in self._data.scanned_automation_ids:
            self._data.scanned_automation_ids.discard(automation_id)
            LOGGER.debug("Cleared scanned status for automation %s", automation_id)
            await self.async_save()

    # =========================================================================
    # Automation Resource Tracking
    # =========================================================================

    def get_automation_resources(
        self, automation_id: str
    ) -> AutomationResourcesDict | None:
        """Get the resources used by an automation."""
        return self._data.automation_resources.get(automation_id)

    def _ensure_automation_resources(self, automation_id: str) -> None:
        """Ensure automation resources entry exists."""
        if automation_id not in self._data.automation_resources:
            self._data.automation_resources[automation_id] = AutomationResourcesDict(
                acl_keys=[],
                binding_keys=[],
                group_keys=[],
            )

    async def async_clear_automation_resources(self, automation_id: str) -> None:
        """Clear the automation resources mapping (not the resources themselves)."""
        if automation_id in self._data.automation_resources:
            del self._data.automation_resources[automation_id]
            await self.async_save()

    # =========================================================================
    # Binding Preferences
    # =========================================================================

    def get_binding_preference(self, automation_id: str) -> str | None:
        """Get the binding preference for an automation.

        Args:
            automation_id: The automation entity ID.

        Returns:
            'group', 'unicast', or None (meaning auto-detect).
        """
        return self._data.binding_preferences.get(automation_id)

    async def async_set_binding_preference(
        self, automation_id: str, preference: str | None
    ) -> None:
        """Set the binding preference for an automation.

        Args:
            automation_id: The automation entity ID.
            preference: 'group', 'unicast', 'auto', or None to remove preference.
        """
        if preference is None or preference == "auto":
            # Remove preference to use auto-detection
            self._data.binding_preferences.pop(automation_id, None)
            LOGGER.debug(
                "Cleared binding preference for %s (using auto-detect)",
                automation_id,
            )
        else:
            self._data.binding_preferences[automation_id] = preference
            LOGGER.debug(
                "Set binding preference for %s to %s",
                automation_id,
                preference,
            )
        await self.async_save()

    # =========================================================================
    # Group ID Allocation
    # =========================================================================

    def allocate_group_id(self) -> int:
        """Allocate a new unique group ID.

        Returns:
            A unique group ID not currently in use.

        Raises:
            RuntimeError: If no group IDs are available.
        """
        used_ids = {m["group_id"] for m in self._data.group_resources.values()}
        for gid in range(AUTOBIND_GROUP_ID_START, AUTOBIND_GROUP_ID_MAX + 1):
            if gid not in used_ids:
                return gid
        raise RuntimeError("No available group IDs")

    def get_existing_group_id(self, automation_id: str) -> int | None:
        """Get the group ID used by an automation, if any."""
        resources = self._data.automation_resources.get(automation_id)
        if not resources or not resources["group_keys"]:
            return None
        # Return the first group ID (automations typically use one group)
        first_key = resources["group_keys"][0]
        group_resource = self._data.group_resources.get(first_key)
        return group_resource["group_id"] if group_resource else None

    # =========================================================================
    # Generic Resource Helpers
    # =========================================================================

    def _get_resource_store(self, resource_type: ResourceType) -> dict[str, Any]:
        """Get the resource dictionary for a given resource type."""
        match resource_type:
            case ResourceType.ACL:
                return self._data.acl_resources
            case ResourceType.BINDING:
                return self._data.binding_resources
            case ResourceType.GROUP:
                return self._data.group_resources

    def _get_automation_keys_list(
        self, automation_id: str, resource_type: ResourceType
    ) -> list[str]:
        """Get the list of keys for a resource type in automation resources."""
        resources = self._data.automation_resources.get(automation_id)
        if not resources:
            return []
        match resource_type:
            case ResourceType.ACL:
                return resources["acl_keys"]
            case ResourceType.BINDING:
                return resources["binding_keys"]
            case ResourceType.GROUP:
                return resources["group_keys"]

    def _release_resource(
        self,
        automation_id: str,
        key: str,
        resource_type: ResourceType,
    ) -> bool:
        """Generic release logic for reference-counted resources.

        Args:
            automation_id: The automation releasing this resource.
            key: The resource key.
            resource_type: Type of resource (ACL, BINDING, GROUP).

        Returns:
            True if resource should be removed from device (ref_count reached 0).
        """
        store = self._get_resource_store(resource_type)
        if key not in store:
            return False

        resource = store[key]
        resource["ref_count"] -= 1
        if automation_id in resource["automation_ids"]:
            resource["automation_ids"].remove(automation_id)

        # Remove from automation's resource list
        keys_list = self._get_automation_keys_list(automation_id, resource_type)
        if key in keys_list:
            keys_list.remove(key)

        should_remove = resource["ref_count"] <= 0
        if should_remove:
            del store[key]
            LOGGER.debug(
                "%s %s removed (ref_count=0)", resource_type.value.upper(), key
            )
        else:
            LOGGER.debug(
                "%s %s ref_count decremented to %d",
                resource_type.value.upper(),
                key,
                resource["ref_count"],
            )

        return should_remove

    # =========================================================================
    # ACL Resource Management (Reference Counted)
    # =========================================================================

    def get_acl_resource(self, key: str) -> AclResourceDict | None:
        """Get an ACL resource by key."""
        return self._data.acl_resources.get(key)

    def acl_exists(self, key: str) -> bool:
        """Check if an ACL resource exists."""
        return key in self._data.acl_resources

    def acquire_acl(
        self,
        automation_id: str,
        target_node_id: int,
        source_node_id: int,
        auth_mode: int = 2,
    ) -> tuple[str, bool]:
        """Acquire an ACL resource, incrementing ref count or creating new.

        Args:
            automation_id: The automation acquiring this resource.
            target_node_id: Node where ACL will be created.
            source_node_id: Node/group to grant access to (subject).
            auth_mode: Authentication mode (2=CASE, 3=GROUP).

        Returns:
            Tuple of (key, is_new) where is_new indicates if resource was created.
        """
        key = acl_key(target_node_id, source_node_id, auth_mode)
        is_new = False

        if key in self._data.acl_resources:
            # Only increment ref count if this automation hasn't already acquired it
            if automation_id not in self._data.acl_resources[key]["automation_ids"]:
                self._data.acl_resources[key]["ref_count"] += 1
                self._data.acl_resources[key]["automation_ids"].append(automation_id)
                LOGGER.debug(
                    "ACL %s ref_count incremented to %d (new automation: %s)",
                    key,
                    self._data.acl_resources[key]["ref_count"],
                    automation_id,
                )
            else:
                LOGGER.debug(
                    "ACL %s already acquired by %s, ref_count unchanged at %d",
                    key,
                    automation_id,
                    self._data.acl_resources[key]["ref_count"],
                )
        else:
            # Create new resource entry
            self._data.acl_resources[key] = AclResourceDict(
                target_node_id=target_node_id,
                source_node_id=source_node_id,
                auth_mode=auth_mode,
                ref_count=1,
                automation_ids=[automation_id],
            )
            is_new = True
            LOGGER.debug("ACL %s created with ref_count=1", key)

        # Track in automation resources
        self._ensure_automation_resources(automation_id)
        if key not in self._data.automation_resources[automation_id]["acl_keys"]:
            self._data.automation_resources[automation_id]["acl_keys"].append(key)

        return key, is_new

    def release_acl(self, automation_id: str, key: str) -> bool:
        """Release an ACL resource, decrementing ref count.

        Args:
            automation_id: The automation releasing this resource.
            key: The ACL resource key.

        Returns:
            True if resource should be removed from device (ref_count reached 0).
        """
        return self._release_resource(automation_id, key, ResourceType.ACL)

    # =========================================================================
    # Binding Resource Management (Reference Counted)
    # =========================================================================

    def get_binding_resource(self, key: str) -> BindingResourceDict | None:
        """Get a binding resource by key."""
        return self._data.binding_resources.get(key)

    def binding_exists(self, key: str) -> bool:
        """Check if a binding resource exists."""
        return key in self._data.binding_resources

    def acquire_binding(
        self,
        automation_id: str,
        source_node_id: int,
        source_endpoint: int,
        target: int | str,  # node_id or "gXXXXX"
        target_endpoint: int,
        cluster_ids: list[int] | None = None,
    ) -> tuple[str, bool]:
        """Acquire a binding resource, incrementing ref count or creating new.

        Args:
            automation_id: The automation acquiring this resource.
            source_node_id: Node where binding will be written.
            source_endpoint: Endpoint with binding cluster.
            target: Target node ID (int) or group reference ("gXXXXX").
            target_endpoint: Target endpoint.
            cluster_ids: Optional list of cluster IDs to bind.

        Returns:
            Tuple of (key, is_new) where is_new indicates if resource was created.
        """
        key = binding_key(source_node_id, source_endpoint, target, target_endpoint)
        is_new = False

        if key in self._data.binding_resources:
            # Only increment ref count if this automation hasn't already acquired it
            if automation_id not in self._data.binding_resources[key]["automation_ids"]:
                self._data.binding_resources[key]["ref_count"] += 1
                self._data.binding_resources[key]["automation_ids"].append(
                    automation_id
                )
                LOGGER.debug(
                    "Binding %s ref_count incremented to %d (new automation: %s)",
                    key,
                    self._data.binding_resources[key]["ref_count"],
                    automation_id,
                )
            else:
                LOGGER.debug(
                    "Binding %s already acquired by %s, ref_count unchanged at %d",
                    key,
                    automation_id,
                    self._data.binding_resources[key]["ref_count"],
                )
        else:
            # Parse target
            target_node_id, target_group_id = parse_binding_target(target)

            # Create new resource entry
            self._data.binding_resources[key] = BindingResourceDict(
                source_node_id=source_node_id,
                source_endpoint=source_endpoint,
                target_node_id=target_node_id,
                target_group_id=target_group_id,
                target_endpoint=target_endpoint,
                cluster_ids=cluster_ids or [],
                ref_count=1,
                automation_ids=[automation_id],
            )
            is_new = True
            LOGGER.debug("Binding %s created with ref_count=1", key)

        # Track in automation resources
        self._ensure_automation_resources(automation_id)
        if key not in self._data.automation_resources[automation_id]["binding_keys"]:
            self._data.automation_resources[automation_id]["binding_keys"].append(key)

        return key, is_new

    def release_binding(self, automation_id: str, key: str) -> bool:
        """Release a binding resource, decrementing ref count.

        Args:
            automation_id: The automation releasing this resource.
            key: The binding resource key.

        Returns:
            True if resource should be removed from device (ref_count reached 0).
        """
        return self._release_resource(automation_id, key, ResourceType.BINDING)

    # =========================================================================
    # Group Resource Management (Reference Counted)
    # =========================================================================

    def get_group_resource(self, key: str) -> GroupResourceDict | None:
        """Get a group resource by key."""
        return self._data.group_resources.get(key)

    def group_exists(self, key: str) -> bool:
        """Check if a group resource exists."""
        return key in self._data.group_resources

    def acquire_group(
        self,
        automation_id: str,
        group_id: int,
        group_name: str,
        members: list[tuple[int, int]],  # list of (node_id, endpoint_id)
        epoch_key: str | None = None,
        key_set_index: int | None = None,
    ) -> tuple[str, bool]:
        """Acquire a group resource, incrementing ref count or creating new.

        Args:
            automation_id: The automation acquiring this resource.
            group_id: The Matter group ID.
            group_name: Name for the group.
            members: Target members list of (node_id, endpoint_id) tuples.
            epoch_key: Hex-encoded 16-byte epoch key (for new groups).
            key_set_index: Key set index 1-3 (for new groups).

        Returns:
            Tuple of (key, is_new) where is_new indicates if resource was created.
        """
        key = group_key(group_id)
        is_new = False

        if key in self._data.group_resources:
            # Only increment ref count if this automation hasn't already acquired it
            if automation_id not in self._data.group_resources[key]["automation_ids"]:
                self._data.group_resources[key]["ref_count"] += 1
                self._data.group_resources[key]["automation_ids"].append(automation_id)
                LOGGER.debug(
                    "Group %s ref_count incremented to %d (new automation: %s)",
                    key,
                    self._data.group_resources[key]["ref_count"],
                    automation_id,
                )
            else:
                LOGGER.debug(
                    "Group %s already acquired by %s, ref_count unchanged at %d",
                    key,
                    automation_id,
                    self._data.group_resources[key]["ref_count"],
                )
        else:
            # Create new resource entry with epoch key for future reuse
            self._data.group_resources[key] = GroupResourceDict(
                group_id=group_id,
                group_name=group_name,
                members=[
                    GroupMemberDict(node_id=nid, endpoint_id=eid)
                    for nid, eid in members
                ],
                source_nodes=[],  # Sources are tracked separately
                epoch_key=epoch_key,
                key_set_index=key_set_index,
                ref_count=1,
                automation_ids=[automation_id],
            )
            is_new = True
            LOGGER.debug(
                "Group %s created with ref_count=1, epoch_key=%s",
                key,
                epoch_key[:8] + "..." if epoch_key else None,
            )

        # Track in automation resources
        self._ensure_automation_resources(automation_id)
        if key not in self._data.automation_resources[automation_id]["group_keys"]:
            self._data.automation_resources[automation_id]["group_keys"].append(key)

        return key, is_new

    def _update_group_field(
        self,
        key: str,
        fld: str,
        value: Any,
        log_value: str | None = None,
    ) -> bool:
        """Update a single field on a group resource.

        Args:
            key: The group resource key.
            fld: The field name to update.
            value: The new value.
            log_value: Optional custom value for logging (e.g., truncated key).

        Returns:
            True if update was successful, False if group not found.
        """
        if key not in self._data.group_resources:
            return False

        self._data.group_resources[key][fld] = value  # type: ignore[literal-required]
        LOGGER.debug(
            "Group %s %s updated to %s",
            key,
            fld,
            log_value if log_value is not None else value,
        )
        return True

    def update_group_epoch_key(
        self,
        key: str,
        epoch_key: str,
        key_set_index: int,
    ) -> None:
        """Update the epoch key for a group (after creation on devices).

        Args:
            key: The group resource key.
            epoch_key: Hex-encoded 16-byte epoch key.
            key_set_index: Key set index (1-3).
        """
        truncated_key = epoch_key[:8] + "..." if epoch_key else None
        self._update_group_field(key, "epoch_key", epoch_key, truncated_key)
        self._update_group_field(key, "key_set_index", key_set_index)

    def update_group_source_nodes(
        self,
        key: str,
        source_nodes: list[int],
    ) -> None:
        """Update the source nodes that have GroupKeyMap for this group.

        Args:
            key: The group resource key.
            source_nodes: List of source node IDs with GroupKeyMap.
        """
        self._update_group_field(key, "source_nodes", source_nodes)

    def release_group(self, automation_id: str, key: str) -> bool:
        """Release a group resource, decrementing ref count.

        Args:
            automation_id: The automation releasing this resource.
            key: The group resource key.

        Returns:
            True if resource should be removed from devices (ref_count reached 0).
        """
        return self._release_resource(automation_id, key, ResourceType.GROUP)

    def update_group_members(
        self,
        key: str,
        members: list[tuple[int, int]],
    ) -> None:
        """Update the members of a group resource.

        Args:
            key: The group resource key.
            members: New list of (node_id, endpoint_id) tuples.
        """
        if key not in self._data.group_resources:
            return

        self._data.group_resources[key]["members"] = [
            GroupMemberDict(node_id=nid, endpoint_id=eid) for nid, eid in members
        ]
        LOGGER.debug("Group %s members updated to %d members", key, len(members))

    # =========================================================================
    # Legacy Methods (Backwards Compatibility with existing manager.py)
    # =========================================================================

    async def async_add_acl(self, automation_id: str, acl_entry: AclEntryDict) -> None:
        """Add an ACL entry for an automation (legacy compatibility).

        This adapts the old API to use the new reference-counted storage.
        """
        self.acquire_acl(
            automation_id,
            acl_entry["target_node_id"],
            acl_entry["source_node_id"],
            acl_entry["auth_mode"],
        )
        await self.async_save()

    async def async_add_binding(
        self, automation_id: str, binding_entry: BindingEntryDict
    ) -> None:
        """Add a binding for an automation (legacy compatibility).

        This adapts the old API to use the new reference-counted storage.
        """
        self.acquire_binding(
            automation_id,
            binding_entry["client_node_id"],
            binding_entry["client_endpoint"],
            binding_entry["target_node_id"],
            binding_entry["target_endpoint"],
        )
        await self.async_save()

    async def async_remove_acls_for_automation(
        self, automation_id: str
    ) -> list[AclEntryDict]:
        """Remove all ACL entries for an automation (legacy compatibility).

        Returns the list of removed entries for device cleanup.
        """
        removed: list[AclEntryDict] = []

        if automation_id not in self._data.automation_resources:
            return removed

        acl_keys = list(self._data.automation_resources[automation_id]["acl_keys"])
        for key in acl_keys:
            acl = self.get_acl_resource(key)
            if acl:
                removed.append(
                    AclEntryDict(
                        target_node_id=acl["target_node_id"],
                        source_node_id=acl["source_node_id"],
                        auth_mode=acl["auth_mode"],
                        acl_index=None,
                    )
                )
            self.release_acl(automation_id, key)

        await self.async_save()
        return removed

    async def async_remove_bindings_for_automation(
        self, automation_id: str
    ) -> list[BindingEntryDict]:
        """Remove all bindings for an automation (legacy compatibility).

        Returns the list of removed bindings for device cleanup.
        """
        removed: list[BindingEntryDict] = []

        if automation_id not in self._data.automation_resources:
            return removed

        binding_keys = list(
            self._data.automation_resources[automation_id]["binding_keys"]
        )
        for key in binding_keys:
            binding = self.get_binding_resource(key)
            if binding and binding["target_node_id"] is not None:
                removed.append(
                    BindingEntryDict(
                        client_node_id=binding["source_node_id"],
                        client_endpoint=binding["source_endpoint"],
                        target_node_id=binding["target_node_id"],
                        target_endpoint=binding["target_endpoint"],
                        clusters=[],
                    )
                )
            self.release_binding(automation_id, key)

        await self.async_save()
        return removed
