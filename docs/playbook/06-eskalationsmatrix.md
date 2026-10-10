# 06 Eskalationsmatrix

Wer bei welchem Problem wie schnell handelt. Die Stufen sind nach Wirkung auf Angerufene und
Kunden sortiert, nicht nach technischer Ursache.

## Stufen

| Stufe | Definition | Beispiele | Reaktion | Lösung oder Umgehung | Wer |
| --- | --- | --- | --- | --- | --- |
| S1, sofort pausieren | Angerufene oder Kunde kommen zu Schaden, Rechtsverstoß möglich | Opt-out wird ignoriert und erneut angerufen; Agent nennt Preise oder macht Zusagen; Anrufe außerhalb der erlaubten Zeiten; Termine werden doppelt oder falsch gebucht; Datenabfluss (Transkripte an falsches Ziel) | 15 Minuten, rund um die Uhr | Pause nach Runbook sofort, Ursache innerhalb von 4 Stunden | Bereitschaft → Deployment Lead → fachlicher Owner informiert |
| S2, Betrieb gestört | Pilot läuft, aber Ergebnisse kommen nicht an oder sind falsch | Webhook-Signatur schlägt fehl (alle `call_ended` 401); Outbox-Einträge sterben; CRM-Spiegelung fällt aus (`spiegel` in `/metrics` zeigt `tot`); Lead-Push bricht ab; Tool-Latenz p95 über 1 s | 1 Stunde in Betriebszeiten | 1 Arbeitstag; bis dahin manuelle Nachbereitung aus `calls` | Integrationsentwicklung → technischer Owner |
| S3, Qualität | Gespräche laufen, Kennzahlen weichen ab | Terminquote unter 70 % des Ziels; Häufung `disqualifiziert_ohne_grund`; Persona im Transkript-Review fällt wiederholt durch; Kundenbeschwerde über Ton oder Verständlichkeit | 1 Arbeitstag | nächster Review-Zyklus, Änderung über Änderungsprotokoll und UAT-Lauf | Deployment Lead, fachlicher Owner |
| S4, Verbesserung | Wunsch oder Idee ohne akute Wirkung | neue Persona, zusätzliches CRM-Feld, Formulierungswünsche | Wochenreport | Backlog | Deployment Lead |

## Kommunikation

| Stufe | Kanal | Inhalt | Wer informiert wen |
| --- | --- | --- | --- |
| S1 | Telefon, dann Chat-Kanal des Piloten | Was passiert ist, was pausiert wurde, nächster Schritt, Zeitpunkt der nächsten Meldung | Bereitschaft → Deployment Lead → fachlicher und technischer Owner, Datenschutz bei Datenbezug |
| S2 | Chat-Kanal des Piloten | Symptom, betroffene Anrufe (Zeitraum, Anzahl), Umgehung | Integrationsentwicklung → Deployment Lead → technischer Owner |
| S3 | Wochenreport oder Review-Termin | Beobachtung, Hypothese, Vorschlag, Entscheidung | Deployment Lead → fachlicher Owner |
| S4 | Backlog | | |

Jede S1 und S2 bekommt innerhalb von zwei Arbeitstagen eine kurze Nachlese: Ursache, Wirkung
(wie viele Anrufe, welche Leads), Behebung, Vorbeugung. Sie wandert ins Änderungsprotokoll.

## Erkennen

| Symptom | Wahrscheinliche Stufe | Erster Blick |
| --- | --- | --- |
| `GET /health` antwortet nicht | S2 | Prozess, Zertifikat, DNS |
| `call_ended` → 401 | S2 | Signaturgeheimnis rotiert? `TELLI_WEBHOOK_SECRET` prüfen |
| `GET /outbox` zeigt `tot` | S2 | Antwort des Kundensystems im Eintrag (`letzte_antwort`) |
| Flags `termin_behauptet_ohne_buchung` häufen sich | S3, bei doppelten Buchungen S1 | Kalender-Endpunkt erreichbar? Timeout bei telli zu knapp? |
| `lead.gesperrt` ohne Opt-out im Transkript | S3 | Outcome-Definition, Extraktion |
| Anruf bei gesperrter Nummer | S1 | Sperrliste des Backends gegen Do-not-call der Plattform abgleichen |
| p95 steigt | S2 ab 1 s | Datenbankgröße, Sperre, Netz zum CRM |
| `zuordnung_offen` wächst | S3 | Doppelte Kontakte bei telli, Push-Quelle prüfen |

## Bereitschaft in der ersten Woche

| Tag | Zeit | Bereitschaft (Plattformseite) | Ansprechpartner Kunde |
| --- | --- | --- | --- |
| T0 bis T+5 | Anrufzeiten plus eine Stunde | Deployment Lead, Integrationsentwicklung im Wechsel | technischer Owner |
| T+6 bis T+14 | Betriebszeiten | Integrationsentwicklung | technischer Owner |
| ab T+14 | nach Übergabe | zweites Glied | Betrieb des Kunden |
