# Technische und organisatorische Maßnahmen (Art. 32 DSGVO)

Stand: [Datum] · Fassung 1 (Entwurf) · Gilt für die SHK-Backoffice-Plattform im Betrieb nach HOSTING.md.
Jede Maßnahme verweist auf ihre Umsetzung, damit die Beschreibung mit dem System übereinstimmt und
bei Änderungen am Code mitgepflegt wird. „Geplant“ markiert Maßnahmen, die vor dem Pilot noch
einzurichten sind.

## 1 Vertraulichkeit

### 1.1 Zutrittskontrolle
- Server in zertifizierten Rechenzentren in Deutschland (ISO 27001 des Hosters); kein eigener Serverraum.
- Arbeitsplätze des Anbieters: Geräteverschlüsselung, Bildschirmsperre, keine Kundendaten auf
  Wechseldatenträgern (geplant: Richtlinie).

### 1.2 Zugangskontrolle
- Serverzugang nur per SSH-Schlüssel, Passwort- und Root-Anmeldung abgeschaltet, fail2ban
  (`deploy/server/cloud-init.yaml`).
- Anwendung: Anmeldung über Keycloak (OpenID Connect, Authorization Code mit PKCE), Passwort-Policy
  12 Zeichen mit Historie, Brute-Force-Schutz, Sitzungsablauf 8 h, TOTP für Inhaberrolle empfohlen
  (`deploy/keycloak/shk-realm.json`).
- Maschinenzugänge (Sprachassistent, Workflows) über API-Schlüssel mit konstantem Zeitvergleich
  (`core/auth.py`); Schlüssel nur in `.env` auf dem Server, nie im Repository.
- Admin-Konsole von Keycloak nur aus definierten Netzen erreichbar (`deploy/server/Caddyfile`).

### 1.3 Zugriffskontrolle
- Rollenmodell inhaber, buero, monteur, steuerberater mit Rechten lesen, bearbeiten, status,
  freigeben, freigeben_c, finanzen, verwalten (`core/auth.py`, Tabelle im README).
- Finanzwirksame Aktionen (Rechnung, Storno, Belegversand) nur durch die Inhaberrolle.
- Monteure sehen nur Einsätze ihrer eigenen Ressource (`eigene_ressourcen` in `core/api.py`).
- Zugangsdaten zu Drittsystemen verschlüsselt gespeichert (Fernet, Schlüssel `INTEGRATIONS_KEY`
  außerhalb der Datenbank; `core/geheimnisse.py`), in der Oberfläche nur maskiert sichtbar.

### 1.4 Trennungskontrolle
- Mandantentrennung zweistufig: Filter im Code und Row-Level Security in PostgreSQL mit FORCE; die
  Anwendungsrolle `shk_app` kann Policies weder umgehen noch Schema ändern (`migrations/versions/0002…`,
  `deploy/postgres/rollen.sql`). Nachweis durch Tests gegen PostgreSQL (`tests/test_postgres.py`).
- Entwicklung, Test und Produktion getrennt (SQLite-Demo mit fiktiven Daten; CI ohne Echtdaten).

### 1.5 Pseudonymisierung und Datensparsamkeit
- Keine Audioaufzeichnung; Transkripte nur 90 Tage, danach bleibt die fachliche Zusammenfassung
  (`core/loeschung.py`).
- Rechnungen tragen die Empfängerdaten als Momentaufnahme, der Kundendatensatz kann anonymisiert werden.
- Sprachdienste erhalten nur den laufenden Gesprächsinhalt, keine Kundenhistorie; das Sprachmodell
  erhält die Fachdaten, die für das Gespräch nötig sind (Name, Anlagentyp, Zeitfenster).

## 2 Integrität

### 2.1 Weitergabekontrolle
- Transportverschlüsselung TLS 1.2+ mit automatischen Zertifikaten, HSTS, Content-Security-Policy
  (`deploy/server/Caddyfile`); interne Dienste nur auf localhost bzw. im Docker-Netz.
- Übermittlungen an Drittsysteme ausschließlich über die Transactional Outbox mit Idempotenzschlüssel,
  Wiederholung, Dead-Letter und Protokoll (`core/zustellung.py`); Webhooks mit HMAC-SHA256 signiert.
- Sicherungen außer Haus nur verschlüsselt (restic, `deploy/server/backup.sh`).

### 2.2 Eingabekontrolle
- Append-only Ereignisprotokoll je Mandant mit Akteur (Mensch/KI/System), Name aus dem Anmelde-Token
  und Zeitstempel (`core/domaene.py`); Freigaben protokollieren Entscheider und Zeitpunkt.
- Statusautomat verhindert unzulässige Übergänge; Rechnungsnummern fortlaufend und unveränderlich
  (`core/rechnung.py`).

## 3 Verfügbarkeit und Belastbarkeit

- Tägliche Datenbanksicherung 02:15 Uhr, 14 Tage lokal, optional 14/8/12 Stände verschlüsselt außer
  Haus; Wiederherstellungsskript und vierteljährlicher Wiederherstellungstest (`deploy/server/restore.sh`,
  geplant: Protokoll der Tests).
- Neustart-Policy und Gesundheitsprüfung aller Dienste; Ausrollen mit automatischem Rückfall auf die
  vorige Version (`deploy/server/deploy.sh`).
- Automatische Sicherheitsupdates mit nächtlichem Neustart, Log-Rotation.
- Degradierung statt Ausfall: ohne erreichbares Drittsystem bleibt die Nachricht in der Outbox, ohne
  E-Mail/SMS entsteht eine Büro-Aufgabe („ohne Kanal“), ohne Sprachassistent läuft der Anruf auf die
  Rufumleitung des Betriebs zurück (Carrier-seitig zu konfigurieren).
- Zeitlimits für Datenbank (statement_timeout 15 s) und HTTP-Aufrufe an Drittsysteme.

## 4 Verfahren zur regelmäßigen Überprüfung, Bewertung und Evaluierung

- Automatisierte Tests bei jeder Änderung (CI): Mandantentrennung, Rollen, Tokenprüfung, Löschlauf,
  Adapter mit Attrappen; Konfigurationsprüfung von Compose, Caddy und Keycloak-Realm.
- Löschkonzept als Code mit täglichem Lauf und Protokolleintrag (`loeschung.lauf`), siehe LOESCHKONZEPT.md.
- Datenschutz-Management: jährliche Prüfung dieser TOM, der Subunternehmerliste und der Zugriffsrechte
  (geplant: Verantwortliche Person benennen).
- Meldeprozess für Datenschutzverletzungen: Erkennung über Protokolle und Monitoring, Meldung an den
  Verantwortlichen innerhalb von 24 h (AVV Ziffer 4.4), Dokumentation im Vorfallsregister (geplant).
- Auftragskontrolle: Verarbeitung nur im Rahmen der Konfiguration des Mandanten; Subunternehmer mit AVV.

## 5 Offene Punkte vor dem Pilot

| Maßnahme | Status |
| --- | --- |
| TOTP-Pflicht für Inhaberrolle in Keycloak aktivieren | geplant (Realm: Required Action „Configure OTP“) |
| Monitoring und Alarmierung (Ausfall, Dead-Letter, Plattenplatz) | geplant (z. B. Uptime-Check + E-Mail) |
| Wiederherstellungstest protokollieren | geplant (erster Test nach Erstausrollung) |
| Vorfallsregister und Meldeprozess schriftlich | geplant |
| Datenschutz-Folgenabschätzung | geplant |
| Eingeschränkte Haftung/Freigabeprotokoll beim Sprachmodell-Anbieter (keine Trainingsnutzung) prüfen | geplant |
