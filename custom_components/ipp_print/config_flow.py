"""Einrichtung: Drucker-Adresse eingeben und prüfen. In den Optionen steht die Webhook-Adresse."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_PRINTER_URL, CONF_WEBHOOK_ID, DOMAIN
from .ipp import IppError, IppPrinter, normalize_printer_url

_LOGGER = logging.getLogger(__name__)

CONF_REGENERATE = "regenerate"


class IppPrintConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IppPrintOptionsFlow:
        return IppPrintOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_printer_url(user_input[CONF_PRINTER_URL])
            except ValueError:
                errors[CONF_PRINTER_URL] = "invalid_url"
            else:
                try:
                    info = await IppPrinter(async_get_clientsession(self.hass), url).get_attributes()
                except IppError:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unerwarteter Fehler bei der Prüfung des Druckers")
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(urlparse(url).netloc.lower())
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=info.get("model") or "Drucker",
                        data={CONF_PRINTER_URL: url, CONF_WEBHOOK_ID: webhook.async_generate_id()},
                    )
        schema = vol.Schema({vol.Required(CONF_PRINTER_URL): str})
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input or {}), errors=errors
        )


class IppPrintOptionsFlow(OptionsFlowWithReload):
    """Zeigt die Webhook-Adresse und erlaubt, ein neues Geheimnis zu erzeugen."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input.get(CONF_REGENERATE):
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={**self.config_entry.data, CONF_WEBHOOK_ID: webhook.async_generate_id()}
                )
                # Die Optionen selbst bleiben leer (ändern sich also nicht) - deshalb ausdrücklich neu laden,
                # damit der Webhook unter der neuen Adresse registriert wird.
                self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return self.async_create_entry(data={})
        url = webhook.async_generate_url(self.hass, self.config_entry.data[CONF_WEBHOOK_ID], prefer_external=True)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Optional(CONF_REGENERATE, default=False): bool}),
            description_placeholders={"url": url, "printer": self.config_entry.data[CONF_PRINTER_URL]},
        )
