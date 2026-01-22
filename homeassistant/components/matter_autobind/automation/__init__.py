"""Automation analysis module for Matter AutoBind.

This module contains logic for parsing Home Assistant automations to identify
Matter-eligible automations and extract trigger/action entities.

Submodules:
- analyzer: Parses HA automations to find Matter entities
- filter: Logic for detecting physical state changes
- models: Automation eligibility dataclasses
"""

from .models import ActionInfo, AutomationAnalysis, TriggerInfo

__all__ = [
    "ActionInfo",
    "AutomationAnalysis",
    "TriggerInfo",
]
