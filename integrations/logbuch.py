"""Strukturiertes Logbuch der Integrationsschicht: eine JSON-Zeile je Vorgang, korreliert über call_id.

Im Betrieb liest niemand Prosa-Logs; gesucht wird nach einem Anruf ("was ist in call_7f3 passiert?"),
nach einem Lead oder nach einem Endpunkt mit Fehlern. Darum trägt jede Zeile dieselben Schlüssel:
zeit, ereignis, dazu call_id, lead_id, endpunkt, ms, ok, soweit vorhanden. Aggregatoren (Loki,
CloudWatch, Datadog) lesen das ohne Parser.

Was nie im Log steht (Checkliste 02, Abschnitt F): Transkripte, Geheimnisse, Telefonnummern. Eine
Nummer wird, wenn sie gebraucht wird, nur als Pseudonym (aufbewahrung.nummer_hash) geschrieben.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger("integrations")
LOGGER.addHandler(logging.NullHandler())      # ohne einrichten() (Tests, Demo, Bibliotheksnutzung) bleibt das Logbuch still
VERBOTEN = ("transkript", "transcript", "secret", "token", "api_key", "telefon", "phone")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        felder = getattr(record, "felder", None) or {}
        zeile = {"zeit": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                 "stufe": record.levelname.lower(), "ereignis": record.getMessage(), **felder}
        if record.exc_info:
            zeile["ausnahme"] = self.formatException(record.exc_info)[-500:]
        return json.dumps(zeile, ensure_ascii=False, default=str)


def einrichten(stufe: int = logging.INFO, strom=None) -> logging.Logger:
    """JSON-Zeilen auf stdout (Container-Konvention); mehrfach aufrufbar, ohne doppelte Handler."""
    LOGGER.setLevel(stufe)
    LOGGER.propagate = False
    for h in list(LOGGER.handlers):
        LOGGER.removeHandler(h)
    handler = logging.StreamHandler(strom or sys.stdout)
    handler.setFormatter(JsonFormatter())
    LOGGER.addHandler(handler)
    return LOGGER


def _sauber(felder: dict[str, Any]) -> dict[str, Any]:
    """Verbotene Schlüssel fliegen raus, lange Werte werden gekürzt, None bleibt weg."""
    aus = {}
    for k, v in felder.items():
        if v is None or any(w in k.lower() for w in VERBOTEN):
            continue
        if isinstance(v, str) and len(v) > 300:
            v = v[:300] + "…"
        aus[k] = v
    return aus


def eintrag(ereignis: str, stufe: int = logging.INFO, **felder: Any) -> None:
    """logbuch.eintrag("tool", call_id=..., lead_id=..., endpunkt="tools/update_lead", ms=4.2, ok=True)"""
    if LOGGER.isEnabledFor(stufe):
        LOGGER.log(stufe, ereignis, extra={"felder": _sauber(felder)})


def warnung(ereignis: str, **felder: Any) -> None:
    eintrag(ereignis, logging.WARNING, **felder)


def fehler(ereignis: str, **felder: Any) -> None:
    eintrag(ereignis, logging.ERROR, **felder)
