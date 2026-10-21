"""Kommandozeile des Core-Service.

    python -m core serve [--port 8100]     API und Büro-Oberfläche (/app) starten (DATABASE_URL, CORE_API_KEY)
    python -m core serve --demo            dito, legt bei leerer DB fiktive Demodaten an
    python -m core seed                    fiktive Demodaten anlegen, gibt die Mandanten-ID aus
    python -m core demo                    Demo-Mandant anlegen und Referenzfluss einmal durchspielen
    python -m core worker [--intervall 5]  Outbox-Worker (Zustellung an Adapter, hier CSV-Degradierung)
    python -m core migrate [--ziel head]   Datenbankmigrationen (MIGRATION_DATABASE_URL, Eigentümerrolle)
    python -m core schluessel              INTEGRATIONS_KEY für die Verschlüsselung von Zugangsdaten erzeugen
    python -m core loeschlauf [--trocken]  Löschkonzept anwenden (Fristen je Mandant), täglich per cron
    python -m core tagesaufgaben           Angebotsablauf, Mahnvorschläge, Vertragsabrechnungen (Freigaben), täglich per cron
    python -m core posteingang             E-Mail-Postfächer der Mandanten abrufen und Anfragen zu Vorgängen machen

Verbindungen in PostgreSQL: DATABASE_URL (shk_app, unterliegt Row-Level Security) für den Core,
SYSTEM_DATABASE_URL (shk_system) für Worker, Seed und Mandantenverwaltung. Ohne
SYSTEM_DATABASE_URL wird DATABASE_URL verwendet (SQLite, Entwicklung).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from core.db import Datenbank, migrieren


def system_datenbank() -> Datenbank:
    return Datenbank(os.environ.get("SYSTEM_DATABASE_URL") or None)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m core")
    unter = parser.add_subparsers(dest="befehl", required=True)
    serve = unter.add_parser("serve")
    serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8100")))
    serve.add_argument("--demo", action="store_true")
    unter.add_parser("demo")
    unter.add_parser("seed")
    worker = unter.add_parser("worker")
    worker.add_argument("--intervall", type=float, default=5.0)
    worker.add_argument("--csv-verzeichnis", type=Path, default=Path(os.environ.get("CSV_EXPORT", "export")))
    unter.add_parser("schluessel")
    lauf = unter.add_parser("loeschlauf")
    lauf.add_argument("--trocken", action="store_true")
    unter.add_parser("tagesaufgaben")
    unter.add_parser("posteingang")
    migrate = unter.add_parser("migrate")
    migrate.add_argument("--ziel", default="head")
    args = parser.parse_args()

    if args.befehl == "serve":
        import uvicorn

        from core.api import app_erzeugen
        app = app_erzeugen()
        if args.demo:
            from core.seed import demodaten_anlegen
            with app.state.system_db.sitzungen() as s:
                print(f"Demo-Mandant: {demodaten_anlegen(s)}", flush=True)
        uvicorn.run(app, host=args.host, port=args.port)
    elif args.befehl == "seed":
        from core.seed import demodaten_anlegen
        db = system_datenbank()
        db.schema_anlegen()
        with db.sitzungen() as s:
            print(demodaten_anlegen(s))
    elif args.befehl == "demo":
        from core.demo import referenzfluss
        print(json.dumps(referenzfluss(), indent=2, ensure_ascii=False, default=str))
    elif args.befehl == "migrate":
        migrieren(ziel=args.ziel)
    elif args.befehl == "loeschlauf":
        from sqlalchemy import select

        from core.loeschung import loeschlauf
        from core.modelle import Mandant
        db = system_datenbank()
        db.schema_anlegen()
        with db.sitzungen() as s:
            for m in s.scalars(select(Mandant)).all():
                print(json.dumps({"mandant": m.name, **loeschlauf(s, m, trocken=args.trocken)}, ensure_ascii=False), flush=True)
    elif args.befehl == "tagesaufgaben":
        from datetime import datetime, timezone

        from sqlalchemy import select

        from core.modelle import Mandant
        from core.tagesaufgaben import tagesaufgaben
        db = system_datenbank()
        db.schema_anlegen()
        with db.sitzungen() as s:
            for m in s.scalars(select(Mandant)).all():
                print(json.dumps({"mandant": m.name, **tagesaufgaben(s, m, datetime.now(timezone.utc))}, ensure_ascii=False, default=str), flush=True)
                s.commit()
    elif args.befehl == "posteingang":
        from datetime import datetime, timezone

        from sqlalchemy import select

        from core.anfragen import ImapPostfach, postfach_abrufen
        from core.geheimnisse import konfiguration_entschluesseln
        from core.modelle import Mandant
        db = system_datenbank()
        db.schema_anlegen()
        with db.sitzungen() as s:
            for m in s.scalars(select(Mandant)).all():
                k = konfiguration_entschluesseln(m.integrationen.get("posteingang") or {})
                if not k.get("host"):
                    continue
                postfach = ImapPostfach(k["host"], k["benutzer"], k["passwort"], k.get("ordner", "INBOX"), int(k.get("port", 993)))
                try:
                    print(json.dumps({"mandant": m.name, **postfach_abrufen(s, m, postfach, datetime.now(timezone.utc))}, ensure_ascii=False), flush=True)
                    s.commit()
                finally:
                    postfach.schliessen()
    elif args.befehl == "schluessel":
        from core.geheimnisse import schluessel_erzeugen
        print(schluessel_erzeugen())
    elif args.befehl == "worker":
        from core.adapter import Register
        from core.zustellung import abarbeiten
        db = system_datenbank()
        db.schema_anlegen()
        register = Register(args.csv_verzeichnis)
        while True:
            with db.sitzungen() as s:
                ergebnis = abarbeiten(s, register)
            if any(ergebnis.values()):
                print(json.dumps(ergebnis), flush=True)
            time.sleep(args.intervall)


if __name__ == "__main__":
    main()
