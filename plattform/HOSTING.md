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
| `deploy/server/docker-compose.prod.yml` | Override: Images aus GHCR, Neustart-Policy, Log-Rotation, Caddy, Keycloak im Produktionsmodus |
| `deploy/server/Caddyfile` | Reverse-Proxy, TLS, HSTS, CSP, `/docs` nicht öffentlich |
| `deploy/server/deploy.sh` | Ausrollen: Pull, Sicherung, Migration, Neustart, Gesundheitsprüfung, Rückfall auf vorige Version |
| `deploy/server/backup.sh`, `restore.sh` | Sicherung und Wiederherstellung |
| `deploy/postgres/rollen.sql`, `init/` | Datenbankrollen und Keycloak-Datenbank beim ersten Start |
| `deploy/keycloak/shk-realm.json` | Realm `shk`: Rollen, Client `shk-buero` (PKCE), Mandanten-Claim, Passwort-Policy |
| `.github/workflows/plattform-deploy.yml` | Images bauen, nach GHCR schieben, per SSH ausrollen (manuell oder Tag `plattform-v*`) |

## Einrichtung Schritt für Schritt

Was nur Sie erledigen können, ist mit **(Sie)** markiert: Verträge, Konten, DNS.

1. **Server bestellen (Sie):** Ubuntu 24.04, Standort Deutschland, in `cloud-init.yaml` den eigenen
   öffentlichen SSH-Schlüssel eintragen und den Inhalt als Cloud-Config angeben. AVV mit dem
   Anbieter abschließen.
2. **DNS (Sie):** A- (und AAAA-)Einträge für `backoffice.<domain>` und `auth.<domain>` auf die
   Server-IP.
3. **Geheimnisse:** auf dem Server `cd /opt/shk/plattform && cp .env.example .env`, alle Werte mit
   `openssl rand -base64 32` erzeugen, `SHK_DOMAIN=backoffice.<domain>`, `ACME_EMAIL`,
   `SHK_ADMIN_NETZE` (Büro-IP) setzen. `.env` gehört nie ins Repository.
4. **Erstes Ausrollen:** Images entstehen im Workflow „Plattform ausrollen“ (Actions, Eingabe
   Version). Danach auf dem Server `plattform/deploy/server/deploy.sh 0.2.0`. Solange die Plattform
   nicht auf `main` liegt: `git checkout plattform/backend-grundgeruest` in `/opt/shk`.
   Ist das GHCR-Paket privat, einmalig `docker login ghcr.io` mit einem Token (Recht `read:packages`).
5. **Ersten Mandanten anlegen:**
   `curl -H "X-API-Key: $CORE_API_KEY" -H 'Content-Type: application/json' -d '{"name":"Betrieb"}' http://127.0.0.1:8100/mandanten`
   (die ausgegebene `id` ist die Mandanten-ID).
6. **Benutzer in Keycloak:** Admin-Konsole `https://auth.<domain>/admin` (nur aus `SHK_ADMIN_NETZE`),
   Realm `shk`: zuerst einen eigenen Admin-Benutzer im Realm `master` anlegen und den
   Bootstrap-Admin löschen. Dann je Person einen Benutzer mit Attribut `mandant` = Mandanten-ID und
   einer Realm-Rolle (`inhaber`, `buero`, `monteur`, `steuerberater`). TOTP für Inhaber empfohlen.
7. **Automatisches Ausrollen (optional):** in GitHub unter Settings → Environments ein Environment
   `produktion` mit Pflicht-Reviewer anlegen; Variable `DEPLOY_HOST`, Secrets `DEPLOY_SSH_KEY`
   (privater Schlüssel zum Nutzer `shk`) und `DEPLOY_KNOWN_HOSTS` (`ssh-keyscan <host>`).
8. **Telefonie (Sie):** SIP-Trunk bei einem deutschen Carrier, Rufumleitung der Betriebsnummer,
   Zugangsdaten in `deploy/livekit/`, dann `SHK_PROFILE_ZUSATZ="--profile voice" deploy.sh <version>`.
   Firewall für 5060/udp auf die IP-Bereiche des Carriers einschränken.

## Sicherung und Wiederherstellung

- Täglich 02:15 Uhr: `pg_dump` der Datenbanken `shk` und `keycloak` nach `/var/backups/shk`,
  14 Tage lokal. Mit `RESTIC_REPOSITORY` und `RESTIC_PASSWORD_FILE` zusätzlich verschlüsselt außer
  Haus (z. B. Hetzner Storage Box), 14 tägliche, 8 wöchentliche, 12 monatliche Stände.
- Wiederherstellen: `deploy/server/restore.sh /var/backups/shk/shk-<stempel>.dump`. Row-Level
  Security, Policies und Rechte sind Teil des Dumps; danach laufen die Migrationen erneut.
- Wiederherstellung mindestens quartalsweise auf einem Testserver üben und protokollieren (TOM).
- Zusätzlich Snapshots des Anbieters aktivieren; sie ersetzen die Datenbanksicherung nicht.

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
