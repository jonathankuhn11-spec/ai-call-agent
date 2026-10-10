# telli-Konfiguration: Inbound-Service für Techniker-Termine und Störungen (Telekommunikation)

Kunde: FlinkNet · Agent: Mia · Richtung: inbound · Backend: `https://backend.kunde.example`

Erzeugt aus `integrations/usecases/telko_inbound.py` mit `python -m integrations konfigpaket`. Nicht von Hand ändern: Änderungen gehören in die Use-Case-Definition, sonst driften Prompt und Backend.

## Reihenfolge im Portal

| Schritt | Wo | Datei | Prüfung |
| --- | --- | --- | --- |
| 1 | Settings → Contact properties | `contact_properties.json` | Schlüssel exakt übernehmen; `python -m integrations push --trocken` zeigt, was gesendet wird |
| 2 | Agent anlegen, Stimme und Sprache | `prompt.txt` Abschnitt 1 | KI-Kennzeichnung in der Begrüßung |
| 3 | Agent → Prompt | `prompt.txt` | Variablen `{{...}}` werden im Editor vorgeschlagen |
| 4 | Agent → Tools (je Tool) | `custom_tools.json` | Header `Authorization` als Secret, Body-Felder wie angegeben, Timeout 3 s |
| 5 | Agent → Analysis / Outcomes | `outcomes.json` | Schlüssel stimmen mit `OUTCOME_SCHLUESSEL` der Nachbereitung überein |
| 6 | Integrations → Calendar | `calendar.json` | nur eine Variante aktiv |
| 7 | Webhook configuration | `webhook.json` | Testzustellung aus dem Portal wird mit `verarbeitet` beantwortet |
| 8 | Settings → Integrations → Contact Lookup | `contact_lookup.json` | Testanruf von bekannter Nummer wird mit Namen begrüßt |
| 9 | Testanrufe | `testszenarien.md` | Ergebnis gegen CRM und Kalender (Playbook 03) |

## Anrufverhalten

| Einstellung | Wert |
| --- | --- |
| Weiterleitung | Warm Transfer an die Service-Hotline (Transfer-Ziel: Phone-Number-Property oder feste Nummer) |
| Anruffenster | Inbound rund um die Uhr; außerhalb der Servicezeiten nur Störungen aufnehmen |
| Maximale Dauer | 8 Minuten, danach weiterleiten |
| Mailbox | entfällt (Inbound) |

Backend-Regel: Vor `verify_customer` gibt kein Tool Daten heraus (`VERIFIZIERUNG_PFLICHT`).
Backend-Regel: Termine innerhalb von 24 Stunden sind nicht änderbar (`MINDESTVORLAUF_H`).

Scoping: `docs/use-cases/telko-inbound/SCOPING.md`
