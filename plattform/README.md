# SHK-Backoffice-Plattform: Backend-Grundgerüst

Umsetzung der [Backend-Architektur](https://claude.ai/code/artifact/5e3d448e-c793-4d16-9df1-ab6385ae2abf)
als lauffähiger Code: kanonisches Datenmodell mit CRM und Disposition, Servicefall-Statusautomat,
Event-Log, Transactional Outbox, Freigabe-Inbox (Klassen A/B/C), Digital Twin des Betriebs,
OWL-Ontologie mit Turtle-/SPARQL-Export und ein Voice-Agent auf LiveKit Agents mit Tool-API.

Das Verzeichnis ist in sich geschlossen und kann unverändert in ein eigenes Repository umziehen.

```
plattform/
├── core/                 Core-Service (FastAPI, SQLAlchemy)
│   ├── modelle.py        kanonisches Datenmodell (29 Entitäten)
│   ├── domaene.py        Statusautomat, Event-Log, Outbox, Freigabe-Policies
│   ├── disposition.py    freie Zeitfenster: Qualifikation, Arbeitszeit, Fahrzeitpuffer, Kollision
│   ├── regeln.py         deterministische Regeln: Rufnummern, Dringlichkeit, Sicherheitsfälle
│   ├── twin.py           Digital Twin: Anlage (Wartung, Historie) und Betrieb (Bestand, Auslastung)
│   ├── ontologie.py      Anlagentypen-Vokabular, RDF-Export
│   ├── zustellung.py     Outbox-Worker mit Backoff, Dead-Letter, CSV-Degradierung
│   ├── management.py     Wartungsplanung, Kennzahlen, Plantafel, SPARQL über den Zwilling
│   ├── api.py            REST-API inkl. /voice/* für den Agenten
│   ├── auth.py           API-Key für Maschinen, OIDC-Token für Personen, Rollen und Rechte
│   ├── seed.py           fiktive Demodaten relativ zum heutigen Datum
│   ├── demo.py           Referenzfluss „Heizungsausfall bis Outbox“
│   └── ui/               Büro-Oberfläche (statisch, ohne Build-Schritt), ausgeliefert unter /app
├── migrations/           Alembic: Grundschema, Row-Level Security je Mandant
├── voice/                Voice-Agent (LiveKit Agents) und Tool-Client
├── ontologie/shk.ttl     Domänenontologie (OWL 2, Brick-Abgleich)
├── deploy/livekit/       LiveKit-Server, SIP-Gateway, Trunk und Dispatch-Regel
├── deploy/keycloak/      Realm „shk“: Rollen, Client mit PKCE, Mandanten-Claim
├── deploy/postgres/      Datenbankrollen (shk_app unter RLS, shk_system) und Init-Skripte
├── deploy/server/        Serverbetrieb: Compose-Override, Caddy, cloud-init, Deploy, Backup
├── HOSTING.md            Hosting in Deutschland, Einrichtung, Sicherung, Sicherheitskonzept
├── STIMME.md             Eigene Stimme des Inhabers für den Sprachagenten (Voice Cloning, Anbieter, Einwilligung)
├── docker-compose.yml    Profile: Basis, voice, ops (n8n), auth (Keycloak), ontologie (Fuseki), routing (OSRM), push (ntfy), monitoring (Prometheus, Alertmanager)
├── KATALOG.md            Open-Source-Komponenten mit Lizenz und Einbindungsart
├── drittanbieter/        mitgeführte Fremdartefakte: Lizenztexte, Brick-Ontologie, KoSIT-Validator, Wheelhouse, Image-Spiegel (spiegeln.sh)
└── VOICE-AGENT.md        Auswahl des Voice-Agenten und Aufbau des Voice-Backends
```

## Schnellstart in einem Befehl

```bash
cd plattform
./schnellstart.sh --name "Autohaus Beispiel" --branche kfz \
  --mitarbeiter "Ali Yilmaz:kfz_mechatroniker" --sachmittel "Bühne 1:hebebuehne"
```

Baut das Container-Image (oder zieht `SHK_IMAGE=ghcr.io/<owner>/shk-core:latest`), startet den Core mit
SQLite im Volume `shk-daten`, erzeugt einen API-Schlüssel (`.schnellstart.env`) und richtet den Betrieb
über den Assistenten ein: Branche, Betriebsdaten, Team, Sachmittel, Leistungskatalog, Online-Buchung.
Danach `http://localhost:8100/` öffnen; die Ansicht **Einrichtung** zeigt die restlichen Schritte
(IBAN, öffentliche Adresse, Impressum, Kundenimport). Varianten: `--demo` (fiktive Daten),
`--ohne-docker` (Python-Umgebung, Server im Vordergrund), `--postgres` (`docker compose` mit
PostgreSQL und Worker für den Dauerbetrieb). Für Internet-Betrieb mit TLS, Anmeldung und Telefonie:
HOSTING.md. Kunden aus der bisherigen Software: `python -m core import-kunden kunden.csv --mandant <ID>`
(oder in der Oberfläche unter Einrichtung; `--pruefen` zeigt nur die Spaltenzuordnung).

Entwicklung ohne Container:

```bash
pip install -r requirements.txt
python -m core demo                 # Referenzfluss im Speicher, ohne Telefonie und LLM
python -m pytest -q                 # Tests (SQLite)
python -m core serve --demo         # API und Oberfläche auf :8100 mit fiktiven Demodaten
python -m core einrichten --name "Müller Haustechnik" --branche shk   # eigener Betrieb statt Demodaten
```

Danach `http://localhost:8100/` öffnen (Oberfläche) bzw. `/docs` (OpenAPI). Ohne gesetzten
`CORE_API_KEY` bleibt das Schlüsselfeld der Anmeldung leer.

## Büro-Oberfläche

| Ansicht | Inhalt |
| --- | --- |
| Cockpit | Kennzahlen, Auslastung je Monteur, dringende Vorgänge, letzte Ereignisse (Betriebs-Zwilling); **Kundenkarte** (wo welche Kunden sind: Punkte je Kunde mit Dringlichkeit, Wartungsstatus, Vertrag, heutige Einsätze; Betriebsstandort; Zoom/Verschieben; Hintergrund vom konfigurierbaren Kachelserver), Kunden je Ort, Kachel „Kundendaten vollständig“ und Liste, welche Angaben fehlen; Tagestouren der Monteure als Linien mit Fahrzeit und Kilometern |
| Anruf simulieren | spielt die Werkzeugkette des Voice-Agenten ohne Telefon durch, inkl. Sicherheitsfall Gasgeruch |
| Freigabe-Inbox | offene Aktionen der Klassen B und C, Freigeben/Ablehnen mit Protokolleintrag |
| Vorgänge | Kanban über den Statusautomaten; Detail mit Terminvorschlag aus der Disposition und Statuswechsel |
| Plantafel | Einsätze je Monteur und Tag, vorgemerkt vs. bestätigt; Feiertage des Bundeslands markiert; **Tour je Monteur und Tag** (Fahrzeit und Kilometer je Anfahrt ab Betrieb, optimierte Reihenfolge, „Reihenfolge übernehmen“ taktet die Termine neu); **Kalender-Abo** (ICS-Adressen je Monteur und für den Betrieb) |
| Kunden & Anlagen | **Erneuerungskalender** je Anlage (Bauteile mit Nutzungsdauer, Prüfpflichten mit Rechtsgrundlage, Rücklage je Monat, Erneuert/Geprüft buchen), **Zählerstände** mit klimabereinigtem Jahresverbrauch, kWh/m²a und Hinweisen, **Gewährleistung/Garantie** je Kunde; CRM-Suche mit Spalte „Vollständig“ (Rufnummer, E-Mail, Anschrift, Koordinaten, Objekt, Anlage, Einwilligung) und Filter auf unvollständige Datensätze; Kunde anlegen und bearbeiten (inkl. Koordinaten), Einwilligungen mit Zeitstempel setzen und entziehen, Objekt und Anlage anlegen, Adressen geokodieren; Kundendetail mit Prüfliste und Anlagen-Zwilling (Wartungsstatus, Ontologieklasse) |
| Wartungsplanung | fällige Anlagen aus dem Zwilling, Planung als Servicefälle plus Kundenerinnerung zur Freigabe |
| Ontologie | vorbereitete SPARQL-Abfragen und freies Abfragefeld über TBox und Instanzdaten, optional mit RDFS-Inferenz (owlrl) |
| Ereignisprotokoll | Event-Log und Outbox inkl. Dead-Letter |
| Mein Einsatz (mobil) | Tagesliste des Monteurs mit Anfahrtszeit, Navigationslinks (geo-URI, Google Maps, Apple Karten, OSM) und Rufnummer, Stempeluhr (Arbeit/Fahrt/Pause), Rapport mit Fotos, Messwerten und Kundenunterschrift (Canvas, PDF), Belegfoto |
| Belege | Eingangsrechnungen und Lieferscheine: XRechnung/ZUGFeRD strukturiert, PDF und Fotos per Texterkennung mit Konfidenz; Kamera-Scan mehrseitig, Abruf von Scanner-Postfach und Eingangsordner; Prüfen, Zuordnen zum Vorgang, Übergabe an Lexware Office/sevDesk/DATEV (Belegtransfer, Upload-Mail) und im Belegpaket |
| Zeiten | Stempel- und Nachtragszeiten je Monteur und Monat, Lohnexport (CSV mit Nacht- und Sonntagsminuten) |
| Bereitschaft & Fristen | Bereitschaftsplan (vom Sprachagenten bei Notfällen genannt), Prüf-, Unterweisungs-, Zertifikats- und Vertragsfristen mit Folgefrist |
| Vorgang (Detail) | zusätzlich Rapporte, Fotos und Nachkalkulation (Deckungsbeitrag aus Zeiten, Belegen und Rechnungen); Kunde: Dokumentenablage |
| Anfragen | schriftlicher Eingang über Kontaktformular (öffentlicher Endpunkt mit Token, Honeypot, Drosselung) und IMAP-Postfach; Bestandskunden werden erkannt, aus Anfragen mit Rückkanal entstehen Vorgänge mit Dringlichkeitsbewertung |
| Angebote | Positionen aus dem Artikelstamm oder frei, eigener Nummernkreis, PDF; Versand (Freigabe B), Annahme erzeugt den Auftrag als Vorgang mit Auftragsbestätigung, Rechnung aus dem Angebot (Freigabe C), Ablauf im Tageslauf |
| Wartungsverträge | Laufzeit, Kündigungsfrist, Verlängerung, periodische Abrechnung als Freigabe C im Tageslauf, Kündigungsfrist als Frist, Kündigung zum nächstmöglichen Termin |
| Offene Posten | Restbeträge und Überfälligkeit, Zahlung buchen (Teilzahlung, Skonto, Ausbuchung), CSV-Bankimport mit Zuordnung über die Rechnungsnummer, Mahnlauf (Erinnerung B, Mahnung mit Gebühr C), Mahnungs-PDF |
| Auswertungen | Umsatz und Zahlungseingang je Monat, offene Posten, Angebotsquote, Vertragsbestand, Deckungsbeitrag je Monteur, Anfragen je Kanal |
| Lager & Großhandel | Bestände je Zentrallager und Fahrzeug mit Mindestbestand, Buchungen (Zugang/Abgang), automatische Abbuchung aus dem Rapport (Material mit Artikelnummer), Bestellvorschlag je Lieferant mit Übergabe als **IDS-Connect-Warenkorb** an den Shop des Großhändlers, **DATANORM**-Preislisten (Version 4 und 5) mit Trockenlauf in den Artikelstamm, CSV-Export |
| Globale Suche | Suchfeld in der Seitenleiste (Strg+K) über Kunden (auch Rufnummer), Vorgänge, Rechnungen, Angebote, Belege, Anfragen und Artikel, nach Rechten gefiltert; Treffer öffnen die Detailansicht |
| Rapport (Ergänzung) | **Wartungs-Checkliste** je Anlagentyp (Gas-Brennwert, Öl, Wärmepumpe, Solarthermie, Klima, Trinkwasser, Warmwasser, Sanitär) mit Prüfpunkten, Messwertgrenzen und Normbezug; Befunde (ok/auffällig/offen) landen am Rapport, Auffälligkeiten als Kundenhinweis |
| Angebote (Ergänzung) | **BEG-Förderrechner** (KfW 458 Heizungsförderung mit Grund-, Klimageschwindigkeits-, Einkommens- und Effizienz-Bonus; BAFA-Einzelmaßnahmen mit iSFP-Bonus) mit Übernahme als Vorbehaltstext in die Angebotseinleitung |
| Kundenportal (öffentlich) | `/portal/#<vorgang>/<token>`: Zeitachse „Anfrage eingegangen → Termin geplant → Wir sind unterwegs (aus der Fahrtzeit des Monteurs, Ankunft) → Arbeit erledigt → Rechnung“, Termin bestätigen oder absagen, **Wunschtermin** aus freien Fenstern wählen (ersetzt den Vorschlag, Freigabe im Büro), Termin als ICS in den eigenen Kalender, Angebote und Rechnungen als PDF, **GiroCode** zur Rechnung (Banking-App scannen), **Bewertung** mit Sternen nach erledigter Arbeit (kritische Bewertungen gehen als Hinweis ans Büro), „Ihr Haus/Ihr Fahrzeug“ mit Erneuerungen und Zählerstandsmeldung; Dunkelmodus, 44-px-Bedienelemente; Link steht in Terminbestätigung und Terminerinnerung (Tageslauf, Vortag) |
| Integrationen | Hub aller Anbindungen mit Zustand (aktiv, eingerichtet, gestört, aus), Outbox-Zahlen je Ziel (zugestellt, ausstehend, Dead Letter, letzter Fehler), **Verbindung prüfen** mit den gespeicherten Zugangsdaten (WhatsApp, Stripe, Lexware, sevDesk, seven.io, ntfy …), Einrichtungsformulare je Ziel, Webhook-Adressen für WhatsApp und Stripe, Systemdienste (Telefonie, OSRM, Nominatim, OIDC, Scanner, Großhandel) |
| Website | Eigene Internetseite des Betriebs aus den Stammdaten (`/web/<slug>` oder eigene Domain): Claim, Über uns, Leistungen mit optionalen Preisen, Online-Buchung, Kundenstimmen (Freigabe je Text, Mindeststerne, gekürzte Namen), Notdienst-Leiste, Team, Anfahrt/Einzugsgebiet, Kontaktformular mit Übernahme in die Anfragen (Honeypot, 30/h), Impressum- und Datenschutz-**Entwurf**, Sitemap, robots.txt, JSON-LD LocalBusiness mit Bewertungen; Vorschau vor Veröffentlichung, `noindex` solange nicht aktiv; serverseitig gerendert ohne Cookies und Skripte |
| Empfehlungen | Zusatzgeschäft je Kunde aus Erneuerungskalender, Ausfallrisiko, Heizungstausch mit Förderung (fossil ≥ 20 Jahre), Wartungsvertrag (10 % unter Einzelwartung), Saisonpaketen der Branche (SHK, Elektro, Kfz, Fahrrad, Dach, Garten, Salons …) und Folgeterminen; Potenzial, Annahmequote und Umsatz der letzten 90 Tage, Kunden nach Potenzial; Ein-Klick-Angebot (Entwurf oder Versand per Freigabe B), Vertrag anlegen, „Kein Bedarf“ (180 Tage Ruhe); Vorschläge auch im Kundendetail, im Vorgang (vor Ort anbieten), beim Anruf und im Kundenportal („Für Sie empfohlen“, Angebot anfordern und annehmen) |
| Einrichtung | Assistent mit Fortschritt (Pflichtschritte: Branche, Betriebsdaten, IBAN, Team, Leistungen, Kunden, öffentliche Adresse, Impressum/Datenschutz; optional Sachmittel, Online-Buchung, Telefonie), alles in einem Formular (idempotent), **Kundenimport aus CSV** (Spalten und Trennzeichen werden erkannt, Vorschau, Dubletten nach Rufnummer/E-Mail; Spalte „Kennzeichen“ legt je Kunde das Fahrzeug mit Kartei, HU-Fälligkeit und Kilometerstand an); Cockpit-Banner, solange Schritte fehlen |
| Sachmittel | Hebebühne, Arbeitsplatz, Raum, Ersatzfahrzeug, Gerät, Prüfstand als Ressourcen der Art `sachmittel` (Einstellungen → Team und Sachmittel); Leistungen und Vorgänge nennen das nötige Sachmittel, die Disposition bietet nur Fenster an, in denen eines frei ist, der Termin belegt es (automatische Wahl, Kollisionsprüfung) |
| Anlagenkartei | Je Anlagentyp eigene Felder (`branchen.ANLAGENKARTEI`): Fahrzeug (Kennzeichen, FIN, Erstzulassung, Kilometerstand, HU bis, Kraftstoff, Reifenlager, nächster Service, Schlüssel), Fahrrad, Elektroanlage, PV, Lüftung, Dach, Gartenanlage; validiert (Zahl, Datum, Text), HU-Datum setzt die Prüfpflicht im Erneuerungskalender, Kilometerstand wird als Zählerstand geführt; Direktannahme-Checklisten je Typ |
| Einstellungen (Ergänzung 2) | DATEV-Datenservices (Client-ID/-Secret, Berater-/Mandantennummer, Sandbox/Produktion) mit „DATEV verbinden“ (DATEV-Login, OAuth2 mit PKCE), IDS-Shop-Adresse, Portal-Absagefrist, Impressum-/Datenschutz-Links |
| Einstellungen (Branche) | **Branchenprofil** (17: SHK, Elektro, Friseur/Barbier, Kosmetik, Nagelstudio, Massage, Physiotherapie, Fußpflege, Hundesalon, Fahrradwerkstatt, Kfz-Werkstatt, Maler, Dachdecker, Garten- und Landschaftsbau, Gebäudereinigung, Tischlerei, Schlüsseldienst; je Profil Anlagentypen und Sachmittel): setzt Begriffe der Oberfläche (Monteur/Stylist/Therapeut, Einsatz/Termin, Kundenkartei), legt den Leistungskatalog mit Terminlänge, Qualifikation und Bruttopreis an, Takt und Puffer der Disposition, Wiederkehr-Intervall, Rolle des Sprachagenten, rechtliche Hinweise (Art. 9 DSGVO, KassenSichV, Heilmittel); Online-Buchung aktivieren/erneuern/abschalten |
| Kundenkartei (Branche) | Branchenspezifische Karteifelder (Farbrezeptur, Hauttyp, Kontraindikationen, Tier, Fahrzeug …); Gesundheitsdaten sind mit 🔒 markiert und nur mit Einwilligung „gesundheitsdaten“ beschreibbar; No-Show-Zähler je Kunde; Objekte/Anlagen nur in Handwerksprofilen |
| Gutscheine | Wertgutscheine ausstellen (Code XXXX-XXXX-XXXX, gültig bis Ende des dritten Folgejahres, § 195 BGB), teilweise einlösen, stornieren (nur ungenutzt), PDF mit QR-Code; Übersicht offener Gutscheinwerte |
| Vorgang (Ergänzung) | „Nicht erschienen“ für vergangene bestätigte Termine: Termin wird markiert, Zähler am Kunden erhöht, Vorgang zurück auf „qualifiziert“ |
| Online-Buchung (öffentlich) | `/buchung/#<token>`: Leistung wählen, optional Mitarbeiter, freie Zeiten je Tag (über den Tag verteilt, Mindestvorlauf, Buchungshorizont), Daten mit Einwilligung; erzeugt Kunde (Dublette über Rufnummer), Vorgang (Quelle web) und Terminvorschlag, der per Freigabe `termin_bestaetigen` (Klasse B, per Policy automatisch) verbindlich wird; Terminbestätigung mit Branchenhinweisen und No-Show-Hinweis; Drosselung je Token, Honeypot |
| Plantafel (Optimierer) | **Tagesplan optimieren**: alle Monteure eines Tages als Tourenplanungsproblem mit Zeitfenstern (VRPTW) – Fahrzeit, Verspätung gegenüber zugesagten Zeiten, Überstunden, Qualifikationen; lokale Suche (Relocate, Swap, 2-opt) plus Simulated Annealing, deterministisch; Toleranz für bestätigte Termine wählbar; Vorschlag mit Begründung, „Plan übernehmen“ erzeugt neue Terminbestätigungen |
| Warteliste | Einträge mit Wunschzeitraum/Tageszeit; Lücken der nächsten 7 Tage mit bestem Kandidaten; Absagen (Büro oder Portal) bieten die Lücke automatisch dem bestpassenden Eintrag an (Priorität, Wartezeit, Passung, Fahrweg), Reservierung mit Frist, Verfall und Weitergabe (Kaskade), Annahme im Portal |
| Dienstplan | Wochenplan je Mitarbeiter (Früh/Tag/Spät/Bereitschaft/frei, eigene Zeiten), ArbZG-Prüfung (Tageshöchstarbeitszeit, Pausen, Ruhezeit, Wochenstunden, Sonntagsarbeit), Wochenvorschlag aus Bedarf und Fairness, Zeitkonto (Soll/Ist/Saldo), Urlaubskonten, Qualifikationsmatrix mit Nachschulungsfristen |
| Cockpit (Ergänzung) | **Ausfallrisiko der Anlagen** (Weibull-Überlebensmodell je Anlagentyp mit Wartungsrückstand, Störhistorie, Rapportergebnis, Vertrag; Empfehlungen, Ersatzteilvorhaltung) und **Auslastungsprognose 8 Wochen** (Holt-Winters bzw. Saisonprior, Prognoseintervall, Kapazität aus Arbeitszeiten/Abwesenheiten/Feiertagen, Engpass- und Unterauslastungswarnungen) |
| Kunden (Dubletten) | „Dubletten prüfen“: Kölner Phonetik, Jaro-Winkler, Rufnummer, E-Mail, normalisierte Adresse mit Blocking; Zusammenführen hängt alle Fremdschlüssel generisch um, füllt Stammfelder auf, vereinigt Einwilligungen/Kartei, sichert die Quelle im Ereignisprotokoll |
| Vorgang (Dauer) | Neue Vorgänge ohne Dauerangabe erhalten die **lernende Einsatzdauer** aus Rapporten (hierarchisches Shrinkage Anlagentyp × Kategorie → Kategorie → Betrieb → Standard, 80-%-Quantil im 15-min-Raster); Termine im Vorgang lassen sich absagen (Lücke an die Warteliste) |
| Plantafel (Ergänzung) | Abwesenheiten (Urlaub, Krankheit, Schulung) sperren den Monteur ganztägig in Disposition und Sprachagent |
| Einstellungen (Ergänzung) | Bundesland (Feiertage), OSRM-Server, Kontoinhaber (GiroCode), Push an den Inhaber (ntfy), Artikel- und Leistungsstamm, Kontaktformular (Token, Snippet, Danke-Seite), IMAP-Posteingang, Angebots- und Mahnparameter, Bewertungslink (Bewertungsanfrage nach erledigtem Rapport als Freigabe B) |

Mit Docker: `cp .env.example .env`, Werte setzen, `docker compose up -d` (PostgreSQL, Migration,
Core, Worker). Anmeldung über Keycloak mit `--profile auth` und `OIDC_ISSUER=http://localhost:8180/realms/shk`,
Voice mit `--profile voice` (siehe [VOICE-AGENT.md](VOICE-AGENT.md)). Fahrzeiten mit `--profile routing` nach `deploy/osrm/vorbereiten.sh` (Kartenausschnitt von Geofabrik, `ROUTING_URL=http://osrm:5000`), Push mit `--profile push` (ntfy, `NTFY_BASE_URL`), Metriken und Alarme mit `--profile monitoring` (`deploy/monitoring/`, Prometheus unter 127.0.0.1:9090). Serverbetrieb: [HOSTING.md](HOSTING.md).

## Anmeldung, Rollen, Mandantentrennung

| Prinzipal | Nachweis | Mandant | Rechte |
| --- | --- | --- | --- |
| Maschine (Voice-Agent, n8n, Administration) | `X-API-Key` | Header `X-Mandant` | alle |
| Person | `Authorization: Bearer` (Keycloak, Authorization Code + PKCE) | Claim `mandant` im Token | nach Rolle |
| Entwicklung | weder `CORE_API_KEY` noch `OIDC_ISSUER` gesetzt | Header `X-Mandant` | alle |

| Recht | inhaber | buero | monteur | steuerberater |
| --- | :-: | :-: | :-: | :-: |
| lesen (CRM, Vorgänge, Plantafel, Twin, Ontologie) | ✓ | ✓ | ✓ | ✓ |
| bearbeiten (CRM, Servicefälle, Termine, Wartung, Voice-Tools) | ✓ | ✓ | | |
| Status „durchgeführt“ melden | ✓ | ✓ | ✓ | |
| Freigaben Klasse A/B | ✓ | ✓ | | |
| Freigaben Klasse C (finanzwirksam) | ✓ | | | |
| Rechnungen lesen, PDF/XRechnung, Belegpaket | ✓ | ✓ | | ✓ |
| Monteure/Ressourcen verwalten, Stammdaten, Anbindungen | ✓ | | | |

Monteure mit Anmeldename (`Ressource.benutzer` = `preferred_username` aus Keycloak) sehen in Termine,
Servicefälle und Plantafel nur die Einsätze ihrer eigenen Ressource; `GET /ich` meldet
`eingeschraenkt: true` und die zugeordneten Ressourcen.

Bei Personen stammt der Name im Event-Log aus dem Token; eine Angabe `wer` in der Anfrage wird
ignoriert. In PostgreSQL erzwingt zusätzlich Row-Level Security die Mandantentrennung
(Migration `0002`): Der Core läuft als Rolle `shk_app` ohne DDL- und ohne Bypass-Recht, jede
Transaktion setzt `app.mandant`, ohne diesen Wert sind keine Zeilen sichtbar. Worker, Seed und
Mandantenverwaltung nutzen `shk_system` (`SYSTEM_DATABASE_URL`). Schemaänderungen laufen nur über
`python -m core migrate` (`MIGRATION_DATABASE_URL`, Eigentümerrolle).

## API im Überblick

Fachendpunkte verlangen einen Mandantenkontext: bei Maschinen `X-Mandant`, bei Personen aus dem Token.

| Bereich | Endpunkte |
| --- | --- |
| CRM | `POST/GET /kunden`, `GET/PATCH /kunden/{id}` (mit `lat`/`lon`, Detail mit `vollstaendigkeit`), `PUT /kunden/{id}/einwilligungen`, `POST /kunden/{id}/objekte`, `POST /objekte/{id}/anlagen`, `GET /anlagentypen` |
| Touren, Kalender, Feiertage | `GET /tour?ressource_id=&tag=` (Fahrzeiten ab Betrieb, Linie, Vorschlag), `POST /tour/umsetzen`, `GET /kalender/links`, `GET /kalender/{ressource_id}.ics?token=` und `GET /kalender/betrieb/{mandant_id}.ics?token=` (öffentlich mit Token), `GET /feiertage?jahr=` |
| Betrieb/Monitoring | `GET /metrics` (Prometheus, Recht „verwalten“, hinter Caddy nicht öffentlich); Push-Ziel `inhaber` über `PUT /integrationen/inhaber` (ntfy, webhook) |
| Kundenkarte | `GET /kunden/karte` (Kundenpunkte, heutige Einsätze, Betriebsstandort, Kunden je Ort, Kachelserver), `GET /kunden/vollstaendigkeit`, `POST /kunden/geokodieren?limit=&alle=` (Nominatim, max. 1 Anfrage/s; CLI `python -m core geokodieren`) |
| Disposition | `POST/GET /ressourcen`, `GET /disposition/zeitfenster`, `GET /termine` |
| Vorgänge | `POST/GET /servicefaelle`, `GET /servicefaelle/{id}` (mit Event-Historie), `POST /servicefaelle/{id}/status`, `POST /servicefaelle/{id}/termine` |
| Freigabe | `GET /freigaben`, `POST /freigaben`, `POST /freigaben/{id}/entscheidung` |
| Betrieb | `GET /outbox?status=tot` (Dead-Letter), `GET /ereignisse` |
| Digital Twin | `GET /twin/betrieb`, `GET /twin/anlagen/{id}` |
| Ontologie | `GET /ontologie/tbox`, `GET /ontologie/export?mit_tbox=true` (text/turtle), `POST /ontologie/sparql` (nur SELECT/ASK) |
| Management | `POST /wartung/planen`, `GET /kennzahlen`, `GET /plantafel`, `GET/POST /mandanten`, `PATCH /ressourcen/{id}` |
| Finanzen | `GET /rechnungen`, `GET /rechnungen/{id}` (+ `/pdf` mit GiroCode, `/xrechnung`), `POST /rechnungen/{id}/storno`, `POST /kanzlei/belegpaket` (Rechnung und Storno entstehen über Freigaben Klasse C) |
| Betrieb/Einstellungen | `GET /betrieb`, `PUT /betrieb/stammdaten`, `GET /integrationen` (maskiert), `PUT /integrationen/{ziel}` (Ziele `fachsoftware`, `endkunde`, `finanz`, `kanzlei`) |
| Datenschutz | `GET /kunden/{id}/auskunft` (Art. 15), `POST /kunden/{id}/loeschen` (Art. 17, Sperrfristen), `POST /loeschlauf?trocken=true` |
| Anmeldung | `GET /auth/konfiguration` (öffentlich), `GET /ich` (Name, Rollen, Rechte) |
| Einsatz | `GET /einsatz/heute`, `POST /zeiten/start`, `POST /zeiten/{id}/stop`, `POST/GET /zeiten`, `GET /zeiten/lohnexport?monat=`, `POST /servicefaelle/{id}/rapport`, `GET /servicefaelle/{id}/rapporte`, `GET /rapporte/{id}/pdf`, `GET /servicefaelle/{id}/kalkulation` |
| Belege und Anhänge | `POST /belege` (Multipart: Datei und/oder Felder), `POST /belege/scan` (Multipart `bilder`, Kamera-Scan mehrseitig → PDF), `POST /belege/scaneingang/abrufen`, `GET /belege`, `GET/PATCH/DELETE /belege/{id}`, `POST /belege/{id}/uebergabe` (Steuertools), `GET/PUT /betrieb/scanner`, `POST /anhaenge` (Multipart), `GET /anhaenge?bezug_typ=&bezug_id=`, `GET /anhaenge/{id}/inhalt`, `DELETE /anhaenge/{id}` |
| Bereitschaft, Fristen | `GET/POST /bereitschaft`, `GET /bereitschaft/aktuell`, `DELETE /bereitschaft/{id}`, `GET/POST /fristen`, `POST /fristen/{id}/erledigt`, `DELETE /fristen/{id}` |
| Voice-Tools | `POST /voice/kunde-suchen`, `/voice/zeitfenster`, `/voice/servicefall`, `/voice/notdienst`, `/voice/gespraech-beendet` |
| Artikel, Angebote | `GET/POST /artikel`, `PATCH /artikel/{id}`, `GET/POST /angebote`, `GET/PATCH /angebote/{id}` (+ `/pdf`), `POST /angebote/{id}/senden`, `/annehmen`, `/ablehnen`, `/rechnung` |
| Forderungen | `GET /offene-posten`, `GET/POST /zahlungen`, `POST /zahlungen/bankimport` (Multipart CSV), `POST /mahnlauf`, `GET /mahnungen`, `GET /mahnungen/{id}/pdf` |
| Verträge, Abwesenheiten | `GET/POST /vertraege`, `GET /vertraege/{id}`, `POST /vertraege/{id}/kuendigen`, `GET/POST /abwesenheiten`, `DELETE /abwesenheiten/{id}` |
| Anfragen | `GET /anfragen`, `POST /anfragen/web/{token}` (öffentlich, Formular oder JSON), `GET/PUT /betrieb/webformular`, `GET/PUT /betrieb/posteingang`, `POST /posteingang/abrufen` |
| Lager, Großhandel | `GET /lager?lager=`, `POST /lager/buchen`, `GET /lager/bestellvorschlag`, `GET /lager/export.csv`, `POST /lager/warenkorb` (IDS-Connect-Formular als HTML), `POST /artikel/datanorm` (Multipart `datei`, `lieferant`, `aufschlag_prozent`, `trocken`) |
| Suche, Checklisten, Förderung | `GET /suche?q=`, `GET /checklisten`, `GET /checklisten/{anlagentyp}`, `POST /foerderung/rechner`, `GET /foerderung/heizungsarten` |
| Kundenportal (öffentlich, Token) | `GET /portal/` (Seite), `GET /portal/faelle/{id}?token=`, `POST /portal/faelle/{id}/termine/{termin_id}?token=` (`bestaetigen`/`absagen`), `…/angebote/{id}.pdf`, `…/rechnungen/{id}.pdf`; Büro: `GET /servicefaelle/{id}/portal` (Link) |
| Branche, Kartei, No-Show | `GET /branchen`, `GET/PUT /betrieb/branche` (`leistungen_anlegen`), `GET /voice/kontext` (Branchenkontext für den Sprachagenten), `PUT /kunden/{id}/kartei`, `POST /termine/{id}/nicht-erschienen` |
| Integrationen, WhatsApp, Online-Zahlung | `GET /integrationen/status` (Katalog mit Zustand, Outbox, Dienste), `POST /integrationen/{ziel}/{typ}/pruefen` (Verbindungstest), Ziel `endkunde` mit Kanal `whatsapp` (Meta Cloud API: `phone_number_id`, `token`, `verify_token`, Vorlagen), Ziel `zahlung` mit `stripe` (`secret_key`, `webhook_secret`); öffentlich `GET/POST /integrationen/whatsapp/webhook` (Verify-Token, eingehende Nachrichten → Kommunikation am offenen Vorgang oder neue Anfrage, idempotent je Nachrichten-ID), `POST /integrationen/stripe/webhook` (Signatur `t`/`v1`, `checkout.session.completed` → Zahlung `quelle=stripe`, idempotent je Session); Portal `POST /portal/faelle/{id}/rechnungen/{rid}/zahlung` (Checkout-URL, Rückkehr mit `?bezahlt=1`), Portal-Daten mit `naechster_schritt`, `betrieb.whatsapp` (wa.me-Link aus `whatsapp_nummer`), `rechnungen[].online_zahlung`; Einwilligung `whatsapp`; Website-Felder `faq`, Bereiche `ablauf`, `faq` |
| Website und Upsell | `GET/PUT /betrieb/website` (Slug, Domain, Claim, Farbe, Bereiche, Mindeststerne), `POST /bewertungen/{ereignis_id}/veroeffentlichen`; öffentlich `GET /web/{slug}` (`?vorschau=1`, `?gesendet=1`), `…/impressum`, `…/datenschutz`, `…/sitemap.xml`, `…/robots.txt`, `POST /web/{slug}/anfrage` (Formular), `GET /web/domain-pruefen?domain=` (Caddy on-demand TLS); eigene Domain per Host-Kopf; `GET /kunden/{id}/empfehlungen`, `GET /servicefaelle/{id}/empfehlungen`, `POST /kunden/{id}/empfehlungen/{schluessel}/angebot` (`versenden`), `…/wartungsvertrag/vertrag`, `…/{schluessel}/ablehnen`, `GET /upsell/uebersicht`, `GET /upsell/pakete`; Portal `POST /portal/faelle/{id}/empfehlungen/{schluessel}/anfordern`, `POST …/angebote/{aid}/annehmen`; `/voice/kunde-suchen` liefert `empfehlungen`; Tageslauf `upsell_kampagne` (Einwilligung E-Mail/SMS, alle 180 Tage, Freigabe B) |
| Einrichtung, Sachmittel, Anlagenkartei, Portal-UX | `GET /betrieb/einrichtung` (Fortschritt, nächste Schritte), `POST /betrieb/einrichten` (Branche, Name, Stammdaten, Mitarbeiter, Sachmittel, Online-Buchung; idempotent), `POST /kunden/import` (multipart CSV, `pruefen`), `POST /ressourcen` mit `art=sachmittel`, `sachmittel_typ`; `GET /disposition/zeitfenster?sachmittel=`, `POST /servicefaelle` mit `sachmittel`, Termine mit `sachmittel_id`; `POST /objekte/{id}/anlagen` und `PATCH /anlagen/{id}` mit `merkmale`; Portal: `GET/POST /portal/faelle/{id}/alternativen`, `GET …/termine/{tid}.ics`, `GET …/rechnungen/{rid}/girocode.png`, `POST …/bewertung`; Büro: `GET /bewertungen`; CLI `python -m core einrichten`, `python -m core import-kunden` |
| Gutscheine | `GET/POST /gutscheine`, `GET /gutscheine/{code}`, `POST /gutscheine/{code}/einloesen`, `POST /gutscheine/{code}/storno`, `GET /gutscheine/{code}/pdf` |
| Online-Buchung (öffentlich, Token) | `GET/PUT /betrieb/buchung?aktiv=` (Büro), `GET /buchung/` (Seite), `GET /buchung/{token}` (Betrieb, Leistungen, Mitarbeiter), `GET /buchung/{token}/zeiten?leistung_id=&ressource_id=&ab=`, `POST /buchung/{token}` |
| Optimierung, Warteliste | `GET /plantafel/optimieren?tag=&toleranz_minuten=`, `POST /plantafel/optimieren/umsetzen`, `GET /warteliste?tage=`, `POST /servicefaelle/{id}/warteliste`, `POST /warteliste/{id}/zurueckziehen`, `POST /warteliste/anbieten?ressource_id=&beginn=&ende=`, `POST /termine/{id}/absagen` |
| Lebenszyklus, Dienstplan, Zähler, Garantie, Qualifikationen | `GET /anlagen/{id}/bauteile`, `POST …/bauteile/anlegen` (Katalog), `POST …/bauteile` (eigenes), `POST /bauteile/{id}/erneuert`, `DELETE /bauteile/{id}`, `GET /kunden/{id}/erneuerungskalender?jahre=`, `GET /lebenszyklus/faellig?vorlauf_tage=`, `GET /lebenszyklus/katalog`; `GET /dienstplan?von=`, `GET /dienstplan/pruefung`, `GET/POST /dienstplan/vorschlag`, `POST /dienste`, `DELETE /dienste/{id}`, `GET /dienstplan/schichten`, `GET /zeitkonto?von=&bis=`, `GET /urlaub?jahr=`; `GET/POST /kunden/{id}/zaehlerstaende`, `GET /zaehler/auffaellig`, `POST /portal/faelle/{id}/zaehlerstand?token=`; `GET /servicefaelle/{id}/garantie`, `GET /kunden/{id}/gewaehrleistung`; `GET /qualifikationen/matrix`, `GET /qualifikationen/katalog`, `PATCH /ressourcen/{id}` (`qualifikationen_ablauf`, `wochenstunden`, `urlaubsanspruch_tage`) |
| Risiko, Prognose, Schätzung, Dubletten | `GET /risiko?limit=`, `GET /risiko/anlagen/{id}`, `GET /prognose/auslastung?wochen=`, `GET /schaetzung/dauer?anliegen=&anlagentyp=`, `GET /schaetzung/uebersicht`, `GET /dubletten?schwelle=`, `POST /kunden/{id}/zusammenfuehren` |
| DATEV-Datenservices | `PUT /integrationen/finanz` bzw. `/kanzlei` mit Kanal `datev`, `GET /integrationen/datev/anmeldung` (Anmelde-URL), `GET /integrationen/datev/rueckruf?code=&state=` (öffentlich, OAuth-Rückruf); Outbox-Aktionen `rechnung_uebermitteln` (Belegbilderservice, Rechnungsausgang), `beleg_uebermitteln` (Rechnungseingang), `belegpaket` (EXTF-Buchungsstapel an den Buchungsdatenservice) |
| Auswertung, Tageslauf | `GET /auswertungen?monate=12`, `POST /tagesaufgaben` (auch `python -m core tagesaufgaben`, Cron 06:30; `python -m core posteingang` und `python -m core scaneingang` alle 15 Minuten) |

Ontologie in Fuseki laden (Profil `ontologie`):
`curl -X POST -H "X-Mandant: …" localhost:8100/ontologie/export?mit_tbox=true -o daten.ttl` und
`curl -u admin:… -H "Content-Type: text/turtle" --data-binary @daten.ttl localhost:3030/shk/data`.

## Abweichungen von der Architektur und Begründung

| Architektur | Umsetzung | Grund |
| --- | --- | --- |
| Core in TypeScript/NestJS | Python/FastAPI | eine Sprache mit dem Voice-Agenten (LiveKit Agents, Pipecat sind Python-first) und dem bestehenden Prototyp |
| Schema je Mandant | Spalte `mandant_id`, RLS-fähig | einfachere Migrationen; Row-Level Security ohne Schemaänderung nachrüstbar |
| pg-boss als Queue | eigene Outbox-Tabelle + Worker | pg-boss ist Node-spezifisch; Semantik (Idempotenz, Backoff, Dead-Letter) identisch |
| Voice-SaaS im MVP | LiveKit Agents selbst betrieben, SaaS über Tool-API anschließbar | Anpassbarkeit und Datenhoheit, siehe VOICE-AGENT.md |
| CRM und Plantafel einkaufen | eigener CRM-/Dispositionskern im kanonischen Modell | dient Betrieben ohne Fachsoftware als System of Record; mit Fachsoftware bleibt sie führend und wird über Adapter (ExternalRef) synchronisiert |

## Stand und offene Punkte

Getestet (pytest): Referenzfluss Ende-zu-Ende, Statusautomat, Freigabeklassen, Dubletten,
Mandantentrennung, Disposition (Qualifikation, Wochenende, Kollision, Puffer), Sicherheitsfall
Gasgeruch, Outbox-Idempotenz und Dead-Letter, Digital Twin, SPARQL über Export und TBox, Tool-Client
des Voice-Agenten, OIDC-Tokenprüfung (Signatur, Ablauf, Audience, Issuer, Mandanten-Claim) und
Rollenmodell. Gegen PostgreSQL (`PG_TEST_URL`, in CI): Migrationen entsprechen dem Modell,
Row-Level Security trennt Mandanten auch ohne Filter im Code, `shk_app` darf kein DDL, Worker
arbeitet mandantenübergreifend. Manuell: Browser-Anmeldung gegen Keycloak 26.0.7 mit dem
mitgelieferten Realm, Oberfläche hinter Caddy mit Produktions-CSP. In CI zusätzlich: Docker-Build,
Import des Voice-Agenten, `docker compose config`, `caddy validate`.

Zusätzlich getestet (Runde 3): DATANORM-4/5-Import, Lagerbuchung und Rapport-Abbuchung, Bestellvorschlag, IDS-Formular, Checklisten-Befunde, Suche nach Rechten, Förderrechner, Kundenportal (Bestätigung, Absage mit Rückstufung, Frist, fremdes Token, Terminerinnerung idempotent), DATEV-Anmeldung mit PKCE-State, Rückruf, Token-Erneuerung, Belegbild- und Buchungsstapel-Upload mit Attrappe, Debitorenliste im Belegpaket.

Zusätzlich getestet (Runde 4): Branchenliste und Umstellung mit Leistungskatalog (idempotent), Sprachagent-Kontext, Kartei (unbekannte Felder, Gesundheitsdaten ohne/mit Einwilligung, Auskunft, Anonymisierung), No-Show (Zukunft abgelehnt, Zähler, Rückstufung), Online-Buchung (Angebot, Zeiten nach Qualifikation und Mitarbeiter, Mindestvorlauf, Honeypot, Dublette, Freigabe, Terminbestätigung mit Hinweisen, Abschalten), SHK-Buchung mit Adresse, Gutscheine (Teileinlösung, Storno, Ablauf, PDF), Wiederkehr-Erinnerung im Tageslauf (idempotent, Einwilligung).

Zusätzlich getestet (Runde 5): Optimiererkern (Bündelung, harte Qualifikation, fixe bestätigte Termine), Tagesplan über die API (Ersparnis, Umsetzung, Idempotenz, Toleranz), Warteliste (Eintrag, Lücken, Nachbesetzung nach Büro-Absage, Portal-Annahme mit Absage des späteren Termins, Verfall und Weitergabe im Tageslauf), Weibull-Hazard (Monotonie, Kovariaten, Treiber, Empfehlungen), Holt-Winters auf synthetischer Saison, Prognose mit Prior und Intervallen, Dauer-Schätzung (Kategorien, Ebenen, Shrinkage, Einfluss auf neue Vorgänge), Kölner Phonetik gegen Referenzwerte, Jaro-Winkler, Dublettenfund und generische Zusammenführung mit Audit.

Zusätzlich getestet (Runde 6): Bauteile aus dem Katalog beim Anlegen der Anlage (Fälligkeiten, Status, Rücklage, Idempotenz), Erneuerungskalender, Erneuert/Geprüft mit Ereignis, eigene Bauteile mit Validierung, Erinnerung nur mit Einwilligung und einmalig; Dienste übersteuern die Regelarbeitszeit in Disposition und Kapazität, ArbZG-Prüfung (10 h, Pausen, 11 h Ruhezeit, Sonntag), Wochenvorschlag (Abwesenheit, Fairness, Schichtbetrieb, Übernahme, keine Doppelungen), Zeitkonto mit Gutschrift, Urlaubskonto; Gradtagszahl-Anteile, klimabereinigte Anomalie mit Hinweis, rückläufiger Stand/Zählerwechsel, Takten; Gewährleistung aus eigenem Rapport (BGB/VOB), Herstellergarantie aus Frist, Hinweis als Ereignis beim neuen Vorgang; Qualifikationsmatrix (abgelaufen/läuft ab/fehlt, Abdeckung, Schlüsselpersonenrisiko) und Wirkung abgelaufener Qualifikationen auf die Disposition; Portal „Ihr Haus“ mit Zählerstandsmeldung; Löschkonzept mit Bauteilen und Zählerständen.

Zusätzlich getestet (Runde 9): Integrationsstatus (Katalog, Kundenkanäle, Dienste), Verbindungstests (ohne Zugangsdaten, unbekanntes Ziel 404, falscher Typ 422, WhatsApp-Prüfaufruf mit Bearer-Token, maskierte Geheimnisse, E-Mail ohne Prüfaufruf); WhatsApp-Adapter (Nummernformat E.164, kein Versand ohne Einwilligung, Vorlage mit Parameter, Freitext ohne Vorlage, keine Rechnungen); WhatsApp-Webhook (Verify-Token richtig/falsch/ungültig, Nachricht am offenen Vorgang mit Kommunikation, Idempotenz, neue Anfrage mit Kanal whatsapp, unbekannte phone_number_id, Statusmeldungen, kein JSON); Stripe (Portal ohne Einrichtung 409, Konfigurationsprüfung, Balance-Test, Checkout mit Cent-Betrag/Zahlungsarten/Idempotency-Key/ExternalRef, Webhook mit falscher, zu alter und gültiger Signatur, unbekannte Session 404, fremde Ereignisse ignoriert, Buchung idempotent, danach 409 und nächster Schritt „Bewertung“); Portal-nächster-Schritt (freie Zeiten, Vorschlag bestätigen, vorgemerkt, alles geplant), wa.me-Link und Farbe in Portal und Buchung; Website mit Ablauf, FAQ (eigene, Standard, abschaltbar), WhatsApp-Knopf, Kontaktwegen, Skip-Link und lesbaren Qualifikationen.

Zusätzlich getestet (Runde 8): Website-Rendering aus Stammdaten (404 bis zur Aktivierung, Vorschau mit `noindex`, Slug-Normalisierung, Farb- und Domain-Validierung, Cache-Control, JSON-LD, Notdienst, Team, Einzugsgebiet, kein Skript, Buchungslink erst nach Aktivierung, Impressum/Datenschutz/Sitemap/robots, Ereignis `website.geaendert`); Kontaktformular (Honeypot, Pflichtfelder als 422 im HTML, Redirect mit Bestätigung, Übernahme in Anfragen mit Adresse, Idempotenz innerhalb der Stunde), eigene Domain per Host-Kopf und `domain-pruefen`, Kundenstimmen erst nach Freigabe und ab Mindeststernen, Slug-/Domain-Kollision zwischen Mandanten; Empfehlungen (Erneuerung mit Netto-Positionen, Förderung ab 20 Jahren, Saisonpaket im November, Paket per Stichwort im Anliegen, Angebot mit Versandfreigabe, 180-Tage-Unterdrückung und 409, Beratung ohne Festpreis 409, Ablehnen, Vertrag, Übersicht je Art, Voice-Empfehlungen, Paketkataloge Kfz/SHK, Folgetermin Friseur); Portal (Angebot anfordern → Entwurf + Hinweis, Annahme nur versendeter Angebote); Kampagne (ohne Einwilligung nichts, mit Einwilligung Freigabe B, am Folgetag nichts). 

Zusätzlich getestet (Runde 7): Sachmittel in Zeitfenstern, automatische Wahl beim Termin, Kollision bei einziger Bühne, nicht angelegtes Sachmittel, Typprüfung; Fahrzeugkartei (unbekannte Felder, ungültiges Datum, Zahlen), HU-Datum → Prüfpflicht, Kilometerstand → Zählerstand, Merge beim Ändern, Anzeige im Portal; 17 Profile mit Anlagentypen, Sachmitteln, Checklisten und Kartei je Typ, Umstellung auf Dachdecker mit Sprachkontext; Einrichtungsstatus, Assistent idempotent mit Buchungs-Token, Validierung; CSV-Import (Trennzeichen, Synonyme, Vor-/Nachname, Latin-1, Vorschau ohne Schreiben, Fahrzeuge, Dubletten im zweiten Lauf, fehlende Namensspalte); CLI `einrichten` und `import-kunden` als Unterprozess; Portal-Zeitachse mit „unterwegs“ aus der Fahrtzeit, ICS je Termin, Bewertung erst nach erledigter Arbeit mit Hinweis ans Büro und Auswertung, GiroCode-PNG zur Rechnung; Wunschtermin aus Alternativen (Vergangenheit abgelehnt, alter Vorschlag zurückgezogen, Freigabe offen, kein Zugriff ohne Token). Browser-Prüfung mit Playwright ohne Konsolenfehler (Einrichtung, Kundenimport, Sachmittel, Fahrzeugkartei, Portal hell/dunkel).

Zusätzlich getestet: Rechnungsnummern, XRechnung 3.0 (UBL, Pflichtfelder), PDF, Storno, Adapter
Lexware/sevDesk/E-Mail/SMS/Webhook mit Attrappen, DATEV-Buchungsstapel im Belegpaket, verschlüsselte
Zugangsdaten, Löschlauf mit Fristen und Sperrfristen, Auskunft, Sichtbarkeit der Monteure. Scanner-Eingang
(Kamera-PDF, Scan-to-Mail mit Absenderliste, Eingangsordner) und Belegübergabe an Lexware Office, sevDesk,
DATEV-Belegtransfer-Ordner und Upload-Mail mit Attrappen, idempotent je Zielsystem.

Betriebsmodule (`core/einsatz.py`, `core/belege.py`, Migration 0005): Zeitstempel mit automatischem
Wechsel, Rapport mit Unterschrift (PNG) und PDF samt Statuswechsel auf „durchgeführt“,
Nachkalkulation (Sätze aus den Stammdaten), Bereitschaft mit Überschneidungsprüfung und
Sprachagenten-Werkzeug, Fristen mit Folgefrist, Anhänge (Fotos auf 1600 px verkleinert, 10 MB),
Lohnexport, Belegeingang aus XRechnung (eigener Export als Testfall), ZUGFeRD (PDF mit eingebettetem
XML), Text-Heuristik mit Konfidenz, manuell, Schutz übergebener Belege, Eingangsbelege und
ER-Buchungen im DATEV-Stapel, Löschfristen für Belege (10 Jahre) und Zeiten (2 Jahre), Rollen des
Monteurs (nur eigene Ressource und Einsätze). Texterkennung für Fotos braucht Tesseract
(`docker build --build-arg MIT_OCR=1`); ohne OCR bleibt der Beleg mit Hinweis zur Nacherfassung.

Scanner-Eingang und Belegübergabe (`core/scanner.py`, `core/adapter/belegtransfer.py`, Migration 0007): drei
Eingangswege mit Dublettenschutz über `Beleg.import_kennung` (Message-ID, Dateihash): Scan-to-Mail (eigenes
IMAP-Postfach des Multifunktionsgeräts, nur eingetragene Absender, je Anhang ein Beleg), Eingangsordner
(Netzlaufwerk des Scanners, im Container eingehängt, verarbeitete Dateien nach `verarbeitet/`) und Kamera-Scan
(mehrere Fotos werden zu einem Graustufen-PDF zusammengefasst). Geprüfte Belege (`PATCH status=geprueft`) gehen
über die Outbox (`beleg_uebermitteln`) an die Steuertools: Lexware Office (Files-API, Belegtyp `voucher`),
sevDesk (Beleg-Entwurf, Status 50, ohne Buchungskonto), DATEV-Belegtransfer-Ordner
(`<Berater>_<Mandant>/Rechnungseingang/JJJJ-MM/`) oder Upload-Mail an DATEV Unternehmen online
(`upload_email` am E-Mail-Kanal der Kanzlei). Jede Übergabe ist idempotent (ExternalRef je Zielsystem), die
Kontierung bleibt im Zielsystem (§ 6 Nr. 3 StBerG).

Auftrags- und Forderungsmodule (`core/angebote.py`, `core/forderungen.py`, `core/vertraege.py`, `core/anfragen.py`,
`core/tagesaufgaben.py`, Migration 0006): Angebote mit Artikelstamm und Nummernkreis, Versand/Annahme/Ablehnung/Ablauf,
Auftrag und Rechnung aus dem Angebot; Zahlungen mit Teilzahlung und Statusfortschreibung, CSV-Bankimport (UTF-8 und
cp1252, Trennzeichen automatisch, idempotent je Umsatz), dreistufiges Mahnwesen ausschließlich über Freigaben mit
Karenz, Fristen und Gebühren aus den Stammdaten; Wartungsverträge mit Laufzeitlogik, Abrechnungslauf und
Kündigungsfrist; Abwesenheiten in der Disposition; Anfrageeingang über Webformular (Token je Betrieb, Honeypot,
60 Anfragen je Stunde) und IMAP mit Autoreply-Filter, Erkennung von Bestandskunden und Sicherheitsfall;
Bewertungsanfrage nach erledigtem Rapport; Auswertungen; Löschfristen für Angebote (3 Jahre), beendete Verträge
(3 Jahre), Anfragen (1 Jahr). Alles Finanz- und Kundenwirksame entsteht weiterhin nur als Freigabe.

Zustellung und Anbindungen (`core/adapter/`): je Mandant und Ziel wählbar unter „Einstellungen“;
ohne Konfiguration CSV-Degradierung (`fachsoftware`, `finanz`) bzw. Büro-Aufgabe „ohne Kanal“
(`endkunde`). Zugangsdaten liegen mit `INTEGRATIONS_KEY` verschlüsselt in der Datenbank.
Fachsoftware-Adapter für openHandwerk, pds u. a. folgen nach API-Klärung über denselben Mechanismus
(`Webhook` als generischer Übergang).

Rechtliche Unterlagen für den Pilot (Entwürfe, juristisch prüfen lassen): `recht/` mit AVV, TOM, VVT,
Löschkonzept (deckungsgleich mit `core/loeschung.py`) und Datenschutzhinweis für das Telefon.

Bereitgestellt, aber nur mit Konten des Betreibers ausführbar:
- echter Anruf über SIP: TELEFONIE.md, `deploy/livekit/trunk-anlegen.sh`, `python -m voice.probe`
- Erstausrollung: HOSTING.md, `deploy/server/einrichten.sh` (erzeugt `.env`, rollt aus, legt Mandant an; `--lokal` baut die Images auf dem Server)
- eigene Stimme: STIMME.md (ElevenLabs, Cartesia oder Azure Custom Neural Voice über `TTS_ANBIETER`/`TTS_STIMME`)
- eigenes Repository: `deploy/repo-abspalten.sh` (git subtree split, Workflows angepasst)
- Finanzanbindung (Lexware Office, sevDesk, DATEV, XRechnung über Mustang Project)
- AVV, TOMs, Löschkonzept: rechtliche Unterlagen vor dem ersten Pilotkunden
- Namespace der Ontologie (`https://w3id.org/shk-backoffice/ont#`) ist ein Platzhalter
