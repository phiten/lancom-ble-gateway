"""Config flow für Lancom BLE Integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, CONF_WEBHOOK_ID, DEFAULT_WEBHOOK_ID, CONF_AP_MACS


class LancomBLEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            webhook_id = user_input.get(CONF_WEBHOOK_ID, DEFAULT_WEBHOOK_ID)
            ap_macs = user_input.get(CONF_AP_MACS, "")

            await self.async_set_unique_id(webhook_id)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Lancom BLE Gateway ({webhook_id})",
                data={CONF_WEBHOOK_ID: webhook_id, CONF_AP_MACS: ap_macs},
            )

        schema = vol.Schema({
            vol.Optional(CONF_WEBHOOK_ID, default=DEFAULT_WEBHOOK_ID): cv.string,
            vol.Optional(CONF_AP_MACS, default=""): cv.string,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return LancomBLEOptionsFlow(config_entry)


class LancomBLEOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema({
            vol.Optional(CONF_WEBHOOK_ID, default=self.config_entry.data.get(CONF_WEBHOOK_ID, DEFAULT_WEBHOOK_ID)): cv.string,
            vol.Optional(CONF_AP_MACS, default=self.config_entry.data.get(CONF_AP_MACS, "")): cv.string,
        })
        return self.async_show_form(step_id="init", data_schema=schema)