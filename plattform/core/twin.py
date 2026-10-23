"""Digital Twin: konsolidierte Zustandssicht auf Anlage und Betrieb.

Der Zwilling ist keine eigene Datenhaltung, sondern eine Projektion des kanonischen Modells
und des Event-Logs. Anlagen-Zwilling: Stammdaten, Wartungszustand, Servicehistorie, offene
Vorgänge. Betriebs-Zwilling: Vorgangsbestand je Status, Auslastung je Monteur, fällige
Wartungen (Grundlage für proaktives Wartungsgeschäft). Telemetrie (z. B. Störmeldungen von
Wärmepumpen über Herstellerportale) wird später als weiteres Ereignis eingespeist.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.dienstplan import arbeitszeit, dienstkarte
from core.disposition import BLOCKIERENDE_STATUS
from core.domaene import als_utc
from core.modelle import Anlage, Ereignis, Mandant, Ressource, Servicefall, Termin
from core.ontologie import anlagentyp

OFFENE_STATUS = ("eingegangen", "qualifiziert", "eingeplant")


def naechste_wartung(a: Anlage) -> date | None:
    if not a.wartungsintervall_monate:
        return None
    if a.letzte_wartung is None:
        return date.min  # nie gewartet: sofort fällig
    monat = a.letzte_wartung.month - 1 + a.wartungsintervall_monate
    jahr = a.letzte_wartung.year + monat // 12
    monat = monat % 12 + 1
    tag = min(a.letzte_wartung.day, 28)
    return date(jahr, monat, tag)


def wartungsstatus(a: Anlage, heute: date) -> str:
    faellig = naechste_wartung(a)
    if faellig is None:
        return "ohne_intervall"
    if faellig <= heute:
        return "ueberfaellig"
    if faellig <= heute + timedelta(days=60):
        return "bald_faellig"
    return "aktuell"


def anlagen_zwilling(s: Session, mandant_id: str, anlage_id: str, heute: date) -> dict[str, Any] | None:
    a = s.get(Anlage, anlage_id)
    if a is None or a.mandant_id != mandant_id:
        return None
    faelle = s.scalars(select(Servicefall).where(Servicefall.anlage_id == a.id).order_by(Servicefall.erstellt.desc())).all()
    typ = anlagentyp(a.anlagentyp)
    faellig = naechste_wartung(a)
    return {
        "anlage": {"id": a.id, "typ": a.anlagentyp, "bezeichnung": typ.bezeichnung, "ontologieklasse": typ.klasse,
                   "hersteller": a.hersteller, "modell": a.modell, "baujahr": a.baujahr,
                   "alter_jahre": heute.year - a.baujahr if a.baujahr else None},
        "objekt": {"id": a.objekt.id, "bezeichnung": a.objekt.bezeichnung, "ort": a.objekt.ort},
        "kunde": {"id": a.objekt.kunde.id, "name": a.objekt.kunde.name, "vertragsstatus": a.objekt.kunde.vertragsstatus},
        "wartung": {"letzte": a.letzte_wartung, "naechste": None if faellig in (None, date.min) else faellig,
                    "status": wartungsstatus(a, heute), "benoetigte_qualifikation": typ.qualifikation},
        "offene_vorgaenge": [{"id": f.id, "status": f.status, "dringlichkeit": f.dringlichkeit, "anliegen": f.anliegen}
                             for f in faelle if f.status in OFFENE_STATUS],
        "historie": [{"id": f.id, "status": f.status, "anliegen": f.anliegen, "erstellt": f.erstellt} for f in faelle],
    }


def betriebs_zwilling(s: Session, mandant: Mandant, jetzt_: datetime, tage: int = 5) -> dict[str, Any]:
    m = mandant.id
    bestand = dict(s.execute(select(Servicefall.status, func.count()).where(Servicefall.mandant_id == m)
                             .group_by(Servicefall.status)).all())
    notdienst_offen = s.scalar(select(func.count()).select_from(Servicefall).where(
        Servicefall.mandant_id == m, Servicefall.dringlichkeit == "notdienst", Servicefall.status.in_(OFFENE_STATUS)))
    bis = als_utc(jetzt_) + timedelta(days=tage)
    auslastung = []
    stammdaten = mandant.stammdaten
    dienste = dienstkarte(s, m, jetzt_.date(), jetzt_.date() + timedelta(days=tage))
    for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m, Ressource.aktiv)):
        termine = s.scalars(select(Termin).where(Termin.ressource_id == r.id, Termin.status.in_(BLOCKIERENDE_STATUS),
                                                 Termin.beginn >= als_utc(jetzt_), Termin.beginn < bis)).all()
        gebucht = sum((als_utc(t.ende) - als_utc(t.beginn)).total_seconds() for t in termine) / 3600
        kapazitaet = 0.0
        for i in range(tage):
            az = arbeitszeit(r, jetzt_.date() + timedelta(days=i), dienste, stammdaten)
            if az:
                kapazitaet += (datetime.combine(date.min, az[1]) - datetime.combine(date.min, az[0])).seconds / 3600
        auslastung.append({"ressource_id": r.id, "name": r.name, "gebucht_h": round(gebucht, 1),
                           "kapazitaet_h": round(kapazitaet, 1),
                           "quote": round(gebucht / kapazitaet, 2) if kapazitaet else None})
    heute = jetzt_.date()
    wartungen = [{"anlage_id": a.id, "typ": a.anlagentyp, "kunde": a.objekt.kunde.name, "status": wartungsstatus(a, heute)}
                 for a in s.scalars(select(Anlage).where(Anlage.mandant_id == m))
                 if wartungsstatus(a, heute) in ("ueberfaellig", "bald_faellig")]
    letzte = s.scalars(select(Ereignis).where(Ereignis.mandant_id == m).order_by(Ereignis.id.desc()).limit(20)).all()
    return {
        "betrieb": {"id": m, "name": mandant.name},
        "vorgaenge_je_status": bestand,
        "notdienst_offen": notdienst_offen,
        "auslastung": auslastung,
        "wartungspotenzial": wartungen,
        "letzte_ereignisse": [{"typ": e.typ, "akteur": e.akteur, "zeit": e.zeit, "entitaet_id": e.entitaet_id} for e in letzte],
    }
