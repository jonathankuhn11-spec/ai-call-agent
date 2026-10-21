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
| Monteure/Ressourcen verwalten | ✓ | | | |

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
| Management | `POST /wartung/planen`, `GET /kennzahlen`, `GET /plantafel`, `GET/POST /mandanten` |
| Anmeldung | `GET /auth/konfiguration` (öffentlich), `GET /ich` (Name, Rollen, Rechte) |
| Voice-Tools | `POST /voice/kunde-suchen`, `/voice/zeitfenster`, `/voice/servicefall`, `/voice/gespraech-beendet` |

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

Nicht getestet bzw. offen:
- echter Anruf über SIP (braucht Carrier-Trunk, Server mit öffentlicher IP, STT/TTS-Schlüssel)
- Erstausrollung auf einem echten Server (cloud-init, Let's Encrypt, GHCR), Backup im Containerbetrieb
- Fachsoftware-Adapter (openHandwerk, pds …): abhängig von der API-Klärung, bis dahin CSV-Degradierung
- Finanzanbindung (Lexware Office, sevDesk, DATEV, XRechnung über Mustang Project)
- Zuordnung Monteur zu Ressource (Monteure sehen derzeit alle Einsätze des Mandanten)
- AVV, TOMs, Löschkonzept: rechtliche Unterlagen vor dem ersten Pilotkunden
- Namespace der Ontologie (`https://w3id.org/shk-backoffice/ont#`) ist ein Platzhalter
