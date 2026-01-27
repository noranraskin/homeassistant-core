"""WebSocket API for Matter AutoBind panel."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ENABLE_DEBUG_PANEL,
    CONF_ENABLE_GROUP_BINDINGS,
    DOMAIN,
    LOGGER,
    WS_TYPE_DELETE_ACL_ENTRY,
    WS_TYPE_DELETE_BINDING_ENTRY,
    WS_TYPE_DELETE_GROUP_ENTRY,
    WS_TYPE_DELETE_GROUP_KEY_MAP_ENTRY,
    WS_TYPE_DELETE_GROUP_KEY_SET,
    WS_TYPE_DELETE_RESOURCE,
    WS_TYPE_FORCE_RECONCILE,
    WS_TYPE_GET_AUTOMATION_DETAIL,
    WS_TYPE_GET_DASHBOARD_DATA,
    WS_TYPE_GET_DEBUG_CONFIG,
    WS_TYPE_GET_DEVICE_RAW_DATA,
    WS_TYPE_GET_MATTER_DEVICES,
    WS_TYPE_GET_NODE_RAW_DATA,
    WS_TYPE_SET_BINDING_PREFERENCE,
)

if TYPE_CHECKING:
    from . import MatterAutoBindData


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register all WebSocket commands."""
    websocket_api.async_register_command(hass, ws_get_dashboard_data)
    websocket_api.async_register_command(hass, ws_get_automation_detail)
    websocket_api.async_register_command(hass, ws_set_binding_preference)
    websocket_api.async_register_command(hass, ws_get_node_raw_data)
    websocket_api.async_register_command(hass, ws_delete_resource)
    websocket_api.async_register_command(hass, ws_force_reconcile)
    # Debug panel commands
    websocket_api.async_register_command(hass, ws_get_debug_config)
    websocket_api.async_register_command(hass, ws_get_matter_devices)
    websocket_api.async_register_command(hass, ws_get_device_raw_data)
    websocket_api.async_register_command(hass, ws_delete_acl_entry)
    websocket_api.async_register_command(hass, ws_delete_binding_entry)
    websocket_api.async_register_command(hass, ws_delete_group_entry)
    websocket_api.async_register_command(hass, ws_delete_group_key_map_entry)
    websocket_api.async_register_command(hass, ws_delete_group_key_set)


def _get_runtime_data(hass: HomeAssistant) -> MatterAutoBindData | None:
    """Get the runtime data for the integration."""
    if DOMAIN not in hass.data:
        return None
    # Get the first (and typically only) config entry's data
    for entry_data in hass.data[DOMAIN].values():
        return entry_data
    return None


# =============================================================================
# Dashboard Data
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_GET_DASHBOARD_DATA,
    }
)
@websocket_api.async_response
async def ws_get_dashboard_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle get_dashboard_data command.

    Returns a list of all automations with their binding status.
    """
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        data = await runtime_data.manager.get_dashboard_data()
        connection.send_result(msg["id"], {"automations": data})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error getting dashboard data")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Automation Detail
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_GET_AUTOMATION_DETAIL,
        vol.Required("automation_id"): cv.string,
    }
)
@websocket_api.async_response
async def ws_get_automation_detail(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle get_automation_detail command.

    Returns detailed information about a specific automation including
    all source and target devices with their node information.
    """
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        detail = await runtime_data.manager.get_automation_detail(msg["automation_id"])
        connection.send_result(msg["id"], detail)
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error getting automation detail")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Binding Preference
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_SET_BINDING_PREFERENCE,
        vol.Required("automation_id"): cv.string,
        vol.Required("preference"): vol.In(["group", "unicast", "auto", "none"]),
    }
)
@websocket_api.async_response
async def ws_set_binding_preference(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle set_binding_preference command.

    Sets the binding strategy preference for an automation and triggers reconciliation.
    """
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    # Check if groups are enabled (required for "group" preference)
    config_entries = hass.config_entries.async_entries(DOMAIN)
    if config_entries:
        groups_enabled = config_entries[0].options.get(
            CONF_ENABLE_GROUP_BINDINGS, False
        )
    else:
        groups_enabled = False

    if msg["preference"] == "group" and not groups_enabled:
        connection.send_error(
            msg["id"],
            "groups_disabled",
            "Group bindings are not enabled in integration settings",
        )
        return

    try:
        await runtime_data.manager.set_binding_preference(
            msg["automation_id"],
            msg["preference"],
        )
        connection.send_result(msg["id"], {"success": True})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error setting binding preference")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Debug: Node Raw Data
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_GET_NODE_RAW_DATA,
        vol.Required("node_id"): vol.Coerce(int),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get_node_raw_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle get_node_raw_data command.

    Returns raw ACL, Binding, Groups, and GroupKeyMap data from a device.
    Only available to admin users.
    """
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    # Check if debug panel is enabled
    config_entries = hass.config_entries.async_entries(DOMAIN)
    if config_entries:
        debug_enabled = config_entries[0].options.get(CONF_ENABLE_DEBUG_PANEL, False)
    else:
        debug_enabled = False

    if not debug_enabled:
        connection.send_error(
            msg["id"],
            "debug_disabled",
            "Debug panel is not enabled in integration settings",
        )
        return

    try:
        data = await runtime_data.manager.get_node_raw_data(msg["node_id"])
        connection.send_result(msg["id"], data)
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error getting node raw data")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Debug: Delete Resource
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_DELETE_RESOURCE,
        vol.Required("node_id"): vol.Coerce(int),
        vol.Required("resource_type"): vol.In(
            ["acl", "binding", "group", "group_key_map"]
        ),
        vol.Required("resource_data"): dict,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_resource(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle delete_resource command.

    Deletes a specific ACL, binding, or group entry from a device.
    Only available to admin users with debug panel enabled.
    """
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    # Check if debug panel is enabled
    config_entries = hass.config_entries.async_entries(DOMAIN)
    if config_entries:
        debug_enabled = config_entries[0].options.get(CONF_ENABLE_DEBUG_PANEL, False)
    else:
        debug_enabled = False

    if not debug_enabled:
        connection.send_error(
            msg["id"],
            "debug_disabled",
            "Debug panel is not enabled in integration settings",
        )
        return

    try:
        success = await runtime_data.manager.delete_resource(
            msg["node_id"],
            msg["resource_type"],
            msg["resource_data"],
        )
        connection.send_result(msg["id"], {"success": success})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error deleting resource")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Force Reconcile
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_FORCE_RECONCILE,
        vol.Optional("automation_id"): cv.string,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_force_reconcile(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle force_reconcile command.

    Forces re-analysis and reconciliation of an automation or all automations.
    """
    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        automation_id = msg.get("automation_id")
        if automation_id:
            await runtime_data.manager.force_reconcile_automation(automation_id)
        else:
            await runtime_data.manager.force_reconcile_all()
        connection.send_result(msg["id"], {"success": True})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error forcing reconcile")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Debug Panel: Configuration
# =============================================================================


def _is_debug_enabled(hass: HomeAssistant) -> bool:
    """Check if debug panel is enabled in config."""
    config_entries = hass.config_entries.async_entries(DOMAIN)
    if config_entries:
        return config_entries[0].options.get(CONF_ENABLE_DEBUG_PANEL, False)
    return False


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_GET_DEBUG_CONFIG,
    }
)
@websocket_api.async_response
async def ws_get_debug_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Get debug panel configuration.

    Returns whether debug features are enabled.
    """
    debug_enabled = _is_debug_enabled(hass)
    connection.send_result(
        msg["id"],
        {
            "debug_enabled": debug_enabled,
        },
    )


# =============================================================================
# Debug Panel: List Matter Devices
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_GET_MATTER_DEVICES,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get_matter_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Get list of all Matter devices.

    Returns device info including node ID for each Matter device.
    Only available to admin users with debug panel enabled.
    """
    if not _is_debug_enabled(hass):
        connection.send_error(
            msg["id"],
            "debug_disabled",
            "Debug panel is not enabled in integration settings",
        )
        return

    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        devices = await runtime_data.manager.get_matter_devices()
        connection.send_result(msg["id"], {"devices": devices})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error getting Matter devices")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Debug Panel: Get Device Raw Data
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_GET_DEVICE_RAW_DATA,
        vol.Required("node_id"): vol.Coerce(int),
        vol.Optional("force_refresh", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get_device_raw_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Get comprehensive raw data from a Matter device.

    Returns ALL ACLs, bindings, groups, group keys, and group key maps.
    Only available to admin users with debug panel enabled.

    Args:
        force_refresh: If True, fetch live data from the device instead of
            using cached data. Defaults to False for performance.

    """
    if not _is_debug_enabled(hass):
        connection.send_error(
            msg["id"],
            "debug_disabled",
            "Debug panel is not enabled in integration settings",
        )
        return

    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        data = await runtime_data.manager.get_device_raw_data(
            msg["node_id"],
            force_refresh=msg.get("force_refresh", False),
        )
        connection.send_result(msg["id"], data)
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error getting device raw data")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Debug Panel: Delete ACL Entry
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_DELETE_ACL_ENTRY,
        vol.Required("node_id"): vol.Coerce(int),
        vol.Required("acl_index"): vol.Coerce(int),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_acl_entry(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a specific ACL entry by index.

    Only available to admin users with debug panel enabled.
    """
    if not _is_debug_enabled(hass):
        connection.send_error(
            msg["id"],
            "debug_disabled",
            "Debug panel is not enabled in integration settings",
        )
        return

    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        success = await runtime_data.manager.delete_acl_entry(
            msg["node_id"], msg["acl_index"]
        )
        connection.send_result(msg["id"], {"success": success})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error deleting ACL entry")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Debug Panel: Delete Binding Entry
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_DELETE_BINDING_ENTRY,
        vol.Required("node_id"): vol.Coerce(int),
        vol.Required("endpoint"): vol.Coerce(int),
        vol.Required("binding_index"): vol.Coerce(int),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_binding_entry(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a specific binding entry by index.

    Only available to admin users with debug panel enabled.
    """
    if not _is_debug_enabled(hass):
        connection.send_error(
            msg["id"],
            "debug_disabled",
            "Debug panel is not enabled in integration settings",
        )
        return

    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        success = await runtime_data.manager.delete_binding_entry(
            msg["node_id"], msg["endpoint"], msg["binding_index"]
        )
        connection.send_result(msg["id"], {"success": success})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error deleting binding entry")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Debug Panel: Delete Group Entry
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_DELETE_GROUP_ENTRY,
        vol.Required("node_id"): vol.Coerce(int),
        vol.Required("endpoint"): vol.Coerce(int),
        vol.Required("group_id"): vol.Coerce(int),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_group_entry(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a group membership entry.

    Only available to admin users with debug panel enabled.
    """
    if not _is_debug_enabled(hass):
        connection.send_error(
            msg["id"],
            "debug_disabled",
            "Debug panel is not enabled in integration settings",
        )
        return

    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        success = await runtime_data.manager.delete_group_entry(
            msg["node_id"], msg["endpoint"], msg["group_id"]
        )
        connection.send_result(msg["id"], {"success": success})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error deleting group entry")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Debug Panel: Delete GroupKeyMap Entry
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_DELETE_GROUP_KEY_MAP_ENTRY,
        vol.Required("node_id"): vol.Coerce(int),
        vol.Required("entry_index"): vol.Coerce(int),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_group_key_map_entry(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a GroupKeyMap entry by index.

    Only available to admin users with debug panel enabled.
    """
    if not _is_debug_enabled(hass):
        connection.send_error(
            msg["id"],
            "debug_disabled",
            "Debug panel is not enabled in integration settings",
        )
        return

    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        success = await runtime_data.manager.delete_group_key_map_entry(
            msg["node_id"], msg["entry_index"]
        )
        connection.send_result(msg["id"], {"success": success})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error deleting GroupKeyMap entry")
        connection.send_error(msg["id"], "error", str(err))


# =============================================================================
# Debug Panel: Delete GroupKeySet
# =============================================================================


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_DELETE_GROUP_KEY_SET,
        vol.Required("node_id"): vol.Coerce(int),
        vol.Required("key_set_id"): vol.Coerce(int),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_group_key_set(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a GroupKeySet by its KeySetId.

    Uses the KeySetRemove command to remove the key set from the device.
    Only available to admin users with debug panel enabled.
    """
    if not _is_debug_enabled(hass):
        connection.send_error(
            msg["id"],
            "debug_disabled",
            "Debug panel is not enabled in integration settings",
        )
        return

    runtime_data = _get_runtime_data(hass)
    if runtime_data is None:
        connection.send_error(
            msg["id"],
            "not_loaded",
            "Matter AutoBind integration not loaded",
        )
        return

    try:
        success = await runtime_data.manager.delete_group_key_set(
            msg["node_id"], msg["key_set_id"]
        )
        connection.send_result(msg["id"], {"success": success})
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Error deleting GroupKeySet")
        connection.send_error(msg["id"], "error", str(err))
