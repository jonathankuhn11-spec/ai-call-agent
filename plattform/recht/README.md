# Rechtliche Unterlagen für den Pilotbetrieb

**Status: Entwürfe.** Die Texte sind fachlich auf die Plattform zugeschnitten (sie beschreiben, was
der Code tatsächlich tut), ersetzen aber keine Rechtsberatung. Vor dem ersten Pilotkunden von einer
Anwältin oder einem Anwalt für IT-/Datenschutzrecht prüfen lassen; Platzhalter in eckigen Klammern
ausfüllen. Die Unterlagen gehören zur Produktdokumentation und werden mit dem Code versioniert.

| Datei | Zweck | Wer unterschreibt / pflegt |
| --- | --- | --- |
| [AVV.md](AVV.md) | Auftragsverarbeitungsvertrag nach Art. 28 DSGVO zwischen SHK-Betrieb (Verantwortlicher) und Anbieter (Auftragsverarbeiter), mit Anlagen Subunternehmer und TOM | beide Parteien |
| [TOM.md](TOM.md) | Technische und organisatorische Maßnahmen nach Art. 32 DSGVO, je Maßnahme mit Verweis auf die Umsetzung im Code bzw. Hosting | Anbieter, jährlich prüfen |
| [VVT.md](VVT.md) | Verzeichnis von Verarbeitungstätigkeiten nach Art. 30 DSGVO: Vorlage für den Betrieb (Abs. 1) und Verzeichnis des Anbieters (Abs. 2) | Betrieb bzw. Anbieter |
| [LOESCHKONZEPT.md](LOESCHKONZEPT.md) | Löschkonzept (Datenarten, Fristen, Löschklassen, Umsetzung durch `core/loeschung.py`, Betroffenenrechte) | Anbieter, Betrieb bestätigt Fristen |
| [DATENSCHUTZHINWEIS-TELEFON.md](DATENSCHUTZHINWEIS-TELEFON.md) | Ansagetext für den Telefonassistenten, Informationspflichten nach Art. 13 DSGVO, Hinweise zu § 201 StGB und Art. 22 DSGVO | Betrieb (Ansage), Anbieter (Vorlage) |

## Rollen

- **Verantwortlicher:** der SHK-Betrieb. Er entscheidet über Zweck und Mittel (Kundenbetreuung,
  Terminvergabe, Abrechnung) und ist Ansprechpartner der Betroffenen.
- **Auftragsverarbeiter:** [ANBIETER, Rechtsform, Anschrift], Betreiber der Plattform. Verarbeitet nur
  auf dokumentierte Weisung (Konfiguration im Mandanten, Freigaben, Löschfristen).
- **Weitere Auftragsverarbeiter (Subunternehmer):** Hosting, Sprachdienste, Sprachmodell, E-Mail-/
  SMS-Versand, Telefonie. Liste in AVV Anlage 2; Änderungen werden dem Betrieb vorab angezeigt.
- **Eigenständig Verantwortliche:** Steuerkanzlei (Belegpaket), Buchhaltungssoftware-Anbieter mit
  eigenem Vertrag des Betriebs (Lexware Office, sevDesk), Telefonanbieter des Betriebs.

## Was vor dem Pilot noch zu tun ist

1. Anbieterangaben, Vertragsdaten und Subunternehmer-Anlage ausfüllen; AVV mit jedem Betrieb schließen.
2. AVV bzw. Standardvertragsklauseln mit jedem Subunternehmer vorhalten (Hoster, Microsoft Azure,
   Anthropic, seven.io, Carrier); Drittlandübermittlung dokumentieren (Transfer Impact Assessment, kurz).
3. Datenschutz-Folgenabschätzung (Art. 35): wegen KI-gestützter Verarbeitung von Telefonaten
   voraussichtlich erforderlich; Vorlage der Datenschutzkonferenz nutzen, Ergebnis zu den TOM legen.
4. Datenschutzbeauftragte/r prüfen: ab 20 Personen mit regelmäßiger Verarbeitung (§ 38 BDSG) oder
   bei DSFA-Pflicht benennen.
5. Prozess für Betroffenenanfragen festlegen (Auskunft, Löschung innerhalb eines Monats; technisch
   über `/kunden/{id}/auskunft` und `/kunden/{id}/loeschen`).
