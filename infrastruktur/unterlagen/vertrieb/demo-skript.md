# Demo-Skript (20 Minuten)

Live-Vorführung beim Betrieb oder per Bildschirmfreigabe. Grundlage ist ein Demo-Mandant mit fiktiven Daten
(`python -m core serve --demo` laut `plattform/README.md`), niemals echte Kundendaten eines anderen Betriebs.

## Vorbereitung (am Vortag)

- [ ] Demo-Mandant zurückgesetzt, Kalender mit freien Zeitfenstern für morgen gefüllt
- [ ] Demo-Rufnummer leitet auf den Telefonassistenten, Testanruf erfolgreich
- [ ] Testtelefon geladen, Lautsprecher funktioniert
- [ ] Büro-Oberfläche und „Mein Einsatz“ auf dem Handy angemeldet
- [ ] Fallback: Bildschirmaufzeichnung eines Anrufs, falls Netz oder Telefonie vor Ort versagen

## Ablauf

| Minute | Schritt | Was gezeigt wird | Kernsatz |
| --- | --- | --- | --- |
| 0–2 | Rahmen | Was gleich passiert | „Ich rufe jetzt bei Ihrem künftigen Assistenten an, Sie hören mit.“ |
| 2–7 | Live-Anruf als Endkunde | KI-Hinweis, Anliegen „Heizung aus, kein Warmwasser“, Adresse wird vorgelesen, zwei Terminvorschläge | „Er sagt als Erstes, dass er eine KI ist. Das ist Pflicht und schafft Vertrauen.“ |
| 7–10 | Vorgang im Büro | Vorgang mit Zusammenfassung, Anlage, Dringlichkeit, Terminvorschlag in der Freigabe-Inbox | „Nichts ist rausgegangen. Sie entscheiden.“ |
| 10–12 | Freigabe | Termin freigeben, SMS-Bestätigung an das Testtelefon | „Ein Klick, und der Kunde hat die Bestätigung.“ |
| 12–14 | Sicherheitsfall | Zweiter Anruf „Es riecht nach Gas“ | „Bei Gas bietet er nie einen Termin an, sondern verweist an 112 und den Entstörungsdienst.“ |
| 14–17 | Monteur und Rapport | „Mein Einsatz“ auf dem Handy, Rapport mit Unterschrift, daraus Rechnungsentwurf (Freigabe C) | „Vom Rapport zur Rechnung ohne Abtippen.“ (Pakete Pro, Premium) |
| 17–19 | Belege | Foto einer Quittung, Übergabe an das Steuertool | „Wir kontieren nicht. Ihr Steuerberater bekommt den Beleg dort, wo er ihn braucht.“ (Premium) |
| 19–20 | Abschluss | Pilotablauf und Erfolgskriterien | „Acht Wochen, messbare Ziele, jederzeit beendbar.“ |

## Nach der Demo

- Fragen notieren, die nicht beantwortet werden konnten, und binnen zwei Werktagen nachreichen.
- Pilotangebot erstellen: `python infrastruktur/build.py --unterlagen --kunde infrastruktur/unterlagen/kunden/<betrieb>.toml --pdf`.
