"""Gemeinsame Test-Helfer: ein Fake-Drucker auf Basis von aioclient_mock."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ipp_print import ipp
from custom_components.ipp_print.const import CONF_PRINTER_URL, CONF_WEBHOOK_ID, DOMAIN

PRINTER_IP = "192.168.1.20"
PRINTER_URL = f"ipp://{PRINTER_IP}:631/ipp/print"
HTTP_URL = f"http://{PRINTER_IP}:631/ipp/print"
WEBHOOK_ID = "test_webhook_secret_0123456789abcdef"

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Pages /Count 2 >>\nendobj\n2 0 obj\n<< /Type /Page >>\nendobj\n3 0 obj\n<< /Type /Page >>\nendobj\n"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def ipp_reply(status: int, request_id: int, groups: list[tuple[int, list[tuple[int, str, Any]]]]) -> bytes:
    out = bytearray(b"\x02\x00" + status.to_bytes(2, "big") + request_id.to_bytes(4, "big"))
    for tag, attrs in groups:
        out.append(tag)
        for attr_tag, name, value in attrs:
            out += ipp.encode_attribute(attr_tag, name, value)
    out.append(ipp.TAG_END)
    return bytes(out)


class FakePrinter:
    """Entschlüsselt IPP-Anfragen, merkt sie sich und antwortet wie ein Drucker."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.state = 3  # idle
        self.model = "ECOSYS M5526cdw"
        self.job_status = 0
        self.job_message = ""
        self.down = False
        self.next_job_id = 41
        # Get-Job-Attributes: job_id -> Liste von (job-state-Zahl, [Gründe]); wird der Reihe nach abgearbeitet,
        # der letzte Eintrag wiederholt sich (so wie ein Drucker in einem Zustand verharrt, bis sich etwas ändert).
        # "down" statt eines Eintrags simuliert einen Verbindungsabbruch bei genau dieser Abfrage.
        # Kein Eintrag für eine job-id = der Drucker kennt den Auftrag nicht (mehr) - wie beim echten Kyocera
        # gemessen (IPP-Status 0x0400 auf eine erfundene Auftragsnummer).
        self.job_attribute_sequences: dict[int, list[Any]] = {}

    @property
    def prints(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["operation"] == ipp.OP_PRINT_JOB]

    async def handle(self, method: str, url: Any, data: Any):
        if self.down:
            return AiohttpClientMockResponse(method, url, exc=aiohttp.ClientConnectionError("Drucker aus"))
        req = ipp.parse_response(data)  # Anfragen haben dasselbe Format wie Antworten (Operation statt Status)
        op_attrs = req.attributes(ipp.TAG_OPERATION)
        record = {
            "operation": req.status,
            "op": op_attrs,
            "job": req.attributes(ipp.TAG_JOB),
            "payload": req.payload,
        }
        self.requests.append(record)
        first = lambda key: op_attrs[key][0]  # noqa: E731
        base = [
            (ipp.CHARSET, "attributes-charset", "utf-8"),
            (ipp.LANGUAGE, "attributes-natural-language", "en"),
        ]
        if req.status == ipp.OP_GET_JOB_ATTRIBUTES:
            job_id = first("job-id")
            sequence = self.job_attribute_sequences.get(job_id)
            if not sequence:
                body = ipp_reply(0x0400, req.request_id, [(ipp.TAG_OPERATION, base)])
            else:
                step = sequence.pop(0) if len(sequence) > 1 else sequence[0]
                if step == "down":
                    return AiohttpClientMockResponse(method, url, exc=aiohttp.ClientConnectionError("Drucker aus"))
                state_code, reasons = step
                job_attrs = [(ipp.ENUM, "job-state", state_code), (ipp.KEYWORD, "job-state-reasons", list(reasons) or "none")]
                body = ipp_reply(0, req.request_id, [(ipp.TAG_OPERATION, base), (ipp.TAG_JOB, job_attrs)])
            return AiohttpClientMockResponse(method, url, response=body)
        if req.status == ipp.OP_GET_PRINTER_ATTRIBUTES:
            groups = [
                (ipp.TAG_OPERATION, base),
                (
                    ipp.TAG_PRINTER,
                    [
                        (0x41, "printer-make-and-model", self.model),
                        (ipp.ENUM, "printer-state", self.state),
                        (ipp.KEYWORD, "printer-state-reasons", "none"),
                        (ipp.BOOLEAN, "printer-is-accepting-jobs", True),
                        (ipp.MIME, "document-format-supported", ["application/pdf", "image/jpeg"]),
                    ],
                ),
            ]
            body = ipp_reply(0, req.request_id, groups)
        elif self.job_status:
            attrs = base + ([(0x41, "status-message", self.job_message)] if self.job_message else [])
            body = ipp_reply(self.job_status, req.request_id, [(ipp.TAG_OPERATION, attrs)])
        else:
            groups = [(ipp.TAG_OPERATION, base)]
            if req.status == ipp.OP_PRINT_JOB:
                self.next_job_id += 1
                groups.append((ipp.TAG_JOB, [(ipp.INTEGER, "job-id", self.next_job_id)]))
            body = ipp_reply(0, req.request_id, groups)
        return AiohttpClientMockResponse(method, url, response=body)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Lädt Integrationen aus custom_components/."""


@pytest.fixture
def printer(aioclient_mock: AiohttpClientMocker) -> FakePrinter:
    fake = FakePrinter()
    aioclient_mock.post(HTTP_URL, side_effect=fake.handle)
    return fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="ECOSYS M5526cdw",
        unique_id=f"{PRINTER_IP}:631",
        data={CONF_PRINTER_URL: PRINTER_URL, CONF_WEBHOOK_ID: WEBHOOK_ID},
    )


@pytest.fixture
async def setup_integration(hass: HomeAssistant, config_entry: MockConfigEntry, printer: FakePrinter):
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "webhook", {})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
