# Deployment-Playbook: von der Unterschrift zum ersten produktiven Anruf

Dieses Playbook beschreibt, wie ein Voice-Agent-Pilot bei einem Kunden aufgesetzt, integriert,
abgenommen und in Betrieb genommen wird. Es ist auf das Projekt in diesem Repository zugeschnitten
(PV-Lead-Qualifizierung, [`SCOPING.md`](../../SCOPING.md) als ausgefülltes Beispiel,
[`integrations/`](../../integrations) als Backend), die Vorlagen sind aber für jeden Outbound-
oder Inbound-Use-Case gedacht.

## Phasen und Dokumente

| Phase | Dauer | Ergebnis | Dokument |
| --- | --- | --- | --- |
| 1. Scoping-Workshop | Woche 0, 90 Minuten plus Nacharbeit | unterschriebenes Scoping: KPIs, Kriterien, Leitfaden, Edge Cases, Integrationen, Recht | [01-scoping-workshop.md](01-scoping-workshop.md) |
| 2. Integration | Woche 1 | Plattform und Backend verbunden, Testdaten, Signaturen, Staging-Abnahme | [02-integrations-checkliste.md](02-integrations-checkliste.md) |
| 3. UAT | Woche 1 bis 2 | Agent und Integration gegen Abnahmekriterien geprüft, Sign-off | [03-uat-abnahmekriterien.md](03-uat-abnahmekriterien.md) |
| 4. Go-live | Woche 2 bis 4 | Soft-Launch, Hochfahren, Go/No-Go | [04-go-live-runbook.md](04-go-live-runbook.md) |
| 5. Betrieb | ab Woche 2 | Wochenreport, Eskalation, Verbesserungsschleife | [05-kpis-und-reporting.md](05-kpis-und-reporting.md), [06-eskalationsmatrix.md](06-eskalationsmatrix.md) |
| 6. Übergabe | Woche 4 | Kunde betreibt selbst, Deployment-Team im zweiten Glied | [07-enablement.md](07-enablement.md) |

## Rollen

| Rolle | Wer | Verantwortung |
| --- | --- | --- |
| Deployment Lead | Plattformseite | Pilot von Unterschrift bis Übergabe, Workshop, Leitfaden, UAT, Go/No-Go |
| Integrationsentwicklung | Plattformseite, mit IT des Kunden | Custom Tools, Kalender, Webhooks, CRM-Anbindung, Staging |
| Fachlicher Owner | Kunde, Vertriebsleitung | Leitfaden, Qualifizierungskriterien, Abnahme, Transkript-Reviews |
| Technischer Owner | Kunde, IT | Zugänge, Sandbox, Geheimnisse, Freigabe des Datenflusses |
| Datenschutz | Kunde, DSB | AVV, Einwilligungsgrundlage, Aufbewahrung, Aufzeichnung |
| Betrieb | beide, wechselnd | Monitoring, Bereitschaft, Eskalation |

## Drei Regeln, die sich bewährt haben

1. **Erst das Scoping unterschreiben, dann bauen.** Ein Leitfaden, der sich in Woche 2 noch ändert,
   kostet den UAT. Änderungen laufen danach über das Änderungsprotokoll im Scoping.
2. **Jede Integration wird mit echten Zustellungen abgenommen, nicht mit Beschreibungen.** Ein
   Webhook gilt als angebunden, wenn eine echte Nachricht aus dem Webhook-Portal gegen Staging
   verarbeitet wurde, nicht wenn der Endpunkt existiert.
3. **Das Ergebnis eines Anrufs kommt aus dem Backend.** Die Pass-Rate im UAT und die KPIs im
   Betrieb werden gegen CRM und Kalender gemessen, nicht gegen das, was der Agent sagt.
