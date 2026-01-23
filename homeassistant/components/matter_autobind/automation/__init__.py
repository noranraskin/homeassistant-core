"""Automation analysis module for Matter AutoBind.

This module contains logic for parsing Home Assistant automations to identify
Matter-eligible automations and extract trigger/action entities.

Submodules:
- analyzer: Parses HA automations to find Matter entities
- filter: Logic for detecting physical state changes
- models: Automation eligibility dataclasses
"""

from .analyzer import AutomationAnalyzer, to_legacy_eligibility_result
from .filter import is_physical_state_change, should_suppress_automation
from .models import ActionInfo, AutomationAnalysis, EligibilityReason, TriggerInfo

__all__ = [
    "ActionInfo",
    "AutomationAnalysis",
    "AutomationAnalyzer",
    "EligibilityReason",
    "TriggerInfo",
    "is_physical_state_change",
    "should_suppress_automation",
    "to_legacy_eligibility_result",
]
