"""Physical state change detection for Matter AutoBind.

This module provides logic to determine if a state change originated from
a physical device interaction (button press) versus a UI/automation action.
This is critical for automation suppression - when a binding handles the
direct control, we don't want the automation to also fire.
"""

from __future__ import annotations

import logging

from homeassistant.core import Event, EventStateChangedData, callback

_LOGGER = logging.getLogger(__name__)


@callback
def is_physical_state_change(
    event: Event[EventStateChangedData],
    logger: logging.Logger | None = None,
) -> bool:
    """Determine if a state change originated from a physical device interaction.

    This is used to differentiate between:
    - UI/service call initiated changes -> automation should run normally
    - Physical device button presses -> automation should be suppressed
      (the binding handles the direct control)

    The detection uses the event context:
    - user_id set -> UI interaction (not physical)
    - parent_id set -> automation/script triggered (not physical)
    - Neither set -> likely physical device interaction

    Note: This heuristic may have edge cases with some integrations
    (e.g., Zigbee2MQTT) that might set parent_id even for physical events.
    Consider adding a configuration option to disable this detection if
    users experience false positives.

    Args:
        event: The state changed event to analyze.
        logger: Optional logger for debug output.

    Returns:
        True if the change was from physical device interaction (suppress automation).
        False if the change was from UI/HA service call (run automation normally).
    """
    log = logger or _LOGGER
    entity_id = event.data["entity_id"]
    context = event.context

    # If user_id is set, this was from the UI or a user-initiated service call
    if context.user_id is not None:
        log.debug(
            "State change for %s originated from UI (user_id: %s)",
            entity_id,
            context.user_id,
        )
        return False

    # If parent_id is set but no user_id, it's from an automation/script
    # which means it could be from our own automation triggering
    if context.parent_id is not None:
        log.debug(
            "State change for %s originated from automation/script (parent_id: %s)",
            entity_id,
            context.parent_id,
        )
        return False

    # If neither user_id nor parent_id is set, this is likely from:
    # - Device state update (physical button press)
    # - Integration pushing state (e.g., Matter server reporting device change)
    log.debug(
        "State change for %s originated from device (no context parent/user) - "
        "this is a physical interaction, automation will be suppressed",
        entity_id,
    )
    return True


def should_suppress_automation(
    event: Event[EventStateChangedData],
    physical_detection_enabled: bool = True,
    logger: logging.Logger | None = None,
) -> bool:
    """Check if an automation should be suppressed for this state change.

    This is a wrapper around is_physical_state_change that respects
    the user's configuration for disabling physical detection.

    Args:
        event: The state changed event to analyze.
        physical_detection_enabled: If False, always returns False
            (never suppress, useful for debugging).
        logger: Optional logger for debug output.

    Returns:
        True if the automation should be suppressed.
        False if the automation should run normally.
    """
    if not physical_detection_enabled:
        return False

    return is_physical_state_change(event, logger)
