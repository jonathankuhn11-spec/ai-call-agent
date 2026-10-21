# Telefonie: vom SIP-Trunk bis zum ersten echten Anruf

Was der Code mitbringt: LiveKit SIP als Gateway, der Voice-Agent, Trunk- und Dispatch-Vorlagen in
`deploy/livekit/`, ein Anlege-Skript und eine Verbindungsprüfung. Was nur der Betreiber beschaffen
kann, ist mit **(Sie)** markiert: Carrier-Vertrag, Rufnummer, Sprachdienst-Schlüssel, Server.

## 1 Carrier-Auswahl (Sie)

Anforderungen an den SIP-Trunk: Registrierung oder IP-Authentifizierung, Codec G.711a (alaw), DTMF per
RFC 2833, deutsche Rufnummer, Rufumleitung der Betriebsnummer möglich, AVV verfügbar.

| Anbieter | Produkt | Eignung | Hinweis |
| --- | --- | --- | --- |
| sipgate | sipgate trunking | Pilot, geringe Grundgebühr, Minutenpreise | IP-Authentifizierung und Registrierung; Portierung bestehender Nummern |
| easybell | SIP-Trunk (Business) | Pilot, Flatrate-Optionen | Registrierung; gut dokumentierte Asterisk-Beispiele, auf LiveKit übertragbar |
| Telekom | CompanyFlex SIP-Trunk | Betriebe mit Telekom-Anschluss | Registrierung mit Registrar-Domain je Vertrag |
| Plusnet / QSC | SIP-Trunk | größere Betriebe | IP-Authentifizierung |

Empfehlung für den Pilot: sipgate trunking oder easybell, weil die Einrichtung in unter einer Stunde
gelingt und keine Mindestlaufzeit nötig ist. Rufnummer des Betriebs nicht portieren; stattdessen eine
neue Trunk-Nummer buchen und die Betriebsnummer **bei Besetzt, Nichtmelden und außerhalb der
Bürozeit** dorthin umleiten (Rufumleitung beim bestehenden Anbieter des Betriebs). So bleibt der
Betrieb jederzeit erreichbar, auch wenn der Server ausfällt.

## 2 Server und Netz

- Server mit öffentlicher IPv4 (HOSTING.md). LiveKit SIP läuft mit `network_mode: host`, der
  Container bindet 5060/udp und RTP 10000–20000/udp (`deploy/livekit/sip.yaml`).
- Firewall (`cloud-init.yaml`) öffnet 5060/udp und den RTP-Bereich. Nach Vertragsschluss die Regel auf
  die Netze des Carriers einschränken, z. B. `ufw allow from 217.10.64.0/20 to any port 5060 proto udp`
  (sipgate; den tatsächlichen Bereich aus der Carrier-Dokumentation nehmen).
- Kein NAT vor dem Server; falls doch, `use_external_ip: true` bleibt gesetzt und der Carrier muss
  die öffentliche IP sehen (STUN ist nicht vorgesehen).

## 3 Sprachdienste (Sie)

| Dienst | Konto | Variablen in `.env` |
| --- | --- | --- |
| Azure AI Speech (STT/TTS), Region Germany West Central | Azure-Abonnement, Ressource „Speech“ | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION=germanywestcentral`, `TTS_STIMME` |
| Claude (LLM) | Anthropic Console oder EU-Gateway | `ANTHROPIC_API_KEY`, optional `LLM_BASE_URL` |
| LiveKit | lokal, Schlüsselpaar selbst erzeugen | `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` (identisch in `deploy/livekit/sip.yaml`) |

Schlüssel entstehen mit `openssl rand -hex 16` (API-Key) und `openssl rand -base64 32` (Secret);
`deploy/server/einrichten.sh` erledigt das.

## 4 Einrichtung in dieser Reihenfolge

1. `.env` befüllen (siehe oben), `deploy/livekit/sip.yaml` mit demselben Schlüsselpaar.
2. `deploy/livekit/inbound-trunk.json`: Rufnummer in E.164 (`+4922112345678`), Benutzername und
   Passwort des Trunks. Bei IP-Authentifizierung stattdessen `allowed_addresses` mit den
   Carrier-Netzen eintragen und die Auth-Felder leer lassen.
3. Dienste starten: `SHK_PROFILE_ZUSATZ="--profile voice" deploy/server/deploy.sh <version>`.
4. Trunk und Dispatch-Regel anlegen: `deploy/livekit/trunk-anlegen.sh` (nutzt die LiveKit-CLI `lk`,
   installiert sie bei Bedarf nach `~/.local/bin`). Das Skript ist idempotent: bestehende Objekte
   gleichen Namens werden vorher gelöscht.
5. Prüfen: `python -m voice.probe` meldet, ob Umgebung, LiveKit, SIP-Port, Core-Tool-API und
   Sprachdienste erreichbar sind.
6. Testanruf von einem Mobiltelefon auf die Trunk-Nummer. Erwartung: Begrüßung mit KI-Offenlegung
   innerhalb von zwei Sekunden, Rückfrage zum Anliegen, Terminvorschlag, nach dem Auflegen ein
   Servicefall mit Transkript in der Büro-Oberfläche.
7. Erst danach die Rufumleitung der Betriebsnummer schalten.

## 5 Ausfallverhalten

| Störung | Verhalten | Rückfall |
| --- | --- | --- |
| Server oder LiveKit nicht erreichbar | Carrier erhält keine Antwort auf INVITE | Rufumleitung zeitgesteuert, der Anruf landet nach Timeout auf der Mailbox des Carriers; alternativ zweite Umleitungsstufe auf ein Mobiltelefon des Betriebs (beim Carrier einstellen) |
| STT/TTS-Dienst gestört | Agent antwortet nicht | Anrufer hört Stille; deshalb Timeout in `voice/agent.py` und Rückrufvormerkung; Überwachung per `voice.probe` im Cron empfohlen |
| LLM-Dienst gestört | Werkzeugaufrufe bleiben aus | Agent sagt Rückruf zu (`rueckruf_vormerken` läuft direkt gegen den Core) |
| Core nicht erreichbar | Werkzeuge liefern Fehler | Agent nimmt Rufnummer auf und sagt Rückruf zu, das Transkript wird nach Rückkehr des Core nachgereicht (Outbox im Agenten, geplant) |

## 6 Rechtliches

- Ansage mit KI-Offenlegung und Datenschutzhinweis ist die erste Äußerung des Agenten
  (`voice/agent.py`, `BEGRUESSUNG`); Wortlaut und Langfassung in `recht/DATENSCHUTZHINWEIS-TELEFON.md`.
- Keine Audioaufzeichnung; Transkript 90 Tage (`recht/LOESCHKONZEPT.md`).
- Der Carrier ist Subunternehmer oder Vertragspartner des Betriebs (AVV Anlage 2).

## 7 Kosten je Minute (Richtwerte, Stand Oktober 2026, selbst prüfen)

| Posten | Richtwert |
| --- | --- |
| Carrier (Festnetz eingehend) | 0,00–0,01 € |
| Azure STT (Standard) | ≈ 0,016 € |
| Azure TTS Neural | ≈ 0,003 € bei 200 Zeichen/Minute Agentenanteil |
| Claude Haiku (≈ 3 000 Token/Minute inkl. Prompt) | ≈ 0,003 € |
| Summe | ≈ 0,02–0,03 €/Minute, Server anteilig |
