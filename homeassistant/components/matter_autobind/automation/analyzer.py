"""Automation analysis for Matter AutoBind.

This module provides the AutomationAnalyzer class which parses Home Assistant
automations to identify Matter-eligible automations and extract trigger/action
entity information.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.automation import (
    DATA_COMPONENT,
    devices_in_automation,
    entities_in_automation,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_ENTITY_ID, CONF_PLATFORM
from homeassistant.exceptions import HomeAssistantError

from ..store import EligibilityStatus
from .models import ActionInfo, AutomationAnalysis, EligibilityReason, TriggerInfo

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr, entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Domain constants
MATTER_DOMAIN = "matter"
AUTOBIND_DOMAIN = "matter_autobind"


class AutomationAnalyzer:
    """Analyzes Home Assistant automations for Matter binding eligibility.

    This class extracts trigger and action entities from automations,
    determines if they reference Matter devices, and checks eligibility
    for Matter binding creation.

    Example:
        analyzer = AutomationAnalyzer(hass, entity_registry, device_registry)
        analysis = await analyzer.analyze("automation.my_automation")

        if analysis.eligible:
            print(f"Triggers: {analysis.trigger_entities}")
            print(f"Actions: {analysis.action_entities}")
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        device_registry: dr.DeviceRegistry,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the analyzer.

        Args:
            hass: Home Assistant instance.
            entity_registry: Entity registry for lookups.
            device_registry: Device registry for lookups.
            logger: Optional logger for debug output.
        """
        self._hass = hass
        self._entity_registry = entity_registry
        self._device_registry = device_registry
        self._logger = logger or _LOGGER

    async def analyze(self, automation_id: str) -> AutomationAnalysis:
        """Analyze an automation for Matter binding eligibility.

        This is the main entry point. It extracts all trigger and action
        information and determines eligibility.

        Args:
            automation_id: The entity_id of the automation to analyze.

        Returns:
            AutomationAnalysis with all extracted information and eligibility status.
        """
        # Get the automation entity
        state = self._hass.states.get(automation_id)
        if not state:
            return AutomationAnalysis(
                automation_id=automation_id,
                eligible=False,
                reason=EligibilityReason.NO_TRIGGERS,
                reason_detail=f"Automation {automation_id} not found",
            )

        # Access the automation entity directly
        automation_entity = self._get_automation_entity(automation_id)
        if automation_entity is None:
            return AutomationAnalysis(
                automation_id=automation_id,
                eligible=False,
                reason=EligibilityReason.NOT_CHECKED,
                reason_detail=f"Could not access automation entity {automation_id}",
            )

        # Extract triggers and actions
        triggers = await self._extract_triggers(automation_id, automation_entity)
        actions = await self._extract_actions(automation_id, automation_entity)

        # Check for conditions (makes automation ineligible)
        has_conditions = self._has_conditions(automation_entity)

        # Build the analysis result
        analysis = AutomationAnalysis(
            automation_id=automation_id,
            eligible=False,  # Will be set below
            reason=EligibilityReason.NOT_CHECKED,
            triggers=triggers,
            actions=actions,
            has_conditions=has_conditions,
        )

        # Determine eligibility
        self._determine_eligibility(analysis)

        return analysis

    def _get_automation_entity(self, automation_id: str) -> Any | None:
        """Get the automation entity object.

        Args:
            automation_id: The entity_id of the automation.

        Returns:
            The automation entity, or None if not found.
        """
        if DATA_COMPONENT not in self._hass.data:
            return None

        return self._hass.data[DATA_COMPONENT].get_entity(automation_id)

    async def _extract_triggers(
        self, automation_id: str, automation_entity: Any
    ) -> list[TriggerInfo]:
        """Extract trigger information from an automation.

        Args:
            automation_id: The entity_id of the automation.
            automation_entity: The automation entity object.

        Returns:
            List of TriggerInfo objects.
        """
        triggers: list[TriggerInfo] = []

        # Get trigger entities/devices from trigger config
        trigger_entity_ids: list[str] = []
        trigger_device_ids: list[str] = []

        # Try to access trigger_config using getattr to avoid linter warnings
        # about private attribute access. This is wrapped in try/except for
        # future compatibility if HA changes internal structure.
        try:
            trigger_config = getattr(automation_entity, "_trigger_config", None)
            if trigger_config is not None:
                for trigger_conf in trigger_config:
                    platform = trigger_conf.get(CONF_PLATFORM, "unknown")

                    if platform in ("state", "numeric_state"):
                        entity_ids = trigger_conf.get(CONF_ENTITY_ID, [])
                        if isinstance(entity_ids, str):
                            entity_ids = [entity_ids]
                        # Build trigger info for each entity
                        triggers.extend(
                            self._build_trigger_info(eid, platform)
                            for eid in entity_ids
                        )
                        trigger_entity_ids.extend(entity_ids)

                    elif platform == "device":
                        device_id = trigger_conf.get(CONF_DEVICE_ID)
                        if device_id:
                            if isinstance(device_id, list):
                                trigger_device_ids.extend(device_id)
                            else:
                                trigger_device_ids.append(device_id)

                        # Also check for entity_id in device triggers
                        entity_id = trigger_conf.get(CONF_ENTITY_ID)
                        if entity_id:
                            entity_list = (
                                entity_id
                                if isinstance(entity_id, list)
                                else [entity_id]
                            )
                            triggers.extend(
                                self._build_trigger_info(eid, platform)
                                for eid in entity_list
                            )

        except (AttributeError, TypeError):
            self._logger.debug(
                "Automation %s does not have trigger_config (may be unavailable)",
                automation_id,
            )

        # Convert device IDs to entities - collect existing entity_ids first
        existing_entity_ids = {t.entity_id for t in triggers}
        for device_id in trigger_device_ids:
            device_entities = self._get_matter_entities_for_device(device_id)
            triggers.extend(
                self._build_trigger_info(entity_id, "device")
                for entity_id in device_entities
                if entity_id not in existing_entity_ids
            )
            existing_entity_ids.update(device_entities)

        return triggers

    async def _extract_actions(
        self, automation_id: str, automation_entity: Any
    ) -> list[ActionInfo]:
        """Extract action information from an automation.

        Args:
            automation_id: The entity_id of the automation.
            automation_entity: The automation entity object.

        Returns:
            List of ActionInfo objects.
        """
        actions: list[ActionInfo] = []

        # Get action entities from action_script
        try:
            action_script = automation_entity.action_script
            action_entity_ids = list(action_script.referenced_entities)
            action_device_ids = list(action_script.referenced_devices)
        except AttributeError:
            self._logger.debug(
                "Automation %s does not have action_script",
                automation_id,
            )
            return actions

        # Build ActionInfo for each entity using list comprehension
        actions = [
            self._build_action_info(entity_id) for entity_id in action_entity_ids
        ]

        # Convert device IDs to entities
        existing_entity_ids = {a.entity_id for a in actions}
        for device_id in action_device_ids:
            device_entities = self._get_matter_entities_for_device(device_id)
            actions.extend(
                self._build_action_info(entity_id)
                for entity_id in device_entities
                if entity_id not in existing_entity_ids
            )
            existing_entity_ids.update(device_entities)

        return actions

    def _build_trigger_info(self, entity_id: str, trigger_type: str) -> TriggerInfo:
        """Build a TriggerInfo object for an entity.

        Args:
            entity_id: The entity_id.
            trigger_type: The trigger platform type.

        Returns:
            TriggerInfo with Matter information filled in.
        """
        is_matter = self._is_matter_entity(entity_id)
        node_id = None
        endpoint_id = None

        # TODO: Extract node_id and endpoint_id from entity registry
        # This requires looking up the device and parsing the unique_id

        return TriggerInfo(
            entity_id=entity_id,
            trigger_type=trigger_type,
            node_id=node_id,
            endpoint_id=endpoint_id,
            is_matter_entity=is_matter,
        )

    def _build_action_info(self, entity_id: str, service: str = "") -> ActionInfo:
        """Build an ActionInfo object for an entity.

        Args:
            entity_id: The entity_id.
            service: The service call (if known).

        Returns:
            ActionInfo with Matter information filled in.
        """
        is_matter = self._is_matter_entity(entity_id)
        node_id = None
        endpoint_id = None

        return ActionInfo(
            entity_id=entity_id,
            service=service,
            node_id=node_id,
            endpoint_id=endpoint_id,
            is_matter_entity=is_matter,
        )

    def _has_conditions(self, automation_entity: Any) -> bool:
        """Check if the automation has conditions.

        Args:
            automation_entity: The automation entity object.

        Returns:
            True if the automation has conditions.
        """
        # Check if there are conditions defined using getattr
        # to avoid linter warnings about private attribute access
        cond_func = getattr(automation_entity, "_cond_func", None)
        return cond_func is not None

    def _determine_eligibility(self, analysis: AutomationAnalysis) -> None:
        """Determine the eligibility of an automation analysis.

        Modifies the analysis object in place to set eligible, reason, and reason_detail.

        Args:
            analysis: The AutomationAnalysis to evaluate.
        """
        # Check for conditions
        if analysis.has_conditions:
            analysis.eligible = False
            analysis.reason = EligibilityReason.HAS_CONDITIONS
            analysis.reason_detail = "Automation has conditions (not supported)"
            return

        # Get Matter triggers and actions
        matter_triggers = [t for t in analysis.triggers if t.is_matter_entity]
        matter_actions = [a for a in analysis.actions if a.is_matter_entity]

        # Check for Matter triggers
        if not matter_triggers:
            if not matter_actions:
                analysis.eligible = False
                analysis.reason = EligibilityReason.NO_MATTER_TRIGGERS
                analysis.reason_detail = "No Matter devices found in automation"
            else:
                analysis.eligible = False
                analysis.reason = EligibilityReason.NO_MATTER_TRIGGERS
                analysis.reason_detail = (
                    "No Matter devices found in automation triggers"
                )
            return

        # Check for Matter actions
        if not matter_actions:
            analysis.eligible = False
            analysis.reason = EligibilityReason.NO_MATTER_ACTIONS
            analysis.reason_detail = "No Matter devices found in automation actions"
            return

        # All basic checks passed - eligible
        # Note: Binding/client cluster checks are done separately by the manager
        analysis.eligible = True
        analysis.reason = EligibilityReason.ELIGIBLE
        analysis.reason_detail = "Automation is eligible for Matter binding"

    def _is_matter_entity(self, entity_id: str) -> bool:
        """Check if an entity belongs to the Matter or Matter AutoBind integration.

        Args:
            entity_id: The entity_id to check.

        Returns:
            True if the entity is from the Matter or Matter AutoBind integration.
        """
        entity_entry = self._entity_registry.async_get(entity_id)
        if entity_entry is None:
            return False

        return entity_entry.platform in (MATTER_DOMAIN, AUTOBIND_DOMAIN)

    def _get_matter_entities_for_device(self, device_id: str) -> list[str]:
        """Get all Matter entities for a device.

        Args:
            device_id: The device_id to look up.

        Returns:
            List of Matter entity_ids for the device.
        """
        return [
            entry.entity_id
            for entry in self._entity_registry.entities.values()
            if entry.device_id == device_id
            and entry.platform in (MATTER_DOMAIN, AUTOBIND_DOMAIN)
        ]

    async def get_referenced_entities(self, automation_id: str) -> list[str]:
        """Get all entities referenced by an automation.

        This uses the automation component's built-in tracking.

        Args:
            automation_id: The entity_id of the automation.

        Returns:
            List of entity_ids referenced by the automation.
        """
        try:
            return entities_in_automation(self._hass, automation_id)
        except HomeAssistantError as err:
            self._logger.warning(
                "Failed to get entities for automation %s: %s",
                automation_id,
                err,
            )
            return []

    async def get_referenced_devices(self, automation_id: str) -> list[str]:
        """Get all devices referenced by an automation.

        This uses the automation component's built-in tracking.

        Args:
            automation_id: The entity_id of the automation.

        Returns:
            List of device_ids referenced by the automation.
        """
        try:
            return devices_in_automation(self._hass, automation_id)
        except HomeAssistantError as err:
            self._logger.warning(
                "Failed to get devices for automation %s: %s",
                automation_id,
                err,
            )
            return []


def to_legacy_eligibility_result(
    analysis: AutomationAnalysis,
) -> tuple[bool, EligibilityStatus, list[str], list[str], str]:
    """Convert AutomationAnalysis to legacy tuple format.

    This provides backwards compatibility with the existing manager.py
    check_automation_eligibility return format.

    Args:
        analysis: The AutomationAnalysis to convert.

    Returns:
        Tuple of (is_eligible, status, trigger_entities, action_entities, reason).
    """
    # Map EligibilityReason to EligibilityStatus
    reason_to_status = {
        EligibilityReason.ELIGIBLE: EligibilityStatus.ELIGIBLE,
        EligibilityReason.NO_TRIGGERS: EligibilityStatus.NOT_CHECKED,
        EligibilityReason.NO_MATTER_TRIGGERS: EligibilityStatus.INELIGIBLE_NO_MATTER_TRIGGER,
        EligibilityReason.NO_MATTER_ACTIONS: EligibilityStatus.INELIGIBLE_NO_MATTER_ACTION,
        EligibilityReason.HAS_CONDITIONS: EligibilityStatus.INELIGIBLE_HAS_CONDITIONS,
        EligibilityReason.NO_CLIENT_CLUSTER: EligibilityStatus.INELIGIBLE_NO_CLIENT_CLUSTER,
        EligibilityReason.NO_BINDING_CLUSTER: EligibilityStatus.INELIGIBLE_NO_BINDING_CLUSTER,
        EligibilityReason.NOT_CHECKED: EligibilityStatus.NOT_CHECKED,
    }

    status = reason_to_status.get(analysis.reason, EligibilityStatus.NOT_CHECKED)

    return (
        analysis.eligible,
        status,
        analysis.trigger_entities,
        analysis.action_entities,
        analysis.reason_detail or str(analysis.reason.value),
    )
