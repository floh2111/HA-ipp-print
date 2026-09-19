# IPP Print für Home Assistant

Drucken auf einem **Netzwerkdrucker mit IPP** (fast alle aktuellen Drucker) – direkt aus dem **Teilen-Menü des
iPhones**, von unterwegs und ohne VPN. Die Datei geht über Home Assistant, das ohnehin bei dir zu Hause läuft und
den Drucker im Heimnetz erreicht. Der Drucker selbst wird dabei nie ins Internet gestellt.

```
iPhone: Datei → Teilen → „Drucken“ ──► Webhook in Home Assistant ──► IPP ──► Drucker im Heimnetz
```

*English: custom integration that turns Home Assistant into a print gateway for any IPP network printer. Send a
PDF/JPEG to a secret webhook (e.g. from an iOS Shortcut in the share sheet) and it is printed – defaults: black &
white, single-sided, 1 copy; colour, duplex and copies on request. No cloud, no VPN, no extra packages. The setup
instructions below are in German; UI strings are available in German and English.*

## Was ist enthalten?

| Was | Beschreibung |
|---|---|
| **Webhook** | Nimmt PDF- und JPEG-Dateien entgegen und druckt sie. Standard: **Schwarzweiß, einseitig, 1 Kopie**. Farbe, beidseitig und Kopien (1–20) per Angabe in der Adresse. |
| **Sensor** „Zustand“ | Bereit / Druckt / Gestoppt (alle 5 Minuten abgefragt). |
| **Sensor** „Letzter Druck“ | Zeitpunkt; Attribute: Name, Datei, Kopien, Farbe, Seiten, Seitenzahl, Auftragsnummer. |
| **Ereignis** `ipp_print_job` | Nach jedem Druck – z. B. um dem anderen eine Nachricht zu schicken („Anna hat 3 Seiten gedruckt“). |

Was gedruckt werden kann: **PDF** und **JPEG** (auch TIFF und PostScript). Alles andere (Word, Pages, Fotos im
HEIC-Format, Webseiten …) wandelt der iPhone-Kurzbefehl unten vorher in ein PDF um.

## Installation

### Über HACS (empfohlen)

1. In Home Assistant **HACS → ⋮ → Benutzerdefinierte Repositories** öffnen.
2. `https://github.com/floh2111/HA-ipp-print` mit Kategorie **Integration** hinzufügen.
3. „IPP Print“ suchen, **Herunterladen** und Home Assistant neu starten.

### Manuell

Den Ordner `custom_components/ipp_print` in das `custom_components`-Verzeichnis kopieren und neu starten.

## Einrichtung

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → IPP Print**.
2. Die Adresse des Druckers eintragen – am besten die **IP-Adresse** (z. B. `192.168.1.20`). Namen mit `.local`
   werden in Home Assistant oft nicht aufgelöst. Gib dem Drucker im Router eine **feste Adresse**, sonst ändert
   sie sich irgendwann. Die IP steht im Bedienfeld oder in der Weboberfläche des Druckers; unter macOS zeigt
   `ippfind` die Drucker im Netz.
3. Danach erscheint eine Benachrichtigung mit deiner **Webhook-Adresse**. Sie steht auch jederzeit unter
   *Geräte & Dienste → IPP Print → Konfigurieren*. Sie sieht so aus:
   `https://<deine-ha-adresse>/api/webhook/<langes-geheimnis>`

> **Wichtig:** Damit die Adresse von außen funktioniert, muss in Home Assistant unter *Einstellungen → System →
> Netzwerk* die **externe Adresse** stehen (z. B. Cloudflare-Tunnel). Steht dort nichts, zeigt Home Assistant eine
> interne Adresse an – dann setzt du vor `/api/webhook/…` einfach deine externe Adresse ein.

Zum Prüfen die Adresse im Browser öffnen: Es kommt eine Meldung wie „Drucker bereit (…, idle)“. Gedruckt wird
dabei nichts.

## Kurzbefehl auf dem iPhone („Teilen → Drucken“)

Die App **Kurzbefehle** neu öffnen, **+**, Name: **Drucken**. Dann oben auf ⓘ (Einstellungen) tippen und
**„Im Share Sheet anzeigen“** einschalten. Als akzeptierte Typen mindestens *Bilder, PDFs, Dokumente* und *Safari-
Webseiten* lassen, „Wenn kein Input vorhanden“ auf **„Fortfahren“**/„Fragen“. Die Bezeichnungen der Aktionen können je
nach iOS-Version leicht abweichen.

Aktionen, in dieser Reihenfolge:

1. **Aus Menü auswählen** – Eingabe „Wie drucken?“ mit diesen Einträgen. In jedem Zweig steht eine Aktion
   **Text** und danach **Variable festlegen** (Name `Optionen`):

   | Menüeintrag | Text |
   |---|---|
   | Schwarzweiß, einseitig (Standard) | `color=bw&sides=one&copies=1` |
   | Farbe | `color=color&sides=one&copies=1` |
   | Beidseitig | `color=bw&sides=two&copies=1` |
   | Mehr … | erst **Nach Eingabe fragen** (Zahl, Standard 1, „Kopien“) und ein Menü „Farbe?“/„Beidseitig?“, dann Text `color=…&sides=…&copies=` + die Zahl |

2. **PDF erstellen** – aus der **Kurzbefehl-Eingabe**. (Ist die Eingabe schon ein PDF, bleibt es eins; Fotos, Pages,
   Word und Webseiten werden umgewandelt.)
3. **Text** mit dem Namen der Datei und **URL-codieren** darauf (damit Umlaute und Leerzeichen keine Probleme
   machen) → Variable `Dateiname`.
4. **Inhalt von URL abrufen**:
   - URL: `https://<deine-ha-adresse>/api/webhook/<geheimnis>?name=Florian&filename=` + `Dateiname` + `&` + `Optionen`
   - Methode **POST**, Header `Content-Type` = `application/pdf`
   - Anfragetext: **Datei** → das Ergebnis von *PDF erstellen*
5. **Wert für „message“ im Wörterbuch abrufen** (aus dem Ergebnis) → **Mitteilung anzeigen**. So siehst du „Gedruckt: …
   (2 Seiten, Schwarzweiß, einseitig)“ oder eine verständliche Fehlermeldung („Der Drucker ist nicht erreichbar …“).

Beim ersten Ausführen fragt iOS, ob der Kurzbefehl Daten an die Domain senden darf – **Immer erlauben**.
Dein Partner legt denselben Kurzbefehl auf dem eigenen iPhone an (und ändert `name=` auf den eigenen Namen).

**Android:** Mit der kostenlosen App *HTTP Shortcuts* geht dasselbe: ein Shortcut mit „Als Teilen-Ziel
verwenden“, Methode POST, Datei als Anfragetext, dieselbe Adresse und dieselben Angaben.

## Angaben in der Adresse (Query-Parameter)

| Angabe | Werte | Standard |
|---|---|---|
| `color` | `bw` (Schwarzweiß), `color` | `bw` |
| `sides` | `one` (einseitig), `two` (beidseitig, lange Kante), `two-short` (kurze Kante) | `one` |
| `copies` | 1–20 | `1` |
| `name` | wer druckt (erscheint am Drucker/im Ereignis) | „Unbekannt“ |
| `filename` | Name des Auftrags | „Dokument“ |

Antwort als JSON: `{"ok": true, "message": "Gedruckt: … ", "job_id": 42, "pages": 2}`; bei Fehlern `ok: false`
mit HTTP-Status 400 (ungültige Angabe), 413 (zu groß), 415 (Dateityp), 429 (zu viele Aufträge), 502/504 (Drucker
lehnt ab bzw. ist nicht erreichbar).

## Ereignis für Automationen

```yaml
automation:
  - alias: "Gedruckt: Bescheid geben"
    triggers:
      - trigger: event
        event_type: ipp_print_job
    actions:
      - action: notify.notify
        data:
          message: >-
            {{ trigger.event.data.name }} hat „{{ trigger.event.data.filename }}“ gedruckt
            ({{ trigger.event.data.copies }}×{{ ', Farbe' if trigger.event.data.color else '' }}).
```

## Sicherheit

- **Die Webhook-Adresse ist ein Passwort:** Wer sie kennt, kann drucken. Sie ist lang und zufällig, aber gib sie nicht
  weiter und poste sie nirgends. Unter *Konfigurieren* kannst du jederzeit eine **neue erzeugen** – die alte ist dann sofort
  ungültig (in den Kurzbefehlen neu eintragen).
- Schutz vor Missbrauch: höchstens **20 Aufträge pro Stunde und 60 pro Tag**, **15 MB** pro Datei, **20 Kopien** pro
  Auftrag. Fehlgeschlagene Versuche zählen nicht mit.
- Der Drucker bleibt im Heimnetz. Er wird nur von Home Assistant angesprochen; die meisten Netzwerkdrucker verlangen
  keine Anmeldung und dürfen deshalb **nie** direkt ins Internet gestellt werden.
- Die Dateien werden nicht gespeichert: Sie gehen im Arbeitsspeicher an den Drucker und sind danach weg.
- Fremde Dateien: Der Drucker rendert jede Datei selbst. Gib die Adresse deshalb nur an Menschen, denen du vertraust.

## Grenzen

- Home Assistant nimmt Uploads bis 16 MB an; die Integration begrenzt auf 15 MB.
- Ist Home Assistant oder der Drucker aus, wird nicht gedruckt (der Kurzbefehl zeigt dann eine Fehlermeldung, es geht
  nichts verloren).
- Die Seitenzahl in der Antwort wird ohne PDF-Bibliothek ermittelt und kann bei komprimierten PDFs fehlen; gedruckt
  wird trotzdem korrekt.
- Kein Scannen, keine Papierfach-Auswahl, keine Druckersteuerung – nur Drucken.

## Entwicklung

```bash
pip install -r requirements_test.txt
pytest
```

Die Tests laufen gegen einen Fake-Drucker, der die IPP-Anfragen entschlüsselt (Optionen, Dokument, Fehler) und prüfen
Kodierung (mit festen Byte-Vergleichen), Einrichtung, Webhook, Missbrauchsschutz und Sensoren.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
