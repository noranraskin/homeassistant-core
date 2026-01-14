"""Config flow for the Matter AutoBind integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


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
        )
