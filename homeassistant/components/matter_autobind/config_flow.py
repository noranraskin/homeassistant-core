"""Config flow for the Matter AutoBind integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_ENABLE_DEBUG_PANEL, CONF_ENABLE_GROUP_BINDINGS, DOMAIN


class MatterAutoBindConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Matter AutoBind.

    This integration requires no user input - it automatically discovers
    Matter devices with client clusters and creates entities for them.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step.

        The integration installs automatically without any user configuration.
        Only one instance of this integration is allowed.
        """
        # Check if already configured
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Create entry immediately without asking for anything
        return self.async_create_entry(
            title="Matter AutoBind",
            data={},
            options={
                CONF_ENABLE_GROUP_BINDINGS: False,
                CONF_ENABLE_DEBUG_PANEL: False,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return MatterAutoBindOptionsFlow()


class MatterAutoBindOptionsFlow(OptionsFlow):
    """Handle Matter AutoBind options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENABLE_GROUP_BINDINGS,
                        default=self.config_entry.options.get(
                            CONF_ENABLE_GROUP_BINDINGS, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_ENABLE_DEBUG_PANEL,
                        default=self.config_entry.options.get(
                            CONF_ENABLE_DEBUG_PANEL, False
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )
