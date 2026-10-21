# Drittanbieter: mitgeführte und gespiegelte Fremdkomponenten

Die Plattform soll ohne externe Quellen baubar, prüfbar und betreibbar sein. Dieses Verzeichnis führt deshalb alle
Fremdartefakte mit, die dafür nötig sind, mit Lizenztext, Version, Herkunft und Prüfsumme. Es wird nichts in eigenen
Code hineinkopiert: Bibliotheken bleiben Pakete, Server bleiben Images, Regelwerke bleiben Dateien. Eine Übersicht
der eingesetzten Open-Source-Komponenten mit Bewertung steht in [`../KATALOG.md`](../KATALOG.md).

| Bestandteil | Inhalt | Im Repository | Erzeugt durch |
| --- | --- | --- | --- |
| `lizenzen/` | Lizenztexte aller verwendeten Komponenten (siehe Tabelle unten) | ja | manuell, aus den Projektrepositorien |
| `ontologie/Brick-1.4.1.ttl` | Brick-Ontologie (BSD-3-Clause), Ziel der `skos:broadMatch`-Abgleiche in `ontologie/shk.ttl`; Test prüft die Zielklassen | ja | `spiegeln.sh` (Prüfsumme) |
| `xrechnung/` | KoSIT-Validator 1.5.0 und XRechnung-Konfiguration 3.0.2 (Apache-2.0); `pruefen.sh` validiert erzeugte XRechnungen; die Testsuite nutzt ihn, wenn er entpackt ist | Skripte ja, Binärdateien nein | `spiegeln.sh` |
| `wheelhouse/` | alle Python-Pakete aus `requirements.txt` (und mit `--voice` aus `requirements-voice.txt`) als Wheels für Python 3.12/manylinux; Build mit `--build-arg OFFLINE=1` | nein | `spiegeln.sh` |
| `images/` | Container-Images aus `IMAGES.txt` als `docker save`-Archive | nein | `spiegeln.sh --images` |
| `QUELLEN.txt`, `IMAGES.txt`, `SHA256SUMS` | Herkunft, Version, Lizenz und Prüfsumme je Artefakt; alle Images mit fester Version statt `latest` | ja | manuell |

Binärartefakte (Wheels, Validator, Image-Archive, zusammen über 2 GB mit Voice) gehören nicht in die Versionsverwaltung.
Sie werden mit `drittanbieter/spiegeln.sh` reproduzierbar erzeugt und über `SHA256SUMS` geprüft; auf dem Server oder in
einem Artefaktspeicher (z. B. Hetzner Storage Box, GitHub Release) liegen sie als Kopie.

```sh
drittanbieter/spiegeln.sh --voice --images   # alles laden; --pruefen prüft nur Prüfsummen
docker build --build-arg OFFLINE=1 -t shk-core .   # installiert Pakete nur aus wheelhouse/
drittanbieter/xrechnung/pruefen.sh rechnung.xml    # KoSIT-Prüfbericht (HTML/XML) nach xrechnung/berichte/
```

## Lizenzen der Komponenten

Nur permissive Lizenzen werden mitgeführt oder in das Image eingebaut. Komponenten unter Copyleft (AGPL, GPL) sind
bewusst nicht enthalten und werden auch nicht als Dienst betrieben; sie stehen im Katalog nur als Referenz. n8n
(Sustainable Use License) ist ein optionales Compose-Profil für den internen Betrieb und wird nicht an Dritte angeboten.

| Komponente | Version | Lizenz | Einbindung | Lizenztext |
| --- | --- | --- | --- | --- |
| FastAPI | 0.143.0 | MIT | Python-Paket | `lizenzen/fastapi.txt` |
| uvicorn | 0.53.0 | BSD-3-Clause | Python-Paket | `lizenzen/uvicorn.txt` |
| pydantic | 2.13.5 | MIT | Python-Paket | `lizenzen/pydantic.txt` |
| SQLAlchemy | 2.0.55 | MIT | Python-Paket | `lizenzen/sqlalchemy.txt` |
| Alembic | 1.20.0 | MIT | Python-Paket | `lizenzen/alembic.txt` |
| psycopg | 3.2.10 | LGPL-3.0 (unverändert als Bibliothek genutzt, dynamisch geladen) | Python-Paket | `lizenzen/psycopg.txt` |
| httpx | 0.28.1 | BSD-3-Clause | Python-Paket | `lizenzen/httpx.txt` |
| PyJWT | 2.15.1 | MIT | Python-Paket | `lizenzen/pyjwt.txt` |
| rdflib | 7.6.0 | BSD-3-Clause | Python-Paket | `lizenzen/rdflib.txt` |
| reportlab | 5.0.1 | BSD-3-Clause | Python-Paket | `lizenzen/reportlab.txt` |
| python-multipart | 0.0.32 | Apache-2.0 | Python-Paket | `lizenzen/python-multipart.txt` |
| pypdf | 6.17.0 | BSD-3-Clause | Python-Paket | `lizenzen/pypdf.txt` |
| Pillow | 12.3.0 | MIT-CMU | Python-Paket | `lizenzen/pillow.txt` |
| lxml | 6.1.3 | BSD-3-Clause | Python-Paket (Tests) | `lizenzen/lxml.txt` |
| LiveKit Agents (inkl. Plugins) | 1.8.8 | Apache-2.0 | Python-Paket (Voice) | `lizenzen/livekit-agents.txt` |
| LiveKit Server, LiveKit SIP | v1.13.10, v1.21.0 | Apache-2.0 | Container-Image | `lizenzen/livekit.txt` |
| Silero VAD | über LiveKit-Plugin | MIT | Modell im Voice-Image | `lizenzen/silero-vad.txt` |
| Keycloak | 26.0.7 | Apache-2.0 | Container-Image | `lizenzen/keycloak.txt` |
| PostgreSQL + pgvector | 16 / 0.8.7 | PostgreSQL License | Container-Image | `lizenzen/pgvector.txt` |
| Apache Jena Fuseki | 5.1.0 | Apache-2.0 | Container-Image (Profil `ontologie`) | `lizenzen/jena.txt` |
| Caddy | 2.10 | Apache-2.0 | Container-Image | `lizenzen/caddy.txt` |
| Brick Schema | 1.4.1 | BSD-3-Clause | Datei `ontologie/Brick-1.4.1.ttl` | `lizenzen/brick.txt` |
| KoSIT-Validator | 1.5.0 | Apache-2.0 | Datei `xrechnung/` (Java) | `lizenzen/kosit-validator.txt`, `kosit-validator-NOTICE.txt` |
| XRechnung-Validator-Konfiguration | 3.0.2 (2025-07-10) | Apache-2.0 | Datei `xrechnung/konfiguration/` | `lizenzen/xrechnung-konfiguration.txt` |

Prüfung am 21.10.2026: die von `core/rechnung.py` erzeugte XRechnung besteht den KoSIT-Validator mit der Konfiguration
3.0.2 (Schema, Schematron, Bewertung „ACCEPTABLE“, keine Warnungen). Die Testsuite wiederholt diese Prüfung automatisch,
sobald Validator und Java vorhanden sind (`tests/test_drittanbieter.py`).
