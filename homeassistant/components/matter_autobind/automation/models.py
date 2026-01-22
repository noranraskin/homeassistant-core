"""Automation eligibility dataclasses for Matter AutoBind.

This module contains data structures for representing automation analysis
results and eligibility status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EligibilityReason(StrEnum):
    """Detailed reasons for automation eligibility status."""

    # Eligible reasons
    ELIGIBLE = "eligible"

    # Ineligible: Trigger issues
    NO_TRIGGERS = "no_triggers_found"
    NO_MATTER_TRIGGERS = "no_matter_triggers"
    NO_STATE_TRIGGERS = "no_state_triggers"
    COMPLEX_TRIGGER = "complex_trigger_logic"

    # Ineligible: Action issues
    NO_ACTIONS = "no_actions_found"
    NO_MATTER_ACTIONS = "no_matter_actions"
    UNSUPPORTED_SERVICE = "unsupported_service_call"
    COMPLEX_ACTION = "complex_action_logic"

    # Ineligible: Structure issues
    HAS_CONDITIONS = "has_conditions"
    MIXED_DOMAINS = "mixed_matter_non_matter"

    # Ineligible: Cluster issues
    NO_CLIENT_CLUSTER = "trigger_no_client_cluster"
    NO_BINDING_CLUSTER = "trigger_no_binding_cluster"
    NO_SERVER_CLUSTER = "action_no_server_cluster"

    # Not yet checked
    NOT_CHECKED = "not_checked"


@dataclass
class TriggerInfo:
    """Information about an automation trigger.

    Captures details about a trigger entity including its Matter
    node/endpoint information if available.
    """

    entity_id: str
    """The entity_id used in the trigger."""

    trigger_type: str
    """The trigger platform type (e.g., 'state', 'device')."""

    node_id: int | None = None
    """The Matter node ID if this is a Matter entity."""

    endpoint_id: int | None = None
    """The Matter endpoint ID if this is a Matter entity."""

    is_matter_entity: bool = False
    """True if this entity belongs to Matter or Matter AutoBind integration."""

    has_client_cluster: bool = False
    """True if the device has client clusters for binding."""

    has_binding_cluster: bool = False
    """True if the device has the Binding cluster (0x001E)."""

    client_cluster_ids: list[int] = field(default_factory=list)
    """List of client cluster IDs available on this device."""


@dataclass
class ActionInfo:
    """Information about an automation action.

    Captures details about an action target including the service call
    and Matter node/endpoint information if available.
    """

    entity_id: str
    """The entity_id targeted by the action."""

    service: str
    """The service call (e.g., 'light.turn_on', 'switch.toggle')."""

    node_id: int | None = None
    """The Matter node ID if this is a Matter entity."""

    endpoint_id: int | None = None
    """The Matter endpoint ID if this is a Matter entity."""

    is_matter_entity: bool = False
    """True if this entity belongs to Matter or Matter AutoBind integration."""

    has_server_cluster: bool = False
    """True if the device has server clusters matching the service."""

    required_cluster_ids: list[int] = field(default_factory=list)
    """List of cluster IDs required for this service call."""


@dataclass
class AutomationAnalysis:
    """Complete analysis result for an automation.

    This is the output of the AutomationAnalyzer and contains all
    information needed to determine eligibility and create bindings.
    """

    automation_id: str
    """The entity_id of the automation."""

    eligible: bool
    """True if this automation is eligible for Matter binding."""

    reason: EligibilityReason
    """The reason for the eligibility status."""

    reason_detail: str | None = None
    """Optional human-readable detail about the reason."""

    triggers: list[TriggerInfo] = field(default_factory=list)
    """Information about all triggers in the automation."""

    actions: list[ActionInfo] = field(default_factory=list)
    """Information about all actions in the automation."""

    has_conditions: bool = False
    """True if the automation has conditions (makes it ineligible)."""

    @property
    def trigger_entities(self) -> list[str]:
        """Return list of Matter trigger entity IDs."""
        return [t.entity_id for t in self.triggers if t.is_matter_entity]

    @property
    def action_entities(self) -> list[str]:
        """Return list of Matter action entity IDs."""
        return [a.entity_id for a in self.actions if a.is_matter_entity]

    @property
    def trigger_node_ids(self) -> list[int]:
        """Return list of Matter trigger node IDs."""
        return [t.node_id for t in self.triggers if t.node_id is not None]

    @property
    def action_node_ids(self) -> list[int]:
        """Return list of Matter action node IDs."""
        return [a.node_id for a in self.actions if a.node_id is not None]

    @property
    def all_required_cluster_ids(self) -> set[int]:
        """Return set of all cluster IDs required by actions."""
        clusters: set[int] = set()
        for action in self.actions:
            clusters.update(action.required_cluster_ids)
        return clusters
