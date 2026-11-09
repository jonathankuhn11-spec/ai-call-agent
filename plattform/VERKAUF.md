# Verkaufsreife: Abdeckung der Lead-Probleme P1–P10

Die Leadlisten codieren belegte Probleme der Betriebe als P1–P10 (Problemkatalog in der Leadliste, Blatt „Problemkatalog“).
Diese Matrix ordnet jedem Code die Produktfunktion, die Edition, den Vorführweg in der Demo und den automatisierten Nachweis zu.
Demo: `python -m core serve --demo`, dann `http://localhost:8100/`.

| Code | Problem | Lösung im Produkt | Edition | Vorführen | Nachweis (Tests) |
| --- | --- | --- | --- | --- | --- |
| P1 | keine (funktionsfähige) Website | Betriebswebsite aus den Stammdaten, eigene Domain mit automatischem TLS, SEO (JSON-LD, Sitemap) | Basis | Ansicht „Website“ › Vorschau | `test_runde8.py` |
| P2 | kein Kontakt-/Anfrageformular | Formular ohne JavaScript, Honeypot, Drosselung, **bis zu drei Fotos** am Vorgang | Basis | Website › „Anfrage senden“ | `test_runde13.py::test_anfrage_mit_fotos_und_notfall_alarmiert` |
| P3 | keine Online-Terminbuchung | Online-Buchung freier Zeitfenster (Qualifikation, Fahrzeit, Sachmittel) | Pro | Website › „Online Termin buchen“ | `test_runde4.py` |
| P4 | kein Kundenportal/Statusprozess | Portal-Link je Auftrag: Status, Termin, Rapport, Rechnung, Bewertung, Zahlung | Basis (Zahlung: Pro) | Vorgang › Portal-Link | `test_runde3.py`, `test_runde9.py` |
| P5 | Notdienst nur Telefon/Anrufbeantworter | 24/7-Telefonagent mit Dringlichkeitsregeln, Weiterleitung zur Bereitschaft; **schriftliche Notfälle alarmieren wie Anrufe**, Notfall-Kennzeichnung im Formular | Basis | „Anruf simulieren“ (Gasgeruch); Formular mit „Notfall“ | `test_runde13.py::test_schriftlicher_notfall_per_email_alarmiert` |
| P6 | veraltete Website/Baukasten | Website-Check als Gesprächseinstieg; Ablösung durch die Betriebswebsite | – / Basis | `python -m core website-check <url>` | `test_runde13.py::test_website_check_altseite_belegt_probleme` |
| P7 | Mobilnummer als Hauptnummer | **Rufumleitungsassistent**: bedingte Umleitung (Nichtannahme, Besetzt, nicht erreichbar) per antippbarem GSM-Code, Nummer bleibt | Basis | Einrichtung › Rufumleitung | `test_runde13.py::test_rufumleitung_codes_und_einrichtungsschritt` |
| P8 | Büro/Telefon personell nicht gedeckt | Telefonagent, Freigabe-Inbox, Disposition, Angebote/Rechnungen mit Freigabeklassen | Basis–Premium | Cockpit, Freigabe-Inbox | gesamte Suite |
| P9 | Rechts-/DSGVO-Risiko auf der Website | Website ohne Cookies und Fremdskripte, Impressum-/Datenschutzentwurf (§ 5 DDG, Art. 13 DSGVO); Website-Check belegt Google Fonts/Maps/Tracking ohne Einwilligung, TMG-Verweise, Platzhalter | Basis | Website-Check-Bericht | `test_runde13.py::test_website_check_*` |
| P10 | keine Karriereseite trotz Personalsuche | **Karriereseite** mit Stellen (Google for Jobs), Kurzbewerbung ohne Anschreiben, Ansicht „Bewerbungen“ mit Löschfristen | Basis | Website › Karriere: Stellenangebote | `test_runde13.py::test_karriereseite_*`, `::test_kurzbewerbung_*` |

## Website-Check im Vertrieb

```bash
python -m core website-check www.beispiel-haustechnik.de            # Kurzbericht für die Gesprächsnotiz
python -m core website-check --datei urls.txt --csv check.csv        # Leadliste (eine Adresse je Zeile)
```

Je Befund: Problemcode, Schwere, Beleg (Fundstelle) und passende KUHN-Leistung; dazu ein Bedarfsscore 0–100. Abgerufen werden
höchstens Startseite, Impressum und Datenschutzerklärung (öffentliche Seiten, keine Formulare, kein Login). Die Befunde der
Kategorie P9 sind Indizien und keine Rechtsberatung; im Anschreiben sachlich formulieren („uns ist aufgefallen …“).

## Preise und Editionen

Identisch mit `infrastruktur/unterlagen/preise.toml` und der Website (Abschnitt „Preise“): Basis 349 €, Pro 649 €, Premium 999 € netto
je Monat; Einrichtung 1.490 / 2.990 / 4.990 €; 300 / 600 / 1.000 Inklusivminuten, Mehrminute 0,25 €. Anrufe werden nie wegen eines
erschöpften Kontingents abgewiesen; die Betreiberkonsole weist Mehrminuten je Mandant aus. Bei Preisänderungen `core/produkt.py`
(`EDITIONEN`, `MEHRMINUTE_NETTO`), `preise.toml` und die Website gemeinsam ändern.

## Was nur der Betreiber erledigen kann

Server und Domain, SIP-Trunk mit Ortsnetznummer (Ziel der Rufumleitung), Sprachdienst-Schlüssel, GPU-Server für die eigene Stimme,
juristische Prüfung der Rechtstexte (`recht/`, Impressum-/Datenschutzentwurf, Datenschutzhinweis zur Bewerbung).
