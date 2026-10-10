# 03 UAT und Abnahmekriterien

Der User-Acceptance-Test hat zwei Hälften: der Agent (führt er das Gespräch richtig?) und die
Integration (passiert danach das Richtige in den Systemen?). Beide werden gegen den Zustand von
CRM und Kalender gemessen, nicht gegen das Transkript.

## Teil 1: Agent gegen Personas

Aus jedem Edge Case des Scopings entsteht eine Persona mit erwartetem Ergebnis. Für die
PV-Lead-Qualifizierung sind es zehn ([`simulate.py`](../../simulate.py)):

| Persona | Erwartetes Ergebnis | Zusätzlich geprüft |
| --- | --- | --- |
| Eigentümer, interessiert | `termin_gebucht` | Termin im System |
| Mieter | `disqualifiziert` | kein Termin |
| Eigentümer Mehrfamilienhaus | `disqualifiziert` | kein Termin |
| Keine Zeit, Rückruf gewünscht | `rueckruf_vereinbart` | Aufgabe mit Zeitpunkt |
| Höfliches Nein | `opt_out` | Sperrliste |
| Preisfrager | `termin_gebucht` | kein Preis, keine Prozentzahl genannt |
| Falsche Person | `falsche_person` | Lead unverändert |
| Vage Angaben | `termin_gebucht` | Soll-Felder leer, Termin im System |
| Werbung gesehen | `termin_gebucht` | kein Abschweifen |
| Aggressiv | `opt_out` | sofortiges Ende |

**Bestanden** ist ein Gespräch, wenn das Ergebnis stimmt, die Buchung im CRM mit der Erwartung
übereinstimmt, kein verbotener Inhalt gesagt wurde und höchstens zwei Tool-Fehler auftraten.

**Stichprobe:** mindestens fünf Durchläufe je Persona (`--runs 5`, 50 Gespräche); bei
Temperatur 0,8 schwanken Einzelläufe stark. Zielwert für die Abnahme: **mindestens 90 %
bestanden, 100 % bei Buchungs- und Opt-out-Personas** (ein falscher Termin oder ein ignorierter
Opt-out ist ein Abnahmefehler, keine Statistik).

**Jeder Fehlschlag wird gelesen.** Das Transkript entscheidet, ob der Agent, der Test oder die
Erwartung falsch war (im Lauf v5 war es einmal der Test: Der „aggressive“ Kunde war nur beschäftigt).

## Teil 2: Integration gegen den Simulator

[`integrations/simulator.py`](../../integrations/simulator.py) spielt die Plattform und fährt
fünf Gespräche gegen das Backend. Erwartung je Gespräch:

| Persona | Backend danach | Ereignisse an Kundensystem |
| --- | --- | --- |
| `happy_path` | Status qualifiziert, Termin in `appointments`, Slot belegt | `termin.gebucht`, `anruf.beendet` |
| `mieter` | Status disqualifiziert, `eigentuemer = false` | `anruf.beendet` |
| `keine_zeit` | Status rueckruf, Aufgabe „Rückruf“ mit Wunschzeitpunkt | `aufgabe.erstellt`, `anruf.beendet` |
| `opt_out` | Status opt_out, Nummer in Sperrliste | `lead.gesperrt`, `anruf.beendet` |
| `behauptet_termin` | kein Termin, Aufgabe „Prüfen: termin_behauptet_ohne_buchung“ | `aufgabe.erstellt`, `anruf.beendet` |
| `nicht_erreicht` (Mailbox) | Lead unverändert, Anruf protokolliert | `anruf.beendet` |

Dazu: Wiederholung des `call_ended`-Webhooks ändert nichts (`duplikat`); jede Ereignis-ID kommt
beim Kundensystem genau einmal an; alle Zustellungen sind signaturgeprüft. `python -m integrations
demo` druckt genau diese Tabelle; die Tests in `tests/test_integrations_e2e.py` erzwingen sie.

## Teil 3: Echte Testanrufe (Staging, Telefon)

Vor dem Sign-off mindestens ein echter Anruf je Persona mit Teammitgliedern als Kunden, über die
Plattform, gegen Staging. Geprüft wird dasselbe wie im Simulator, zusätzlich:

- [ ] Latenz: Pause nach einer Kundenaussage unter 1,5 s im Mittel, Tool-Aufrufe sichtbar in `GET /metrics`
- [ ] Verständlichkeit: Zahlen, Termine und Adressen werden korrekt wiederholt
- [ ] Unterbrechung: Kunde fällt dem Agenten ins Wort, Agent reagiert
- [ ] Mailbox: Agent hinterlässt keine Nachricht, Wiederholung wird geplant
- [ ] KI-Kennzeichnung in der Begrüßung hörbar

## Abnahmeprotokoll (Vorlage)

```markdown
# UAT-Abnahme <Kunde>, <Datum>
Stand des Agenten: <Version / Label aus eval_history.json> · Backend: <Commit>

## Agent (Simulation)
Gespräche: <n> · Bestanden: <x> (<%>) · Buchungs-/Opt-out-Personas: <x/y>
Fehlschläge und Bewertung:
| Persona | Lauf | Fehler | Ursache (Agent / Test / Erwartung) | Maßnahme |

## Integration (Simulator)
Demo-Lauf: <bestanden/nicht bestanden> · Outbox: <zugestellt/tot>

## Echte Testanrufe
| Persona | Tester | Ergebnis im CRM | Latenz | Auffälligkeiten |

## Offene Punkte vor Go-live
| Punkt | Verantwortlich | bis |

## Entscheidung
[ ] Abgenommen  [ ] Abgenommen mit Auflagen  [ ] Nicht abgenommen
Unterschriften: fachlicher Owner · Deployment Lead
```
