"""Matter-specific dataclasses for the Matter AutoBind integration.

This module contains data structures specific to Matter protocol operations,
such as binding targets and ACL entries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class BindingTarget(ABC):
    """Base class for binding targets.

    A binding target can be either a specific node (unicast) or a group (multicast).
    """

    endpoint_id: int
    """The target endpoint ID."""

    cluster_ids: list[int]
    """List of cluster IDs this binding covers."""

    @abstractmethod
    def to_tlv_dict(self) -> dict[str, Any]:
        """Convert to TLV dictionary format for Matter protocol.

        Returns:
            Dictionary with Matter TLV structure for binding.
        """

    @property
    @abstractmethod
    def target_key(self) -> int | str:
        """Return the target identifier for store keys.

        Returns:
            Node ID (int) for unicast, or 'g{group_id}' (str) for group.
        """


@dataclass
class NodeTarget(BindingTarget):
    """A binding target pointing to a specific Matter node (unicast).

    Used for direct device-to-device bindings where the source device
    sends commands directly to the target device.
    """

    node_id: int
    """The target Matter node ID."""

    def to_tlv_dict(self) -> dict[str, Any]:
        """Convert to TLV dictionary format for Matter protocol.

        Returns:
            Dictionary with Matter TLV structure for node binding.
        """
        # Binding cluster Binding struct TLV format:
        # 1 = Node (node ID)
        # 2 = Group (not used for unicast)
        # 3 = Endpoint
        # 4 = Cluster (primary cluster)
        return {
            "1": self.node_id,
            "3": self.endpoint_id,
            "4": self.cluster_ids[0] if self.cluster_ids else 6,  # Default to OnOff
        }

    @property
    def target_key(self) -> int:
        """Return the node ID as the target key."""
        return self.node_id


@dataclass
class GroupTarget(BindingTarget):
    """A binding target pointing to a Matter group (multicast).

    Used when a source device needs to control multiple target devices
    simultaneously through group messaging.
    """

    group_id: int
    """The Matter group ID."""

    def to_tlv_dict(self) -> dict[str, Any]:
        """Convert to TLV dictionary format for Matter protocol.

        Returns:
            Dictionary with Matter TLV structure for group binding.
        """
        # Binding cluster Binding struct TLV format:
        # 1 = Node (not used for group)
        # 2 = Group (group ID)
        # 3 = Endpoint (target endpoint within group)
        # 4 = Cluster (primary cluster)
        return {
            "2": self.group_id,
            "3": self.endpoint_id,
            "4": self.cluster_ids[0] if self.cluster_ids else 6,  # Default to OnOff
        }

    @property
    def target_key(self) -> str:
        """Return the group reference as the target key."""
        return f"g{self.group_id}"


@dataclass
class AclEntry:
    """Represents an ACL (Access Control List) entry on a Matter device.

    ACLs grant privileges to subjects (nodes or groups) to access
    specific endpoints and clusters on the target device.
    """

    target_node_id: int
    """The node ID where this ACL is written."""

    subject: int
    """The subject granted access (node ID for CASE, group ID for GROUP)."""

    auth_mode: int
    """Authentication mode: 2=CASE (node-to-node), 3=GROUP (multicast)."""

    privilege: int
    """Privilege level: 3=Operate, 4=Manage, 5=Administer."""

    endpoint_id: int
    """Target endpoint (0 for node-wide access)."""

    cluster_id: int | None = None
    """Optional: specific cluster access. None for all clusters on endpoint."""

    @property
    def is_case_auth(self) -> bool:
        """Return True if this uses CASE (node-to-node) authentication."""
        return self.auth_mode == 2

    @property
    def is_group_auth(self) -> bool:
        """Return True if this uses GROUP (multicast) authentication."""
        return self.auth_mode == 3

    def to_tlv_dict(self) -> dict[str, Any]:
        """Convert to TLV dictionary format for Matter protocol.

        Returns:
            Dictionary with Matter TLV structure for ACL entry.
        """
        # ACL struct TLV format:
        # 1 = Privilege (3=Operate, 4=Manage, 5=Administer)
        # 2 = AuthMode (2=CASE, 3=Group)
        # 3 = Subjects (list of subject IDs)
        # 4 = Targets (list of target structs, or null for all)
        entry: dict[str, Any] = {
            "1": self.privilege,
            "2": self.auth_mode,
            "3": [self.subject],
        }

        # Add target constraints if not node-wide access
        if self.endpoint_id != 0 or self.cluster_id is not None:
            target: dict[str, Any] = {}
            if self.cluster_id is not None:
                target["0"] = self.cluster_id
            target["1"] = self.endpoint_id
            entry["4"] = [target]

        return entry


@dataclass
class GroupMember:
    """A member of a Matter group.

    Represents a (node_id, endpoint_id) pair that participates in a group.
    """

    node_id: int
    """The Matter node ID."""

    endpoint_id: int
    """The endpoint ID on the node."""

    def as_tuple(self) -> tuple[int, int]:
        """Return as a (node_id, endpoint_id) tuple."""
        return (self.node_id, self.endpoint_id)


@dataclass
class GroupInfo:
    """Information about a Matter group managed by this integration.

    Groups enable multicast messaging from source devices to multiple
    target devices simultaneously.
    """

    group_id: int
    """The Matter group ID (0x0001 - 0xFFFF)."""

    group_name: str
    """Human-readable name for the group."""

    members: list[GroupMember]
    """List of (node_id, endpoint_id) members in the group."""

    source_node_ids: list[int]
    """Node IDs of source devices that send to this group."""

    epoch_key: bytes | None = None
    """The epoch key for group encryption (16 bytes)."""

    key_set_index: int | None = None
    """The index in the GroupKeySetWrite."""

    def get_member_set(self) -> frozenset[tuple[int, int]]:
        """Return members as an immutable frozenset of tuples."""
        return frozenset(m.as_tuple() for m in self.members)
