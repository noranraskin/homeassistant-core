"""Matter client adapter for Matter AutoBind.

This module provides a MatterAdapter class that wraps the Matter client
to provide high-level operations for ACL and binding management.
It abstracts the low-level Matter protocol operations from the manager.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

# Binding Cluster ID
CLUSTER_ID_BINDING = 0x001E

if TYPE_CHECKING:
    from homeassistant.components.matter.helpers import (  # pylint: disable=hass-component-root-import
        get_matter as _get_matter,
    )
else:
    from homeassistant.components.matter.helpers import (  # pylint: disable=hass-component-root-import
        get_matter as _get_matter,
    )


def get_matter(hass: HomeAssistant) -> Any:
    """Wrapper for get_matter to satisfy type checking."""
    return _get_matter(hass)


_LOGGER = logging.getLogger(__name__)


class MatterAdapter:
    """Adapter for Matter client operations.

    Provides high-level methods for ACL and binding management on Matter devices.
    This class abstracts the low-level Matter protocol details from the manager.

    Example:
        adapter = MatterAdapter(hass)
        await adapter.write_acl(target_node_id=5, subject=3, auth_mode=2)
        await adapter.write_binding(source_node=3, source_ep=1, target=5, target_ep=1)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger | None = None,
        debug_overwrite_acls: bool = False,
    ) -> None:
        """Initialize the adapter.

        Args:
            hass: Home Assistant instance.
            logger: Optional logger for debug output.
            debug_overwrite_acls: If True, clean up duplicate/stale ACLs on write.
        """
        self._hass = hass
        self._logger = logger or _LOGGER
        self._debug_overwrite_acls = debug_overwrite_acls

    def _get_node(self, node_id: int) -> Any | None:
        """Get a node by ID.

        Args:
            node_id: The node ID.

        Returns:
            The MatterNode instance or None.
        """
        try:
            matter_client = self._get_matter_client()
            return matter_client.node_devices.get(node_id)
        except (RuntimeError, AttributeError):
            return None

    def _get_binding_endpoint(self, node_id: int, source_endpoint: int) -> int:
        """Get the endpoint ID that has the Binding cluster.

        Usually this is the same as source_endpoint, but we should verify.
        If the specific endpoint has the Binding cluster, use it.
        Otherwise, search for any endpoint with the Binding cluster.

        Args:
            node_id: The node ID.
            source_endpoint: The proposed source endpoint.

        Returns:
            The endpoint ID to write bindings to.
        """
        node = self._get_node(node_id)
        if not node:
            return source_endpoint

        # Check if requested endpoint has Binding cluster
        endpoint = node.endpoints.get(source_endpoint)
        if endpoint and endpoint.has_cluster(CLUSTER_ID_BINDING):
            return source_endpoint

        # Fallback: search across all endpoints
        for ep_id, ep in node.endpoints.items():
            if ep.has_cluster(CLUSTER_ID_BINDING):
                self._logger.debug(
                    "Endpoint %d does not have Binding cluster, using endpoint %d instead",
                    source_endpoint,
                    ep_id,
                )
                return ep_id

        self._logger.warning(
            "Node %d does not have Binding cluster on any endpoint", node_id
        )
        return source_endpoint

    def _get_matter_client(self) -> Any:
        """Get the Matter client.

        Returns:
            The Matter client instance.

        Raises:
            RuntimeError: If Matter integration is not available.
        """
        try:
            matter = get_matter(self._hass)
        except (KeyError, StopIteration) as err:
            raise RuntimeError("Matter integration not available") from err
        return matter.matter_client

    async def _read_modify_write(
        self,
        node_id: int,
        attribute_path: str,
        modify_fn: Callable[[list[Any]], list[Any]],
        verify: bool = True,
        operation_name: str = "modify",
    ) -> tuple[bool, list[Any]]:
        """Read-modify-write helper for list attributes.

        This encapsulates the common pattern of:
        1. Read current list from attribute
        2. Modify the list (filter, add, etc.)
        3. Write updated list back
        4. Optionally verify the write

        Args:
            node_id: The node to operate on.
            attribute_path: The attribute path (e.g., "0/31/0").
            modify_fn: Function that takes current list and returns modified list.
            verify: Whether to verify the write succeeded.
            operation_name: Name for logging (e.g., "ACL write", "binding remove").

        Returns:
            Tuple of (success, final_list) where final_list is the verified list
            or the expected list if verify=False.
        """
        matter_client = self._get_matter_client()

        # Read current
        current_resp = await matter_client.read_attribute(node_id, attribute_path)
        current_list = current_resp.get(attribute_path, [])
        if not isinstance(current_list, list):
            current_list = []

        # Modify
        updated_list = modify_fn(current_list)

        # Skip write if no changes
        if updated_list == current_list:
            return True, current_list

        # Write
        await matter_client.write_attribute(
            node_id=node_id,
            attribute_path=attribute_path,
            value=updated_list,
        )

        # Verify
        if verify:
            verify_resp = await matter_client.read_attribute(node_id, attribute_path)
            verify_list = verify_resp.get(attribute_path, [])
            # Check count matches (simple verification)
            if len(verify_list) == len(updated_list):
                self._logger.debug(
                    "✓ %s verified on node %d: %d entries",
                    operation_name,
                    node_id,
                    len(verify_list),
                )
                return True, verify_list
            self._logger.error(
                "✗ %s failed on node %d: expected %d entries, got %d",
                operation_name,
                node_id,
                len(updated_list),
                len(verify_list),
            )
            return False, verify_list

        return True, updated_list

    # =========================================================================
    # ACL Operations
    # =========================================================================

    async def write_acl(
        self,
        target_node_id: int,
        subject: int,
        auth_mode: int = 2,
    ) -> bool:
        """Write ACL entry to a Matter device.

        Args:
            target_node_id: The node to write the ACL to.
            subject: The subject to grant access (node_id for CASE, group_id for GROUP).
            auth_mode: The authentication mode:
                2 = CASE (node-to-node unicast, subject is source node ID)
                3 = GROUP (group multicast, subject is group ID)

        Returns:
            True if write succeeded, False otherwise.
        """
        try:
            matter_client = self._get_matter_client()
        except RuntimeError:
            self._logger.warning("Matter integration not available for ACL write")
            return False

        auth_mode_name = "GROUP" if auth_mode == 3 else "CASE"

        try:
            acl_path = "0/31/0"
            self._logger.debug(
                "Reading current ACLs from node %d path %s", target_node_id, acl_path
            )
            current_acls = await matter_client.read_attribute(target_node_id, acl_path)
            current_acl_list = current_acls.get(acl_path, [])
            self._logger.debug(
                "Current ACLs on node %d: %d entries",
                target_node_id,
                len(current_acl_list),
            )

            # Get fabric ID
            server_info = matter_client.server_info
            if server_info is None:
                self._logger.error("Matter server info not available")
                return False
            fabric_id = server_info.fabric_id

            # Clean up ACLs in debug mode
            if self._debug_overwrite_acls:
                current_acl_list = self._cleanup_acls(
                    current_acl_list, fabric_id, target_node_id
                )

            # Check if ACL already exists
            for acl_entry in current_acl_list:
                if isinstance(acl_entry, dict):
                    subjects = acl_entry.get("3", []) or []
                    entry_auth_mode = acl_entry.get("2", 0)
                else:
                    subjects = getattr(acl_entry, "subjects", []) or []
                    entry_auth_mode = getattr(acl_entry, "authMode", 0)

                if subject in subjects and entry_auth_mode == auth_mode:
                    self._logger.debug(
                        "ACL for subject %d already exists on node %d",
                        subject,
                        target_node_id,
                    )
                    return True

            # Create new ACL entry
            new_acl = {
                "1": 3,  # Privilege: Operate
                "2": auth_mode,  # AuthMode: CASE or GROUP
                "3": [subject],  # Subjects
                "4": None,  # Targets (None = all)
                "254": fabric_id,  # FabricIndex
            }

            updated_list = [*current_acl_list, new_acl]
            self._logger.info(
                "Writing %s ACL to node %d for subject %d (total: %d entries)",
                auth_mode_name,
                target_node_id,
                subject,
                len(updated_list),
            )

            await matter_client.write_attribute(
                node_id=target_node_id,
                attribute_path=acl_path,
                value=updated_list,
            )

            # Verify write
            verify_resp = await matter_client.read_attribute(target_node_id, acl_path)
            verify_list = verify_resp.get(acl_path, [])

            if len(verify_list) >= len(updated_list):
                self._logger.info(
                    "✓ ACL write verified on node %d for subject %d",
                    target_node_id,
                    subject,
                )
                acl_verified = True
            else:
                self._logger.error(
                    "✗ ACL write failed on node %d: expected %d entries, got %d",
                    target_node_id,
                    len(updated_list),
                    len(verify_list),
                )
                acl_verified = False

        except (HomeAssistantError, OSError, ValueError) as err:
            self._logger.error("Failed to write ACL: %s", err)
            return False
        else:
            return acl_verified

    def _cleanup_acls(
        self,
        acl_list: list[Any],
        fabric_id: int,
        node_id: int,
    ) -> list[Any]:
        """Clean up duplicate and stale ACL entries.

        Args:
            acl_list: Current ACL entries.
            fabric_id: Our fabric ID.
            node_id: The node ID (for logging).

        Returns:
            Cleaned ACL list.
        """
        self._logger.warning(
            "⚠️ DEBUG_OVERWRITE_ACLS: Cleaning up ACLs on node %d", node_id
        )
        cleaned_entries: list[dict[str, Any]] = []
        seen_acls: set[tuple[int, int, tuple[int, ...]]] = set()

        for acl_entry in acl_list:
            if not isinstance(acl_entry, dict):
                continue

            if "1" not in acl_entry or "2" not in acl_entry:
                self._logger.warning(
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

            # Create signature for deduplication
            sig = (privilege, entry_auth_mode, subjects)
            if sig in seen_acls:
                self._logger.warning(
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
                self._logger.debug("Keeping admin ACL: subjects=%s", subjects)
                continue

            # Remove other Operate ACLs
            self._logger.info(
                "Removing stale ACL: privilege=%d, authMode=%d, subjects=%s",
                privilege,
                entry_auth_mode,
                subjects,
            )

        self._logger.info(
            "After cleanup, node %d has %d ACL entries", node_id, len(cleaned_entries)
        )
        return cleaned_entries

    async def remove_acl(
        self,
        target_node_id: int,
        subject: int,
        auth_mode: int = 2,
    ) -> bool:
        """Remove ACL entry from a Matter device.

        Args:
            target_node_id: The node to remove ACL from.
            subject: The subject to remove (node_id for CASE, group_id for GROUP).
            auth_mode: The authentication mode (2=CASE, 3=GROUP).

        Returns:
            True if removal succeeded, False otherwise.
        """
        try:
            self._get_matter_client()
        except RuntimeError:
            return False

        auth_mode_name = "GROUP" if auth_mode == 3 else "CASE"

        def filter_acl(acl_list: list[Any]) -> list[Any]:
            """Filter out entries matching subject AND auth_mode."""
            result = []
            for acl_entry in acl_list:
                if isinstance(acl_entry, dict):
                    subjects = acl_entry.get("3", []) or []
                    entry_auth_mode = acl_entry.get("2", 0)
                else:
                    subjects = getattr(acl_entry, "subjects", []) or []
                    entry_auth_mode = getattr(acl_entry, "authMode", 0)

                if subject not in subjects or entry_auth_mode != auth_mode:
                    result.append(acl_entry)
            return result

        try:
            success, _ = await self._read_modify_write(
                node_id=target_node_id,
                attribute_path="0/31/0",
                modify_fn=filter_acl,
                verify=False,  # Remove doesn't need strict verification
                operation_name=f"{auth_mode_name} ACL remove",
            )
            if success:
                self._logger.info(
                    "Removed %s ACL from node %d for subject %d",
                    auth_mode_name,
                    target_node_id,
                    subject,
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            self._logger.error("Failed to remove ACL: %s", err)
            return False
        else:
            return success

    # =========================================================================
    # Binding Operations
    # =========================================================================

    async def write_binding(
        self,
        source_node_id: int,
        source_endpoint: int,
        target: int | str,
        target_endpoint: int,
    ) -> bool:
        """Write binding entry to a Matter device.

        Args:
            source_node_id: The node to write the binding to.
            source_endpoint: The source endpoint.
            target: Target node ID (int) or group reference (str like "g32768").
            target_endpoint: The target endpoint.

        Returns:
            True if write succeeded, False otherwise.
        """
        try:
            matter_client = self._get_matter_client()
        except RuntimeError:
            self._logger.warning("Matter integration not available for binding write")
            return False

        try:
            # Determine correct endpoint for binding
            binding_endpoint = self._get_binding_endpoint(
                source_node_id, source_endpoint
            )
            binding_path = f"{binding_endpoint}/30/0"
            self._logger.info(
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

            if not isinstance(current_bindings, list):
                current_bindings = []

            # Filter invalid bindings and check for existing
            valid_bindings = self._filter_valid_bindings(
                current_bindings, source_node_id
            )

            # Get fabric ID
            server_info = matter_client.server_info
            if server_info is None:
                self._logger.error("Matter server info not available")
                return False
            fabric_id = server_info.fabric_id

            # Create binding entry
            is_group = isinstance(target, str) and target.startswith("g")
            if is_group:
                group_id = int(str(target)[1:])
                new_binding = {"254": fabric_id, "2": group_id}

                # Check if exists
                if any(
                    b.get("2") == group_id
                    for b in valid_bindings
                    if isinstance(b, dict)
                ):
                    self._logger.info(
                        "✓ Group binding to %d already exists on node %d",
                        group_id,
                        source_node_id,
                    )
                    return True
            else:
                new_binding = {
                    "254": fabric_id,
                    "1": int(target),
                    "3": target_endpoint,
                    "4": None,
                }

                # Check if exists
                for b in valid_bindings:
                    if (
                        isinstance(b, dict)
                        and b.get("1") == int(target)
                        and b.get("3") == target_endpoint
                    ):
                        self._logger.info(
                            "✓ Unicast binding to node %d already exists on node %d",
                            target,
                            source_node_id,
                        )
                        return True

            updated_bindings = [*valid_bindings, new_binding]

            await matter_client.write_attribute(
                node_id=source_node_id,
                attribute_path=binding_path,
                value=updated_bindings,
            )

            # Verify
            verify_resp = await matter_client.read_attribute(
                source_node_id, binding_path
            )
            verify_bindings = verify_resp.get(binding_path, [])

            if len(verify_bindings) > len(current_bindings):
                self._logger.info(
                    "✓ BINDING WRITE VERIFIED: node %d now has %d bindings",
                    source_node_id,
                    len(verify_bindings),
                )
                binding_verified = True
            else:
                self._logger.error(
                    "✗ BINDING WRITE FAILED: node %d has %d bindings (expected %d)",
                    source_node_id,
                    len(verify_bindings),
                    len(updated_bindings),
                )
                binding_verified = False

        except (HomeAssistantError, OSError, ValueError) as err:
            self._logger.error("Failed to write binding: %s", err)
            return False
        else:
            return binding_verified

    def _filter_valid_bindings(
        self,
        bindings: list[Any],
        node_id: int,
    ) -> list[dict[str, Any]]:
        """Filter out invalid binding entries.

        Args:
            bindings: Current binding entries.
            node_id: The node ID (for logging).

        Returns:
            List of valid bindings.
        """
        valid_bindings: list[dict[str, Any]] = []

        for binding in bindings:
            if isinstance(binding, dict):
                if "254" in binding and ("1" in binding or "2" in binding):
                    valid_bindings.append(binding)
                else:
                    self._logger.warning(
                        "Filtering invalid binding on node %d: %s", node_id, binding
                    )
            elif hasattr(binding, "fabricIndex"):
                if hasattr(binding, "node") or hasattr(binding, "group"):
                    # Convert to dict
                    valid_bindings.append(dict(binding))
                else:
                    self._logger.warning(
                        "Filtering invalid binding on node %d: %s", node_id, binding
                    )

        return valid_bindings

    async def ensure_binding(
        self,
        source_node_id: int,
        source_endpoint: int,
        target: int | str,
        target_endpoint: int,
    ) -> bool:
        """Ensure a binding exists on a Matter device.

        Checks if binding already exists on device before writing.
        This fixes store/device sync issues from previous failed writes.

        Args:
            source_node_id: The node to check/write binding on.
            source_endpoint: The source endpoint.
            target: Target node ID or group reference.
            target_endpoint: The target endpoint.

        Returns:
            True if binding exists or was created, False otherwise.
        """
        try:
            matter_client = self._get_matter_client()
        except RuntimeError:
            self._logger.warning("Matter integration not available for binding check")
            return False

        try:
            # Determine correct endpoint for binding
            binding_endpoint = self._get_binding_endpoint(
                source_node_id, source_endpoint
            )
            binding_path = f"{binding_endpoint}/30/0"
            current_bindings_resp = await matter_client.read_attribute(
                source_node_id, binding_path
            )
            current_bindings = current_bindings_resp.get(binding_path, [])

            if not isinstance(current_bindings, list):
                current_bindings = []

            is_group = isinstance(target, str) and target.startswith("g")
            target_value = int(str(target)[1:]) if is_group else int(target)

            # Check if binding exists
            for binding in current_bindings:
                if isinstance(binding, dict):
                    if (is_group and binding.get("2") == target_value) or (
                        not is_group and binding.get("1") == target_value
                    ):
                        self._logger.debug(
                            "Binding to %s already exists on node %d",
                            target,
                            source_node_id,
                        )
                        return True

            # Binding doesn't exist, write it
            self._logger.info(
                "Binding to %s not found on node %d, writing it now",
                target,
                source_node_id,
            )
            return await self.write_binding(
                source_node_id, source_endpoint, target, target_endpoint
            )

        except (HomeAssistantError, OSError, ValueError) as err:
            self._logger.error("Failed to ensure binding: %s", err)
            return False

    async def remove_binding(
        self,
        source_node_id: int,
        source_endpoint: int,
        target: int | str,
        target_endpoint: int,
    ) -> bool:
        """Remove binding entry from a Matter device.

        Args:
            source_node_id: The node to remove binding from.
            source_endpoint: The source endpoint.
            target: Target node ID or group reference.
            target_endpoint: The target endpoint.

        Returns:
            True if removal succeeded, False otherwise.
        """
        try:
            self._get_matter_client()
        except RuntimeError:
            return False

        is_group = isinstance(target, str) and target.startswith("g")
        target_value = int(str(target)[1:]) if is_group else int(target)

        def filter_binding(binding_list: list[Any]) -> list[Any]:
            """Filter out entries matching target."""
            result = []
            for binding in binding_list:
                if isinstance(binding, dict):
                    if is_group:
                        if binding.get("2") != target_value:
                            result.append(binding)
                    elif binding.get("1") != target_value:
                        result.append(binding)
                else:
                    result.append(binding)
            return result

        try:
            binding_endpoint = self._get_binding_endpoint(
                source_node_id, source_endpoint
            )
            binding_path = f"{binding_endpoint}/30/0"

            success, _ = await self._read_modify_write(
                node_id=source_node_id,
                attribute_path=binding_path,
                modify_fn=filter_binding,
                verify=False,
                operation_name="binding remove",
            )
            if success:
                self._logger.info(
                    "Removed binding from node %d to %s", source_node_id, target
                )

        except (HomeAssistantError, OSError, ValueError) as err:
            self._logger.error("Failed to remove binding: %s", err)
            return False
        else:
            return success

    # =========================================================================
    # Read Operations
    # =========================================================================

    async def read_attribute(
        self,
        node_id: int,
        attribute_path: str,
    ) -> Any:
        """Read an attribute from a Matter device.

        Args:
            node_id: The node to read from.
            attribute_path: The attribute path (e.g., "1/30/0").

        Returns:
            The attribute value, or None if read failed.
        """
        try:
            matter_client = self._get_matter_client()
            result = await matter_client.read_attribute(node_id, attribute_path)
            return result.get(attribute_path)
        except (RuntimeError, HomeAssistantError, OSError, ValueError) as err:
            self._logger.error(
                "Failed to read attribute %s from node %d: %s",
                attribute_path,
                node_id,
                err,
            )
            return None

    async def write_attribute(
        self,
        node_id: int,
        attribute_path: str,
        value: Any,
    ) -> bool:
        """Write an attribute to a Matter device.

        Args:
            node_id: The node to write to.
            attribute_path: The attribute path.
            value: The value to write.

        Returns:
            True if write succeeded, False otherwise.
        """
        try:
            matter_client = self._get_matter_client()
            await matter_client.write_attribute(
                node_id=node_id,
                attribute_path=attribute_path,
                value=value,
            )
        except (RuntimeError, HomeAssistantError, OSError, ValueError) as err:
            self._logger.error(
                "Failed to write attribute %s to node %d: %s",
                attribute_path,
                node_id,
                err,
            )
            return False
        else:
            return True
