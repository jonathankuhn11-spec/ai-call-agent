"""Kommandozeile des Core-Service.

    python -m core serve [--port 8100]     API und Büro-Oberfläche (/app) starten (DATABASE_URL, CORE_API_KEY)
    python -m core serve --demo            dito, legt bei leerer DB fiktive Demodaten an
    python -m core seed                    fiktive Demodaten anlegen, gibt die Mandanten-ID aus
    python -m core demo                    Demo-Mandant anlegen und Referenzfluss einmal durchspielen
    python -m core worker [--intervall 5]  Outbox-Worker (Zustellung an Adapter, hier CSV-Degradierung)
    python -m core migrate [--ziel head]   Datenbankmigrationen (MIGRATION_DATABASE_URL, Eigentümerrolle)

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
    elif args.befehl == "worker":
        from core.zustellung import CsvAdapter, abarbeiten
        db = system_datenbank()
        db.schema_anlegen()
        adapter = {ziel: CsvAdapter(ziel, args.csv_verzeichnis) for ziel in ("fachsoftware", "endkunde", "finanz")}
        while True:
            with db.sitzungen() as s:
                ergebnis = abarbeiten(s, adapter)
            if any(ergebnis.values()):
                print(json.dumps(ergebnis), flush=True)
            time.sleep(args.intervall)


if __name__ == "__main__":
    main()
