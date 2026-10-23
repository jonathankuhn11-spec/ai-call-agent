# Einrichtungsprotokoll

Betrieb: {{kunde.firma}} · Paket {{paket.name}} · Mandant-ID: ________ · Einrichtung durch: ________ · Datum: ________

Jeder Schritt wird mit Kürzel und Uhrzeit abgezeichnet. Abweichungen in der Spalte „Notiz“.

## 1 Vorbereitung bei {{auftritt.marke}}

| Nr. | Schritt | Referenz | erledigt | Notiz |
| --- | --- | --- | --- | --- |
| 1.1 | AVV und Pilotvereinbarung bzw. Vertrag unterschrieben vorliegend | `datenschutz/avv.md`, `vertrag/` | | |
| 1.2 | Onboarding-Fragebogen vollständig, Rückfragen geklärt | `onboarding/fragebogen.md` | | |
| 1.3 | Mandant angelegt (Name, Bürozeiten, Zeitzone) | Büro-Oberfläche, Einstellungen | | |
| 1.4 | Benutzer in Keycloak angelegt, Rollen vergeben, TOTP für Inhaber aktiviert | `plattform/HOSTING.md` Abschnitt 6 | | |
| 1.5 | Kunden- und Artikelstamm importiert, Stichprobe von 10 Datensätzen geprüft | | | |
| 1.6 | Monteure, Qualifikationen, Abwesenheiten, Bereitschaftsplan erfasst | | | |
| 1.7 | Zeitfenster, Termindauern, Vorlauf konfiguriert | | | |
| 1.8 | Notdienstnummer und Gasnetzbetreiber-Entstörung hinterlegt | | | |
| 1.9 | Ansagetext und Gesprächsregeln eingetragen | `datenschutz/ki-offenlegung.md` | | |
| 1.10 | Rufnummer im SIP-Trunk dem Mandanten zugeordnet | `plattform/TELEFONIE.md` | | |
| 1.11 | Buchhaltungsanbindung (Lexware/sevDesk-Schlüssel bzw. DATEV-Belegtransfer) eingerichtet und Testbeleg übertragen | | | |
| 1.12 | E-Mail-Posteingang/Scan-to-Mail eingerichtet, Absenderliste gepflegt | | | |
| 1.13 | SMS-Versand: Absendername, Einwilligungstext | | | |
| 1.14 | Monitoring für Mandant aktiv (Anruf-Test alle 15 Minuten oder SIP-Registrierung) | | | |

## 2 Gemeinsam mit dem Betrieb

| Nr. | Schritt | erledigt | Notiz |
| --- | --- | --- | --- |
| 2.1 | Rufumleitung beim Telefonanbieter eingerichtet (Bedingung: ______) | | |
| 2.2 | Rückfallebene geprüft: Plattform simuliert nicht erreichbar → Anruf landet auf Mailbox/Mobil | | |
| 2.3 | Testanruf Neukunde, Standardanliegen | | |
| 2.4 | Testanruf Bestandskunde (Erkennung über Rufnummer) | | |
| 2.5 | Testanruf Notfall außerhalb der Bürozeit → Notdienstnummer wird genannt | | |
| 2.6 | Testanruf Gasgeruch → Sicherheitshinweis, kein Termin | | |
| 2.7 | Testanruf „Ich möchte einen Menschen sprechen“ → Rückruf vorgemerkt | | |
| 2.8 | Freigabe eines Termins durch das Büro, SMS-Bestätigung empfangen | | |
| 2.9 | Einweisung Büro und Inhaber durchgeführt | | |
| 2.10 | Einweisung Monteure („Mein Einsatz“, Rapport) durchgeführt | | |
| 2.11 | Go-live-Abnahme unterschrieben | | |

## 3 Nach dem Go-live

| Nr. | Schritt | Termin | erledigt |
| --- | --- | --- | --- |
| 3.1 | Kontrolle der ersten 20 echten Gespräche (Transkript, Erfassung, Einstufung) | Tag 1–3 | |
| 3.2 | Erste Wochenabstimmung, Gesprächsregeln nachschärfen | Woche 1 | |
| 3.3 | Monatsbericht (`vertrieb/email-vorlagen.md` Nr. 5) | Monatsende | |
