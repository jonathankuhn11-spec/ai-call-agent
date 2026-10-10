# 01 Scoping-Workshop

Ziel: Nach 90 Minuten steht, was der Agent tut, woran der Pilot gemessen wird, was er nicht tun
darf und welche Systeme er braucht. Ergebnis ist ein unterschriebenes Scoping-Dokument nach der
Vorlage unten; [`SCOPING.md`](../../SCOPING.md) ist das ausgefüllte Beispiel für die
PV-Lead-Qualifizierung.

## Vorbereitung (Deployment Lead, vor dem Termin)

- [ ] 20 anonymisierte Beispiel-Leads oder Beispielanrufe vom Kunden anfordern (echte Fälle schlagen jede Annahme)
- [ ] Aktuellen Prozess verstehen: Wer ruft heute an, nach welchem Skript, mit welcher Quote?
- [ ] Systemlandschaft abfragen: CRM, Kalender, Telefonanlage, Ticketsystem, Automationsplattform
- [ ] Teilnehmer: fachlicher Owner, technischer Owner, Datenschutz, ein erfahrener Telefonierer des Kunden

## Agenda (90 Minuten)

| Minute | Block | Leitfragen |
| --- | --- | --- |
| 0–10 | Problem | Was kostet der heutige Zustand (Zeit, verlorene Leads, Reaktionszeit)? Woran merkt der Kunde in vier Wochen, dass der Pilot funktioniert? |
| 10–25 | Ziel und KPIs | Welche drei Zahlen entscheiden über den Rollout? Zielwerte, Baseline heute, Messquelle. Vorschlag: Speed-to-Lead, Qualifizierungsquote, Terminquote, Fehlerquote, Übergabequalität |
| 25–40 | Zielgruppe und Qualifizierung | Welche Angaben machen einen Lead qualifiziert? Welche disqualifizieren hart? Welche sind Soll, nicht Muss? Was passiert mit Unklaren? |
| 40–55 | Leitfaden | Reihenfolge der Fragen, Begrüßung mit KI-Kennzeichnung, Terminangebot, Verabschiedung. Was darf der Agent nie sagen (Preise, Zusagen, Förderungen)? |
| 55–65 | Edge Cases | Falsche Person, keine Zeit, Mailbox, aggressiv, Preisfrage, Rückfragen, Kunde spricht kein Deutsch. Je Fall: gewünschtes Verhalten und CRM-Ergebnis |
| 65–75 | Integrationen | Woher kommen Leads, wohin gehen Ergebnisse, wo liegen Termine, wer bekommt Rückrufaufgaben? Wer stellt Sandbox und Zugänge? |
| 75–85 | Recht | Einwilligung (§ 7 UWG), KI-Transparenz (AI Act Art. 50), Aufzeichnung, Aufbewahrung, AVV, Drittland |
| 85–90 | Nächste Schritte | Verantwortliche, Termine für Integration, UAT, Go-live |

## Fragen, die oft vergessen werden

- Anrufzeiten und Zeitzone: Wann darf angerufen werden, wie viele Versuche, in welchem Abstand?
- Was passiert mit einem Lead, der dreimal nicht erreicht wurde?
- Wer darf einen Termin stornieren, und erfährt der Agent davon?
- Gibt es Leads, die nie angerufen werden dürfen (Bestandskunden, Sperrliste, Beschwerdeführer)?
- Welche Nummer wird angezeigt, und ist sie beim Kunden erreichbar, wenn jemand zurückruft?
- Wie heißt die Firma am Telefon, wie heißt der Agent, siezen oder duzen?
- Wer liest in Woche 2 täglich Transkripte, und wie viele Minuten hat diese Person?

## Vorlage Scoping-Dokument

```markdown
# Deployment-Scoping: <Use Case> für <Kunde>
Stand: <Datum> · Version: 1.0 · Unterschrieben von: <fachlicher Owner>, <Deployment Lead>

## 1. Problem
<Heutiger Zustand in drei Sätzen, mit Zahlen.>

## 2. Ziel und KPIs
| KPI | Definition | Baseline | Ziel Pilot | Quelle |
|---|---|---|---|---|

## 3. Qualifizierungskriterien
Muss: ... · Soll: ... · Disqualifiziert hart bei: ...

## 4. Gesprächsleitfaden
Begrüßung (mit KI-Kennzeichnung) → Fragen in Reihenfolge → Terminangebot (max. zwei) → Verabschiedung

## 5. Edge Cases
| Fall | Verhalten | Ergebnis im CRM |
|---|---|---|

## 6. Integrationen
| System | Richtung | Was | Verantwortlich | Sandbox vorhanden |
|---|---|---|---|---|

## 7. Guardrails
Was der Agent nie sagt, nie tut, und wie der Code das erzwingt.

## 8. Rechtliches (vor Go-live)
Einwilligung · Transparenz · Aufzeichnung · AVV · Aufbewahrung

## 9. Pilotplan
| Phase | Inhalt | Dauer | Go/No-Go-Kriterium |
|---|---|---|---|

## 10. Änderungsprotokoll
| Datum | Änderung | Grund | Freigabe |
|---|---|---|---|
```

## Nacharbeit (innerhalb von zwei Arbeitstagen)

- [ ] Scoping-Dokument ausfüllen und zur Unterschrift schicken
- [ ] Personas für den UAT aus den Edge Cases ableiten (eine je Fall, erwartetes Ergebnis je Persona)
- [ ] Outcome-Schlüssel für den Agenten festlegen (siehe Checkliste, Abschnitt „Outcomes“)
- [ ] Zugänge und Sandbox beim technischen Owner anfordern
