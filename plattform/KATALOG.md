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
| Brick Schema | BSD-3-Clause | Ontologie für Gebäudetechnik | aktiv als Abgleich (`skos:broadMatch`) | Ausbau für TGA-Anlagen |
| SAREF4BLDG (ETSI) | offene ETSI-Spezifikation | Gebäude-Ontologie | Referenz | Abgleich mit IFC |
| Eclipse Ditto | EPL-2.0 | Digital-Twin-Plattform für Gerätezustände | Referenz | erst bei Telemetrie (z. B. Wärmepumpen-Störmeldungen) |
| Eclipse BaSyx | MIT/EPL-2.0 | Asset Administration Shell (Industrie 4.0) | Referenz | falls Hersteller AAS liefern |

## Finanzen und E-Rechnung

| Projekt | Lizenz | Rolle | Einbindung | Einschätzung |
| --- | --- | --- | --- | --- |
| Mustang Project | Apache-2.0 | ZUGFeRD/Factur-X erzeugen und prüfen | Referenz | Finanzanbindung Stufe 1 |
| KoSIT-Validator + XRechnung-Konfiguration | Apache-2.0 | Validierung XRechnung | Referenz | Pflichtprüfung vor Versand |

## Bezug zu bestehenden eigenen Repositories

- `ai-call-agent` (dieses Repository): Telli-Adapter, Outbox, Guardrails, UAT-Simulation aus dem
  PV-Prototyp. Der Telli-Adapter ist die SaaS-Alternative zum selbst betriebenen LiveKit-Agenten.
- `energy-system-twin`: als Vorarbeit für Telemetrie und Anlagenzustand prüfen, bevor Eclipse
  Ditto eingeführt wird (Inhalt nicht gesichtet).
