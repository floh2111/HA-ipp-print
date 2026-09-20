"""Sensoren: Druckerzustand und letzter Druckauftrag."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import IppPrintConfigEntry, PrintManager, StatusCoordinator
from .const import DOMAIN, SIGNAL_JOB

PARALLEL_UPDATES = 0

PRINTER_STATES = ["idle", "processing", "stopped"]


async def async_setup_entry(
    hass: HomeAssistant, entry: IppPrintConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    async_add_entities([PrinterStateSensor(entry, data.status), LastJobSensor(entry, data.manager)])


def _device(entry: IppPrintConfigEntry, model: str | None) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, model=model or None)


class PrinterStateSensor(CoordinatorEntity[StatusCoordinator], SensorEntity):
    """Bereit / druckt / gestoppt."""

    _attr_has_entity_name = True
    _attr_translation_key = "printer_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PRINTER_STATES
    _attr_icon = "mdi:printer"

    def __init__(self, entry: IppPrintConfigEntry, coordinator: StatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_state"
        self._attr_device_info = _device(entry, coordinator.data.get("model"))

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.data.get("state")
        return state if state in PRINTER_STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "accepting_jobs": self.coordinator.data.get("accepting"),
            "reasons": self.coordinator.data.get("reasons", []),
        }


class LastJobSensor(SensorEntity):
    """Zeitpunkt des letzten Druckauftrags (Wer, Datei, Optionen als Attribute)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "last_job"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:printer-check"

    def __init__(self, entry: IppPrintConfigEntry, manager: PrintManager) -> None:
        self._entry = entry
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_last_job"
        self._attr_device_info = _device(entry, entry.runtime_data.status.data.get("model"))

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_JOB.format(self._entry.entry_id), self._handle_job)
        )

    @callback
    def _handle_job(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> datetime | None:
        return self._manager.last.time if self._manager.last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self._manager.last
        if last is None:
            return {}
        return {
            "name": last.name,
            "filename": last.filename,
            "copies": last.copies,
            "color": last.color,
            "sides": last.sides,
            "pages": last.pages,
            "page_range": last.page_range,
            "job_id": last.job_id,
        }
