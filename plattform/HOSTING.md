# Hosting: Betrieb auf einem eigenen Server in Deutschland

## Empfehlung

Für Pilot und erste Kunden genügt **ein virtueller Server bei einem deutschen Anbieter** (Hetzner
Cloud, IONOS Cloud oder STACKIT), Standort Deutschland, mit Ubuntu 24.04 LTS. Begründung:

- **DSGVO und Datenhoheit:** Gesprächsinhalte, Kundendaten und Rechnungsdaten bleiben in
  Deutschland; mit dem Anbieter wird ein AVV nach Art. 28 DSGVO geschlossen.
- **Telefonie:** LiveKit SIP braucht eine öffentliche IP und UDP-Portbereiche für RTP. Serverless-
  bzw. PaaS-Angebote (Vercel, Render, Fly-Funktionen) scheiden dafür aus.
- **Einfachheit:** Ein `docker compose` mit allen Diensten; Skalierung später durch Aufteilen
  (Datenbank als Managed PostgreSQL, Voice-Strecke auf eigene Instanz).

Dimensionierung als Ausgangswert (eigene Schätzung, nicht gemessen): 4 vCPU, 8 GB RAM, 80 GB SSD
für Core, PostgreSQL, Keycloak, Caddy und einen Voice-Agenten mit wenigen parallelen Gesprächen.
STT, TTS und LLM laufen als Dienste (Azure Speech germanywestcentral, Claude), nicht auf dem Server.
Aktuelle Preise beim Anbieter prüfen.

## Aufbau auf dem Server

```
Internet ──443/80──▶ Caddy (TLS, Let's Encrypt, Sicherheits-Header)
                       ├─ backoffice.<domain>  ──▶ core:8100 (API, Büro-Oberfläche unter /app)
                       └─ auth.<domain>        ──▶ keycloak:8080 (OIDC, Admin-Konsole nur aus SHK_ADMIN_NETZE)
Carrier ──5060/udp, 10000–20000/udp──▶ LiveKit SIP ─▶ LiveKit ─▶ voice-agent ─▶ core (127.0.0.1:8100)
core, worker ──▶ PostgreSQL (Rollen shk_app mit RLS, shk_system, keycloak)
nachts 02:15 ──▶ backup.sh: pg_dump lokal 14 Tage, optional restic außer Haus
```

| Datei | Zweck |
| --- | --- |
| `deploy/server/cloud-init.yaml` | Grundeinrichtung: Nutzer `shk`, Docker, Firewall (ufw), fail2ban, automatische Sicherheitsupdates, Backup-Cron |
| `deploy/server/docker-compose.prod.yml`, `docker-compose.lokal.yml` | Override: Images aus GHCR (oder mit `--lokal` auf dem Server gebaut), Neustart-Policy, Log-Rotation, Caddy, Keycloak im Produktionsmodus |
| `deploy/server/Caddyfile` | Reverse-Proxy, TLS, HSTS, CSP, `/docs` nicht öffentlich |
| `deploy/server/deploy.sh` | Ausrollen: Pull, Sicherung, Migration, Neustart, Gesundheitsprüfung, Rückfall auf vorige Version |
| `deploy/server/backup.sh`, `restore.sh` | Sicherung und Wiederherstellung |
| `deploy/postgres/rollen.sql`, `init/` | Datenbankrollen und Keycloak-Datenbank beim ersten Start |
| `deploy/keycloak/shk-realm.json` | Realm `shk`: Rollen, Client `shk-buero` (PKCE), Mandanten-Claim, Passwort-Policy |
| `.github/workflows/plattform-deploy.yml` | Images bauen, nach GHCR schieben, per SSH ausrollen (manuell oder Tag `plattform-v*`) |

## Sofort testen, ohne Server (10 Minuten)

Auf dem eigenen Rechner mit Docker Desktop (Windows, macOS) oder Docker Engine:

```
git clone https://github.com/jonathankuhn11-spec/ai-call-agent.git && cd ai-call-agent
git checkout plattform/backend-grundgeruest && cd plattform
cp .env.example .env
docker compose up -d --build postgres migrate core worker
docker compose exec core python -m core seed      # fiktiver Demo-Betrieb mit Vorgängen, Belegen, Angeboten
```

Danach `http://localhost:8100/app` (Oberfläche, Mandant „Demo Haustechnik Köln“ wählen) und
`http://localhost:8100/docs` (API). Ohne Docker genügt Python 3.12: `pip install -r requirements.txt`
und `python -m core serve --demo` (SQLite, keine weitere Einrichtung). Beides ist ein lokaler Test
ohne Anmeldung und ohne Telefonie; der Serverbetrieb folgt unten.

## Einrichtung Schritt für Schritt

Was nur Sie erledigen können, ist mit **(Sie)** markiert: Verträge, Konten, DNS.

1. **Server bestellen (Sie):** Ubuntu 24.04, Standort Deutschland, in `cloud-init.yaml` den eigenen
   öffentlichen SSH-Schlüssel eintragen und den Inhalt als Cloud-Config angeben. AVV mit dem
   Anbieter abschließen.
2. **DNS (Sie):** A- (und AAAA-)Einträge für `backoffice.<domain>` und `auth.<domain>` auf die
   Server-IP.
3. **Images bauen:** Der Workflow „Plattform“ veröffentlicht bei jedem Push automatisch
   `ghcr.io/<owner>/shk-core:<sha12>`, `:<zweig>` (z. B. `plattform-backend-grundgeruest`) und auf dem
   Standardzweig `:latest`, jeweils auch als `-voice`-Variante; `SHK_VERSION` in `.env` kann daher ein
   Zweigname, ein Commit oder `latest` sein. Der Workflow „Plattform ausrollen“ (Eingabe Version, z. B. `0.2.0`)
   erzeugt zusätzlich Versions-Tags. Ist das GHCR-Paket privat, auf dem Server einmalig `docker login ghcr.io`
   mit einem Token (Recht `read:packages`). Ganz ohne GHCR baut der Server selbst:
   `deploy/server/einrichten.sh 0.2.0 --lokal` bzw. `SHK_BUILD_LOKAL=1` in `.env`
   (Override `deploy/server/docker-compose.lokal.yml`, Bauzeit ca. 3–5 Minuten auf 4 vCPU).
   Für einen Test ohne Domain und TLS reicht `plattform/schnellstart.sh` (ein Container mit SQLite bzw.
   `--postgres` mit `docker compose`), siehe README.
4. **Erstausrollung:** auf dem Server als Nutzer `shk`
   `cd /opt/shk/plattform && deploy/server/einrichten.sh 0.2.0` (oder `… 0.2.0 --lokal`). Das Skript fragt Domain, E-Mail und
   Büro-Netz ab, erzeugt `.env` mit Zufallsgeheimnissen (inkl. `INTEGRATIONS_KEY`), prüft die
   DNS-Einträge, ruft `deploy.sh` auf und legt den ersten Mandanten an. `.env` gehört nie ins
   Repository. Für jede weitere Version genügt `deploy/server/deploy.sh <version>`.
5. **Mandant und Stammdaten:** die Mandanten-ID steht nach Schritt 4 in `.env` (`CORE_MANDANT`);
   Firmendaten, IBAN und Kanzlei-Adresse trägt der Inhaber in der Oberfläche unter „Einstellungen“ ein.
6. **Benutzer in Keycloak:** Admin-Konsole `https://auth.<domain>/admin` (nur aus `SHK_ADMIN_NETZE`),
   Realm `shk`: zuerst einen eigenen Admin-Benutzer im Realm `master` anlegen und den
   Bootstrap-Admin löschen. Dann je Person einen Benutzer mit Attribut `mandant` = Mandanten-ID und
   einer Realm-Rolle (`inhaber`, `buero`, `monteur`, `steuerberater`). TOTP für Inhaber empfohlen.
7. **Automatisches Ausrollen (optional):** in GitHub unter Settings → Environments ein Environment
   `produktion` mit Pflicht-Reviewer anlegen; Variable `DEPLOY_HOST`, Secrets `DEPLOY_SSH_KEY`
   (privater Schlüssel zum Nutzer `shk`) und `DEPLOY_KNOWN_HOSTS` (`ssh-keyscan <host>`).
8. **Telefonie (Sie):** SIP-Trunk bei einem deutschen Carrier und Sprachdienst-Schlüssel, dann nach
   TELEFONIE.md: `deploy/livekit/trunk-anlegen.sh`, `python -m voice.probe`, Testanruf, zuletzt die
   Rufumleitung der Betriebsnummer. Firewall für 5060/udp auf die IP-Bereiche des Carriers einschränken.
9. **Vor dem ersten Pilotkunden:** AVV mit Anlagen aus `recht/` unterschreiben lassen, Löschlauf
   (täglich 02:45 Uhr per Cron, `python -m core loeschlauf`) nach der ersten Nacht im Protokoll prüfen
   (`GET /ereignisse`, Typ `loeschung.lauf`).
10. **Eigenes Repository (optional):** leeres Repository auf GitHub anlegen und
    `plattform/deploy/repo-abspalten.sh <url>` ausführen; danach `cloud-init.yaml` des Servers auf die
    neue Quelle zeigen lassen (das Skript passt Pfade und Workflows an).

## Sicherung und Wiederherstellung

- Täglich 02:15 Uhr: `pg_dump` der Datenbanken `shk` und `keycloak` nach `/var/backups/shk`,
  14 Tage lokal. Mit `RESTIC_REPOSITORY` und `RESTIC_PASSWORD_FILE` zusätzlich verschlüsselt außer
  Haus (z. B. Hetzner Storage Box), 14 tägliche, 8 wöchentliche, 12 monatliche Stände.
- Wiederherstellen: `deploy/server/restore.sh /var/backups/shk/shk-<stempel>.dump`. Row-Level
  Security, Policies und Rechte sind Teil des Dumps; danach laufen die Migrationen erneut.
- Wiederherstellung mindestens quartalsweise auf einem Testserver üben und protokollieren (TOM).
- Zusätzlich Snapshots des Anbieters aktivieren; sie ersetzen die Datenbanksicherung nicht.

## Betriebswebsites unter eigener Domain

Jeder Betrieb erhält unter `https://<SHK_DOMAIN>/web/<slug>` eine eigene Internetseite (Ansicht **Website**). Für eine eigene Domain
des Betriebs (z. B. `mueller-haustechnik.de`):

1. Der Betrieb trägt die Domain in der Ansicht Website ein; der Core kennt sie dann unter `GET /web/domain-pruefen?domain=…`.
2. Der Betrieb (oder sein Domain-Anbieter) setzt einen `CNAME` von `www.mueller-haustechnik.de` auf `<SHK_DOMAIN>`; für die
   nackte Domain einen `A`/`AAAA`-Eintrag auf die Server-IP (oder ALIAS/ANAME, falls der Anbieter das kann).
3. Caddy stellt beim ersten Aufruf das Zertifikat aus (`on_demand_tls` mit `ask` gegen den Core, siehe `deploy/server/Caddyfile`);
   nur eingetragene Domains bekommen ein Zertifikat. Der Core erkennt die Domain am `Host`-Kopf und liefert Startseite, Impressum,
   Datenschutz, Sitemap, robots.txt und das Kontaktformular aus; alle anderen Pfade antworten mit 404.

Impressum und Datenschutzerklärung entstehen aus den Stammdaten (Inhaber/in, Anschrift, USt-IdNr., Kammer-Zusatz) und sind
**Entwürfe**, die vor Veröffentlichung juristisch geprüft gehören (§ 5 DDG, Art. 13 DSGVO, § 36 VSBG).

## Sicherheit im Überblick

| Ebene | Maßnahme |
| --- | --- |
| Netz | nur 22, 80, 443 (und SIP/RTP bei Telefonie) offen; Core, Datenbank, LiveKit-API nur lokal |
| Transport | TLS über Caddy, HSTS; Admin-Konsole von Keycloak nur aus definierten Netzen |
| Anmeldung | OIDC mit PKCE, Passwort-Policy, Brute-Force-Schutz, optional TOTP; API-Key nur für Maschinen |
| Autorisierung | Rollen inhaber, buero, monteur, steuerberater; Klasse-C-Freigaben nur Inhaber |
| Daten | Row-Level Security je Mandant in PostgreSQL; Core-Rolle ohne DDL- und ohne Bypass-Recht |
| Betrieb | automatische Sicherheitsupdates, fail2ban, Log-Rotation, Sicherung mit Aufbewahrungsfristen |
| Nachvollziehbarkeit | Event-Log mit Akteur; bei Personen der Name aus dem Token, nicht aus der Anfrage |

## Stand der Prüfung

Geprüft: Migrationen und Row-Level Security gegen PostgreSQL 16 (automatisierte Tests, auch nach
Dump/Restore), Anmeldung über Keycloak 26.0.7 im Browser mit dem mitgelieferten Realm, Oberfläche
hinter Caddy mit der Produktions-CSP, `docker compose config` und `caddy validate`.
Nicht geprüft: Erstausrollung auf einem echten Server (cloud-init, Let's Encrypt, GHCR-Pull),
`backup.sh`/`restore.sh` im Container-Betrieb, Telefonie über einen Carrier. Die Images von LiveKit
(`latest`) vor dem Produktivbetrieb auf eine geprüfte Version pinnen.
