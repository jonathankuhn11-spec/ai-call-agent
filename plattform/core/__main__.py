"""Kommandozeile des Core-Service.

    python -m core serve [--port 8100]     API starten (DATABASE_URL, CORE_API_KEY aus der Umgebung)
    python -m core demo                    Demo-Mandant anlegen und Referenzfluss einmal durchspielen
    python -m core worker [--intervall 5]  Outbox-Worker (Zustellung an Adapter, hier CSV-Degradierung)
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from core.db import Datenbank


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m core")
    unter = parser.add_subparsers(dest="befehl", required=True)
    serve = unter.add_parser("serve")
    serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8100")))
    unter.add_parser("demo")
    worker = unter.add_parser("worker")
    worker.add_argument("--intervall", type=float, default=5.0)
    worker.add_argument("--csv-verzeichnis", type=Path, default=Path(os.environ.get("CSV_EXPORT", "export")))
    args = parser.parse_args()

    if args.befehl == "serve":
        import uvicorn

        from core.api import app_erzeugen
        uvicorn.run(app_erzeugen(), host=args.host, port=args.port)
    elif args.befehl == "demo":
        from core.demo import referenzfluss
        print(json.dumps(referenzfluss(), indent=2, ensure_ascii=False, default=str))
    elif args.befehl == "worker":
        from core.zustellung import CsvAdapter, abarbeiten
        db = Datenbank()
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
