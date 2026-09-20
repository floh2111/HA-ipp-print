"""IPP Print: Dateien per Webhook (z. B. aus dem iPhone-Teilen-Menü) auf einem Netzwerkdrucker drucken."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
import logging
import re
import time
from typing import Any

from aiohttp import web

from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ANNOUNCED,
    CONF_PRINTER_URL,
    CONF_WEBHOOK_ID,
    DEFAULT_COPIES,
    DOMAIN,
    EVENT_PRINT_JOB,
    MAX_BYTES,
    MAX_COPIES,
    MAX_JOBS_PER_DAY,
    MAX_JOBS_PER_HOUR,
    SIGNAL_JOB,
    STATUS_INTERVAL_SECONDS,
)
from .ipp import (
    IppConnectionError,
    IppError,
    IppPrinter,
    IppResponseError,
    PrintJob,
    count_pdf_pages,
    detect_format,
    format_page_ranges,
    parse_page_ranges,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

_SIDES = {"one": False, "two": True, "two-long": True, "two-short": "short"}
_SIDES_TEXT = {False: "einseitig", True: "beidseitig", "short": "beidseitig (kurze Kante)"}


def _clean(value: str | None, default: str, limit: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", value or "").strip()
    return (text or default)[:limit]


def _reply(status: int, ok: bool, message: str, **extra: Any) -> web.Response:
    return web.json_response({"ok": ok, "message": message, **extra}, status=status)


@dataclass
class LastJob:
    time: Any
    name: str
    filename: str
    copies: int
    color: bool
    sides: str
    pages: int | None
    job_id: int | None
    page_range: str | None = None


class PrintManager:
    """Nimmt Druckaufträge per Webhook entgegen, prüft sie und schickt sie an den Drucker."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, printer: IppPrinter) -> None:
        self.hass = hass
        self.entry = entry
        self.printer = printer
        self.last: LastJob | None = None
        self._lock = asyncio.Lock()  # immer nur ein Auftrag gleichzeitig zum Drucker
        self._times: deque[float] = deque()

    # --- Missbrauchsschutz ------------------------------------------------------------------
    def _rate_limited(self) -> bool:
        now = time.monotonic()
        while self._times and now - self._times[0] > 86400:
            self._times.popleft()
        if len(self._times) >= MAX_JOBS_PER_DAY:
            return True
        return sum(1 for t in self._times if now - t <= 3600) >= MAX_JOBS_PER_HOUR

    # --- Webhook ------------------------------------------------------------------------------
    async def handle_webhook(self, hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        if request.method == "GET":  # Erreichbarkeit/Status prüfen (z. B. im Browser)
            try:
                info = await self.printer.get_attributes()
            except IppError as err:
                return _reply(504, False, f"Der Drucker ist nicht erreichbar: {err}")
            return _reply(200, True, f"Drucker bereit ({info['model']}, {info['state']})", printer=info["model"], state=info["state"])
        return await self._handle_print(request)

    async def _handle_print(self, request: web.Request) -> web.Response:
        query = request.query
        name = _clean(query.get("name"), "Unbekannt", 40)
        filename = _clean(query.get("filename"), "Dokument", 100)
        try:
            copies = int(query.get("copies", DEFAULT_COPIES))
        except ValueError:
            return _reply(400, False, "Ungültige Kopienzahl.")
        if not 1 <= copies <= MAX_COPIES:
            return _reply(400, False, f"Kopien: 1 bis {MAX_COPIES} sind erlaubt.")
        color_param = query.get("color", "bw")
        if color_param not in ("bw", "color"):
            return _reply(400, False, "Farbe: bw oder color.")
        sides_param = query.get("sides", "one")
        if sides_param not in _SIDES:
            return _reply(400, False, "Seiten: one, two oder two-short.")
        duplex = _SIDES[sides_param]
        page_ranges = None
        if query.get("pages", "").strip():  # leer = alle Seiten (so schickt es ein Kurzbefehl, wenn nichts eingetippt wurde)
            try:
                page_ranges = parse_page_ranges(query["pages"])
            except ValueError:
                return _reply(400, False, "Seiten: bitte so angeben: 1-3,5 (Seitenzahlen ab 1).")

        if request.content_length is not None and request.content_length > MAX_BYTES:
            return _reply(413, False, f"Die Datei ist zu groß (maximal {MAX_BYTES // 1024 // 1024} MB).")
        data = await request.read()
        if not data:
            return _reply(400, False, "Die Datei ist leer.")
        if len(data) > MAX_BYTES:
            return _reply(413, False, f"Die Datei ist zu groß (maximal {MAX_BYTES // 1024 // 1024} MB).")
        mime = detect_format(data)
        if mime is None:
            return _reply(415, False, "Dateityp nicht unterstützt. Bitte als PDF oder JPEG senden.")
        if page_ranges and mime != "application/pdf":
            return _reply(400, False, "Einen Seitenbereich gibt es nur bei PDF-Dateien.")
        if self._rate_limited():
            return _reply(429, False, "Zu viele Druckaufträge in kurzer Zeit. Bitte später noch einmal versuchen.")

        job = PrintJob(
            user=name, name=filename, mime=mime, copies=copies, color=color_param == "color", duplex=duplex,
            page_ranges=page_ranges,
        )
        async with self._lock:
            try:
                job_id = await self.printer.print_job(job, data)
            except IppConnectionError as err:
                _LOGGER.warning("Drucker nicht erreichbar: %s", err)
                return _reply(504, False, "Der Drucker ist nicht erreichbar (ausgeschaltet oder nicht im Netz?).")
            except IppResponseError as err:
                _LOGGER.warning("Drucker hat den Auftrag abgelehnt: %s", err)
                return _reply(502, False, f"Der Drucker hat den Auftrag abgelehnt ({err.message or f'0x{err.status:04x}'}).")
            except IppError as err:
                _LOGGER.warning("Unerwartete Druckerantwort: %s", err)
                return _reply(502, False, "Unerwartete Antwort vom Drucker.")
        self._times.append(time.monotonic())

        pages = count_pdf_pages(data) if mime == "application/pdf" else None
        sides_text = _SIDES_TEXT[duplex]
        color_text = "Farbe" if job.color else "Schwarzweiß"
        range_text = format_page_ranges(page_ranges) if page_ranges else None
        self.last = LastJob(dt_util.utcnow(), name, filename, copies, job.color, sides_text, pages, job_id, range_text)
        self.hass.bus.async_fire(
            EVENT_PRINT_JOB,
            {
                "name": name, "filename": filename, "copies": copies, "color": job.color, "sides": sides_text,
                "pages": pages, "page_range": range_text, "job_id": job_id,
            },
        )
        async_dispatcher_send(self.hass, SIGNAL_JOB.format(self.entry.entry_id))
        detail = ", ".join(
            part
            for part in (
                f"Seiten {range_text}" if range_text else (f"{pages} Seite{'n' if pages != 1 else ''}" if pages else ""),
                color_text,
                sides_text,
                f"{copies}×" if copies > 1 else "",
            )
            if part
        )
        return _reply(200, True, f"Gedruckt: {filename} ({detail})", job_id=job_id, pages=pages)


class StatusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fragt regelmäßig den Zustand des Druckers ab (bereit, druckt, gestoppt)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, printer: IppPrinter) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(seconds=STATUS_INTERVAL_SECONDS)
        )
        self.printer = printer

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.printer.get_attributes()
        except IppError as err:
            raise UpdateFailed(f"Drucker nicht erreichbar: {err}") from err


@dataclass
class RuntimeData:
    manager: PrintManager
    status: StatusCoordinator
    webhook_id: str  # die beim Start registrierte Adresse (entry.data kann sich vor dem Entladen schon geändert haben)


type IppPrintConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: IppPrintConfigEntry) -> bool:
    printer = IppPrinter(async_get_clientsession(hass), entry.data[CONF_PRINTER_URL])
    manager = PrintManager(hass, entry, printer)
    status = StatusCoordinator(hass, entry, printer)
    await status.async_config_entry_first_refresh()
    entry.runtime_data = RuntimeData(manager, status, entry.data[CONF_WEBHOOK_ID])

    webhook.async_register(
        hass, DOMAIN, "IPP Print", entry.data[CONF_WEBHOOK_ID], manager.handle_webhook,
        local_only=False, allowed_methods=["GET", "POST"],
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not entry.data.get(CONF_ANNOUNCED):
        url = webhook.async_generate_url(hass, entry.data[CONF_WEBHOOK_ID], prefer_external=True)
        persistent_notification.async_create(
            hass,
            f"Die Webhook-Adresse zum Drucken (**geheim halten**):\n\n`{url}`\n\n"
            "Sie steht auch unter *Einstellungen → Geräte & Dienste → IPP Print → Konfigurieren*. "
            "Die Einrichtung des Kurzbefehls steht in der Anleitung: https://github.com/floh2111/HA-ipp-print",
            title="IPP Print eingerichtet",
            notification_id=f"{DOMAIN}_{entry.entry_id}_setup",
        )
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ANNOUNCED: True})
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IppPrintConfigEntry) -> bool:
    webhook.async_unregister(hass, entry.runtime_data.webhook_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
