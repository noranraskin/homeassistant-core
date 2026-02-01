"""Resource reconciliation for Matter AutoBind.

This module handles reconciliation between desired and current resource states,
computing diffs and applying the minimal operations to sync device resources.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

from ..const import CONF_ENABLE_GROUP_BINDINGS
from ..matter import MatterAdapter
from ..store import MatterBindingStore, acl_key, binding_key, group_key
from .groups import GroupManager


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

    # Set of (target_node_id, source_node_id, auth_mode) tuples
    acls: set[tuple[int, int, int]]

    # Set of (source_node, source_ep, target (int or "gXXXX"), target_ep) tuples
    bindings: set[tuple[int, int, int | str, int]]

    # Set of (group_id, frozenset of (node_id, endpoint_id)) tuples
    groups: set[tuple[int, frozenset[tuple[int, int]]]]

    # The group ID to use if needed (None if single target)
    group_id: int | None = None


class ResourceReconciler:
    """Reconciler for Matter automation resources.

    This class handles the computation of desired state and reconciliation
    of ACLs, bindings, and groups between the store and Matter devices.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        store: MatterBindingStore,
        adapter: MatterAdapter,
        group_manager: GroupManager,
        logger: logging.Logger,
    ) -> None:
        """Initialize the reconciler.

        Args:
            hass: Home Assistant instance.
            config_entry: The config entry for options.
            store: The binding store for persistence.
            adapter: The Matter adapter for device operations.
            group_manager: The group manager for group operations.
            logger: Logger instance.
        """
        self._hass = hass
        self._config_entry = config_entry
        self._store = store
        self._adapter = adapter
        self._group_manager = group_manager
        self._logger = logger

    # =========================================================================
    # State Computation
    # =========================================================================

    def compute_desired_state(
        self,
        trigger_nodes: list[NodeInfo],
        action_nodes: list[NodeInfo],
        existing_group_id: int | None = None,
        automation_id: str | None = None,
    ) -> DesiredResourceState:
        """Compute what resources SHOULD exist for this automation.

        Args:
            trigger_nodes: List of trigger node info.
            action_nodes: List of action node info.
            existing_group_id: Reuse existing group ID if available.
            automation_id: Optional automation ID for preference lookup.

        Returns:
            DesiredResourceState with all required resources.
        """
        acls: set[tuple[int, int, int]] = set()
        bindings: set[tuple[int, int, int | str, int]] = set()
        groups: set[tuple[int, frozenset[tuple[int, int]]]] = set()
        group_id: int | None = None

        # Check if group bindings are enabled globally
        enable_groups = self._config_entry.options.get(
            CONF_ENABLE_GROUP_BINDINGS, False
        )

        # Check per-automation preference
        preference: str | None = None
        if automation_id:
            preference = self._store.get_binding_preference(automation_id)

        # Handle "none" preference - disable bindings completely
        if preference == "none":
            self._logger.info(
                "Automation %s: bindings disabled (explicit preference)",
                automation_id,
            )
            return DesiredResourceState(
                acls=acls,
                bindings=bindings,
                groups=groups,
                group_id=None,
            )

        # Decide binding strategy
        # Priority: 1) Per-automation preference, 2) Global setting + target count
        if preference == "unicast":
            use_groups = False
            self._logger.debug(
                "Automation %s: using unicast (explicit preference)",
                automation_id,
            )
        elif preference == "group":
            if not enable_groups:
                self._logger.warning(
                    "Automation %s prefers groups but groups are disabled globally",
                    automation_id,
                )
                use_groups = False
            else:
                use_groups = True
                self._logger.debug(
                    "Automation %s: using groups (explicit preference)",
                    automation_id,
                )
        else:
            # Auto-detect: use groups if >1 target AND groups enabled
            use_groups = len(action_nodes) > 1 and enable_groups
            self._logger.debug(
                "Automation %s: auto-detected %s (targets=%d, groups_enabled=%s)",
                automation_id,
                "groups" if use_groups else "unicast",
                len(action_nodes),
                enable_groups,
            )

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
                self._logger.info(
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

            # ACLs: Each target needs GROUP ACL (authMode=3) for receiving
            # group multicast messages. CASE ACLs are NOT needed for group mode
            # since the switch sends to the group, not individual nodes.
            for target in action_nodes:
                # GROUP ACL for group messages
                acls.add((target.node_id, group_id, 3))

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

    def get_current_resource_state(self, automation_id: str) -> DesiredResourceState:
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
                        acl["auth_mode"],
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

    # =========================================================================
    # Reconciliation
    # =========================================================================

    async def reconcile_acls(
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
                await self._adapter.write_acl(target_node, subject, auth_mode)
            self._logger.debug(
                "Acquired ACL %s (new=%s, auth_mode=%d)", key, is_new, auth_mode
            )

        for target_node, subject, auth_mode in to_remove:
            key = acl_key(target_node, subject, auth_mode)
            should_remove = self._store.release_acl(automation_id, key)
            if should_remove:
                # Actually remove ACL from device
                await self._adapter.remove_acl(target_node, subject, auth_mode)
            self._logger.debug("Released ACL %s (removed=%s)", key, should_remove)

    async def reconcile_bindings(
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
            self._logger.info(
                "Ensuring binding on device: node %d ep %d -> %s (store new=%s)",
                source_node,
                source_ep,
                target,
                is_new,
            )
            await self._adapter.write_binding(source_node, source_ep, target, target_ep)
            self._logger.debug("Acquired binding %s (new=%s)", key, is_new)

        for source_node, source_ep, target, target_ep in to_remove:
            key = binding_key(source_node, source_ep, target, target_ep)
            should_remove = self._store.release_binding(automation_id, key)
            if should_remove:
                # Actually remove binding from device
                await self._adapter.remove_binding(
                    source_node, source_ep, target, target_ep
                )
            self._logger.debug("Released binding %s (removed=%s)", key, should_remove)

    async def reconcile_groups(
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
                self._logger.debug(
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
                source_ids = [n.node_id for n in source_nodes] if source_nodes else None
                await self._group_manager.create_group_on_devices(
                    gid, members, source_ids
                )
            self._logger.debug("Acquired group %s (new=%s)", key, is_new)

        # Groups to update (same ID, different members or sources)
        for gid in current_by_id.keys() & desired_by_id.keys():
            current_members = current_by_id[gid]
            desired_members = desired_by_id[gid]
            key = group_key(gid)

            # Check if group should be removed (less than 2 targets remaining)
            if len(desired_members) < 2:
                self._logger.info(
                    "Group %d reduced to %d target(s), removing group",
                    gid,
                    len(desired_members),
                )
                # Get source nodes BEFORE releasing (release deletes from store)
                group_resource = self._store.get_group_resource(key)
                source_node_ids = (
                    set(group_resource.get("source_nodes", []))
                    if group_resource
                    else set()
                )
                should_remove = self._store.release_group(automation_id, key)
                if should_remove:
                    # Remove from all devices (targets AND sources)
                    await self._group_manager.remove_group_completely(
                        gid, current_members, source_node_ids
                    )
                continue

            # Handle member changes
            if current_members != desired_members:
                members_to_add = desired_members - current_members
                members_to_remove = current_members - desired_members

                # For new target members, set up GroupKeyMap first
                if members_to_add:
                    source_ids = (
                        [n.node_id for n in source_nodes] if source_nodes else None
                    )
                    await self._group_manager.setup_group_key_for_new_members(
                        gid, members_to_add, source_ids
                    )

                # Update target group membership
                for node_id, endpoint_id in members_to_add:
                    await self._group_manager.add_device_to_group(
                        gid, node_id, endpoint_id
                    )
                for node_id, endpoint_id in members_to_remove:
                    await self._group_manager.remove_device_from_group(
                        gid, node_id, endpoint_id
                    )

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
                    await self._group_manager.setup_group_key_for_sources(
                        gid, sources_to_add
                    )

                # Remove GroupKeyMap from old sources
                if sources_to_remove:
                    await self._group_manager.remove_group_key_from_sources(
                        gid, sources_to_remove
                    )

                # Update store with new source list
                if sources_to_add or sources_to_remove:
                    self._store.update_group_source_nodes(key, list(desired_source_ids))

        # Groups to remove (no longer needed)
        for gid in current_by_id.keys() - desired_by_id.keys():
            key = group_key(gid)
            # Get source nodes BEFORE releasing (release deletes from store)
            group_resource = self._store.get_group_resource(key)
            source_node_ids = (
                set(group_resource.get("source_nodes", [])) if group_resource else set()
            )
            should_remove = self._store.release_group(automation_id, key)
            if should_remove:
                # Remove from all devices (targets AND sources)
                members = current_by_id[gid]
                await self._group_manager.remove_group_completely(
                    gid, members, source_node_ids
                )
            self._logger.debug("Released group %s (removed=%s)", key, should_remove)

    # =========================================================================
    # Resource Release
    # =========================================================================

    async def release_all_resources(self, automation_id: str) -> None:
        """Release all resources for an automation.

        Args:
            automation_id: The automation to clean up.
        """
        self._logger.info("Releasing all resources for automation %s", automation_id)

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
                await self._adapter.remove_binding(
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
                await self._adapter.remove_acl(
                    acl["target_node_id"],
                    acl["source_node_id"],
                    acl["auth_mode"],
                )

        # Release groups last
        for key in list(resources["group_keys"]):
            group = self._store.get_group_resource(key)
            should_remove = self._store.release_group(automation_id, key)
            if should_remove and group:
                members = frozenset(
                    (m["node_id"], m["endpoint_id"]) for m in group["members"]
                )
                await self._group_manager.remove_group_from_devices(
                    group["group_id"], members
                )

        # Clear automation resources mapping
        await self._store.async_clear_automation_resources(automation_id)
