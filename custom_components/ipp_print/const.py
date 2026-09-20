"""Konstanten der IPP-Print-Integration."""

from __future__ import annotations

DOMAIN = "ipp_print"
# Muss mit "version" in manifest.json übereinstimmen (ein Test prüft das).
VERSION = "0.2.0"

CONF_PRINTER_URL = "printer_url"
CONF_WEBHOOK_ID = "webhook_id"
CONF_ANNOUNCED = "announced"

# Vorgaben, wenn die Anfrage nichts anderes verlangt: Schwarzweiß, einseitig, 1 Kopie
DEFAULT_COPIES = 1
MAX_COPIES = 20

# Home Assistant nimmt Anfragen bis 16 MB an - etwas Luft lassen
MAX_BYTES = 15 * 1024 * 1024

# Schutz, falls die Webhook-Adresse in falsche Hände gerät
MAX_JOBS_PER_HOUR = 20
MAX_JOBS_PER_DAY = 60

STATUS_INTERVAL_SECONDS = 300

EVENT_PRINT_JOB = f"{DOMAIN}_job"
SIGNAL_JOB = f"{DOMAIN}_job_{{}}"  # .format(entry_id)
