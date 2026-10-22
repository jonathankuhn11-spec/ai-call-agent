# Leistungsbeschreibung und Service-Level-Vereinbarung

> **ENTWURF zur anwaltlichen Prüfung.** Anlage zum Vertrag mit {{kunde.firma}}, Paket **{{paket.name}}**.
> Die Preise entsprechen `unterlagen/preise.toml` und dem Abschnitt „Preise“ der Website.

## 1 Pakete

| | Basis | Pro | Premium |
| --- | --- | --- | --- |
| Monatspauschale (netto) | 349 € | 649 € | 999 € |
| Einrichtung einmalig (netto) | 1.490 € | 2.990 € | 4.990 € |
| Gesprächsminuten inklusive je Monat | 300 | 600 | 1.000 |
| KI-Telefonagent mit Notfallerkennung und Rückrufmanagement | ✓ | ✓ | ✓ |
| Kundenverwaltung mit Kontakthistorie, Terminvorschläge mit Kalenderabgleich | ✓ | ✓ | ✓ |
| SMS-Terminerinnerung (mit dokumentierter Einwilligung des Endkunden) | ✓ | ✓ | ✓ |
| Disposition, mobile Monteursansicht „Mein Einsatz“ | | ✓ | ✓ |
| Anfragen per E-Mail und Webformular | | ✓ | ✓ |
| Angebote, Rapport mit Unterschrift, Rechnung als PDF und XRechnung | | ✓ | ✓ |
| Offene Posten, Mahnwesen, Bewertungsanfrage | | ✓ | ✓ |
| Belegeingang mit Übergabe an DATEV-Belegtransfer, Lexware Office oder sevDesk | | | ✓ |
| Wartungsverträge, Bereitschaftsplan, Zeiterfassung, Nachkalkulation, Auswertungen | | | ✓ |
| Mehrere Standorte, bevorzugter Support (Abschnitt 6) | | | ✓ |

Weitere Gesprächsminuten: 0,25 € netto je angefangene Minute. Gezählt wird die Verbindungsdauer zwischen
Annahme durch den Telefonassistenten und Gesprächsende.

## 2 Freigabeklassen

| Klasse | Wirkung | Beispiele |
| --- | --- | --- |
| A | läuft automatisch, ohne Außenwirkung mit Rechtsfolgen | Anruf annehmen, Anliegen erfassen, Kundenstamm pflegen, Terminerinnerung an zugesagte Termine |
| B | vorbereitet, Büro oder Inhaber bestätigen | Termin zusagen, Angebot versenden, Beleg an das Steuertool übergeben, Bewertungsanfrage |
| C | ausschließlich Inhaberin oder Inhaber | Rechnung, Storno, Mahnung, Abrechnung von Wartungsverträgen |

Der Kunde kann Aktionen in eine höhere Klasse verschieben, nicht in eine niedrigere.

## 3 Einrichtungsleistung

Mit der Einrichtungspauschale abgegolten sind: Erstgespräch und Onboarding-Fragebogen, Anlage des Mandanten
und der Benutzer, Übernahme von Kunden- und Artikelstamm aus einer Datei (CSV oder Excel, bis 5.000 Datensätze),
Konfiguration von Bürozeiten, Notdienst, Gesprächsregeln und Ansagetext, Anbindung einer Buchhaltungssoftware,
Testanrufe mit dem Kunden, Go-live-Abnahme und eine Einweisung von bis zu zwei Stunden für Büro und Monteure.

## 4 Verfügbarkeit

1. Zugesagt ist eine Verfügbarkeit von **99,0 % im Kalendermonat** für (a) die Annahme umgeleiteter Anrufe
   durch den Telefonassistenten und (b) die Büro-Oberfläche, jeweils gemessen am Übergang des Rechenzentrums
   zum Internet bzw. zum SIP-Netz des Carriers.
2. Nicht als Ausfallzeit zählen: angekündigte Wartung (mindestens drei Werktage vorher, zwischen 22 und 6 Uhr,
   höchstens vier Stunden je Monat), Störungen im Telefonnetz oder beim Telefonanbieter des Kunden, vom
   Kunden zu vertretende Ursachen und höhere Gewalt.
3. Während eines Ausfalls greift die Rückfallebene des Kunden: Antwortet die Plattform nicht, stellt der
   Carrier den Anruf nach Zeitablauf auf die Mailbox oder die hinterlegte Mobilnummer durch.
4. Gutschrift bei Unterschreitung: je angefangene 0,5 Prozentpunkte unter 99,0 % werden 5 % der
   Monatspauschale gutgeschrieben, höchstens 25 % je Monat. Die Gutschrift ist auf Anforderung des Kunden
   binnen 30 Tagen nach Monatsende zu gewähren. Während einer Pilotphase entfallen Gutschriften.

## 5 Datensicherung

Tägliche Sicherung der Datenbank mit 14 Tagen Aufbewahrung am Server sowie verschlüsselte Zweitsicherung
außer Haus. Wiederherstellungsziel: Datenstand höchstens 24 Stunden alt (RPO), Wiederanlauf binnen
eines Werktags (RTO).

## 6 Support und Störungsbehebung

Supportzeiten: {{vertrag.support_zeiten}} · Telefon {{vertrag.support_telefon}} · E-Mail {{vertrag.support_email}}.
Für Priorität 1 außerhalb der Supportzeiten: {{vertrag.notfall_telefon}}.

| Priorität | Definition | Reaktionszeit | Ziel bis Umgehung |
| --- | --- | --- | --- |
| P1 kritisch | Telefonassistent nimmt keine Anrufe an, Büro-Oberfläche für alle unerreichbar, Verdacht auf Datenschutzverletzung | 1 Stunde, täglich 7–20 Uhr | 4 Stunden (Rückfallebene greift sofort) |
| P2 hoch | wesentliche Funktion gestört, z. B. keine Terminvorschläge, Rechnungsversand nicht möglich | 4 Stunden in Supportzeiten | nächster Werktag |
| P3 normal | einzelne Funktion fehlerhaft, Umgehung möglich | nächster Werktag | 5 Werktage |
| P4 Anfrage | Fragen, Änderungswünsche, Anpassung von Gesprächsregeln | 2 Werktage | nach Absprache |

Im Paket Premium halbieren sich die Reaktionszeiten für P2 bis P4. Das Verfahren ist in
`betrieb/support-eskalation.md` beschrieben.

## 7 Leistungsgrenzen

- Der Telefonassistent ist keine Notrufstelle. Bei Gasgeruch oder Gefahr für Leib und Leben verweist er
  an 112 bzw. den Entstörungsdienst des Netzbetreibers und bietet keinen Termin an.
- Keine Preisauskünfte, Ferndiagnosen oder Gewährleistungszusagen am Telefon.
- Keine Speicherung von Gesprächsaudio. Gesprächstexte werden nach dem Löschkonzept gelöscht.
- Keine Kontierung und keine Buchung von Belegen.
