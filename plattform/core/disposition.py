"""Disposition: freie Zeitfenster unter Qualifikations-, Arbeitszeit- und Fahrzeit-Constraints.

Das ist bewusst ein Greedy-Verfahren über ein festes Raster: Es beantwortet im Telefonat in
Millisekunden „Wann kann jemand kommen?“. Für Tourenoptimierung über mehrere Einsätze
(Fahrzeiten zwischen Adressen) ist im Komponentenkatalog VROOM bzw. OR-Tools mit OSRM vorgesehen;
die Schnittstelle `freie_zeitfenster()` bleibt dabei gleich.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.domaene import als_utc
from core.dienstplan import arbeitszeit, dienstkarte
from core.qualifikationen import gueltig as qualifikation_gueltig
from core.modelle import Abwesenheit, Mandant, Ressource, Termin

ZEITZONE = ZoneInfo("Europe/Berlin")
BLOCKIERENDE_STATUS = ("vorgeschlagen", "bestaetigt")


@dataclass(frozen=True)
class Zeitfenster:
    ressource_id: str
    ressource_name: str
    beginn: datetime
    ende: datetime


def _belegungen(s: Session, mandant_id: str, ressource_ids: list[str], von: datetime, bis: datetime,
                puffer: timedelta) -> dict[str, list[tuple[datetime, datetime]]]:
    termine = s.scalars(select(Termin).where(
        Termin.mandant_id == mandant_id, Termin.ressource_id.in_(ressource_ids),
        Termin.status.in_(BLOCKIERENDE_STATUS), Termin.ende > von - puffer, Termin.beginn < bis + puffer))
    belegt: dict[str, list[tuple[datetime, datetime]]] = {r: [] for r in ressource_ids}
    for t in termine:
        belegt[t.ressource_id].append((als_utc(t.beginn) - puffer, als_utc(t.ende) + puffer))
    von_tag, bis_tag = von.astimezone(ZEITZONE).date(), bis.astimezone(ZEITZONE).date()
    for a in s.scalars(select(Abwesenheit).where(Abwesenheit.mandant_id == mandant_id, Abwesenheit.ressource_id.in_(ressource_ids),
                                                 Abwesenheit.bis >= von_tag, Abwesenheit.von <= bis_tag)):
        belegt[a.ressource_id].append((als_utc(datetime.combine(a.von, datetime.min.time(), ZEITZONE)),
                                       als_utc(datetime.combine(a.bis + timedelta(days=1), datetime.min.time(), ZEITZONE))))
    return belegt


def freie_zeitfenster(s: Session, mandant: Mandant, ab: datetime, tage: int = 5, dauer_minuten: int = 90,
                      qualifikation: str | None = None, anzahl: int = 5, je_ressource: int = 2) -> list[Zeitfenster]:
    """Früheste freie Fenster ab `ab`, sortiert nach Beginn; höchstens `je_ressource` je Monteur,
    damit der Agent echte Alternativen anbietet statt fünf Fenster desselben Tages."""
    ab = als_utc(ab)
    ressourcen = [r for r in s.scalars(select(Ressource).where(Ressource.mandant_id == mandant.id, Ressource.aktiv))
                  if qualifikation is None or qualifikation_gueltig(r, qualifikation, ab.astimezone(ZEITZONE).date())]
    if not ressourcen:
        return []
    puffer = timedelta(minutes=mandant.puffer_minuten)
    raster = timedelta(minutes=mandant.raster_minuten)
    dauer = timedelta(minutes=dauer_minuten)
    bis = ab + timedelta(days=tage + 1)
    belegt = _belegungen(s, mandant.id, [r.id for r in ressourcen], ab, bis, puffer)
    erster_tag = ab.astimezone(ZEITZONE).date()
    dienste = dienstkarte(s, mandant.id, erster_tag, erster_tag + timedelta(days=tage + 1), [r.id for r in ressourcen])

    kandidaten: list[Zeitfenster] = []
    for r in ressourcen:
        gefunden = 0
        tag: date = erster_tag
        for _ in range(tage + 1):
            az = arbeitszeit(r, tag, dienste, mandant.stammdaten)
            if az:
                tagesbeginn = datetime.combine(tag, az[0], ZEITZONE)
                tagesende = datetime.combine(tag, az[1], ZEITZONE)
                beginn = max(tagesbeginn, _aufrunden(ab.astimezone(ZEITZONE), raster, tagesbeginn))
                while beginn + dauer <= tagesende and gefunden < je_ressource:
                    ende = beginn + dauer
                    if not any(b < ende and beginn < e for b, e in belegt[r.id]):
                        kandidaten.append(Zeitfenster(r.id, r.name, beginn.astimezone(ZEITZONE), ende.astimezone(ZEITZONE)))
                        gefunden += 1
                        beginn = ende + puffer  # nächstes Angebot nicht direkt überlappend
                    else:
                        beginn += raster
            tag += timedelta(days=1)
    kandidaten.sort(key=lambda z: (z.beginn, z.ressource_name))
    return kandidaten[:anzahl]


def _aufrunden(zeit: datetime, raster: timedelta, bezug: datetime) -> datetime:
    """Rundet auf das nächste Rasterintervall ab Tagesbeginn auf."""
    if zeit <= bezug:
        return bezug
    schritte = -(-(zeit - bezug) // raster)
    return bezug + schritte * raster


def kollidiert(s: Session, mandant: Mandant, ressource_id: str, beginn: datetime, ende: datetime) -> bool:
    puffer = timedelta(minutes=mandant.puffer_minuten)
    for b, e in _belegungen(s, mandant.id, [ressource_id], als_utc(beginn), als_utc(ende), puffer)[ressource_id]:
        if b < als_utc(ende) and als_utc(beginn) < e:
            return True
    return False
