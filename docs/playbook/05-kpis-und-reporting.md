# 05 KPIs und Reporting

Jede Kennzahl hat eine Definition, eine Formel, eine Datenquelle und einen Zielwert aus dem
Scoping. Was nicht aus CRM, Kalender oder Backend messbar ist, ist keine KPI, sondern ein Eindruck.

## Definitionen

| KPI | Definition | Formel | Quelle | Ziel (Beispiel PV) |
| --- | --- | --- | --- | --- |
| Speed-to-Lead | Zeit von Lead-Eingang bis erstem Anrufversuch | Median von `triggered_at` (telli) minus Eingangszeit im CRM | CRM-Zeitstempel, Get Call | < 5 min |
| Erreichbarkeit | Anteil der Leads, die innerhalb des Wiederholungsplans erreicht wurden | erreichte Leads / angerufene Leads | `calls.ergebnis ≠ nicht_erreicht`, je Lead | > 60 % |
| Qualifizierungsquote | Anteil erreichter Leads, deren Muss-Felder nach dem Gespräch gefüllt sind | Leads mit `eigentuemer` und `gebaeudetyp` gesetzt / erreichte Leads | `leads` | > 80 % |
| Terminquote | Anteil qualifizierter Leads mit Termin im System | `appointments` je Lead / qualifizierte Leads | `appointments`, nicht `outcomes` | > 35 % |
| Fehlerquote | Anteil Gespräche mit mindestens einem Korrektur-Flag | Gespräche mit `ergebnis_korrigiert`, `termin_behauptet_ohne_buchung`, `disqualifiziert_ohne_grund`, `feld_verworfen:*` / erreichte Gespräche | `calls.flags` | < 5 % |
| Übergabequalität | Anteil der Übergaben an die Fachberatung, bei denen alle CRM-Felder stimmen | korrekte Stichprobe / Stichprobe (20 pro Woche, manuell) | Review | > 90 % |
| Opt-out-Rate | Anteil erreichter Leads mit Opt-out | `opt_out` / erreichte | `calls` | beobachten, Ziel < 10 % |
| Tool-Latenz p95 | 95. Perzentil der Antwortzeit je Endpunkt | | `GET /metrics` | < 300 ms |
| Zustellquote Outbox | zugestellte Ereignisse / eingereihte | | `GET /outbox` | 100 %, 0 tot |
| Kosten je Termin | Plattformkosten / Termine | Rechnung / `appointments` | Plattform-Abrechnung | aus Scoping |

## Woher die Zahlen kommen

- **`calls`-Tabelle** (Backend): ein Eintrag je Anruf mit `ergebnis`, `turns`, `flags`,
  Transkript; gleiche Form wie beim lokalen UAT, deshalb funktioniert das Dashboard
  ([`dashboard.py`](../../dashboard.py)) für Pilotdaten genauso wie für Simulationsläufe.
- **`leads`, `appointments`, `aufgaben`, `sperrliste`** (Backend): Ground Truth für Quoten.
- **Get Call / List Calls** (telli): Zeitstempel, `ended_reason`, `attempt`, Aufzeichnung.
- **CRM**: Eingangszeit des Leads, Pipeline-Fortschritt nach dem Termin (Angebot, Abschluss),
  also die Zahlen, die nach dem Pilot zählen.

## Wochenreport (Vorlage)

```markdown
# Wochenreport Voice-Agent, KW <nn>, <Kunde>
Zeitraum: <von> bis <bis> · Anteil der Leads im Agenten: <10 / 50 / 100 %>

## Zahlen
| KPI | Diese Woche | Vorwoche | Ziel | Status |
|---|---|---|---|---|
| Speed-to-Lead (Median) | | | < 5 min | |
| Erreichbarkeit | | | > 60 % | |
| Qualifizierungsquote | | | > 80 % | |
| Terminquote | | | > 35 % | |
| Fehlerquote | | | < 5 % | |
| Übergabequalität (Stichprobe 20) | | | > 90 % | |
| Opt-out-Rate | | | < 10 % | |
| Tool-Latenz p95 | | | < 300 ms | |
| Outbox tot | | | 0 | |

## Ergebnisverteilung
| Ergebnis | Anzahl | Anteil |
|---|---|---|

## Was wir gelernt haben (aus den Transkripten)
- <Beobachtung → Maßnahme → Status>

## Änderungen am Agenten diese Woche
| Datum | Änderung | Grund | Wirkung sichtbar ab |
|---|---|---|---|

## Entscheidungen, die wir vom Kunden brauchen
- <Frage, Optionen, Empfehlung, bis wann>
```

## Lesen der Zahlen

- **Terminquote hoch, Übergabequalität niedrig:** Der Agent bucht, aber die Fachberatung bekommt
  falsche oder leere Felder. Ursache meist Extraktion (`feld_verworfen`) oder Mapping.
- **Erreichbarkeit niedrig:** Anrufzeiten, Anzeigenummer (wird sie als Spam markiert?),
  Wiederholungsplan prüfen, bevor am Leitfaden gedreht wird.
- **Fehlerquote steigt nach einer Änderung:** Änderungsprotokoll ansehen; eine Leitfadenänderung
  ohne erneuten UAT-Lauf ist die häufigste Ursache.
- **Opt-out-Rate steigt:** Begrüßung, KI-Kennzeichnung und Anrufzeitpunkt prüfen; Transkripte der
  Opt-outs lesen, nicht nur zählen.
- **`sonstiges` wächst:** Der Agent liefert kein `ergebnis`-Outcome; Outcome-Definition bei telli
  prüfen (Schlüssel, Kategorie-Werte).
