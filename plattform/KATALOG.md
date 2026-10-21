# Komponentenkatalog: Open Source für das SHK-Backoffice

Fremdcode wird **nicht** in dieses Repository kopiert. Komponenten werden als Container
(docker-compose), als Paketabhängigkeit (requirements) oder als Referenz für spätere
Entscheidungen eingebunden. Gründe: Copyleft-Lizenzen (GPL/AGPL) würden sonst auf den eigenen
Code durchschlagen, Sicherheitsupdates kämen nicht an, und Code ohne Lizenz darf man gar nicht
übernehmen. Lizenzangaben sind der Stand nach unserem Kenntnisstand; vor Einbindung jeweils im
Repository des Projekts prüfen.

**Einbindung:** `aktiv` = im Code bzw. docker-compose enthalten · `optional` = Profil oder Adapter
vorbereitet · `Referenz` = geprüfte Alternative für eine spätere Entscheidung.

## Sprache und Telefonie

| Projekt | Lizenz | Rolle | Einbindung | Einschätzung |
| --- | --- | --- | --- | --- |
| LiveKit Agents | Apache-2.0 | Voice-Agent-Framework (VAD, STT, LLM, TTS, Function Calling) | aktiv (`voice/agent.py`) | gewählt, siehe VOICE-AGENT.md |
| LiveKit Server + LiveKit SIP | Apache-2.0 | WebRTC-Medienserver, SIP-Gateway zum Carrier | aktiv (Profil `voice`) | selbst betreibbar in DE |
| Pipecat | BSD-2-Clause | alternatives Voice-Framework | Referenz | gleichwertig, stärker auf Daily/Twilio-Transport ausgerichtet |
| Vocode | MIT | Voice-Agent-Bibliothek | Referenz | geringere Aktivität |
| Silero VAD | MIT | Sprachaktivitätserkennung | aktiv (über LiveKit-Plugin) | Standard |
| faster-whisper | MIT | lokale Spracherkennung | Referenz (im PV-Prototyp genutzt) | für Telefonie-Latenz nur mit GPU |
| Piper | MIT bzw. GPL-3.0 (neuere Fassung) | lokale Sprachsynthese | Referenz | Lizenz der jeweiligen Version prüfen |
| Asterisk / FreeSWITCH | GPL-2.0 / MPL-1.1 | klassische Telefonanlagen | Referenz | nur nötig für komplexe Rufnummernlogik |

## CRM, Termine, Disposition

| Projekt | Lizenz | Rolle | Einbindung | Einschätzung |
| --- | --- | --- | --- | --- |
| eigener Core (`core/`) | eigen | CRM, Anlagen, Servicefall, Disposition | aktiv | domänenspezifisch, Kern der Wechselbarriere |
| Twenty | AGPL-3.0 | generisches CRM | Referenz | AGPL beim SaaS-Betrieb beachten; kein SHK-Anlagenmodell |
| EspoCRM / SuiteCRM | AGPL-3.0 | generisches CRM | Referenz | wie oben |
| Odoo Community | LGPL-3.0 | ERP inkl. CRM; Field Service nur in Enterprise | Referenz | mächtig, aber schwer und modulreich |
| Cal.com | AGPL-3.0 | Online-Terminbuchung | Referenz | Disposition ist im Core gelöst; für Web-Buchung denkbar |
| Google OR-Tools | Apache-2.0 | Tourenplanung, Constraint-Solver | Referenz | Ausbau der Disposition (mehrere Einsätze je Tag) |
| VROOM | BSD-2-Clause | Vehicle Routing | Referenz | schlanker als OR-Tools, braucht Fahrzeitmatrix |
| OSRM / Valhalla | BSD-2-Clause / MIT | Fahrzeiten auf OpenStreetMap | Referenz | ersetzt den pauschalen Fahrzeitpuffer |
| Radicale | GPL-3.0 | CalDAV-Server | Referenz | Kalendersynchronisation der Monteure |

## Workflow, Daten, Betrieb

| Projekt | Lizenz | Rolle | Einbindung | Einschätzung |
| --- | --- | --- | --- | --- |
| PostgreSQL + pgvector | PostgreSQL License | Datenhaltung, Vektorsuche | aktiv | Standard |
| SQLAlchemy | MIT | ORM | aktiv | — |
| FastAPI | MIT | REST-API | aktiv | — |
| n8n | Sustainable Use License (kein OSI-Open-Source) | Eingangsworkflows E-Mail/Formular | optional (Profil `ops`) | interne Nutzung erlaubt; n8n nicht als Dienst an Dritte anbieten |
| Temporal | MIT | Durable Execution | Referenz | Stufe 2 der Architektur |
| Keycloak | Apache-2.0 | OIDC, Rollen | aktiv (Profil `auth`, Realm `deploy/keycloak/`) | Rollen Inhaber, Büro, Monteur, Steuerberater; Mandant als Token-Claim |
| PyJWT | MIT | Prüfung der Access Tokens im Core | aktiv (`core/auth.py`) | JWKS per OIDC-Discovery |
| Alembic | MIT | Datenbankmigrationen | aktiv (`migrations/`) | inkl. Row-Level-Security-Policies |
| Caddy | Apache-2.0 | Reverse-Proxy, automatisches TLS | aktiv (Serverbetrieb) | `deploy/server/Caddyfile` |
| restic | BSD-2-Clause | verschlüsselte Außer-Haus-Sicherung | optional (`backup.sh`) | z. B. Hetzner Storage Box per SFTP |
| Zitadel | AGPL-3.0 (ab v3) | OIDC | Referenz | Lizenzwechsel beachten |
| Langfuse | MIT (Kern) | LLM-Tracing, Evals | Referenz | eigener Stack (ClickHouse, Redis, S3), separat betreiben |
| OpenTelemetry | Apache-2.0 | Tracing, Metriken | Referenz | — |
| Grafana / Loki | AGPL-3.0 | Dashboards, Logs | Referenz | nur intern betrieben unkritisch |

## Ontologie und Digital Twin

| Projekt | Lizenz | Rolle | Einbindung | Einschätzung |
| --- | --- | --- | --- | --- |
| rdflib | BSD-3-Clause | RDF, Turtle, SPARQL in Python | aktiv (`core/ontologie.py`) | — |
| Apache Jena Fuseki | Apache-2.0 | Triple Store, SPARQL-Endpunkt | optional (Profil `ontologie`) | lädt `ontologie/shk.ttl` + Export |
| Protégé | BSD-2-Clause | Ontologie-Editor | Referenz | Pflege von `shk.ttl` |
| Brick Schema | BSD-3-Clause | Ontologie für Gebäudetechnik | aktiv als Abgleich (`skos:broadMatch`), Kopie 1.4.1 in `drittanbieter/ontologie/` | Ausbau für TGA-Anlagen |
| SAREF4BLDG (ETSI) | offene ETSI-Spezifikation | Gebäude-Ontologie | Referenz | Abgleich mit IFC |
| Eclipse Ditto | EPL-2.0 | Digital-Twin-Plattform für Gerätezustände | Referenz | erst bei Telemetrie (z. B. Wärmepumpen-Störmeldungen) |
| Eclipse BaSyx | MIT/EPL-2.0 | Asset Administration Shell (Industrie 4.0) | Referenz | falls Hersteller AAS liefern |

## Finanzen und E-Rechnung

| Projekt | Lizenz | Rolle | Einbindung | Einschätzung |
| --- | --- | --- | --- | --- |
| Mustang Project | Apache-2.0 | ZUGFeRD/Factur-X erzeugen und prüfen | Referenz | Finanzanbindung Stufe 1 |
| KoSIT-Validator + XRechnung-Konfiguration | Apache-2.0 | Validierung XRechnung | aktiv (`drittanbieter/xrechnung/pruefen.sh`, Test in der Suite) | erzeugte XRechnung besteht die Prüfung (Konfiguration 3.0.2) |
| reportlab | BSD-3 | Rechnungs- und Rapport-PDF (`core/rechnung.py`, `core/einsatz.py`) | Bibliothek | genügt für den Pilot; ZUGFeRD-Einbettung später über Mustang |
| pypdf | BSD-3 | eingebettetes ZUGFeRD-XML und Text aus PDF-Belegen (`core/belege.py`) | Bibliothek | reine Python-Bibliothek, keine Systemabhängigkeit |
| Tesseract OCR | Apache-2.0 | Texterkennung fotografierter Belege | optional (`MIT_OCR=1` im Dockerfile, Aufruf per CLI) | Trefferquote bei Fotos begrenzt; Büro prüft die Felder; Alternative: Dokumenten-KI eines Anbieters nur mit AVV |
| Pillow | MIT-CMU | Fotos vor Ablage verkleinern | Bibliothek | — |
| Lexware Office Public API | kommerziell | Kunden und Rechnungen anlegen | `core/adapter/lexware.py` | Partner-API-Key des Betriebs; Rechnungen werden als Entwurf angelegt |
| sevDesk API v1 | kommerziell | Kunden und Rechnungen anlegen | `core/adapter/sevdesk.py` | API-Token des Betriebs |
| DATEV-Format EXTF (Buchungsstapel) | Spezifikation | Belegpaket für die Kanzlei | `core/adapter/belegpaket.py` | Textformat cp1252, ohne DATEV-Schnittstellenvertrag nutzbar (Import durch die Kanzlei) |
| seven.io | kommerziell | SMS-Versand | `core/adapter/sms.py` | deutscher Anbieter, AVV; nur mit dokumentierter Einwilligung |

## Mitgeführte Kopien und Spiegel

Auf Wunsch des Betreibers führt `drittanbieter/` alles mit, was zum Bauen, Prüfen und Betreiben ohne externe Quellen nötig ist:
Lizenztexte aller Komponenten, die Brick-Ontologie 1.4.1 (Ziel der Abgleiche in `ontologie/shk.ttl`), KoSIT-Validator und
XRechnung-Konfiguration 3.0.2 (die erzeugte XRechnung besteht die Prüfung), ein Wheelhouse aller Python-Pakete für
`docker build --build-arg OFFLINE=1`, Image-Archive aller Container (alle Versionen fest, kein `latest`). Binärartefakte
entstehen reproduzierbar mit `drittanbieter/spiegeln.sh` und werden über `SHA256SUMS` geprüft. Copyleft-Komponenten (AGPL,
GPL) werden weder kopiert noch betrieben; Einzelheiten in [`drittanbieter/README.md`](drittanbieter/README.md).

## Bezug zu bestehenden eigenen Repositories

- `ai-call-agent` (dieses Repository): Telli-Adapter, Outbox, Guardrails, UAT-Simulation aus dem
  PV-Prototyp. Der Telli-Adapter ist die SaaS-Alternative zum selbst betriebenen LiveKit-Agenten.
- `energy-system-twin`: als Vorarbeit für Telemetrie und Anlagenzustand prüfen, bevor Eclipse
  Ditto eingeführt wird (Inhalt nicht gesichtet).
