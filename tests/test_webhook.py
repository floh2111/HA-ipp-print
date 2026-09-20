"""Drucken über den Webhook: Standardwerte, Optionen, Prüfungen, Fehler, Missbrauchsschutz."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from homeassistant.core import HomeAssistant

from custom_components.ipp_print.const import EVENT_PRINT_JOB, MAX_BYTES, MAX_JOBS_PER_HOUR

from .conftest import JPEG, PDF, PNG, WEBHOOK_ID, FakePrinter

URL = f"/api/webhook/{WEBHOOK_ID}"


async def test_default_is_black_white_one_sided_one_copy(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    resp = await client.post(f"{URL}?name=Florian&filename=Rechnung.pdf", data=PDF)
    body = await resp.json()
    assert resp.status == 200 and body["ok"] is True
    assert body["message"] == "Gedruckt: Rechnung.pdf (2 Seiten, Schwarzweiß, einseitig)"
    assert body["job_id"] == 42 and body["pages"] == 2

    [job] = printer.prints
    assert job["payload"] == PDF
    assert job["op"]["document-format"] == ["application/pdf"]
    assert job["op"]["requesting-user-name"] == ["Florian"]
    assert job["op"]["job-name"] == ["Rechnung.pdf"]
    assert job["job"]["copies"] == [1]
    assert job["job"]["sides"] == ["one-sided"]
    assert job["job"]["print-color-mode"] == ["monochrome"]


@pytest.mark.parametrize(
    ("query", "copies", "sides", "color", "text"),
    [
        ("color=color", 1, "one-sided", "color", "Farbe, einseitig"),
        ("sides=two", 1, "two-sided-long-edge", "monochrome", "Schwarzweiß, beidseitig"),
        ("sides=two-short&copies=3", 3, "two-sided-short-edge", "monochrome", "beidseitig (kurze Kante), 3×"),
        ("color=color&sides=two&copies=2", 2, "two-sided-long-edge", "color", "Farbe, beidseitig, 2×"),
    ],
)
async def test_options_on_request(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth,
    query: str, copies: int, sides: str, color: str, text: str,
) -> None:
    client = await hass_client_no_auth()
    resp = await client.post(f"{URL}?{query}", data=PDF)
    body = await resp.json()
    assert resp.status == 200, body
    assert text in body["message"]
    job = printer.prints[-1]["job"]
    assert (job["copies"], job["sides"], job["print-color-mode"]) == ([copies], [sides], [color])


async def test_jpeg_is_accepted_and_png_is_not(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    ok = await client.post(URL, data=JPEG, headers={"Content-Type": "application/octet-stream"})
    assert ok.status == 200
    assert printer.prints[-1]["op"]["document-format"] == ["image/jpeg"]  # erkannt am Inhalt, nicht am Content-Type

    before = len(printer.prints)
    bad = await client.post(URL, data=PNG)
    assert bad.status == 415 and "PDF" in (await bad.json())["message"]
    assert len(printer.prints) == before  # nichts an den Drucker geschickt


@pytest.mark.parametrize(
    "query",
    ["copies=0", "copies=21", "copies=abc", "copies=999", "color=rot", "sides=drei"],
)
async def test_invalid_parameters_are_rejected_before_printing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth, query: str
) -> None:
    client = await hass_client_no_auth()
    resp = await client.post(f"{URL}?{query}", data=PDF)
    assert resp.status == 400 and (await resp.json())["ok"] is False
    assert printer.prints == []


async def test_empty_and_oversized_files(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    assert (await client.post(URL, data=b"")).status == 400
    big = b"%PDF-" + b"x" * MAX_BYTES  # knapp über dem Limit, aber unter dem von Home Assistant (16 MB)
    resp = await client.post(URL, data=big)
    assert resp.status == 413
    assert printer.prints == []


async def test_odd_names_are_cleaned(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    await client.post(URL, params={"name": "  Anna\x07 B  ", "filename": "Übung – 1\x07.pdf"}, data=PDF)
    job = printer.prints[-1]["op"]
    assert job["requesting-user-name"] == ["Anna B"]  # Steuerzeichen und Leerraum entfernt
    assert job["job-name"] == ["Übung – 1.pdf"]
    await client.post(URL, data=PDF)  # ohne Angaben
    job = printer.prints[-1]["op"]
    assert (job["requesting-user-name"], job["job-name"]) == (["Unbekannt"], ["Dokument"])
    # Zeilenumbrüche in der Adresse weist schon der Home-Assistant-Server ab
    before = len(printer.prints)
    assert (await client.post(URL, params={"name": "A\r\nX-Evil: 1"}, data=PDF)).status == 400
    assert len(printer.prints) == before


async def test_printer_off_gives_clear_error_and_no_event(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    events = async_capture_events(hass, EVENT_PRINT_JOB)
    printer.down = True
    resp = await client.post(URL, data=PDF)
    body = await resp.json()
    assert resp.status == 504 and "nicht erreichbar" in body["message"]
    await hass.async_block_till_done()
    assert events == []
    printer.down = False
    assert (await client.post(URL, data=PDF)).status == 200  # danach geht es wieder


async def test_printer_rejection_is_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    printer.job_status = 0x0400  # client-error-bad-request
    printer.job_message = "Papierstau"
    resp = await client.post(URL, data=PDF)
    body = await resp.json()
    assert resp.status == 502 and "abgelehnt" in body["message"] and "Papierstau" in body["message"]


async def test_rate_limit_per_hour(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    for _ in range(MAX_JOBS_PER_HOUR):
        assert (await client.post(URL, data=PDF)).status == 200
    resp = await client.post(URL, data=PDF)
    assert resp.status == 429 and "Zu viele" in (await resp.json())["message"]
    assert len(printer.prints) == MAX_JOBS_PER_HOUR


async def test_failed_jobs_do_not_count_towards_limit(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    printer.down = True
    for _ in range(MAX_JOBS_PER_HOUR + 2):
        assert (await client.post(URL, data=PDF)).status == 504
    printer.down = False
    assert (await client.post(URL, data=PDF)).status == 200


async def test_event_and_last_job_sensor(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    from homeassistant.helpers import entity_registry as er

    client = await hass_client_no_auth()
    events = async_capture_events(hass, EVENT_PRINT_JOB)
    registry = er.async_get(hass)
    last_id = registry.async_get_entity_id("sensor", "ipp_print", f"{setup_integration.entry_id}_last_job")
    assert hass.states.get(last_id).state == "unknown"  # noch nie gedruckt

    await client.post(f"{URL}?name=Anna&filename=Foto.jpg&color=color&copies=2", data=JPEG)
    await hass.async_block_till_done()
    [event] = events
    assert event.data == {
        "name": "Anna", "filename": "Foto.jpg", "copies": 2, "color": True, "sides": "einseitig", "pages": None,
        "page_range": None, "job_id": 42,
    }
    state = hass.states.get(last_id)
    assert state.state != "unknown"
    assert state.attributes["name"] == "Anna" and state.attributes["color"] is True and state.attributes["copies"] == 2


async def test_printer_state_sensor(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id("sensor", "ipp_print", f"{setup_integration.entry_id}_state")
    state = hass.states.get(entity_id)
    assert state.state == "idle"
    assert state.attributes["accepting_jobs"] is True and state.attributes["reasons"] == []


async def test_get_checks_printer(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    resp = await client.get(URL)
    body = await resp.json()
    assert resp.status == 200 and body["printer"] == "ECOSYS M5526cdw" and body["state"] == "idle"
    printer.down = True
    assert (await client.get(URL)).status == 504
    assert printer.prints == []  # GET druckt nie


# --- Seitenbereich -------------------------------------------------------------------------------
async def test_no_page_range_by_default(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    await client.post(URL, data=PDF)
    assert "page-ranges" not in printer.prints[-1]["job"]  # alle Seiten
    await client.post(f"{URL}?pages=", data=PDF)  # leere Angabe (Kurzbefehl ohne Eingabe) = ebenfalls alle
    assert "page-ranges" not in printer.prints[-1]["job"]


@pytest.mark.parametrize(
    ("pages", "sent", "shown"),
    [
        ("2-3", [(2, 3)], "Seiten 2-3"),
        ("1-3,5", [(1, 3), (5, 5)], "Seiten 1-3, 5"),
        ("5,1-3", [(1, 3), (5, 5)], "Seiten 1-3, 5"),
        ("1-3,2-5", [(1, 5)], "Seiten 1-5"),
        ("4", [(4, 4)], "Seiten 4"),
    ],
)
async def test_page_range_is_sent_to_printer(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth,
    pages: str, sent: list, shown: str,
) -> None:
    client = await hass_client_no_auth()
    resp = await client.post(URL, params={"pages": pages}, data=PDF)
    body = await resp.json()
    assert resp.status == 200, body
    assert printer.prints[-1]["job"]["page-ranges"] == sent
    assert shown in body["message"]
    assert "2 Seiten" not in body["message"]  # statt der Gesamtseitenzahl steht der gewählte Bereich


async def test_page_range_combines_with_other_options(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    resp = await client.post(f"{URL}?pages=2-3&color=color&sides=two&copies=2", data=PDF)
    assert resp.status == 200
    job = printer.prints[-1]["job"]
    assert (job["page-ranges"], job["copies"], job["sides"], job["print-color-mode"]) == (
        [(2, 3)], [2], ["two-sided-long-edge"], ["color"],
    )
    assert "Seiten 2-3, Farbe, beidseitig, 2×" in (await resp.json())["message"]


@pytest.mark.parametrize("pages", ["0", "3-1", "abc", "1-", "1;3", "1,3,5,7,9,11,13,15,17,19,21"])
async def test_invalid_page_range_is_rejected_before_printing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth, pages: str
) -> None:
    client = await hass_client_no_auth()
    resp = await client.post(URL, params={"pages": pages}, data=PDF)
    assert resp.status == 400 and "1-3,5" in (await resp.json())["message"]
    assert printer.prints == []


async def test_page_range_only_for_pdf(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    resp = await client.post(f"{URL}?pages=1-2", data=JPEG)
    assert resp.status == 400 and "PDF" in (await resp.json())["message"]
    assert printer.prints == []


async def test_page_range_in_event_and_sensor(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    from homeassistant.helpers import entity_registry as er

    client = await hass_client_no_auth()
    events = async_capture_events(hass, EVENT_PRINT_JOB)
    await client.post(f"{URL}?name=Anna&pages=2-3,7", data=PDF)
    await hass.async_block_till_done()
    assert events[0].data["page_range"] == "2-3, 7"
    last_id = er.async_get(hass).async_get_entity_id("sensor", "ipp_print", f"{setup_integration.entry_id}_last_job")
    assert hass.states.get(last_id).attributes["page_range"] == "2-3, 7"


async def test_typographic_dash_from_ios_is_understood(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    client = await hass_client_no_auth()
    resp = await client.post(URL, params={"pages": "1\u20133"}, data=PDF)
    assert resp.status == 200
    assert printer.prints[-1]["job"]["page-ranges"] == [(1, 3)]
