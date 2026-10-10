# 02 Integrations-Checkliste

Eine Integration gilt als fertig, wenn jeder Punkt abgehakt ist und die Abnahme in Staging mit
echten Zustellungen gelaufen ist. Technische Details der Endpunkte stehen in
[`docs/integrationen.md`](../integrationen.md).

## A. Zugänge und Umgebungen

- [ ] Staging-Umgebung des Backends erreichbar unter HTTPS mit gültigem Zertifikat (telli ruft nur HTTPS auf)
- [ ] Sandbox oder Testportal des CRM (HubSpot-Testaccount, Salesforce-Sandbox) mit denselben Eigenschaften wie Produktion
- [ ] Testkalender mit Slots in den nächsten fünf Werktagen
- [ ] Testkontakte mit echten, vom Team kontrollierten Telefonnummern (nie Kundennummern im Test)
- [ ] Geheimnisse angelegt und nur in `.env` oder im Secret Store, nie im Repository: `TELLI_API_KEY`, `TELLI_WEBHOOK_SECRET`, `TOOL_SECRET`, `KUNDE_WEBHOOK_SECRET`, CRM-Token
- [ ] `GET /health` meldet `geheimnisse_vollstaendig: true` (welche fehlen, zeigt `GET /metrics`); Produktionsstart mit `UMGEBUNG=prod`

## B. Konfiguration bei telli

| Schritt | Wo | Prüfung |
| --- | --- | --- |
| Contact properties anlegen: `quelle` (string), `plz` (string), `eigentuemer` (boolean), `gebaeudetyp` (select), `dachflaeche_m2` (number), `jahresverbrauch_kwh` (number) | Settings → Contact properties | `python -m integrations push --trocken` zeigt die Schlüssel, die gesendet werden; nicht angelegte werden stillschweigend ignoriert |
| Agent anlegen, `agent_id` notieren | Agents | `TELLI_AGENT_ID` gesetzt |
| Outcomes des Agenten definieren (siehe unten) | Agent → Analysis / Outcomes | Schlüssel stimmen mit `OUTCOME_SCHLUESSEL` überein |
| Custom Tools anlegen | Agent → Tools | `python -m integrations definitions --url <Staging-URL>` liefert je Tool Name, Beschreibung, Methode, URL, Timeout, Header (Secret), Body (System Variables und LLM-Parameter) |
| Custom Calendar eintragen: `/calendar/available`, `/calendar/book` | Integrations → Custom Calendar | Testbuchung aus dem Portal erscheint in der Tabelle `appointments` |
| Contact Lookup Webhook: `/webhooks/contact-lookup`, Timeout 2 s | Settings → Integrations | Testanruf von bekannter Nummer wird mit Namen begrüßt |
| Webhook-Endpunkt `/webhooks/telli` für `call_ended`, Signaturgeheimnis kopieren | Webhook configuration | echte Testzustellung aus dem Portal wird mit `verarbeitet` beantwortet, zweite Zustellung mit `duplikat` |
| Dialing window, Wiederholungsversuche, Anzeigenummer | Account settings | entspricht Scoping Abschnitt 9 |

### Outcomes des Agenten

Der Agent liefert nach jedem Gespräch diese Outcomes; die Nachbereitung liest sie und prüft sie
gegen das Backend:

| Schlüssel | Typ | Werte |
| --- | --- | --- |
| `ergebnis` | category | `termin_gebucht`, `disqualifiziert`, `rueckruf_vereinbart`, `opt_out`, `falsche_person`, `kein_interesse`, `sonstiges` |
| `rueckruf_zeitpunkt` | string | ISO 8601 wenn möglich, sonst Freitext (dann Standard +24 h) |
| `eigentuemer` | boolean | |
| `gebaeudetyp` | category | `einfamilienhaus`, `doppelhaushaelfte`, `reihenhaus`, `mehrfamilienhaus`, `gewerbe`, `sonstiges` |
| `dachflaeche_m2` | number | 5 bis 2000 |
| `jahresverbrauch_kwh` | number | 500 bis 100000 |

## C. CRM-Anbindung

- [ ] Richtung CRM → Plattform: Welche Leads (Status, Quelle, Alter) werden angerufen, in welchem Takt läuft der Push (`PUSH_TAKT_S`, Ziel Speed-to-Lead < 5 min), löst das CRM `POST /push` aus?
- [ ] Richtung Plattform → CRM: Feldmapping im Workshop festgelegt und in `Feldmapping` eingetragen (Eigenschaften existieren im Portal, Lead-Status-Werte passen zum Vertriebsprozess)
- [ ] Rückruf-Aufgaben: Wer bekommt sie, mit welcher Fälligkeit, in welcher Queue?
- [ ] Sperrliste: Opt-outs landen in der Sperrliste des Backends und in der Do-not-call-Liste der Plattform; wer pflegt sie?
- [ ] Doppelte Kontakte (`409 DUPLICATE_EXTERNAL_ID`): Zuordnungsprozess benannt (`zuordnung_offen()`)
- [ ] Rate Limits des CRM bekannt (HubSpot: Limits je Private App), Push-Lauf bleibt darunter

## D. Kalender

- [ ] Entscheidung: native Anbindung der Plattform (Calendly, Cal.com, HubSpot Meetings) oder eigene Kalenderschnittstelle
- [ ] Zeitzone: Slots in Ortszeit gespeichert, Schnittstelle liefert UTC mit `Z`; Testbuchung um 9:00 Ortszeit erscheint im Kalender um 9:00
- [ ] Wie viele Alternativen bietet der Agent an (Standard zwei im Gespräch, vier über die Kalenderschnittstelle)?
- [ ] Doppelbuchungen ausgeschlossen (Test: zwei Leads, derselbe Slot)
- [ ] Stornierung und Verschiebung: Wer tut das, und wird der Slot wieder frei?

## E. Telefonie

- [ ] Anzeigenummer gehört dem Kunden und ist für Rückrufe erreichbar (Contact Lookup greift)
- [ ] Mailbox-Erkennung: Verhalten bei Voicemail (kein Nachrichtenhinterlassen, Wiederholung nach Plan)
- [ ] Anrufzeiten nach Scoping, Feiertage berücksichtigt
- [ ] Testanruf mit allen Edge Cases per Telefon, nicht nur im Simulator

## F. Sicherheit und Datenschutz

- [ ] Signaturprüfung aktiv auf allen eingehenden Endpunkten (`/health` ohne fehlende Geheimnisse)
- [ ] Geheimnisrotation: Vorgehen beschrieben (Svix erlaubt mehrere Signaturen während der Rotation)
- [ ] Auftragsverarbeitungsvertrag mit der Plattform, Drittlandtransfer geprüft
- [ ] Aufzeichnung: nur mit Einwilligung; `recording_url` läuft nach einer Stunde ab, wird nicht gespeichert
- [ ] Aufbewahrungsfristen für Transkripte in `calls` und Notizen im CRM festgelegt
- [ ] Logs enthalten keine Transkripte und keine Geheimnisse

## G. Abnahme in Staging (alle Haken vor dem UAT)

- [ ] `python -m pytest -q` grün, `python -m integrations demo` läuft durch
- [ ] Lead-Push: ein Testlead erscheint als Kontakt mit Eigenschaften und landet im Dialer
- [ ] Custom Tool: `update_lead` mit ungültigem Wert liefert HTTP 200 mit Fehlertext, der Agent korrigiert sich im Testanruf
- [ ] Kalender: `/available` leer vor Qualifizierung, gefüllt danach; `/book` und `book_appointment` zweimal mit demselben Slot ergeben eine Buchung, ein anderer Slot eine Umbuchung
- [ ] `call_ended`: echte Zustellung aus dem Webhook-Portal verarbeitet; Hülle (`event`, `call`, `contact`) stimmt mit `modelle.py` überein, sonst `CallEndedEreignis.lesen` anpassen
- [ ] Ergebnis-Ground-Truth: Testanruf mit behaupteter, aber nicht erfolgter Buchung erzeugt eine Prüfaufgabe
- [ ] Outbox: Kundensystem empfängt signierte Ereignisse; Kundensystem kurz abschalten, Wiederholung durch den Planer nach 5 s, dann 5 min beobachtet (`GET /metrics` → `planer`)
- [ ] Spiegelung: mit `HUBSPOT_TOKEN` erscheint der Testanruf als Notiz und Aufgabe am Kontakt; CRM kurz abschalten, Auftrag bleibt `offen` und wird nachgeholt
- [ ] `GET /metrics`: p95 je Endpunkt unter 300 ms in Staging (telli-Timeout liegt bei 1 bis 10 s, Latenz addiert sich zur Gesprächspause)
