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
| **Webhook** | Nimmt PDF- und JPEG-Dateien entgegen und druckt sie. Standard: **Schwarzweiß, einseitig, 1 Kopie**. Farbe, beidseitig, Kopien (1–20) und ein **Seitenbereich** (nur bestimmte Seiten eines PDFs) per Angabe in der Adresse. |
| **Sensor** „Zustand“ | Bereit / Druckt / Gestoppt (alle 5 Minuten abgefragt). |
| **Sensor** „Letzter Druck“ | Zeitpunkt; Attribute: Name, Datei, Kopien, Farbe, Seiten (beidseitig oder nicht), Seitenzahl, Seitenbereich, Auftragsnummer, Ergebnis. |
| **Sensor** „Ergebnis“ | Was aus dem letzten Auftrag NACH der Übergabe geworden ist: übergeben, wartet (z. B. kein Papier), gedruckt, fehlgeschlagen oder unklar. |
| **Push-Benachrichtigung** (optional) | Erfolg, „kein Papier“ & Co. direkt aufs Handy – wenn ein Empfänger eingerichtet ist (siehe unten). |
| **Ereignis** `ipp_print_job` | Beim Übergeben an den Drucker – z. B. um dem anderen eine Nachricht zu schicken („Anna hat 3 Seiten gedruckt“). |
| **Ereignis** `ipp_print_job_result` | Zwischenstände und Endergebnis NACH der Übergabe (siehe „Rückmeldung nach dem Drucken“). |

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

4. Optional, unter *Konfigurieren*: **Push-Benachrichtigung an** und/oder **Push je nach Namen** – wer per
   Mitteilung erfährt, wie ein Druckauftrag ausgegangen ist (siehe nächster Abschnitt). Leer lassen, wenn das
   nicht gebraucht wird.

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
   | Mehr … | zwei Aktionen **Nach Eingabe fragen**: (1) *Zahl* „Kopien“, Standard `1`; (2) *Text* „Welche Seiten? (leer = alle, z. B. 1-3,5)“. Dann Text `color=bw&sides=one&copies=` + Variable *Kopien* + `&pages=` + Variable *Seiten*, alles **direkt hintereinander**, ohne Leerzeichen und ohne Pluszeichen |

2. **PDF erstellen** – aus der **Kurzbefehl-Eingabe** (nicht aus dem „Menüergebnis“, das ist nur der Optionstext). (Ist die Eingabe schon ein PDF, bleibt es eins; Fotos, Pages,
   Word und Webseiten werden umgewandelt.)
3. **Inhalt von URL abrufen**:
   - URL: `https://<deine-ha-adresse>/api/webhook/<geheimnis>?name=Florian&` und **direkt dahinter** (ohne Leerzeichen und **ohne Pluszeichen**) die Variable `Optionen`. Sonst wird die Farbe nicht erkannt und immer Schwarzweiß gedruckt.
   - Methode **POST**, Header `Content-Type` = `application/pdf`
   - Anfragetext: **Datei** → das Ergebnis von *PDF erstellen*
4. **Wert für „message“ im Wörterbuch abrufen** (aus dem Ergebnis) → **Mitteilung anzeigen**. So siehst du „Gedruckt: …
   (2 Seiten, Schwarzweiß, einseitig)“ oder eine verständliche Fehlermeldung („Der Drucker ist nicht erreichbar …“).

Beim ersten Ausführen fragt iOS, ob der Kurzbefehl Daten an die Domain senden darf – **Immer erlauben**.
Dein Partner legt denselben Kurzbefehl auf dem eigenen iPhone an (und ändert `name=` auf den eigenen Namen).

**Android:** Mit der kostenlosen App *HTTP Shortcuts* geht dasselbe: ein Shortcut mit „Als Teilen-Ziel
verwenden“, Methode POST, Datei als Anfragetext, dieselbe Adresse und dieselben Angaben.

## Rückmeldung nach dem Drucken

Dass der Drucker den Auftrag **angenommen** hat, heißt noch nicht, dass er auch fertig gedruckt wird – geht z. B.
mitten im Druck das Papier aus, hält er einfach an. IPP Print fragt danach von sich aus beim Drucker nach (alle paar
Sekunden, bis zu 15 Minuten lang) und meldet:

- **wartet** – der Drucker ist angehalten, z. B. „Kein Papier“, „Papierstau“ oder „Toner leer“ (Text kommt vom
  Drucker, unbekannte Meldungen werden möglichst verständlich wiedergegeben statt verschluckt).
- **gedruckt** – fertig.
- **fehlgeschlagen** – abgebrochen, mit Grund, falls der Drucker einen mitliefert.
- **unklar** – der Drucker kennt den Auftrag nicht mehr (meist harmlos: er hat ihn schon abgeschlossen und
  vergessen) oder es hat sich 15 Minuten lang nichts getan.

Das siehst du auf mehreren Wegen, auch kombiniert:

1. **Push-Benachrichtigung**, wenn unter *Konfigurieren* ein Ziel eingetragen ist (eine `notify`-Entität, z. B. die
   deiner Handy-App „Home Assistant“ – in *Entwicklerwerkzeuge → Zustände* nach `notify.` suchen, um den Namen zu
   finden). Dann kommen alle vier Ergebnisse als Mitteilung.
2. **Push je nach Namen**: eigenes Ziel für jede Person, die per `name=` im Kurzbefehl druckt – eine Zeile pro
   Person, z. B.

   ```
   Florian: notify.mobile_app_iphone_von_florian
   Deborah: notify.mobile_app_iphone_von_deborah
   ```

   So bekommt jede:r die Mitteilung zum eigenen Druckauftrag aufs eigene Handy (Groß-/Kleinschreibung egal). Es
   geht auch andersherum – trägst du bei „Florian“ das Handy deiner Partnerin ein, wird sie über deine
   Druckaufträge informiert. Ein Name ohne passende Zeile fällt auf „Push-Benachrichtigung an“ zurück, falls dort
   etwas eingetragen ist.
3. **Ohne** passendes Ziel zeigt Home Assistant bei „wartet“ und „fehlgeschlagen“ trotzdem eine eigene Meldung
   (Glocke oben rechts) – ein normaler Druck bleibt dabei still, um nicht bei jedem Ausdruck zu nerven.
4. **Sensor „Ergebnis“** und die Attribute `status`/`status_reason` am Sensor „Letzter Druck“, für ein Dashboard
   oder eigene Automationen.

## Angaben in der Adresse (Query-Parameter)

| Angabe | Werte | Standard |
|---|---|---|
| `color` | `bw` (Schwarzweiß), `color` | `bw` |
| `sides` | `one` (einseitig), `two` (beidseitig, lange Kante), `two-short` (kurze Kante) | `one` |
| `copies` | 1–20 | `1` |
| `pages` | Seitenbereich, nur bei PDF: `2-3`, `1-3,5`, `7`. Leer = alle Seiten | alle |
| `name` | wer druckt (erscheint am Drucker/im Ereignis) | „Unbekannt“ |
| `filename` | Name des Auftrags | „Dokument“ |

**Seitenbereich:** Angaben wie `1-3,5` (Seitenzahlen ab 1). Leerzeichen und die typografischen Striche, die iOS gern
einsetzt (–), werden verstanden; Überlappendes wird zusammengefasst (`1-3,2-5` → `1-5`). Er gilt nur für PDFs – der
Kurzbefehl wandelt ohnehin alles in ein PDF um. Ist die Angabe ungültig, wird nicht gedruckt und die Meldung sagt es.

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

Für das **Ergebnis nach dem Drucken** (siehe oben) gibt es zusätzlich `ipp_print_job_result`, mit `status`
(`waiting`/`done`/`failed`/`unclear`) und `reason` (Text oder `null`) – nützlich, wenn mehr als eine Push-
Benachrichtigung passieren soll, z. B. zusätzlich eine Lampe blinken lassen, wenn kein Papier mehr da ist:

```yaml
automation:
  - alias: "Kein Papier mehr: Lampe blinken lassen"
    triggers:
      - trigger: event
        event_type: ipp_print_job_result
        event_data:
          status: waiting
    actions:
      - action: light.turn_on
        target:
          entity_id: light.buero
        data:
          flash: long
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
