"""Gesetzliche Feiertage je Bundesland (Paket `holidays`, MIT) für Disposition, Plantafel und Lohnexport."""
from __future__ import annotations

from datetime import date
from functools import lru_cache

import holidays

BUNDESLAENDER = {"BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin", "BB": "Brandenburg", "HB": "Bremen", "HH": "Hamburg",
                 "HE": "Hessen", "MV": "Mecklenburg-Vorpommern", "NI": "Niedersachsen", "NW": "Nordrhein-Westfalen",
                 "RP": "Rheinland-Pfalz", "SL": "Saarland", "SN": "Sachsen", "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein",
                 "TH": "Thüringen"}
STANDARD = "NW"


def bundesland(stammdaten: dict | None) -> str:
    wert = str((stammdaten or {}).get("bundesland") or STANDARD).upper()
    return wert if wert in BUNDESLAENDER else STANDARD


@lru_cache(maxsize=64)
def _kalender(land: str, jahr: int) -> dict[date, str]:
    return dict(holidays.country_holidays("DE", subdiv=land, years=jahr, language="de"))


def feiertag(tag: date, stammdaten: dict | None) -> str | None:
    """Name des Feiertags oder None."""
    return _kalender(bundesland(stammdaten), tag.year).get(tag)


def feiertage(jahr: int, stammdaten: dict | None) -> dict[str, str]:
    return {d.isoformat(): name for d, name in sorted(_kalender(bundesland(stammdaten), jahr).items())}
