"""Konstanten der IPP-Print-Integration."""

from __future__ import annotations

DOMAIN = "ipp_print"
# Muss mit "version" in manifest.json übereinstimmen (ein Test prüft das).
VERSION = "0.3.1"

CONF_PRINTER_URL = "printer_url"
CONF_WEBHOOK_ID = "webhook_id"
CONF_ANNOUNCED = "announced"
CONF_NOTIFY_TARGET = "notify_target"  # optionale notify-Entität (z. B. notify.mobile_app_iphone) für Push-Meldungen
CONF_NOTIFY_BY_NAME = "notify_by_name"  # optional: Push-Ziel je nach "name=" aus dem Kurzbefehl (mehrzeiliger Text)

# Vorgaben, wenn die Anfrage nichts anderes verlangt: Schwarzweiß, einseitig, 1 Kopie
DEFAULT_COPIES = 1
MAX_COPIES = 20

# Home Assistant nimmt Anfragen bis 16 MB an - etwas Luft lassen
MAX_BYTES = 15 * 1024 * 1024

# Schutz, falls die Webhook-Adresse in falsche Hände gerät
MAX_JOBS_PER_HOUR = 20
MAX_JOBS_PER_DAY = 60

STATUS_INTERVAL_SECONDS = 300

EVENT_PRINT_JOB = f"{DOMAIN}_job"  # beim Übergeben an den Drucker (wie bisher)
EVENT_PRINT_JOB_RESULT = f"{DOMAIN}_job_result"  # Zwischenstand (z. B. "wartet, kein Papier") und Endergebnis
SIGNAL_JOB = f"{DOMAIN}_job_{{}}"  # .format(entry_id)

# Verfolgung eines laufenden Auftrags nach der Übergabe: wie oft und wie lange beim Drucker nachgefragt wird, bis
# wir aufgeben (z. B. wenn niemand fehlendes Papier nachlegt). Rein lokales Netzwerk - die Häufigkeit ist unkritisch.
# Als Anzahl Versuche (nicht als Uhrzeit) gezählt, damit es unabhängig von der Systemuhr sauber funktioniert.
JOB_POLL_INTERVAL_SECONDS = 4
JOB_POLL_TIMEOUT_SECONDS = 900
JOB_POLL_MAX_ATTEMPTS = max(1, JOB_POLL_TIMEOUT_SECONDS // JOB_POLL_INTERVAL_SECONDS)
