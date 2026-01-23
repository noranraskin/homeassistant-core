"""Group management for Matter AutoBind.

This module handles all Matter group-related operations including:
- Group creation and removal on devices
- Group key management (KeySetWrite, GroupKeyMap)
- Group membership management (AddGroup, RemoveGroup)
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from chip.clusters import Objects as Clusters

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

if TYPE_CHECKING:
    from homeassistant.components.matter.helpers import (  # pylint: disable=hass-component-root-import
        get_matter as _get_matter,
    )
else:
    from homeassistant.components.matter.helpers import (  # pylint: disable=hass-component-root-import
        get_matter as _get_matter,
    )

from ..store import MatterBindingStore, group_key


def get_matter(hass: HomeAssistant) -> Any:
    """Wrapper for get_matter to satisfy type checking."""
    return _get_matter(hass)


class GroupManager:
    """Manager for Matter group operations.

    This class handles all low-level Matter group operations including:
    - KeySetWrite and GroupKeyMap management
    - AddGroup/RemoveGroup commands
    - Group key propagation to sources and targets
    """

    def __init__(
        self,
        hass: HomeAssistant,
        store: MatterBindingStore,
        logger: logging.Logger,
    ) -> None:
        """Initialize the group manager.

        Args:
            hass: Home Assistant instance.
            store: The binding store for group persistence.
            logger: Logger instance.
        """
        self._hass = hass
        self._store = store
        self._logger = logger

    def _get_matter_client(self) -> Any:
        """Get the Matter client, raising if unavailable."""
        try:
            matter = get_matter(self._hass)
        except (KeyError, StopIteration) as err:
            raise RuntimeError("Matter integration not available") from err
        return matter.matter_client

    # =========================================================================
    # Group Creation and Removal
    # =========================================================================

    async def create_group_on_devices(
        self,
        group_id: int,
        members: frozenset[tuple[int, int]],
        source_node_ids: list[int] | None = None,
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
            source_node_ids: Node IDs of sources needing the key to encrypt.
        """
        try:
            matter_client = self._get_matter_client()
        except RuntimeError:
            self._logger.warning("Matter integration not available for group creation")
            return

        group_name = f"AutoBind-{group_id}"
        key_set_index = (group_id % 16) + 1

        # Generate ONE shared key for all nodes
        epoch_key = secrets.token_bytes(16)
        epoch_start_time = int(time.time() * 1_000_000)

        self._logger.info(
            "Creating group %d with shared key (key=%s...) on %d targets + %d sources",
            group_id,
            epoch_key.hex()[:8],
            len(members),
            len(source_node_ids) if source_node_ids else 0,
        )

        # Save the epoch key to the store for future member additions
        key = group_key(group_id)
        self._store.update_group_epoch_key(key, epoch_key.hex(), key_set_index)

        # Track source node IDs in the store
        self._store.update_group_source_nodes(key, source_node_ids or [])

        # Collect all nodes that need the key
        all_nodes_for_key: set[int] = set()

        # Add source nodes (they need key to ENCRYPT outgoing messages)
        if source_node_ids:
            for node_id in source_node_ids:
                all_nodes_for_key.add(node_id)

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

    async def setup_group_key_for_new_members(
        self,
        group_id: int,
        new_members: frozenset[tuple[int, int]],
        source_node_ids: list[int] | None = None,
    ) -> None:
        """Set up GroupKeyMap on new members using stored epoch key.

        When a group already exists and new members are added, we need to
        write the KeySet and GroupKeyMap to the new devices before calling AddGroup.

        Args:
            group_id: The group ID.
            new_members: New members (node_id, endpoint_id) being added.
            source_node_ids: Source node IDs that also need the key (if new).
        """
        # Get the stored epoch key for this group
        key = group_key(group_id)
        group_resource = self._store.get_group_resource(key)
        if not group_resource:
            self._logger.warning(
                "Group %s not found in store, cannot propagate key", key
            )
            return

        epoch_key_hex = group_resource.get("epoch_key")
        key_set_index = group_resource.get("key_set_index")

        if not epoch_key_hex or not key_set_index:
            self._logger.warning(
                "Group %s missing epoch_key or key_set_index, regenerating",
                key,
            )
            return

        try:
            matter_client = self._get_matter_client()
        except RuntimeError:
            self._logger.warning("Matter integration not available for group key setup")
            return

        epoch_key = bytes.fromhex(epoch_key_hex)
        epoch_start_time = int(time.time() * 1_000_000)

        self._logger.info(
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
        if source_node_ids:
            for node_id in source_node_ids:
                nodes_needing_key.add(node_id)

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

    async def setup_group_key_for_sources(
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
            self._logger.warning("Group %s not found, cannot setup source keys", key)
            return

        epoch_key_hex = group_resource.get("epoch_key")
        key_set_index = group_resource.get("key_set_index")

        if not epoch_key_hex or not key_set_index:
            self._logger.warning(
                "Group %s missing epoch_key, cannot setup source keys", key
            )
            return

        try:
            matter_client = self._get_matter_client()
        except RuntimeError:
            self._logger.warning("Matter integration not available")
            return

        epoch_key = bytes.fromhex(epoch_key_hex)
        epoch_start_time = int(time.time() * 1_000_000)

        self._logger.info(
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

    async def remove_group_key_from_sources(
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
            matter_client = self._get_matter_client()
        except RuntimeError:
            self._logger.warning("Matter integration not available")
            return

        self._logger.info(
            "Removing GroupKeyMap for group %d from %d old source(s): %s",
            group_id,
            len(source_node_ids),
            source_node_ids,
        )

        for node_id in source_node_ids:
            await self._remove_group_key_from_node(matter_client, node_id, group_id)

    async def remove_group_completely(
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
            matter_client = self._get_matter_client()
        except RuntimeError:
            self._logger.warning("Matter integration not available for group removal")
            return

        # Get source nodes from store
        key = group_key(group_id)
        group_resource = self._store.get_group_resource(key)
        source_node_ids = (
            set(group_resource.get("source_nodes", [])) if group_resource else set()
        )

        self._logger.info(
            "Removing group %d completely: %d targets, %d sources",
            group_id,
            len(members),
            len(source_node_ids),
        )

        # Remove from target members (RemoveGroup + remove GroupKeyMap)
        for node_id, endpoint_id in members:
            await self.remove_device_from_group(group_id, node_id, endpoint_id)
            await self._remove_group_key_from_node(matter_client, node_id, group_id)

        # Remove GroupKeyMap from sources (they were never in the group)
        for node_id in source_node_ids:
            await self._remove_group_key_from_node(matter_client, node_id, group_id)

    async def remove_group_from_devices(
        self,
        group_id: int,
        members: frozenset[tuple[int, int]],
    ) -> None:
        """Remove a Matter group from all member devices."""
        for node_id, endpoint_id in members:
            await self.remove_device_from_group(group_id, node_id, endpoint_id)

    # =========================================================================
    # Device Group Membership
    # =========================================================================

    async def add_device_to_group(
        self,
        group_id: int,
        node_id: int,
        endpoint_id: int,
    ) -> None:
        """Add a single device to a Matter group."""
        try:
            matter_client = self._get_matter_client()

            await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=endpoint_id,
                command=Clusters.Groups.Commands.AddGroup(
                    groupID=group_id,
                    groupName=f"AutoBind-{group_id}",
                ),
            )
            self._logger.info("Added node %d to group %d", node_id, group_id)
        except (
            HomeAssistantError,
            OSError,
            ValueError,
            KeyError,
            StopIteration,
            RuntimeError,
        ) as err:
            self._logger.error("Failed to add node %d to group: %s", node_id, err)

    async def remove_device_from_group(
        self,
        group_id: int,
        node_id: int,
        endpoint_id: int,
    ) -> None:
        """Remove a single device from a Matter group."""
        try:
            matter_client = self._get_matter_client()

            await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=endpoint_id,
                command=Clusters.Groups.Commands.RemoveGroup(groupID=group_id),
            )
            self._logger.info("Removed node %d from group %d", node_id, group_id)
        except (
            HomeAssistantError,
            OSError,
            ValueError,
            KeyError,
            StopIteration,
            RuntimeError,
        ) as err:
            self._logger.error("Failed to remove node %d from group: %s", node_id, err)

    # =========================================================================
    # Low-level Key Management
    # =========================================================================

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
                self._logger.debug(
                    "Node %d current GroupKeyMap: %s", node_id, current_map
                )
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
                        self._logger.warning(
                            "Filtering invalid GroupKeyMap entry on node %d: %s",
                            node_id,
                            entry,
                        )
                elif hasattr(entry, "groupId") and hasattr(entry, "groupKeySetID"):
                    # Object format
                    valid_entries.append(entry)
                else:
                    self._logger.warning(
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
                self._logger.debug(
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
                self._logger.debug(
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
                self._logger.info("✓ KeySetWrite succeeded on node %d", node_id)
            except (HomeAssistantError, OSError, ValueError) as err:
                self._logger.warning("KeySetWrite on node %d failed: %s", node_id, err)

            # Write GroupKeyMap (using filtered valid entries)
            try:
                new_map_entry = Clusters.GroupKeyManagement.Structs.GroupKeyMapStruct(
                    groupId=group_id,
                    groupKeySetID=key_set_index,
                    fabricIndex=0,
                )
                updated_map = [*valid_entries, new_map_entry]
                self._logger.debug(
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
                    self._logger.info(
                        "✓ GroupKeyMap VERIFIED: node %d has group %d mapped",
                        node_id,
                        group_id,
                    )
                else:
                    self._logger.error(
                        "✗ GroupKeyMap WRITE FAILED: node %d does not have group %d after write",
                        node_id,
                        group_id,
                    )

            except (HomeAssistantError, OSError, ValueError) as err:
                self._logger.warning(
                    "Failed to write GroupKeyMap on node %d: %s", node_id, err
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            self._logger.error("Failed to setup group key on node %d: %s", node_id, err)

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
                    self._logger.debug(
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
                self._logger.info(
                    "✓ Removed GroupKeyMap for group %d from node %d",
                    group_id,
                    node_id,
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            self._logger.warning(
                "Failed to remove GroupKeyMap for group %d from node %d: %s",
                group_id,
                node_id,
                err,
            )

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
            self._logger.debug(
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
            self._logger.debug("AddGroup response from node %d: %s", node_id, response)

            # Check status
            add_status = None
            if isinstance(response, dict):
                add_status = response.get("status")
            elif hasattr(response, "status"):
                add_status = response.status

            if add_status == 0:
                self._logger.info(
                    "✓ AddGroup SUCCESS: node %d endpoint %d added to group %d",
                    node_id,
                    endpoint_id,
                    group_id,
                )
            else:
                self._logger.warning(
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
                self._logger.info(
                    "✓ GROUP VERIFIED: node %d endpoint %d is member of group %d",
                    node_id,
                    endpoint_id,
                    group_id,
                )
            else:
                self._logger.warning(
                    "✗ GROUP UNVERIFIED: node %d endpoint %d not in group %d",
                    node_id,
                    endpoint_id,
                    group_id,
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            self._logger.error(
                "Failed to add node %d to group %d: %s", node_id, group_id, err
            )
