"""Minimaler IPP-Client (RFC 8010/8011): Drucker abfragen, Auftrag prüfen und drucken.

Bewusst ohne Zusatzpakete (nur aiohttp aus Home Assistant), weil hier nur wenige Operationen gebraucht werden:
Get-Printer-Attributes, Validate-Job und Print-Job.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import itertools
import re
import struct
from typing import Any
from urllib.parse import urlparse

import aiohttp

# --- Operationen und Tags (RFC 8011) -----------------------------------------------------------
OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_PRINTER_ATTRIBUTES = 0x000B

TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04

INTEGER = 0x21
BOOLEAN = 0x22
ENUM = 0x23
NAME = 0x42
KEYWORD = 0x44
URI = 0x45
CHARSET = 0x47
LANGUAGE = 0x48
MIME = 0x49
RANGE = 0x33  # rangeOfInteger: (untere, obere Grenze), je 4 Byte

_INT_TAGS = (INTEGER, ENUM)
_TEXT_TAGS = (0x41, 0x42, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49)  # text/name/keyword/uri/uriScheme/charset/language/mime

PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}

# job-state (RFC 8011 §4.3.7). "processing-stopped" ist der für uns interessante Zustand: der Drucker hat den
# Auftrag angenommen, kommt aber gerade nicht weiter (z. B. kein Papier) - er wartet, bricht aber nicht ab.
JOB_STATES = {
    3: "pending", 4: "pending-held", 5: "processing", 6: "processing-stopped", 7: "canceled", 8: "aborted", 9: "completed",
}

_request_ids = itertools.count(1)


class IppError(Exception):
    """Allgemeiner IPP-Fehler."""


class IppConnectionError(IppError):
    """Drucker nicht erreichbar (Netzwerk, Zeitüberschreitung, kein IPP)."""


class IppResponseError(IppError):
    """Der Drucker hat die Anfrage abgelehnt."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"IPP-Status 0x{status:04x}" + (f": {message}" if message else ""))
        self.status = status
        self.message = message


@dataclass
class IppResponse:
    status: int
    request_id: int
    groups: list[tuple[int, dict[str, list[Any]]]] = field(default_factory=list)
    payload: bytes = b""  # Daten nach dem Ende der Attribute (bei Anfragen: das Dokument)

    def attributes(self, tag: int) -> dict[str, list[Any]]:
        """Alle Attribute einer Gruppe (z. B. TAG_PRINTER), Gruppen gleicher Art zusammengeführt."""
        merged: dict[str, list[Any]] = {}
        for group_tag, attrs in self.groups:
            if group_tag == tag:
                merged.update(attrs)
        return merged


@dataclass
class PrintJob:
    user: str
    name: str
    mime: str
    copies: int = 1
    color: bool = False
    duplex: bool | str = False  # False = einseitig, True/"long" = lange Kante, "short" = kurze Kante
    page_ranges: list[tuple[int, int]] | None = None  # nur diese Seiten drucken (PDF), z. B. [(1, 3), (5, 5)]


# --- Kodierung -----------------------------------------------------------------------------------
def _clean_text(value: str, limit: int = 255) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value)).strip()
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def encode_attribute(tag: int, name: str, value: Any) -> bytes:
    """Ein Attribut; eine Liste als Wert ergibt ein Attribut mit mehreren Werten (1setOf).

    Bei RANGE ist der Wert immer eine Liste von (untere, obere)-Paaren, auch für nur einen Bereich.
    """
    values = value if isinstance(value, (list, tuple)) else [value]
    out = bytearray()
    for index, item in enumerate(values):
        if tag in _INT_TAGS:
            raw = struct.pack(">i", int(item))
        elif tag == BOOLEAN:
            raw = b"\x01" if item else b"\x00"
        elif tag == RANGE:
            raw = struct.pack(">ii", int(item[0]), int(item[1]))
        else:
            raw = str(item).encode("utf-8")
        attr_name = name.encode("ascii") if index == 0 else b""
        out += struct.pack(">BH", tag, len(attr_name)) + attr_name + struct.pack(">H", len(raw)) + raw
    return bytes(out)


def build_request(
    operation: int,
    request_id: int,
    operation_attrs: list[tuple[int, str, Any]],
    job_attrs: list[tuple[int, str, Any]] | None = None,
    payload: bytes = b"",
) -> bytes:
    out = bytearray(struct.pack(">BBHI", 1, 1, operation, request_id))  # IPP 1.1
    out.append(TAG_OPERATION)
    for tag, name, value in operation_attrs:
        out += encode_attribute(tag, name, value)
    if job_attrs:
        out.append(TAG_JOB)
        for tag, name, value in job_attrs:
            out += encode_attribute(tag, name, value)
    out.append(TAG_END)
    return bytes(out) + payload


def parse_response(data: bytes) -> IppResponse:
    if len(data) < 9:
        raise IppError("Antwort zu kurz für IPP")
    _major, _minor, status, request_id = struct.unpack(">BBHI", data[:8])
    response = IppResponse(status, request_id)
    pos = 8
    current: dict[str, list[Any]] | None = None
    last_name = ""
    try:
        while True:
            tag = data[pos]
            pos += 1
            if tag == TAG_END:
                response.payload = data[pos:]
                break
            if tag <= 0x0F:  # neue Attributgruppe
                current = {}
                response.groups.append((tag, current))
                last_name = ""
                continue
            (name_len,) = struct.unpack_from(">H", data, pos)
            pos += 2
            name = data[pos : pos + name_len].decode("utf-8", "replace")
            pos += name_len
            (value_len,) = struct.unpack_from(">H", data, pos)
            pos += 2
            raw = data[pos : pos + value_len]
            if len(raw) != value_len:
                raise IppError("Antwort abgeschnitten")
            pos += value_len
            if tag in _INT_TAGS and value_len == 4:
                value: Any = struct.unpack(">i", raw)[0]
            elif tag == BOOLEAN and value_len == 1:
                value = raw != b"\x00"
            elif tag == RANGE and value_len == 8:
                value = struct.unpack(">ii", raw)
            elif tag in _TEXT_TAGS:
                value = raw.decode("utf-8", "replace")
            else:
                value = raw  # Sammlungen, Datum, Auflösung, ... werden hier nicht gebraucht
            if current is None:
                raise IppError("Attribut außerhalb einer Gruppe")
            if name:
                last_name = name
                current.setdefault(name, []).append(value)
            elif last_name:  # weiterer Wert des vorherigen Attributs
                current[last_name].append(value)
    except (IndexError, struct.error) as err:
        raise IppError("Antwort ist kein gültiges IPP") from err
    return response


def detect_format(data: bytes) -> str | None:
    """Dateiformat an den ersten Bytes erkennen (das Content-Type des Absenders ist oft ungenau)."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data.startswith(b"%!PS"):
        return "application/postscript"
    return None


_PDF_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")


def count_pdf_pages(data: bytes) -> int | None:
    """Seitenzahl eines PDFs, soweit sie sich ohne PDF-Bibliothek erkennen lässt (sonst None)."""
    return len(_PDF_PAGE.findall(data)) or None


MAX_PAGE_RANGES = 10


def parse_page_ranges(text: str) -> list[tuple[int, int]]:
    """'1-3, 5' -> [(1, 3), (5, 5)]. Sortiert und fasst Überlappendes zusammen (IPP verlangt aufsteigend ohne Überlappung).

    Verträgt Leerzeichen und typografische Striche (– —), die iOS gern statt "-" einsetzt.
    Wirft ValueError bei allem, was keine Seitenangabe ist.
    """
    cleaned = re.sub(r"[\u2010-\u2015\u2212]", "-", text or "")
    cleaned = re.sub(r"\s+", "", cleaned)
    ranges: list[tuple[int, int]] = []
    for part in cleaned.split(","):
        if not part:  # z. B. ein Komma am Ende
            continue
        match = re.fullmatch(r"(\d{1,4})(?:-(\d{1,4}))?", part)
        if not match:
            raise ValueError(f"ungültig: {part!r}")
        lower = int(match.group(1))
        upper = int(match.group(2)) if match.group(2) else lower
        if lower < 1 or upper < lower:
            raise ValueError(f"ungültiger Bereich: {part!r}")
        ranges.append((lower, upper))
    if not ranges or len(ranges) > MAX_PAGE_RANGES:
        raise ValueError("keine oder zu viele Bereiche")
    ranges.sort()
    merged = [ranges[0]]
    for lower, upper in ranges[1:]:
        last_lower, last_upper = merged[-1]
        if lower <= last_upper + 1:
            merged[-1] = (last_lower, max(last_upper, upper))
        else:
            merged.append((lower, upper))
    return merged


def format_page_ranges(ranges: list[tuple[int, int]]) -> str:
    """[(1, 3), (5, 5)] -> '1-3, 5'."""
    return ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in ranges)


# job-state-reasons/printer-state-reasons (RFC 8011 §4.3.8/§4.4.12): Schlüsselwörter, oft mit Endung "-warning"/
# "-error"/"-report" für den Schweregrad. Nicht jeder Drucker/Hersteller nutzt genau diese Namen, deshalb ein
# Klartext-Fallback (Bindestriche durch Leerzeichen) für unbekannte Gründe statt einer Fehlermeldung.
_REASON_TEXT = {
    "media-empty": "Kein Papier",
    "media-needed": "Papier fehlt",
    "media-low": "Papier wird knapp",
    "media-jam": "Papierstau",
    "toner-empty": "Toner leer",
    "toner-low": "Toner wird knapp",
    "marker-supply-empty": "Toner/Tinte leer",
    "marker-supply-low": "Toner/Tinte wird knapp",
    "marker-waste-almost-full": "Resttonerbehälter fast voll",
    "marker-waste-full": "Resttonerbehälter voll",
    "door-open": "Klappe offen",
    "cover-open": "Abdeckung offen",
    "input-tray-missing": "Papierfach fehlt",
    "output-tray-missing": "Ausgabefach fehlt",
    "output-area-almost-full": "Ausgabefach fast voll",
    "output-area-full": "Ausgabefach voll",
    "spool-area-full": "Speicher des Druckers ist voll",
    "interpreter-resource-unavailable": "Drucker überlastet",
    "job-canceled-by-user": "Vom Nutzer abgebrochen",
    "job-canceled-by-operator": "Am Drucker abgebrochen",
    "job-completed-with-errors": "Mit Fehlern abgeschlossen",
    "job-completed-with-warnings": "Mit Warnungen abgeschlossen",
    "printer-stopped": "Drucker gestoppt",
}
_REASON_SUFFIX = re.compile(r"-(warning|error|report)$")


def describe_job_reasons(reasons: list[str] | None) -> str | None:
    """['media-empty-warning', 'media-empty-warning'] -> 'Kein Papier'; leer/None -> None."""
    if not reasons:
        return None
    texts: list[str] = []
    for raw in reasons:
        key = _REASON_SUFFIX.sub("", raw)
        text = _REASON_TEXT.get(key, key.replace("-", " "))
        if text not in texts:
            texts.append(text)
    return ", ".join(texts)


def normalize_printer_url(raw: str) -> str:
    """'192.168.1.5', 'drucker.local' oder eine volle ipp(s)://-Adresse -> ipp(s)://host:port/pfad."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("leer")
    if "://" not in value:
        value = "ipp://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("ipp", "ipps") or not parsed.hostname:
        raise ValueError("ungültig")
    port = parsed.port or (631 if parsed.scheme == "ipp" else 443)
    path = parsed.path if parsed.path not in ("", "/") else "/ipp/print"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}:{port}{path}"


# --- Client ----------------------------------------------------------------------------------------
class IppPrinter:
    def __init__(self, session: aiohttp.ClientSession, printer_url: str) -> None:
        self._session = session
        self.printer_uri = normalize_printer_url(printer_url)
        parsed = urlparse(self.printer_uri)
        self._secure = parsed.scheme == "ipps"
        self._http_url = self.printer_uri.replace("ipps://", "https://", 1).replace("ipp://", "http://", 1)

    async def _execute(
        self,
        operation: int,
        operation_attrs: list[tuple[int, str, Any]],
        job_attrs: list[tuple[int, str, Any]] | None = None,
        payload: bytes = b"",
        timeout: float = 20,
    ) -> IppResponse:
        base = [
            (CHARSET, "attributes-charset", "utf-8"),
            (LANGUAGE, "attributes-natural-language", "en"),
            (URI, "printer-uri", self.printer_uri),
        ]
        body = build_request(operation, next(_request_ids) & 0x7FFFFFFF, base + operation_attrs, job_attrs, payload)
        kwargs: dict[str, Any] = {"ssl": False} if self._secure else {}  # Drucker haben meist selbstsignierte Zertifikate
        try:
            async with asyncio.timeout(timeout):
                async with self._session.post(
                    self._http_url, data=body, headers={"Content-Type": "application/ipp"}, **kwargs
                ) as resp:
                    if resp.status != 200:
                        raise IppConnectionError(f"HTTP {resp.status} vom Drucker")
                    raw = await resp.read()
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise IppConnectionError(str(err) or "Verbindung zum Drucker fehlgeschlagen") from err
        response = parse_response(raw)
        if response.status >= 0x0100:
            message = " ".join(str(m) for m in response.attributes(TAG_OPERATION).get("status-message", []))
            raise IppResponseError(response.status, message)
        return response

    @staticmethod
    def _job_attributes(job: PrintJob) -> list[tuple[int, str, Any]]:
        sides = "one-sided"
        if job.duplex:
            sides = "two-sided-short-edge" if job.duplex == "short" else "two-sided-long-edge"
        attrs: list[tuple[int, str, Any]] = [
            (INTEGER, "copies", job.copies),
            (KEYWORD, "sides", sides),
            (KEYWORD, "print-color-mode", "color" if job.color else "monochrome"),
        ]
        if job.page_ranges:
            attrs.append((RANGE, "page-ranges", job.page_ranges))
        return attrs

    def _job_operation_attributes(self, job: PrintJob) -> list[tuple[int, str, Any]]:
        return [
            (NAME, "requesting-user-name", _clean_text(job.user) or "unknown"),
            (NAME, "job-name", _clean_text(job.name) or "Dokument"),
            (MIME, "document-format", job.mime),
        ]

    async def get_attributes(self) -> dict[str, Any]:
        """Name, Zustand und Annahmebereitschaft des Druckers."""
        response = await self._execute(
            OP_GET_PRINTER_ATTRIBUTES,
            [
                (
                    KEYWORD,
                    "requested-attributes",
                    ["printer-state", "printer-state-reasons", "printer-is-accepting-jobs", "printer-make-and-model", "document-format-supported"],
                )
            ],
        )
        attrs = response.attributes(TAG_PRINTER)
        first = lambda key, default=None: (attrs.get(key) or [default])[0]  # noqa: E731
        return {
            "model": first("printer-make-and-model", ""),
            "state": PRINTER_STATES.get(first("printer-state"), "unknown"),
            "reasons": [r for r in attrs.get("printer-state-reasons", []) if r != "none"],
            "accepting": bool(first("printer-is-accepting-jobs", True)),
            "formats": attrs.get("document-format-supported", []),
        }

    async def get_job_attributes(self, job_id: int) -> dict[str, Any]:
        """Zustand eines einzelnen Druckauftrags: 'state' (siehe JOB_STATES) und 'reasons' (z. B. ['media-empty-warning']).

        Kennt der Drucker den Auftrag nicht mehr (z. B. schon abgeschlossen und aus der Historie entfernt), lehnt er
        mit einem Client-Fehler ab (bei einem echten Kyocera ECOSYS: 0x0400) - das behandelt der Aufrufer gesondert.
        """
        response = await self._execute(
            OP_GET_JOB_ATTRIBUTES,
            [(INTEGER, "job-id", job_id), (KEYWORD, "requested-attributes", ["job-state", "job-state-reasons"])],
        )
        attrs = response.attributes(TAG_JOB)
        state_code = (attrs.get("job-state") or [None])[0]
        return {
            "state": JOB_STATES.get(state_code, "unknown"),
            "reasons": [r for r in attrs.get("job-state-reasons", []) if r != "none"],
        }

    async def validate_job(self, job: PrintJob) -> None:
        """Fragt den Drucker, ob er den Auftrag annehmen würde - ohne zu drucken."""
        await self._execute(OP_VALIDATE_JOB, self._job_operation_attributes(job), self._job_attributes(job))

    async def print_job(self, job: PrintJob, data: bytes) -> int | None:
        """Druckt das Dokument; gibt die Auftragsnummer des Druckers zurück."""
        response = await self._execute(
            OP_PRINT_JOB, self._job_operation_attributes(job), self._job_attributes(job), data, timeout=120
        )
        ids = response.attributes(TAG_JOB).get("job-id") or []
        return ids[0] if ids else None
