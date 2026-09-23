"""IPP Print: Dateien per Webhook (z. B. aus dem iPhone-Teilen-Menü) auf einem Netzwerkdrucker drucken."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
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
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ANNOUNCED,
    CONF_NOTIFY_TARGET,
    CONF_PRINTER_URL,
    CONF_WEBHOOK_ID,
    DEFAULT_COPIES,
    DOMAIN,
    EVENT_PRINT_JOB,
    EVENT_PRINT_JOB_RESULT,
    JOB_POLL_INTERVAL_SECONDS,
    JOB_POLL_MAX_ATTEMPTS,
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
    describe_job_reasons,
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
    # Ergebnis NACH der Übergabe an den Drucker (siehe PrintManager._track_job): "submitted" bis geklärt ist, was
    # daraus wurde, dann "waiting" (Drucker hält an, z. B. kein Papier), "done", "failed" oder "unclear" (der
    # Drucker kennt den Auftrag nicht mehr - meist harmlos, wenn er ihn schon abgeschlossen und vergessen hat).
    status: str = "submitted"
    status_reason: str | None = None


# Baustein für die Push-Benachrichtigung/Meldung je nach Ergebnis; None = dafür wird nichts gemeldet.
def _result_message(status: str, filename: str, reason: str | None) -> str | None:
    if status == "waiting":
        return f"⏳ Drucker wartet ({reason or 'unbekannter Grund'}): „{filename}“"
    if status == "done":
        return f"✅ Gedruckt: „{filename}“" + (f" ({reason})" if reason else "")
    if status == "failed":
        return f"❌ Druckauftrag fehlgeschlagen: „{filename}“" + (f" – {reason}" if reason else "")
    return None  # "submitted"/"unclear": nichts extra melden


class PrintManager:
    """Nimmt Druckaufträge per Webhook entgegen, prüft sie und schickt sie an den Drucker."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, printer: IppPrinter) -> None:
        self.hass = hass
        self.entry = entry
        self.printer = printer
        self.last: LastJob | None = None
        self._lock = asyncio.Lock()  # immer nur ein Auftrag gleichzeitig zum Drucker
        self._times: deque[float] = deque()
        self._trackers: dict[int, Callable[[], None]] = {}  # job_id -> Funktion, die die Verfolgung wieder abmeldet

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
        if job_id is not None:
            self._track_job(job_id)

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

    # --- Verfolgung eines Auftrags NACH der Übergabe --------------------------------------------------
    # Der Drucker hat den Auftrag zwar angenommen (Print-Job war erfolgreich), das heißt aber noch nicht, dass er
    # auch fertig gedruckt wird - er kann z. B. wegen fehlenden Papiers anhalten. Hier wird per Get-Job-Attributes
    # regelmäßig nachgefragt, bis ein Endergebnis feststeht oder wir nach JOB_POLL_MAX_ATTEMPTS Versuchen aufgeben
    # (als Anzahl statt als Uhrzeit gezählt, damit es unabhängig von der Systemuhr sauber funktioniert).
    def _track_job(self, job_id: int) -> None:
        self._stop_tracking(job_id)  # zur Sicherheit: falls für dieselbe Nummer noch eine alte Verfolgung offen ist
        attempts_left = JOB_POLL_MAX_ATTEMPTS
        last_reasons: list[str] | None = None

        async def _poll(_now: Any) -> None:
            nonlocal attempts_left, last_reasons
            attempts_left -= 1
            info: dict[str, Any] | None
            try:
                info = await self.printer.get_job_attributes(job_id)
            except IppResponseError as err:
                if 0x0400 <= err.status < 0x0500:  # Drucker kennt den Auftrag nicht (mehr) - meist: schon fertig
                    self._stop_tracking(job_id)
                    self._report(job_id, "unclear", None)
                    return
                _LOGGER.debug("Auftragsstatus (Job %s) abgelehnt: %s", job_id, err)
                info = None
            except IppError as err:
                _LOGGER.debug("Auftragsstatus (Job %s) gerade nicht abrufbar: %s", job_id, err)
                info = None  # z. B. Drucker kurz nicht erreichbar - beim nächsten Mal erneut versuchen

            if info is not None:
                state, reasons = info["state"], info["reasons"]
                if state in ("aborted", "canceled"):
                    self._stop_tracking(job_id)
                    self._report(job_id, "failed", describe_job_reasons(reasons) or "Unbekannter Fehler")
                    return
                if state == "completed":
                    self._stop_tracking(job_id)
                    self._report(job_id, "done", describe_job_reasons(reasons))
                    return
                if state == "processing-stopped" and reasons != last_reasons:
                    last_reasons = reasons
                    self._report(job_id, "waiting", describe_job_reasons(reasons) or "Angehalten")

            if attempts_left <= 0:  # niemand hat z. B. das fehlende Papier nachgelegt - irgendwann aufgeben
                self._stop_tracking(job_id)
                self._report(job_id, "unclear", None)

        self._trackers[job_id] = async_track_time_interval(self.hass, _poll, timedelta(seconds=JOB_POLL_INTERVAL_SECONDS))

    def _stop_tracking(self, job_id: int) -> None:
        remove = self._trackers.pop(job_id, None)
        if remove:
            remove()

    def _report(self, job_id: int, status: str, reason: str | None) -> None:
        """Zwischen-/Endergebnis: Sensor aktualisieren, Ereignis feuern, ggf. benachrichtigen."""
        if self.last is not None and self.last.job_id == job_id:
            self.last.status = status
            self.last.status_reason = reason
            async_dispatcher_send(self.hass, SIGNAL_JOB.format(self.entry.entry_id))
        last = self.last if (self.last and self.last.job_id == job_id) else None
        self.hass.bus.async_fire(
            EVENT_PRINT_JOB_RESULT,
            {
                "job_id": job_id, "status": status, "reason": reason,
                "name": last.name if last else None, "filename": last.filename if last else None,
            },
        )
        self.hass.async_create_task(self._notify(job_id, status, reason, last.filename if last else "Dokument"))

    async def _notify(self, job_id: int, status: str, reason: str | None, filename: str) -> None:
        message = _result_message(status, filename, reason)
        target = self.entry.options.get(CONF_NOTIFY_TARGET)
        notification_id = f"{DOMAIN}_{self.entry.entry_id}_job_{job_id}"
        if target:
            if message is None:
                return
            try:
                await self.hass.services.async_call(
                    "notify", "send_message", {"entity_id": target, "message": message, "title": "🖨️ IPP Print"},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001 - Zielgerät ungültig/entfernt o. Ä.; darf den Rest nicht stören
                _LOGGER.warning("Push-Benachrichtigung an %s fehlgeschlagen: %s", target, err)
            return
        # Ohne konfiguriertes Ziel: nur bei einem Problem eine (sich selbst ersetzende) Meldung in Home Assistant -
        # ist es am Ende doch gut gegangen, wird eine zwischenzeitliche "wartet"-Meldung wieder weggeräumt.
        if status in ("waiting", "failed") and message:
            persistent_notification.async_create(self.hass, message, title="🖨️ IPP Print", notification_id=notification_id)
        elif status in ("done", "unclear"):
            persistent_notification.async_dismiss(self.hass, notification_id)

    def shutdown(self) -> None:
        """Wird beim Entladen der Integration aufgerufen: alle laufenden Verfolgungen beenden."""
        for remove in list(self._trackers.values()):
            remove()
        self._trackers.clear()


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
    entry.runtime_data.manager.shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
