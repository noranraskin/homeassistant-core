"""Test the AutomationAnalyzer class."""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from homeassistant.components.matter_autobind.automation.analyzer import (
    AutomationAnalyzer,
    EligibilityReason,
)
from homeassistant.const import CONF_ENTITY_ID, CONF_PLATFORM
from homeassistant.core import HomeAssistant


@pytest.fixture
def mock_registries():
    """Mock entity and device registries."""
    return MagicMock(), MagicMock()


@pytest.fixture
def analyzer(hass: HomeAssistant, mock_registries):
    """Create an analyzer instance."""
    entity_reg, device_reg = mock_registries
    return AutomationAnalyzer(hass, entity_reg, device_reg)


async def test_analyze_non_existent_automation(analyzer: AutomationAnalyzer) -> None:
    """Test analyzing a non-existent automation."""
    result = await analyzer.analyze("automation.non_existent")

    assert not result.eligible
    assert result.reason == EligibilityReason.NO_TRIGGERS
    assert "not found" in result.reason_detail


async def test_analyze_automation_with_conditions(
    hass: HomeAssistant, analyzer: AutomationAnalyzer
) -> None:
    """Test analyzing an automation with conditions."""
    automation_id = "automation.test"

    # Mock automation entity
    mock_automation = MagicMock()
    # has_conditions checks for _cond_func being present with non-empty config
    mock_cond_func = MagicMock()
    mock_cond_func.config = [{"condition": "state"}]  # Non-empty config
    mock_automation._cond_func = mock_cond_func

    with patch(
        "homeassistant.components.matter_autobind.automation.analyzer.DATA_COMPONENT",
        "automation",
    ):
        hass.data["automation"] = MagicMock()
        hass.data["automation"].get_entity.return_value = mock_automation

        # Mock state to exist
        hass.states.async_set(automation_id, "on")

        result = await analyzer.analyze(automation_id)

    assert not result.eligible
    assert result.reason == EligibilityReason.HAS_CONDITIONS
    assert result.has_conditions


async def test_analyze_automation_with_empty_conditions(
    hass: HomeAssistant, analyzer: AutomationAnalyzer
) -> None:
    """Test analyzing an automation with empty conditions list (UI default)."""
    automation_id = "automation.test"
    trigger_entity = "switch.source"
    action_entity = "light.target"

    # Mock automation entity
    mock_automation = MagicMock()
    # When UI creates automation with no conditions, _cond_func exists but config is empty
    mock_cond_func = MagicMock()
    mock_cond_func.config = []  # Empty config - effectively no conditions
    mock_automation._cond_func = mock_cond_func

    # Mock trigger config
    mock_automation._trigger_config = [
        {CONF_PLATFORM: "state", CONF_ENTITY_ID: trigger_entity}
    ]

    # Mock action script
    mock_script = MagicMock()
    mock_script.referenced_entities = {action_entity}
    mock_script.referenced_devices = set()
    type(mock_automation).action_script = PropertyMock(return_value=mock_script)

    with (
        patch(
            "homeassistant.components.matter_autobind.automation.analyzer.DATA_COMPONENT",
            "automation",
        ),
        patch(
            "homeassistant.components.matter_autobind.automation.analyzer.is_matter_entity",
            return_value=True,
        ),
    ):
        hass.data["automation"] = MagicMock()
        hass.data["automation"].get_entity.return_value = mock_automation
        hass.states.async_set(automation_id, "on")

        result = await analyzer.analyze(automation_id)

    # Empty conditions should NOT make automation ineligible
    assert not result.has_conditions
    assert result.eligible
    assert result.reason == EligibilityReason.ELIGIBLE


async def test_analyze_eligible_automation(
    hass: HomeAssistant, analyzer: AutomationAnalyzer
) -> None:
    """Test analyzing an eligible automation."""
    automation_id = "automation.test"
    trigger_entity = "switch.source"
    action_entity = "light.target"

    # Mock automation entity logic
    mock_automation = MagicMock()
    del mock_automation._cond_func  # No conditions

    # Mock trigger config
    mock_automation._trigger_config = [
        {CONF_PLATFORM: "state", CONF_ENTITY_ID: trigger_entity}
    ]

    # Mock action script
    mock_script = MagicMock()
    mock_script.referenced_entities = {action_entity}
    mock_script.referenced_devices = set()
    type(mock_automation).action_script = PropertyMock(return_value=mock_script)

    with (
        patch(
            "homeassistant.components.matter_autobind.automation.analyzer.DATA_COMPONENT",
            "automation",
        ),
        patch(
            "homeassistant.components.matter_autobind.automation.analyzer.is_matter_entity",
            return_value=True,
        ),
    ):
        hass.data["automation"] = MagicMock()
        hass.data["automation"].get_entity.return_value = mock_automation
        hass.states.async_set(automation_id, "on")

        result = await analyzer.analyze(automation_id)

    assert result.eligible
    assert result.reason == EligibilityReason.ELIGIBLE
    assert len(result.triggers) == 1
    assert result.triggers[0].entity_id == trigger_entity
    assert len(result.actions) == 1
    assert result.actions[0].entity_id == action_entity
