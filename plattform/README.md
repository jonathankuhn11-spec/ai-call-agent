# SHK-Backoffice-Plattform: Backend-Grundgerüst

Umsetzung der [Backend-Architektur](https://claude.ai/code/artifact/5e3d448e-c793-4d16-9df1-ab6385ae2abf)
als lauffähiger Code: kanonisches Datenmodell mit CRM und Disposition, Servicefall-Statusautomat,
Event-Log, Transactional Outbox, Freigabe-Inbox (Klassen A/B/C), Digital Twin des Betriebs,
OWL-Ontologie mit Turtle-/SPARQL-Export und ein Voice-Agent auf LiveKit Agents mit Tool-API.

Das Verzeichnis ist in sich geschlossen und kann unverändert in ein eigenes Repository umziehen.

```
plattform/
├── core/                 Core-Service (FastAPI, SQLAlchemy)
│   ├── modelle.py        kanonisches Datenmodell (12 Entitäten)
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
├── docker-compose.yml    Profile: Basis, voice, ops (n8n), auth (Keycloak), ontologie (Fuseki)
├── KATALOG.md            Open-Source-Komponenten mit Lizenz und Einbindungsart
└── VOICE-AGENT.md        Auswahl des Voice-Agenten und Aufbau des Voice-Backends
```

## Schnellstart

```bash
cd plattform
pip install -r requirements.txt
python -m core demo                 # Referenzfluss im Speicher, ohne Telefonie und LLM
python -m pytest -q                 # Tests (SQLite)
python -m core serve --demo         # API und Oberfläche auf :8100 mit fiktiven Demodaten
```

Danach `http://localhost:8100/` öffnen (Oberfläche) bzw. `/docs` (OpenAPI). Ohne gesetzten
`CORE_API_KEY` bleibt das Schlüsselfeld der Anmeldung leer.

## Büro-Oberfläche

| Ansicht | Inhalt |
| --- | --- |
| Cockpit | Kennzahlen, Auslastung je Monteur, dringende Vorgänge, letzte Ereignisse (Betriebs-Zwilling) |
| Anruf simulieren | spielt die Werkzeugkette des Voice-Agenten ohne Telefon durch, inkl. Sicherheitsfall Gasgeruch |
| Freigabe-Inbox | offene Aktionen der Klassen B und C, Freigeben/Ablehnen mit Protokolleintrag |
| Vorgänge | Kanban über den Statusautomaten; Detail mit Terminvorschlag aus der Disposition und Statuswechsel |
| Plantafel | Einsätze je Monteur und Tag, vorgemerkt vs. bestätigt |
| Kunden & Anlagen | CRM-Suche, Kundendetail mit Anlagen-Zwilling (Wartungsstatus, Ontologieklasse) |
| Wartungsplanung | fällige Anlagen aus dem Zwilling, Planung als Servicefälle plus Kundenerinnerung zur Freigabe |
| Ontologie | vorbereitete SPARQL-Abfragen und freies Abfragefeld über TBox und Instanzdaten |
| Ereignisprotokoll | Event-Log und Outbox inkl. Dead-Letter |
| Mein Einsatz (mobil) | Tagesliste des Monteurs mit Anfahrt und Rufnummer, Stempeluhr (Arbeit/Fahrt/Pause), Rapport mit Fotos, Messwerten und Kundenunterschrift (Canvas, PDF), Belegfoto |
| Belege | Eingangsrechnungen und Lieferscheine: XRechnung/ZUGFeRD strukturiert, PDF und Fotos per Texterkennung mit Konfidenz; Prüfen, Zuordnen zum Vorgang, Übergabe im Belegpaket |
| Zeiten | Stempel- und Nachtragszeiten je Monteur und Monat, Lohnexport (CSV mit Nacht- und Sonntagsminuten) |
| Bereitschaft & Fristen | Bereitschaftsplan (vom Sprachagenten bei Notfällen genannt), Prüf-, Unterweisungs-, Zertifikats- und Vertragsfristen mit Folgefrist |
| Vorgang (Detail) | zusätzlich Rapporte, Fotos und Nachkalkulation (Deckungsbeitrag aus Zeiten, Belegen und Rechnungen); Kunde: Dokumentenablage |
| Anfragen | schriftlicher Eingang über Kontaktformular (öffentlicher Endpunkt mit Token, Honeypot, Drosselung) und IMAP-Postfach; Bestandskunden werden erkannt, aus Anfragen mit Rückkanal entstehen Vorgänge mit Dringlichkeitsbewertung |
| Angebote | Positionen aus dem Artikelstamm oder frei, eigener Nummernkreis, PDF; Versand (Freigabe B), Annahme erzeugt den Auftrag als Vorgang mit Auftragsbestätigung, Rechnung aus dem Angebot (Freigabe C), Ablauf im Tageslauf |
| Wartungsverträge | Laufzeit, Kündigungsfrist, Verlängerung, periodische Abrechnung als Freigabe C im Tageslauf, Kündigungsfrist als Frist, Kündigung zum nächstmöglichen Termin |
| Offene Posten | Restbeträge und Überfälligkeit, Zahlung buchen (Teilzahlung, Skonto, Ausbuchung), CSV-Bankimport mit Zuordnung über die Rechnungsnummer, Mahnlauf (Erinnerung B, Mahnung mit Gebühr C), Mahnungs-PDF |
| Auswertungen | Umsatz und Zahlungseingang je Monat, offene Posten, Angebotsquote, Vertragsbestand, Deckungsbeitrag je Monteur, Anfragen je Kanal |
| Plantafel (Ergänzung) | Abwesenheiten (Urlaub, Krankheit, Schulung) sperren den Monteur ganztägig in Disposition und Sprachagent |
| Einstellungen (Ergänzung) | Artikel- und Leistungsstamm, Kontaktformular (Token, Snippet, Danke-Seite), IMAP-Posteingang, Angebots- und Mahnparameter, Bewertungslink (Bewertungsanfrage nach erledigtem Rapport als Freigabe B) |

Mit Docker: `cp .env.example .env`, Werte setzen, `docker compose up -d` (PostgreSQL, Migration,
Core, Worker). Anmeldung über Keycloak mit `--profile auth` und `OIDC_ISSUER=http://localhost:8180/realms/shk`,
Voice mit `--profile voice` (siehe [VOICE-AGENT.md](VOICE-AGENT.md)). Serverbetrieb: [HOSTING.md](HOSTING.md).

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
| CRM | `POST/GET /kunden`, `GET/PATCH /kunden/{id}`, `POST /kunden/{id}/objekte`, `POST /objekte/{id}/anlagen`, `GET /anlagentypen` |
| Disposition | `POST/GET /ressourcen`, `GET /disposition/zeitfenster`, `GET /termine` |
| Vorgänge | `POST/GET /servicefaelle`, `GET /servicefaelle/{id}` (mit Event-Historie), `POST /servicefaelle/{id}/status`, `POST /servicefaelle/{id}/termine` |
| Freigabe | `GET /freigaben`, `POST /freigaben`, `POST /freigaben/{id}/entscheidung` |
| Betrieb | `GET /outbox?status=tot` (Dead-Letter), `GET /ereignisse` |
| Digital Twin | `GET /twin/betrieb`, `GET /twin/anlagen/{id}` |
| Ontologie | `GET /ontologie/tbox`, `GET /ontologie/export?mit_tbox=true` (text/turtle), `POST /ontologie/sparql` (nur SELECT/ASK) |
| Management | `POST /wartung/planen`, `GET /kennzahlen`, `GET /plantafel`, `GET/POST /mandanten`, `PATCH /ressourcen/{id}` |
| Finanzen | `GET /rechnungen`, `GET /rechnungen/{id}` (+ `/pdf`, `/xrechnung`), `POST /rechnungen/{id}/storno`, `POST /kanzlei/belegpaket` (Rechnung und Storno entstehen über Freigaben Klasse C) |
| Betrieb/Einstellungen | `GET /betrieb`, `PUT /betrieb/stammdaten`, `GET /integrationen` (maskiert), `PUT /integrationen/{ziel}` (Ziele `fachsoftware`, `endkunde`, `finanz`, `kanzlei`) |
| Datenschutz | `GET /kunden/{id}/auskunft` (Art. 15), `POST /kunden/{id}/loeschen` (Art. 17, Sperrfristen), `POST /loeschlauf?trocken=true` |
| Anmeldung | `GET /auth/konfiguration` (öffentlich), `GET /ich` (Name, Rollen, Rechte) |
| Einsatz | `GET /einsatz/heute`, `POST /zeiten/start`, `POST /zeiten/{id}/stop`, `POST/GET /zeiten`, `GET /zeiten/lohnexport?monat=`, `POST /servicefaelle/{id}/rapport`, `GET /servicefaelle/{id}/rapporte`, `GET /rapporte/{id}/pdf`, `GET /servicefaelle/{id}/kalkulation` |
| Belege und Anhänge | `POST /belege` (Multipart: Datei und/oder Felder), `GET /belege`, `GET/PATCH/DELETE /belege/{id}`, `POST /anhaenge` (Multipart), `GET /anhaenge?bezug_typ=&bezug_id=`, `GET /anhaenge/{id}/inhalt`, `DELETE /anhaenge/{id}` |
| Bereitschaft, Fristen | `GET/POST /bereitschaft`, `GET /bereitschaft/aktuell`, `DELETE /bereitschaft/{id}`, `GET/POST /fristen`, `POST /fristen/{id}/erledigt`, `DELETE /fristen/{id}` |
| Voice-Tools | `POST /voice/kunde-suchen`, `/voice/zeitfenster`, `/voice/servicefall`, `/voice/notdienst`, `/voice/gespraech-beendet` |
| Artikel, Angebote | `GET/POST /artikel`, `PATCH /artikel/{id}`, `GET/POST /angebote`, `GET/PATCH /angebote/{id}` (+ `/pdf`), `POST /angebote/{id}/senden`, `/annehmen`, `/ablehnen`, `/rechnung` |
| Forderungen | `GET /offene-posten`, `GET/POST /zahlungen`, `POST /zahlungen/bankimport` (Multipart CSV), `POST /mahnlauf`, `GET /mahnungen`, `GET /mahnungen/{id}/pdf` |
| Verträge, Abwesenheiten | `GET/POST /vertraege`, `GET /vertraege/{id}`, `POST /vertraege/{id}/kuendigen`, `GET/POST /abwesenheiten`, `DELETE /abwesenheiten/{id}` |
| Anfragen | `GET /anfragen`, `POST /anfragen/web/{token}` (öffentlich, Formular oder JSON), `GET/PUT /betrieb/webformular`, `GET/PUT /betrieb/posteingang`, `POST /posteingang/abrufen` |
| Auswertung, Tageslauf | `GET /auswertungen?monate=12`, `POST /tagesaufgaben` (auch `python -m core tagesaufgaben`, Cron 06:30; `python -m core posteingang` alle 15 Minuten) |

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

Zusätzlich getestet: Rechnungsnummern, XRechnung 3.0 (UBL, Pflichtfelder), PDF, Storno, Adapter
Lexware/sevDesk/E-Mail/SMS/Webhook mit Attrappen, DATEV-Buchungsstapel im Belegpaket, verschlüsselte
Zugangsdaten, Löschlauf mit Fristen und Sperrfristen, Auskunft, Sichtbarkeit der Monteure.

Betriebsmodule (`core/einsatz.py`, `core/belege.py`, Migration 0005): Zeitstempel mit automatischem
Wechsel, Rapport mit Unterschrift (PNG) und PDF samt Statuswechsel auf „durchgeführt“,
Nachkalkulation (Sätze aus den Stammdaten), Bereitschaft mit Überschneidungsprüfung und
Sprachagenten-Werkzeug, Fristen mit Folgefrist, Anhänge (Fotos auf 1600 px verkleinert, 10 MB),
Lohnexport, Belegeingang aus XRechnung (eigener Export als Testfall), ZUGFeRD (PDF mit eingebettetem
XML), Text-Heuristik mit Konfidenz, manuell, Schutz übergebener Belege, Eingangsbelege und
ER-Buchungen im DATEV-Stapel, Löschfristen für Belege (10 Jahre) und Zeiten (2 Jahre), Rollen des
Monteurs (nur eigene Ressource und Einsätze). Texterkennung für Fotos braucht Tesseract
(`docker build --build-arg MIT_OCR=1`); ohne OCR bleibt der Beleg mit Hinweis zur Nacherfassung.

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
- Erstausrollung: HOSTING.md, `deploy/server/einrichten.sh` (erzeugt `.env`, rollt aus, legt Mandant an)
- eigenes Repository: `deploy/repo-abspalten.sh` (git subtree split, Workflows angepasst)
- Finanzanbindung (Lexware Office, sevDesk, DATEV, XRechnung über Mustang Project)
- AVV, TOMs, Löschkonzept: rechtliche Unterlagen vor dem ersten Pilotkunden
- Namespace der Ontologie (`https://w3id.org/shk-backoffice/ont#`) ist ein Platzhalter
