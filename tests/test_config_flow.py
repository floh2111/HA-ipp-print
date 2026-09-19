"""Einrichtung und Optionen."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ipp_print.const import CONF_ANNOUNCED, CONF_PRINTER_URL, CONF_WEBHOOK_ID, DOMAIN

from .conftest import PRINTER_IP, PRINTER_URL, WEBHOOK_ID, FakePrinter


async def _answers(client, webhook_id: str) -> bool:
    """Meldet sich unter dieser Webhook-Adresse unsere Integration? (Unbekannte Adressen beantwortet HA anders.)"""
    return '"printer"' in await (await client.get(f"/api/webhook/{webhook_id}")).text()


@pytest.fixture(autouse=True)
async def _http(hass: HomeAssistant):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "webhook", {})


async def test_user_flow_success(hass: HomeAssistant, printer: FakePrinter) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRINTER_URL: PRINTER_IP})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ECOSYS M5526cdw"
    assert result["data"][CONF_PRINTER_URL] == PRINTER_URL
    assert len(result["data"][CONF_WEBHOOK_ID]) >= 32  # langes Geheimnis
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_ANNOUNCED] is True  # Hinweis mit der Webhook-Adresse wurde einmal angezeigt


async def test_user_flow_cannot_connect(hass: HomeAssistant, printer: FakePrinter) -> None:
    printer.down = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRINTER_URL: PRINTER_IP})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "cannot_connect"}
    printer.down = False  # zweiter Versuch klappt
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRINTER_URL: PRINTER_IP})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_invalid_url(hass: HomeAssistant, printer: FakePrinter) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRINTER_URL: "http://falsch"})
    assert result["errors"] == {CONF_PRINTER_URL: "invalid_url"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, printer: FakePrinter, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRINTER_URL: PRINTER_IP})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_options_flow_shows_url_and_keeps_secret(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert WEBHOOK_ID in result["description_placeholders"]["url"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"regenerate": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert setup_integration.data[CONF_WEBHOOK_ID] == WEBHOOK_ID


async def test_options_flow_regenerates_secret(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"regenerate": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    new_id = setup_integration.data[CONF_WEBHOOK_ID]
    assert new_id != WEBHOOK_ID
    # neue Adresse antwortet, die alte nicht mehr
    assert await _answers(client, new_id)
    assert not await _answers(client, WEBHOOK_ID)


async def test_setup_retries_when_printer_is_off(
    hass: HomeAssistant, config_entry: MockConfigEntry, printer: FakePrinter
) -> None:
    printer.down = True
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY  # HA versucht es später von selbst noch einmal


async def test_unload_removes_webhook(hass: HomeAssistant, setup_integration: MockConfigEntry, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    assert await _answers(client, WEBHOOK_ID)
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert not await _answers(client, WEBHOOK_ID)
