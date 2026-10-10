# 04 Go-live-Runbook

Vom Sign-off bis zum Regelbetrieb. Der Soft-Launch mit 10 % der Leads ist kein Zwischenschritt,
sondern der eigentliche Test: Erst hier sprechen echte Kunden mit dem Agenten.

## Zeitplan

| Zeitpunkt | Schritt | Verantwortlich | Prüfung |
| --- | --- | --- | --- |
| T-7 | UAT-Sign-off liegt vor, offene Auflagen terminiert | Deployment Lead | Abnahmeprotokoll |
| T-5 | Produktionsumgebung: Geheimnisse rotiert (Staging-Geheimnisse gelten nicht in Produktion), Start mit `UMGEBUNG=prod`, `GET /health` meldet `geheimnisse_vollstaendig: true` | Integrationsentwicklung | Health-Antwort |
| T-5 | Produktive Webhook-Endpunkte bei telli eingetragen, eine Testzustellung aus dem Portal verarbeitet | Integrationsentwicklung | `verarbeitet` im Log |
| T-3 | Feldmapping im produktiven CRM geprüft: ein Testkontakt bekommt Status, Notiz, Aufgabe | technischer Owner | Kontakt im CRM |
| T-3 | Sperrliste des Kunden importiert (Bestandskunden, Beschwerdeführer, frühere Opt-outs) | technischer Owner | Stichprobe mit `lookup_lead` |
| T-2 | Bereitschaft für die erste Woche eingeteilt, Eskalationsmatrix ausgehängt | Betrieb | Namen und Nummern |
| T-1 | Leads für den Soft-Launch ausgewählt (10 %, zufällig, keine Schlüsselkunden), Lead-Push konfiguriert (`TELLI_AGENT_ID`), Auto-Dialer bei telli noch aus | Deployment Lead | Lead-Liste |
| T-1 | Go/No-Go-Call: Checkliste unten komplett | alle | Protokoll |
| T0 | Auto-Dialer einschalten, `POST /push` auslösen, erste zehn Anrufe live mithören (Dashboard der Plattform) | Deployment Lead, fachlicher Owner | |
| T0 + 2 h | Erste Transkripte lesen, `GET /metrics` und `GET /outbox` prüfen | Deployment Lead | kein `tot`, p95 im Rahmen |
| T+1 bis T+5 | Täglich 30 Minuten Transkript-Review (alle Fehlschläge, 10 % der Erfolge), Fixes nur nach Änderungsprotokoll | fachlicher Owner, Deployment Lead | Review-Notiz je Tag |
| T+5 | Review des Soft-Launch gegen die Hochfahr-Kriterien | alle | Entscheidung 10 % → 50 % |
| T+8 | 50 % der Leads | | |
| T+10 | 100 % der Leads | | |
| T+14 | KPI-Review gegen Scoping, Go/No-Go für den Regelbetrieb, Übergabe beginnt | alle | Wochenreport |

## Go/No-Go-Checkliste (T-1)

- [ ] UAT-Sign-off und alle Auflagen erledigt
- [ ] `python -m pytest -q` grün auf dem Stand, der deployt wird
- [ ] Produktions-Geheimnisse gesetzt, Staging-Geheimnisse widerrufen
- [ ] Echte `call_ended`-Zustellung in Produktion verarbeitet
- [ ] Signaturprüfung auf allen Endpunkten aktiv (Test: unsignierter Request bekommt 401)
- [ ] Outbox-Ziel (Ticketsystem) antwortet 2xx auf eine Testnachricht
- [ ] Sperrliste importiert, Anrufzeiten konfiguriert, Anzeigenummer erreichbar
- [ ] Monitoring: Wer schaut wann auf `/health`, `/metrics`, `/outbox`, Plattform-Dashboard?
- [ ] Rollback-Weg bekannt und in fünf Minuten ausführbar (siehe unten)
- [ ] Kunde hat intern kommuniziert, dass ein KI-Agent anruft (Vertrieb, Support, Empfang)

## Hochfahr-Kriterien (von 10 % auf 50 % auf 100 %)

| Kriterium | Schwelle |
| --- | --- |
| Terminquote | nicht unter 70 % des Zielwerts aus dem Scoping |
| Falsche Buchungen (`termin_behauptet_ohne_buchung`, Doppelbuchungen) | 0 |
| Ignorierte Opt-outs | 0 |
| Übergabequalität (CRM-Felder korrekt in Stichprobe von 20) | ≥ 90 % |
| Tool-Latenz p95 | < 500 ms |
| Tote Outbox-Einträge | 0 ungeklärte |
| Beschwerden von Angerufenen | 0 unbearbeitete |

Ein Kriterium verfehlt: auf der aktuellen Stufe bleiben, Ursache beheben, zwei Tage beobachten.
Falsche Buchung oder ignorierter Opt-out: sofort pausieren (siehe Rollback).

## Rollback und Pause

1. **Pause:** Auto-Dialer des Agenten bei telli deaktivieren (dann plant der Lead-Push keine Anrufe mehr ein, Kontakte werden weiter angelegt). Laufende
   Gespräche enden normal, neue starten nicht. Dauer: unter zwei Minuten.
2. **Backend zurück:** vorherigen Commit deployen; die Datenbank bleibt (Migrationen sind additiv,
   `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`).
3. **Nachbereitung nachholen:** Während einer Störung nicht verarbeitete `call_ended`-Ereignisse
   wiederholt telli nach Plan; nach Behebung reicht Warten oder „Recover“ im Webhook-Portal.
   Doppelte Zustellungen sind durch die Deduplizierung ungefährlich.
4. **Outbox:** Der Planer stellt fällige Einträge von selbst nach; tote Einträge nach Behebung mit `POST /outbox/{id}/erneut` (bei gestopptem Server: `python -m integrations outbox --erneut ID`).
5. **Kommunikation:** Eintrag nach Eskalationsmatrix, fachlicher Owner informiert, Ursache im Änderungsprotokoll.

## Monitoring im Betrieb

| Signal | Quelle | Normal | Handeln bei |
| --- | --- | --- | --- |
| Verfügbarkeit | `GET /health` alle 60 s | `ok`, `geheimnisse_vollstaendig: true`, `planer: true` | 2 Ausfälle in Folge |
| Tool-Latenz | `GET /metrics` p95 je Endpunkt | < 300 ms | > 500 ms (Agent macht hörbare Pausen) |
| Tool-Fehler | `GET /metrics` Fehlerzähler | Einzelfälle | > 5 % eines Endpunkts |
| Outbox | `GET /outbox` | kein `tot` | jeder tote Eintrag |
| Ergebnisverteilung | `calls`-Tabelle, Plattform-Dashboard | wie im UAT | Sprung bei `sonstiges`, `ergebnis_fehlt`, `nicht_erreicht` |
| Flags | Notizen und `calls.flags` | selten | Häufung von `termin_behauptet_ohne_buchung`, `disqualifiziert_ohne_grund` |
| Lead-Push | `GET /metrics` → `push` (letzter Lauf) | `fehler: []` | jeder Fehler, `zuordnung_offen` wächst |
| Spiegelung ins CRM | `GET /metrics` → `spiegel` | kein `tot` | jeder tote Auftrag |
