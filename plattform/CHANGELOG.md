# Änderungsprotokoll

Versionsschema: MAJOR.MINOR.PATCH (`VERSION`, `python -m core version`, `/health`). Jede Runde erhöht MINOR; Hotfixes PATCH.
Datenbankänderungen stehen bei der jeweiligen Version mit Migrationsnummer; `python -m core migrate` vor dem Start der neuen Version.

## 0.13.0 – 2026-11-08 (Runde 13, Migration 0016)

Verkaufsreife gegen die belegten Probleme der Leads (Problemkatalog P1–P10, `VERKAUF.md`).

- **Karriereseite (P10)**: Stellenangebote in den Website-Einstellungen (`stellen`: Titel, Art, Ort, Arbeitszeit, Start, Vergütung, Aufgaben, Profil, Leistungen, Ablaufdatum), öffentlich unter `/web/<slug>/karriere` und `/karriere/<id>`, JobPosting-Auszeichnung für Google for Jobs, Sitemap, „Wir stellen ein“ auf der Startseite. Kurzbewerbung ohne Anschreiben mit bis zu drei Unterlagen (PDF/JPEG/PNG, Signaturprüfung), Pflicht-Datenschutzhinweis, freiwilliger Talentpool. Neue Ansicht **Bewerbungen** (Status, Notiz, Unterlagen, Löschen); Tabelle `bewerbung`, Löschfristen 180 Tage nach Abschluss bzw. 730 Tage mit Talentpool im Löschlauf.
- **Anfrageformular (P2, P5)**: bis zu drei Fotos je Anfrage (JPEG/PNG/WebP/HEIC, Signaturprüfung, Verkleinerung) direkt am Vorgang; Kennzeichnung „Notfall“ hebt auf Notdienst und zeigt nach dem Absenden die Notdienstnummer.
- **Notfallalarm für schriftliche Kanäle**: Website, Webformular, E-Mail und WhatsApp alarmieren bei Notdienst- und Sicherheitsfällen wie ein Anruf (Push mit Priorität 5 und Rückrufnummer); bisher kam nur ein Info-Hinweis.
- **Rufumleitungsassistent (P7)**: `GET/PUT /betrieb/rufumleitung` erzeugt die GSM-Steuercodes (3GPP TS 22.030/22.082) für bedingte Umleitung auf den Telefonassistenten als antippbare `tel:`-Links, Hinweise für iPhone, Android, FRITZ!Box und Telekom-Festnetz; Einrichtungsschritt „Rufumleitung“.
- **Website-Check (P1, P6, P9) für den Vertrieb**: `python -m core website-check <url>` bzw. `--datei urls.txt --csv ergebnis.csv`, `POST /betreiber/website-check`; prüft Erreichbarkeit, HTTPS, Mobiltauglichkeit, CMS-/jQuery-Alter, Copyright-Jahr, Google Fonts/Maps/Tracking ohne Einwilligung, Impressum/Datenschutz (TMG-Verweis, Privacy Shield, Platzhalter), Formular, Buchung, Portal, Notdienst, Mobilnummer, Karriere; Befunde mit Beleg und KUHN-Leistung, Bedarfsscore 0–100. SSRF-Schutz (nur öffentliche Adressen), höchstens drei Seiten.
- **Editionen gemäß Preisliste**: Basis 349 €, Pro 649 €, Premium 999 € netto/Monat (Einrichtung 1.490/2.990/4.990 €, 300/600/1.000 Inklusivminuten, Mehrminute 0,25 €); Basis enthält jetzt Telefonagent und Website (vorher ohne Telefonagent zu 149 €). Betreiberkonsole zeigt Sprachminuten und Mehrminuten je Mandant. Produktname standardmäßig „KUHN“.
- Fehlerbehebungen: „GmbH & Co. KG“ in E-Mail-Signaturen löste einen Kohlenmonoxid-Sicherheitsfall aus; „Heizung austauschen“ galt als Heizungsausfall; die Formularkennung des Webformulars nutzte `hash()` (je Prozess zufällig) und war damit über Worker hinweg nicht idempotent; Hauptnavigation der Betriebswebsite war auf Smartphones nicht ausgeblendet (horizontaler Überlauf); Bestätigungsseiten wurden öffentlich gecacht.

## 0.12.0 – 2026-11-01 (Runde 12, Migration 0015)

- Betreiberstimme: Stimmprofil je Mandant mit Referenzaufnahme (WAV/FLAC/MP3, 5–180 s) und dokumentierter Einwilligung (`core/stimme.py`, Tabelle `stimmprofil`); genau ein aktives Profil; Widerruf löscht die Aufnahme sofort und entfernt die Stimme aus dem Stimmdienst.
- Stimmdienst `stimme/`: selbst gehostete Stimmklonung mit Chatterbox Multilingual (MIT, Resemble AI; Perth-Wasserzeichen) hinter einer OpenAI-kompatiblen Schnittstelle (`/v1/audio/speech`, `/v1/voices`), satzweises Streaming, Test-Engine ohne GPU, Compose-Profil `stimme` mit GPU-Reservierung, eigenes Dockerfile.
- Sprachagent: holt `GET /voice/konfiguration` (Begrüßung mit Offenlegung der nachgebildeten Stimme, Stimmprofil, Weiterleitung, Minutenkontingent), spricht über den Stimmdienst mit Rückfall auf die Standardstimme (`FallbackAdapter`), Mandant aus Job-Metadaten, Werkzeug `weiterleiten` (SIP-Transfer zur Bereitschaft, Policy `notdienst_weiterleiten`), Turn-Handling mit optionalem lokalem End-of-Turn-Modell, vorgezogene Antwortgenerierung, Latenz-Kennzahlen und Gesprächsdauer an den Core.
- Feature `eigene_stimme` (Pro, Enterprise); Sprachminuten je Monat aus den Gesprächsdauern; Einstellungen-Karte „Stimme des Sprachagenten“ (Aufnahme, Einwilligungstext, Hörprobe, Aktivieren, Widerruf); `voice.probe` prüft den Stimmdienst.

## 0.11.0 – 2026-10-28 (Runde 11, keine Migration)
- UX-Überarbeitung der Büro-Oberfläche: Designsystem in `app.css` (Tokens für Farbe, Abstand, Typografie, Radien, Schatten, Fokusringe,
  Dunkelmodus auch per `data-theme`), Navigation in fünf Gruppen mit Icons, Mobilansicht mit Schublade und Kopfleiste, Ladeskelett,
  Seitentitel je Ansicht, Dialoge mit Schließen-Knopf, klebrige Tabellenköpfe, einheitliche Knöpfe/Formulare/Tags/Leerzustände, Druckansicht.
- Portal, Online-Buchung, Registrierungsseite und Betriebswebsite auf dieselben Tokens gebracht (Schatten, Fokus, Kartenradius).

## 0.10.0 – 2026-10-28 (Runde 10, Migration 0014)
- Direkt eingebundene Open-Source-Bausteine: Leaflet 1.9.4 (Kundenkarte mit Kacheln, Touren, Tooltips), uPlot 1.6.31 (Umsatz- und Zahlungsdiagramm),
  openpyxl (Excel-Exporte für Auswertungen, Kunden, offene Posten, Termine), Jinja2 (Textvorlagen in der Sandbox je Betrieb mit Vorschau).
- Produktisierung: Editionen Starter/Pro/Enterprise mit Funktionsumfang und Limits, Ed25519-signierte Lizenzschlüssel (`python -m core lizenz`),
  30-Tage-Testphase, Feature-Gates (HTTP 402), Betreiberkonsole (`/betreiber/*`, Oberfläche „Betreiberkonsole“), Selbstregistrierung
  (`/registrieren`, `REGISTRIERUNG_OFFEN=1`), White-Label über `PRODUKT_*`, Versionsstand in `/health`, PWA-Manifest und Service Worker für „Mein Einsatz“.
- Release-Paket: `release.sh` erzeugt Quell- und Image-Archiv mit Prüfsummen.

## 0.9.0 – 2026-10-28 (Runde 9)
- Integrations-Hub mit Zustand und Verbindungstests, WhatsApp (Meta Cloud API) mit Rückkanal, Stripe Checkout im Portal, kundenfreundliche Layouts.

## 0.8.0 – 2026-10-28 (Runde 8)
- Betriebswebsite je Mandant (Slug oder eigene Domain, Caddy on-demand TLS), Upsell-Modul mit Empfehlungen, Paketen und Kampagne.

## 0.7.0 – 2026-10-27 (Runde 7, Migration 0013)
- Sachmittel in der Disposition, Anlagenkartei für Kfz/PV/Dach, Einrichtungsassistent mit CSV-Import, `schnellstart.sh`, GHCR-Images, Portal mit Zeitachse, Wunschtermin, ICS, GiroCode, Bewertung.

## 0.6.0 – 2026-10-26 (Runde 6, Migration 0012)
- Erneuerungskalender je Anlage, Dienstplan mit ArbZG-Prüfung, Zeit- und Urlaubskonten, klimabereinigte Zählerstände, Gewährleistung/Garantie, Qualifikationsmatrix.

## 0.5.0 – 2026-10-25 (Runde 5)
- Tagesplan-Optimierer, Warteliste, Ausfallrisiko, Prognose, Dauerschätzung, Dublettenabgleich.

## 0.4.0 – 2026-10-24 (Runde 4)
- Branchenprofile (Salons, Praxen, Werkstätten …), Online-Buchung, Kundenkartei, Gutscheine.

## 0.3.0 – 2026-10-23 (Runde 3)
- DATEV-Datenservices, Kundenportal, Großhandel und Lager, Checklisten, Suche, Förderrechner.

## 0.2.0 – 2026-10-23 (Runde 2)
- Touren (OSRM), Kalender-Abo, Feiertage, GiroCode, IBAN-Prüfung, Push (ntfy), Prometheus-Metriken, Ontologie-Inferenz.

## 0.1.0 – 2026-10-22 (Runde 1)
- Core-Service mit Mandanten, CRM, Disposition, Freigaben, Twin, Ontologie, Voice-Werkzeugen, Rechnungen/XRechnung, Adaptern, Betriebs- und Auftragsmodulen, Scanner-Eingang, Löschkonzept, Hosting.
