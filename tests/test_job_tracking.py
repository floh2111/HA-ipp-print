"""Verfolgung eines Druckauftrags NACH der Übergabe: Zwischenstände (z. B. kein Papier), Endergebnis,
Push-Benachrichtigung/Meldung in Home Assistant, Sensor "Ergebnis", und Aufräumen beim Entladen."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events, async_fire_time_changed

from homeassistant.components import persistent_notification as pn
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.ipp_print import notify_target_for, parse_notify_by_name
from custom_components.ipp_print.const import (
    CONF_NOTIFY_BY_NAME,
    CONF_NOTIFY_TARGET,
    EVENT_PRINT_JOB_RESULT,
    JOB_POLL_INTERVAL_SECONDS,
)

from .conftest import PDF, WEBHOOK_ID, FakePrinter

URL = f"/api/webhook/{WEBHOOK_ID}"


def _advance(hass: HomeAssistant, seconds: float = JOB_POLL_INTERVAL_SECONDS + 1):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))


async def _print(hass_client_no_auth, params: str = "") -> int:
    client = await hass_client_no_auth()
    resp = await client.post(f"{URL}{params}", data=PDF)
    body = await resp.json()
    assert resp.status == 200, body
    return body["job_id"]


def _result_sensor(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", "ipp_print", f"{entry.entry_id}_last_job_result")
    return hass.states.get(entity_id).state


def _last_job_attrs(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", "ipp_print", f"{entry.entry_id}_last_job")
    return hass.states.get(entity_id).attributes


def _notification(hass: HomeAssistant, entry: MockConfigEntry, job_id: int) -> dict | None:
    """Home Assistant hält aktive "persistent_notification"-Meldungen intern vor - kein hass.states-Eintrag."""
    notification_id = f"ipp_print_{entry.entry_id}_job_{job_id}"
    return pn._async_get_or_create_notifications(hass).get(notification_id)


async def test_result_sensor_starts_unknown(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert _result_sensor(hass, setup_integration) == "unknown"


async def test_successful_job_reaches_done(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    events = async_capture_events(hass, EVENT_PRINT_JOB_RESULT)
    job_id = await _print(hass_client_no_auth, "?filename=Rechnung.pdf")
    printer.job_attribute_sequences[job_id] = [(9, [])]  # sofort "completed"

    _advance(hass)
    await hass.async_block_till_done()

    assert [e.data for e in events] == [
        {"job_id": job_id, "status": "done", "reason": None, "name": "Unbekannt", "filename": "Rechnung.pdf"}
    ]
    assert _result_sensor(hass, setup_integration) == "done"
    assert _last_job_attrs(hass, setup_integration)["status"] == "done"
    assert _last_job_attrs(hass, setup_integration)["status_reason"] is None
    # ohne konfiguriertes Push-Ziel wird für einen glatten Erfolg keine Meldung in Home Assistant angezeigt
    assert _notification(hass, setup_integration, job_id) is None


async def test_out_of_paper_then_resolved(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    """Drucker hält mangels Papier an, jemand legt welches nach, der Auftrag wird fertig - genau der Praxisfall."""
    events = async_capture_events(hass, EVENT_PRINT_JOB_RESULT)
    job_id = await _print(hass_client_no_auth)
    printer.job_attribute_sequences[job_id] = [(6, ["media-empty-warning"]), (9, [])]

    _advance(hass)
    await hass.async_block_till_done()
    assert events[-1].data["status"] == "waiting"
    assert events[-1].data["reason"] == "Kein Papier"
    assert _result_sensor(hass, setup_integration) == "waiting"
    waiting_notification = _notification(hass, setup_integration, job_id)
    assert waiting_notification is not None
    assert "Kein Papier" in waiting_notification["message"]

    _advance(hass)
    await hass.async_block_till_done()
    assert events[-1].data["status"] == "done"
    assert _result_sensor(hass, setup_integration) == "done"
    # die zwischenzeitliche "wartet"-Meldung ist wieder weg, sobald es doch noch geklappt hat
    assert _notification(hass, setup_integration, job_id) is None


async def test_same_reason_is_not_reported_twice(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    events = async_capture_events(hass, EVENT_PRINT_JOB_RESULT)
    job_id = await _print(hass_client_no_auth)
    printer.job_attribute_sequences[job_id] = [
        (6, ["media-empty-warning"]), (6, ["media-empty-warning"]), (6, ["media-empty-warning"]), (9, []),
    ]
    for _ in range(3):
        _advance(hass)
        await hass.async_block_till_done()
    waiting_events = [e for e in events if e.data["status"] == "waiting"]
    assert len(waiting_events) == 1  # nicht bei jedem Abfragen erneut, solange sich nichts ändert


async def test_reason_change_is_reported_again(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    events = async_capture_events(hass, EVENT_PRINT_JOB_RESULT)
    job_id = await _print(hass_client_no_auth)
    printer.job_attribute_sequences[job_id] = [
        (6, ["media-empty-warning"]), (6, ["media-jam-warning"]), (9, []),
    ]
    for _ in range(2):
        _advance(hass)
        await hass.async_block_till_done()
    waiting = [e.data["reason"] for e in events if e.data["status"] == "waiting"]
    assert waiting == ["Kein Papier", "Papierstau"]


async def test_aborted_job_is_reported_as_failed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    events = async_capture_events(hass, EVENT_PRINT_JOB_RESULT)
    job_id = await _print(hass_client_no_auth, "?filename=Foto.jpg")
    printer.job_attribute_sequences[job_id] = [(8, ["media-jam-error"])]

    _advance(hass)
    await hass.async_block_till_done()
    assert events[-1].data == {"job_id": job_id, "status": "failed", "reason": "Papierstau", "name": "Unbekannt", "filename": "Foto.jpg"}
    assert _result_sensor(hass, setup_integration) == "failed"
    notif = _notification(hass, setup_integration, job_id)
    assert notif is not None and "Papierstau" in notif["message"] and "fehlgeschlagen" in notif["message"]


async def test_canceled_job_without_reason_still_gets_a_message(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    events = async_capture_events(hass, EVENT_PRINT_JOB_RESULT)
    job_id = await _print(hass_client_no_auth)
    printer.job_attribute_sequences[job_id] = [(7, [])]
    _advance(hass)
    await hass.async_block_till_done()
    assert events[-1].data["status"] == "failed"
    assert events[-1].data["reason"] == "Unbekannter Fehler"


async def test_unknown_job_is_reported_as_unclear_without_notification(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    """Drucker kennt den Auftrag nicht (mehr) - wie beim echten Kyocera für eine erfundene Nummer gemessen (0x0400)."""
    events = async_capture_events(hass, EVENT_PRINT_JOB_RESULT)
    job_id = await _print(hass_client_no_auth)
    # kein Eintrag in job_attribute_sequences -> FakePrinter antwortet mit 0x0400, wie der echte Drucker

    _advance(hass)
    await hass.async_block_till_done()
    assert events[-1].data["status"] == "unclear"
    assert _result_sensor(hass, setup_integration) == "unclear"
    assert _notification(hass, setup_integration, job_id) is None


async def test_timeout_gives_up_after_max_duration(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth, monkeypatch
) -> None:
    """Bleibt der Auftrag ewig 'processing' (niemand legt Papier nach), wird irgendwann aufgegeben.

    Die Anzahl der Versuche wird für den Test klein gehalten, statt wirklich JOB_POLL_MAX_ATTEMPTS (± 225) mal
    die Uhr vorzuspulen - Home Assistants Zeitsimulation ersetzt bei sich wiederholenden Intervall-Timern ohnehin
    nicht die tatsächliche Systemzeit (nur bei "point in time"-Timern), zählen ist hier deshalb auch robuster.
    """
    import custom_components.ipp_print as ipp_print_module

    monkeypatch.setattr(ipp_print_module, "JOB_POLL_MAX_ATTEMPTS", 2)
    events = async_capture_events(hass, EVENT_PRINT_JOB_RESULT)
    job_id = await _print(hass_client_no_auth)
    printer.job_attribute_sequences[job_id] = [(5, [])]  # bleibt für immer "processing"

    _advance(hass)
    await hass.async_block_till_done()
    assert events == [], "vor Erreichen der Höchstzahl an Versuchen noch kein Ergebnis"

    _advance(hass)
    await hass.async_block_till_done()
    assert events[-1].data["status"] == "unclear"
    assert _result_sensor(hass, setup_integration) == "unclear"

    # danach ist die Verfolgung wirklich beendet: ein weiterer Tick ändert nichts mehr
    before = len(events)
    _advance(hass)
    await hass.async_block_till_done()
    assert len(events) == before


async def test_printer_temporarily_unreachable_during_tracking_is_retried(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    events = async_capture_events(hass, EVENT_PRINT_JOB_RESULT)
    job_id = await _print(hass_client_no_auth)
    printer.job_attribute_sequences[job_id] = ["down", (9, [])]

    _advance(hass)
    await hass.async_block_till_done()
    assert events == []  # kurzer Aussetzer meldet noch kein Ergebnis

    _advance(hass)
    await hass.async_block_till_done()
    assert events[-1].data["status"] == "done"  # beim nächsten Versuch klappt es wieder


async def test_second_job_does_not_overwrite_first_jobs_late_result(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    """Zwei Aufträge kurz hintereinander: der ältere darf, wenn er endlich fertig ist, nicht den Sensor des
    inzwischen neueren Auftrags überschreiben."""
    job_a = await _print(hass_client_no_auth, "?filename=A.pdf")
    printer.job_attribute_sequences[job_a] = [(6, ["media-empty-warning"])] * 3 + [(9, [])]
    job_b = await _print(hass_client_no_auth, "?filename=B.pdf")
    printer.job_attribute_sequences[job_b] = [(9, [])]
    assert job_b != job_a

    _advance(hass)
    await hass.async_block_till_done()
    # "last" ist jetzt B (der zuletzt gedruckte) und sofort fertig
    assert _last_job_attrs(hass, setup_integration)["filename"] == "B.pdf"
    assert _last_job_attrs(hass, setup_integration)["status"] == "done"

    # A wird im Hintergrund weiterverfolgt und irgendwann auch fertig - darf B's Ergebnis nicht überschreiben
    for _ in range(3):
        _advance(hass)
        await hass.async_block_till_done()
    assert _last_job_attrs(hass, setup_integration)["filename"] == "B.pdf"
    assert _last_job_attrs(hass, setup_integration)["status"] == "done"


async def test_unloading_stops_tracking(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    job_id = await _print(hass_client_no_auth)
    printer.job_attribute_sequences[job_id] = [(5, [])] * 100  # würde ohne Abbruch ewig weiterlaufen

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    calls_before = len(printer.requests)
    _advance(hass, 3600)  # weit über die normale Verfolgungsdauer hinaus
    await hass.async_block_till_done()
    assert len(printer.requests) == calls_before  # keine weiteren Abfragen nach dem Entladen


# --- Push-Benachrichtigung über ein konfiguriertes notify-Ziel --------------------------------------------------
async def test_notify_target_receives_all_outcomes(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    calls = []

    async def fake_send_message(call):
        calls.append({"entity_id": call.data.get("entity_id"), "message": call.data.get("message")})

    hass.services.async_register("notify", "send_message", fake_send_message)
    hass.config_entries.async_update_entry(setup_integration, options={CONF_NOTIFY_TARGET: "notify.mobile_app_iphone"})
    await hass.async_block_till_done()

    job_id = await _print(hass_client_no_auth, "?filename=Rechnung.pdf")
    printer.job_attribute_sequences[job_id] = [(6, ["media-empty-warning"]), (9, [])]

    _advance(hass)
    await hass.async_block_till_done()
    assert calls[-1]["entity_id"] == "notify.mobile_app_iphone"
    assert "Kein Papier" in calls[-1]["message"] and "Rechnung.pdf" in calls[-1]["message"]

    _advance(hass)
    await hass.async_block_till_done()
    assert "Gedruckt" in calls[-1]["message"] and "Rechnung.pdf" in calls[-1]["message"]

    # mit konfiguriertem Ziel gibt es KEINE zusätzliche Home-Assistant-Meldung (kein Doppelt-Alarm)
    assert _notification(hass, setup_integration, job_id) is None


async def test_notify_target_failure_and_success_both_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    calls = []
    hass.services.async_register("notify", "send_message", lambda call: calls.append(call.data.get("message")))
    hass.config_entries.async_update_entry(setup_integration, options={CONF_NOTIFY_TARGET: "notify.mobile_app_iphone"})
    await hass.async_block_till_done()

    job_id = await _print(hass_client_no_auth)
    printer.job_attribute_sequences[job_id] = [(8, ["toner-empty-error"])]
    _advance(hass)
    await hass.async_block_till_done()
    assert calls and "fehlgeschlagen" in calls[-1] and "Toner leer" in calls[-1]


async def test_notify_target_invalid_entity_does_not_raise(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth, caplog
) -> None:
    """Ein gelöschtes/ungültiges Push-Ziel darf die Verarbeitung nie zum Absturz bringen."""
    hass.config_entries.async_update_entry(setup_integration, options={CONF_NOTIFY_TARGET: "notify.does_not_exist"})
    await hass.async_block_till_done()
    job_id = await _print(hass_client_no_auth)
    printer.job_attribute_sequences[job_id] = [(9, [])]

    _advance(hass)
    await hass.async_block_till_done()  # darf nicht werfen
    assert _result_sensor(hass, setup_integration) == "done"


# --- Optionen: Push-Ziel einrichten -----------------------------------------------------------------------------
async def test_options_flow_sets_notify_target(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"regenerate": False, CONF_NOTIFY_TARGET: "notify.mobile_app_iphone"}
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert setup_integration.options[CONF_NOTIFY_TARGET] == "notify.mobile_app_iphone"


async def test_options_flow_without_notify_target_stores_none(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"regenerate": False})
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert setup_integration.options[CONF_NOTIFY_TARGET] is None


async def test_options_flow_regenerate_keeps_notify_target_field_available(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(setup_integration, options={CONF_NOTIFY_TARGET: "notify.mobile_app_iphone"})
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"regenerate": True, CONF_NOTIFY_TARGET: "notify.mobile_app_iphone"}
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert setup_integration.options[CONF_NOTIFY_TARGET] == "notify.mobile_app_iphone"


# --- Push-Ziel je nach Name im Kurzbefehl ------------------------------------------------------------------------
def test_parse_notify_by_name() -> None:
    text = "Florian: notify.mobile_app_iphone_von_florian\nDeborah: notify.mobile_app_iphone_von_deborah"
    assert parse_notify_by_name(text) == {
        "Florian": "notify.mobile_app_iphone_von_florian",
        "Deborah": "notify.mobile_app_iphone_von_deborah",
    }


def test_parse_notify_by_name_accepts_equals_sign_and_trims_whitespace() -> None:
    assert parse_notify_by_name("  Florian  =  notify.x  ") == {"Florian": "notify.x"}


def test_parse_notify_by_name_skips_blank_lines_and_comments() -> None:
    text = "\n# Wer bekommt was\nFlorian: notify.x\n\n  \n# Deborah: notify.y (noch nicht eingerichtet)\n"
    assert parse_notify_by_name(text) == {"Florian": "notify.x"}


def test_parse_notify_by_name_skips_lines_without_separator() -> None:
    assert parse_notify_by_name("Florian notify.x\nDeborah: notify.y") == {"Deborah": "notify.y"}


def test_parse_notify_by_name_empty_input() -> None:
    assert parse_notify_by_name("") == {}
    assert parse_notify_by_name(None) == {}
    assert parse_notify_by_name("   \n  \n") == {}


def test_parse_notify_by_name_ignores_incomplete_entries() -> None:
    assert parse_notify_by_name("Florian:\n: notify.x\n:") == {}


def test_notify_target_for_matches_case_insensitively() -> None:
    by_name = {"Florian": "notify.florian", "deborah": "notify.deborah"}
    assert notify_target_for(by_name, "Florian", "notify.default") == "notify.florian"
    assert notify_target_for(by_name, "florian", "notify.default") == "notify.florian"
    assert notify_target_for(by_name, "FLORIAN", "notify.default") == "notify.florian"
    assert notify_target_for(by_name, "Deborah", "notify.default") == "notify.deborah"


def test_notify_target_for_falls_back_to_default() -> None:
    by_name = {"Florian": "notify.florian"}
    assert notify_target_for(by_name, "Gast", "notify.default") == "notify.default"
    assert notify_target_for(by_name, None, "notify.default") == "notify.default"
    assert notify_target_for(None, "Florian", "notify.default") == "notify.default"
    assert notify_target_for({}, "Florian", "notify.default") == "notify.default"
    assert notify_target_for(by_name, "Gast", None) is None


async def test_notify_by_name_routes_to_the_matching_person(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    calls = []
    hass.services.async_register("notify", "send_message", lambda call: calls.append(dict(call.data)))
    hass.config_entries.async_update_entry(
        setup_integration,
        options={CONF_NOTIFY_BY_NAME: "Florian: notify.florian\nDeborah: notify.deborah"},
    )
    await hass.async_block_till_done()

    job_florian = await _print(hass_client_no_auth, "?name=Florian&filename=Rechnung.pdf")
    printer.job_attribute_sequences[job_florian] = [(9, [])]
    _advance(hass)
    await hass.async_block_till_done()
    assert calls[-1]["entity_id"] == "notify.florian"
    assert "Rechnung.pdf" in calls[-1]["message"]

    job_deborah = await _print(hass_client_no_auth, "?name=Deborah&filename=Steuer.pdf")
    printer.job_attribute_sequences[job_deborah] = [(9, [])]
    _advance(hass)
    await hass.async_block_till_done()
    assert calls[-1]["entity_id"] == "notify.deborah"
    assert "Steuer.pdf" in calls[-1]["message"]


async def test_notify_by_name_case_insensitive_and_unmatched_name(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    calls = []
    hass.services.async_register("notify", "send_message", lambda call: calls.append(dict(call.data)))
    hass.config_entries.async_update_entry(setup_integration, options={CONF_NOTIFY_BY_NAME: "florian: notify.florian"})
    await hass.async_block_till_done()

    job_id = await _print(hass_client_no_auth, "?name=FLORIAN")
    printer.job_attribute_sequences[job_id] = [(9, [])]
    _advance(hass)
    await hass.async_block_till_done()
    assert calls[-1]["entity_id"] == "notify.florian"  # Groß-/Kleinschreibung des Namens spielt keine Rolle


async def test_notify_by_name_falls_back_to_default_target_for_unknown_name(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    calls = []
    hass.services.async_register("notify", "send_message", lambda call: calls.append(dict(call.data)))
    hass.config_entries.async_update_entry(
        setup_integration,
        options={CONF_NOTIFY_BY_NAME: "Florian: notify.florian", CONF_NOTIFY_TARGET: "notify.default"},
    )
    await hass.async_block_till_done()

    job_id = await _print(hass_client_no_auth, "?name=Gast")
    printer.job_attribute_sequences[job_id] = [(9, [])]
    _advance(hass)
    await hass.async_block_till_done()
    assert calls[-1]["entity_id"] == "notify.default"


async def test_notify_by_name_without_default_falls_back_to_ha_notification_for_unknown_name(
    hass: HomeAssistant, setup_integration: MockConfigEntry, printer: FakePrinter, hass_client_no_auth
) -> None:
    hass.config_entries.async_update_entry(setup_integration, options={CONF_NOTIFY_BY_NAME: "Florian: notify.florian"})
    await hass.async_block_till_done()

    job_id = await _print(hass_client_no_auth, "?name=Gast")
    printer.job_attribute_sequences[job_id] = [(8, ["media-jam-error"])]
    _advance(hass)
    await hass.async_block_till_done()
    notif = _notification(hass, setup_integration, job_id)
    assert notif is not None and "Papierstau" in notif["message"]  # kein Push-Ziel, aber die übliche HA-Meldung


async def test_options_flow_sets_notify_by_name(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"regenerate": False, "notify_by_name": "Florian: notify.florian\nDeborah: notify.deborah"}
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert setup_integration.options["notify_by_name"] == "Florian: notify.florian\nDeborah: notify.deborah"


async def test_options_flow_without_notify_by_name_stores_none(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"regenerate": False})
    await hass.async_block_till_done()
    assert setup_integration.options["notify_by_name"] is None
