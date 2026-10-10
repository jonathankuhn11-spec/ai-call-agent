# Deployment-Scoping: Inbound-Service für Techniker-Termine und Störungen

Kunde (fiktiv): FlinkNet, regionaler Internet- und Telefonanbieter · Agent: Mia · Richtung: Inbound
Stand: Oktober 2026 · Version 1.0 · Vorlage: [Playbook 01](../../playbook/01-scoping-workshop.md)

Zweites, ausgefülltes Scoping neben [`SCOPING.md`](../../../SCOPING.md) (PV-Outbound). Gleiches Backend,
andere Branche, andere Richtung, andere Geschäftsregeln: Identitätsprüfung vor jeder Auskunft, Mindestvorlauf
für Terminänderungen, Ticketsystem statt CRM-Pipeline, Weiterleitung an Menschen.

## 1. Problem

Rund 40 % der Anrufe auf der Service-Hotline drehen sich um Techniker-Termine: verschieben, absagen, bestätigen,
„kommt der Techniker wirklich?“. Dazu Störungsmeldungen, bei denen der Mitarbeiter nur Daten aufnimmt und ein
Ticket anlegt. Beides bindet geschulte Mitarbeiter mit Routinearbeit, Wartezeiten steigen zu Stoßzeiten auf über
acht Minuten, und kurzfristig verschobene Termine erreichen die Disposition zu spät (Techniker fährt umsonst).

## 2. Ziel und KPIs

| KPI | Definition | Baseline | Ziel Pilot | Quelle |
|---|---|---|---|---|
| Fallabschlussquote | Anteil Anrufe, die der Agent ohne Mitarbeiter abschließt (Termin verschoben/abgesagt/bestätigt oder Ticket) | 0 % | > 60 % | `calls.ergebnis` |
| Verifizierungsquote | Anteil Anrufe mit erfolgreicher Verifizierung | – | > 85 % | `verifizierungen` |
| Weiterleitungsquote | Anteil Anrufe mit Transfer | 100 % | < 35 % | `calls.ergebnis = weitergeleitet` |
| Leerfahrten | Techniker-Einsätze, bei denen der Kunde nicht angetroffen wurde | 9 % | < 5 % | Disposition |
| Fehlerquote | Anrufe mit Korrektur-Flag (Behauptung ohne Beleg, Daten vor Verifizierung) | – | < 3 % | `calls.flags` |
| Bearbeitungszeit (AHT) | Median Gesprächsdauer | 6,5 min | < 4 min | Get Call `call_length_min` |
| Ticketqualität | Anteil Tickets, die die Technik ohne Rückfrage bearbeiten kann (Stichprobe 20/Woche) | – | > 85 % | Review |

## 3. Identifizierung und Verifizierung

- **Identifizierung:** Contact-Lookup-Webhook über die Telefonnummer; bekannte Nummer → Name und Status als Variablen.
- **Verifizierung (Pflicht vor jeder Auskunft):** Postleitzahl der Anschlussadresse; höchstens drei Versuche, danach Weiterleitung.
  Das Backend gibt vor erfolgreicher Verifizierung keine Termin- oder Vertragsdaten heraus (`VERIFIZIERUNG_PFLICHT`),
  unabhängig vom Prompt.
- **Unbekannte Nummer:** Anliegen aufnehmen, Rückruf durch Mitarbeiter; keine Verifizierung über Namen allein.

## 4. Gesprächsleitfaden

Begrüßung mit KI-Kennzeichnung → Anliegen einordnen → Verifizierung → Termin (bestätigen / verschieben / absagen) oder
Störung (Art, seit wann, Ticket mit Nummer) → Verabschiedung. Vollständig in `docs/telli-konfig/telko_inbound/prompt.txt`
(erzeugt aus `integrations/usecases/telko_inbound.py`).

## 5. Geschäftsregeln (im Code erzwungen)

| Regel | Umsetzung |
|---|---|
| Keine Daten vor Verifizierung | Server verweigert alle Tools außer `verify_customer`, solange für `call_id` keine erfolgreiche Prüfung vorliegt |
| Höchstens drei Verifizierungsversuche | `verify_customer` zählt je Anruf; danach `weiterleiten: true` |
| Termine innerhalb von 24 h nicht änderbar | `cancel_appointment` lehnt ab (`MINDESTVORLAUF_H`), Agent leitet weiter |
| Ein Termin je Kunde | Umbuchung gibt den alten Slot frei; Erinnerung wird neu geplant |
| Ticket nur mit Beschreibung | `create_ticket` verlangt Kategorie und Text; Ticketnummer wird vorgelesen |
| Ergebnis aus dem Backend | Nachbereitung: Ticket, Absage, Umbuchung aus diesem Gespräch zählen, Behauptungen ohne Beleg werden geflaggt |

## 6. Edge Cases

| Fall | Verhalten | Ergebnis |
|---|---|---|
| PLZ dreimal falsch | keine Auskunft, Weiterleitung | `nicht_verifiziert`, Prüfaufgabe |
| Termin morgen früh verschieben | Ablehnung durch Code, Weiterleitung an Disposition | `weitergeleitet` |
| Kunde will nur bestätigen | Termin vorlesen, nichts ändern | `termin_bestaetigt` |
| Störung, Kunde fordert Gutschrift | Ticket mit Hinweis, keine Zusage | `ticket_erstellt` |
| Rechnung, Tarif, Kündigung | Weiterleitung | `weitergeleitet` |
| Unbekannte Nummer ohne Termin | Rückruf aufnehmen | `rueckruf_vereinbart` |
| Beleidigend | Gespräch beenden | `sonstiges` |

## 7. Integrationen

| System | Richtung | Was | Verantwortlich |
|---|---|---|---|
| Auftragssystem (Termine) | lesen/schreiben | Slots, Buchungen, Absagen (Prototyp: DuckDB-Slots) | IT Kunde |
| Ticketsystem | schreiben | Ereignis `ticket.erstellt` über die signierte Outbox | IT Kunde |
| Contact Lookup | lesen | Telefonnummer → Kunde | Backend |
| Telefonie | – | Inbound-Nummer bei telli, Warm Transfer an die Hotline | Plattform |

## 8. Rechtliches

- Identitätsprüfung vor Auskunft (Datenschutz): PLZ als Mindeststandard, im Pilot zusätzlich Kundennummer prüfen, wenn Vertragsdaten genannt werden
- KI-Kennzeichnung in der Begrüßung (AI Act, Art. 50)
- Aufzeichnung nur mit Einwilligung; Ticketinhalte enthalten keine Zahlungsdaten
- Transfer: Der Mitarbeiter sieht das Transkript, nicht die Aufzeichnung

## 9. Pilotplan

| Phase | Inhalt | Dauer | Go/No-Go |
|---|---|---|---|
| Woche 1 | Integration Auftragssystem und Ticketsystem, Staging-Abnahme, Testanrufe aller Edge Cases | 5 Tage | Checkliste komplett |
| Woche 2 | 20 % der Anrufe außerhalb der Stoßzeiten, tägliches Transkript-Review | 5 Tage | Fehlerquote < 3 %, 0 Datenauskünfte ohne Verifizierung |
| Woche 3–4 | 100 %, KPI-Review | 10 Tage | Fallabschlussquote, Leerfahrten |

## 10. Änderungsprotokoll

| Datum | Änderung | Grund | Freigabe |
|---|---|---|---|
| – | – | – | – |
