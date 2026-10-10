# 07 Enablement und Übergabe

Ziel der Übergabe: Der Kunde betreibt den Agenten selbst, liest Transkripte, passt den Leitfaden
in kleinen Schritten an und weiß, wann er das Deployment-Team braucht. Die Übergabe beginnt im
Soft-Launch, nicht danach.

## Wer lernt was

| Rolle beim Kunden | Lernziel | Format | Dauer |
| --- | --- | --- | --- |
| Vertriebsleitung (fachlicher Owner) | Wochenreport lesen, Entscheidungen treffen, Änderungen freigeben | Review-Termin, gemeinsamer Report in Woche 2 und 3 | 2 × 45 min |
| Transkript-Reviewer (1 bis 2 Personen) | Fehlschläge erkennen und einordnen (Agent, Test, Erwartung), Flags verstehen, Beobachtungen so notieren, dass daraus eine Änderung werden kann | gemeinsames Review an Tag 1 bis 3, danach Stichproben | 3 × 30 min |
| Fachberatung (Empfänger der Termine) | Was im CRM steht und woher es kommt, was `Prüfen`-Aufgaben bedeuten, Rückmeldung bei falschen Übergaben | Kurzschulung vor Soft-Launch | 30 min |
| IT (technischer Owner) | Betrieb des Backends: Health, Metriken, Outbox, Geheimnisrotation, Rollback, Lead-Push | Übergabe mit Runbook, einmal gemeinsam durchgespielt | 60 min |
| Support und Empfang | Dass ein KI-Agent anruft, wie Rückrufe erkannt werden (Contact Lookup), wohin Beschwerden gehen | Info-Mail und FAQ | |

## Was der Kunde nach der Übergabe selbst ändern kann

| Änderung | Wo | Absicherung |
| --- | --- | --- |
| Formulierungen des Agenten, Begrüßung, Fragen | Prompt des Agenten bei telli | Änderungsprotokoll, danach fünf Testanrufe mit den Personas |
| Anrufzeiten, Wiederholungsplan | Account-Einstellungen bei telli | Scoping Abschnitt 9 nachziehen |
| Sperrliste | Backend (`sperren`) und Do-not-call der Plattform | beide Listen, nie nur eine |
| Kalender-Slots | Kalenderquelle | `/available` liefert, was frei ist |
| Rückruf-Fälligkeit, Zuständige | Feldmapping, Aufgaben-Queue im CRM | Testaufgabe |

## Was weiterhin über das Deployment-Team läuft

- Änderungen an Qualifizierungskriterien oder Geschäftsregeln (`agent.Tools`): neuer UAT-Lauf
- Neue Tools, neue Outcomes, neue Integrationen: Checkliste und Staging-Abnahme
- Modellwechsel oder Plattform-Updates mit Verhaltensänderung: Regressions-UAT

## Übergabepaket

- [ ] Scoping-Dokument in letzter Fassung mit Änderungsprotokoll
- [ ] UAT-Abnahmeprotokoll und letzter `eval_history.json`-Stand
- [ ] Zugänge: Repository, Backend-Host, telli-Account, Secret Store, Monitoring
- [ ] Runbook mit Rollback, Eskalationsmatrix mit aktuellen Namen
- [ ] Tool-Definitionen (`python -m integrations definitions`) und Feldmapping als Datei
- [ ] Wochenreport-Vorlage, befüllt für die letzten zwei Wochen
- [ ] Liste offener Punkte und Backlog (S3, S4)
- [ ] Termin für den Review nach 30 Tagen Regelbetrieb

## Abschlussgespräch (Woche 4)

Agenda: KPIs gegen Scoping, was anders lief als geplant, was der Kunde beim nächsten Agenten
anders machen würde, Entscheidung über Rollout auf weitere Use Cases oder Regionen. Ergebnis ist
ein einseitiges Memo, das beide Seiten unterschreiben.
