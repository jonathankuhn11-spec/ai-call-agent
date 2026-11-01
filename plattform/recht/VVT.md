# Verzeichnis von Verarbeitungstätigkeiten (Art. 30 DSGVO)

Entwurf. Teil A ist die Vorlage für den SHK-Betrieb (Verantwortlicher, Art. 30 Abs. 1), Teil B das
Verzeichnis des Anbieters als Auftragsverarbeiter (Art. 30 Abs. 2).

## Teil A: Vorlage für den Betrieb

**Verantwortlicher:** [Betrieb, Anschrift, Inhaber/in, Kontakt] · **Datenschutzbeauftragte/r:** [falls benannt]

### A1 Telefonannahme und Vorqualifizierung durch Sprachassistenten

| | |
| --- | --- |
| Zwecke | Erreichbarkeit außerhalb der Bürozeiten und bei Besetzt, Erfassung von Anliegen, Dringlichkeitseinschätzung, Terminvorschlag |
| Rechtsgrundlage | Art. 6 Abs. 1 lit. b (Vertragsanbahnung, Vertrag) für Kunden; lit. f (berechtigtes Interesse an effizienter Erreichbarkeit) für sonstige Anrufende; Einwilligung nur für optionale Kanäle (SMS) |
| Betroffene | Kunden, Interessenten, sonstige Anrufende |
| Datenkategorien | Rufnummer, Name, Anschrift, Anliegen, Anlagendaten, Transkript und Zusammenfassung des Gesprächs, Zeitfensterwunsch |
| Empfänger | Auftragsverarbeiter (Plattform), dessen Subunternehmer (Sprachdienste, Sprachmodell, Carrier) |
| Drittland | ggf. Sprachmodell-Anbieter USA auf Grundlage von Standardvertragsklauseln/DPF, siehe AVV Anlage 2 |
| Löschfristen | Transkript 90 Tage; Zusammenfassung mit dem Vorgang (3 Jahre nach Abschluss); Gespräche ohne Kundenbezug 30 Tage |
| TOM | siehe TOM.md des Auftragsverarbeiters |

### A2 Kunden-, Objekt- und Anlagenverwaltung (CRM, Digital Twin)

| | |
| --- | --- |
| Zwecke | Kundenbetreuung, Wartungsplanung, Dokumentation der Anlagen, Gewährleistung |
| Rechtsgrundlage | Art. 6 Abs. 1 lit. b; lit. c für Prüf- und Dokumentationspflichten (z. B. Gasinstallation, Kehr- und Überprüfungsordnung) |
| Betroffene | Kunden, Eigentümer, Hausverwaltungen, Mieter als Ansprechpersonen |
| Datenkategorien | Stammdaten, Objektanschrift, Anlagentyp, Hersteller, Baujahr, Wartungsstand, Vertragsstatus, Einwilligungen |
| Empfänger | Plattform; Fachsoftware des Betriebs |
| Löschfristen | 3 Jahre nach letzter Aktivität ohne Vertrag (Anonymisierung); mit Wartungsvertrag bis Vertragsende + 3 Jahre |

### A3 Disposition und Einsatzplanung

| | |
| --- | --- |
| Zwecke | Terminvergabe, Zuordnung von Monteuren, Terminbestätigung |
| Rechtsgrundlage | Art. 6 Abs. 1 lit. b (Kunden); lit. b i. V. m. § 26 BDSG (Mitarbeitende) |
| Betroffene | Kunden, Monteure |
| Datenkategorien | Termine, Einsatzorte, Qualifikationen, Arbeitszeiten, Anmeldename der Monteure |
| Empfänger | Plattform; E-Mail-/SMS-Dienstleister für Bestätigungen |
| Löschfristen | mit dem Vorgang |

### A4 Rechnungsstellung und Übergabe an Buchhaltung und Steuerkanzlei

| | |
| --- | --- |
| Zwecke | Abrechnung, Buchführung, steuerliche Pflichten |
| Rechtsgrundlage | Art. 6 Abs. 1 lit. b und lit. c (§ 14 UStG, § 147 AO, § 257 HGB) |
| Betroffene | Kunden |
| Datenkategorien | Rechnungsempfänger, Leistungen, Beträge, Rechnungsnummer, Zahlungsstatus |
| Empfänger | Buchhaltungssoftware (eigener Vertrag), Steuerkanzlei, ggf. Finanzverwaltung |
| Löschfristen | 10 Jahre (Rechnungen), Kundendatensatz kann vorher anonymisiert werden |

### A5 Nutzung der Plattform durch Mitarbeitende

| | |
| --- | --- |
| Zwecke | Zugriffsschutz, Nachvollziehbarkeit von Freigaben und Änderungen |
| Rechtsgrundlage | § 26 BDSG, Art. 6 Abs. 1 lit. c und f |
| Betroffene | Mitarbeitende des Betriebs, Steuerkanzlei |
| Datenkategorien | Anmeldename, Rolle, Zeitpunkt und Art von Aktionen im Ereignisprotokoll |
| Löschfristen | Protokoll 3 Jahre, finanzwirksame Freigaben 10 Jahre |

## Teil B: Verzeichnis des Auftragsverarbeiters

**Auftragsverarbeiter:** [ANBIETER, Anschrift, Vertretung, Kontakt]

| Verantwortlicher (Auftraggeber) | Kategorien der Verarbeitung | Subunternehmer | Drittland | TOM |
| --- | --- | --- | --- | --- |
| je Mandant: [Betrieb] | Hosting und Betrieb der Plattform; Transkription und Auswertung von Telefonaten; Terminplanung; Rechnungserstellung und Übermittlung; Belegpaket; Löschläufe; Sicherungen | Hoster (DE), Microsoft Azure Speech (DE), Anthropic (USA/EU), Carrier (DE), seven.io (DE), E-Mail-Anbieter | Anthropic, soweit kein EU-Endpunkt: Standardvertragsklauseln / DPF | TOM.md |

Eigene Verarbeitungen des Anbieters (Kundenverwaltung, Abrechnung der Betriebe, Support-Kommunikation)
werden in einem gesonderten Verzeichnis nach Art. 30 Abs. 1 geführt.
