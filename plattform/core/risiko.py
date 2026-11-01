"""Prädiktive Wartung: Ausfallrisiko je Anlage aus einem Weibull-Überlebensmodell mit Kovariaten.

Grundhazard. Je Anlagentyp eine Weibull-Verteilung der Lebensdauer mit Formparameter β (> 1: Alterung) und
Skalenparameter η in Jahren (charakteristische Lebensdauer, Prior aus Erfahrungswerten für Haustechnik). Die
kumulierte Hazard-Funktion ist H(t) = (t/η)^β; die bedingte Ausfallwahrscheinlichkeit im nächsten Jahr bei Alter t
ist P = 1 − exp(−[H(t+1) − H(t)]).

Kovariaten (proportional hazards, multiplikativ auf H): Wartungsrückstand (überfällig ×1,5; > 24 Monate ohne
Wartung ×2,0; nie gewartet bei Alter > 2 ×2,5), Störhistorie (je Störfall mit Dringlichkeit notdienst/hoch in den
letzten 24 Monaten ×1,35, gedeckelt bei ×3), Rapportergebnis „nacharbeit“/„ersatzteil“ in 12 Monaten ×1,3,
Wartungsvertrag ×0,85 (dokumentierte Pflege). Unbekanntes Baujahr: Prior-Alter 12 Jahre mit Abschlag.

Kalibrierung. Liegen im eigenen Bestand ≥ 20 beobachtete Störfälle eines Typs vor, wird die Skala η empirisch
nachgeführt (Verhältnis beobachteter zu erwarteter Ausfallrate, auf [0,5; 2] begrenzt) – ein einfacher
Empirical-Bayes-Schritt, der die Priors mit zunehmenden Daten ablöst.

Ausgabe je Anlage: Wahrscheinlichkeit (12 Monate), Klasse (niedrig < 5 %, mittel < 15 %, hoch < 30 %, kritisch),
Treiber als Erklärung, Empfehlung (Wartung vorziehen, Ersatzangebot mit Förderung, Vertrag anbieten,
Ersatzteilvorhaltung). Portfolio: Rangliste, erwartete Ausfälle in der Heizperiode, Ersatzteilempfehlung je Typ.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Anlage, Mandant, Rapport, Servicefall
from core.ontologie import anlagentyp
from core.twin import naechste_wartung, wartungsstatus

WEIBULL: dict[str, tuple[float, float]] = {   # (β, η Jahre)
    "gas_brennwert": (2.3, 18.0), "oel": (2.1, 22.0), "waermepumpe": (1.9, 16.0), "solarthermie": (1.6, 20.0),
    "klima": (2.0, 13.0), "trinkwasser": (1.5, 25.0), "warmwasser": (2.2, 14.0), "sanitaer": (1.4, 30.0),
}
STANDARD = (1.8, 20.0)
ERSATZTEILE: dict[str, list[str]] = {
    "gas_brennwert": ["Zündelektrode/Ionisationselektrode", "Gasarmatur", "Umwälzpumpe", "Ausdehnungsgefäß", "Gebläse"],
    "oel": ["Öldüse", "Ölfilter", "Zündtrafo", "Fotozelle", "Umwälzpumpe"],
    "waermepumpe": ["Verdichter", "Expansionsventil", "Ventilator", "Drucksensor", "Platine"],
    "klima": ["Kondensatpumpe", "Verdichter", "Platine", "Fernbedienung"],
    "warmwasser": ["Opferanode", "Heizflansch", "Thermostat", "Sicherheitsventil"],
    "solarthermie": ["Solarflüssigkeit", "Solarpumpe", "Ausdehnungsgefäß", "Fühler"],
    "trinkwasser": ["Filterkerze", "Druckminderer", "Rückflussverhinderer"],
    "sanitaer": ["Dichtungen", "Kartuschen", "Spülkasten-Ventil"],
}
KLASSEN = ((0.05, "niedrig"), (0.15, "mittel"), (0.30, "hoch"), (1.01, "kritisch"))


def _H(t: float, beta: float, eta: float) -> float:
    return (max(0.0, t) / eta) ** beta


def ausfallwahrscheinlichkeit(alter_jahre: float, beta: float, eta: float, faktor: float = 1.0, horizont_jahre: float = 1.0) -> float:
    """P(Ausfall in [t, t+h] | überlebt bis t) unter proportionalem Hazard-Faktor."""
    dH = (_H(alter_jahre + horizont_jahre, beta, eta) - _H(alter_jahre, beta, eta)) * faktor
    return 1.0 - math.exp(-dH)


def klasse(p: float) -> str:
    return next(name for grenze, name in KLASSEN if p < grenze)


def _kovariaten(s: Session, a: Anlage, heute: date) -> tuple[float, list[str]]:
    faktor, treiber = 1.0, []
    status = wartungsstatus(a, heute)
    letzte = a.letzte_wartung
    if letzte is None and a.baujahr and heute.year - a.baujahr > 2:
        faktor *= 2.5
        treiber.append("nie gewartet")
    elif letzte is not None and (heute - letzte).days > 730:
        faktor *= 2.0
        treiber.append(f"letzte Wartung vor {(heute - letzte).days // 365} Jahren")
    elif status == "ueberfaellig":
        faktor *= 1.5
        treiber.append("Wartung überfällig")
    seit = heute - timedelta(days=730)
    stoerungen = s.scalars(select(Servicefall).where(Servicefall.anlage_id == a.id, Servicefall.dringlichkeit.in_(("notdienst", "hoch")),
                                                     Servicefall.erstellt >= _dt(seit))).all()
    if stoerungen:
        f = min(3.0, 1.35 ** len(stoerungen))
        faktor *= f
        treiber.append(f"{len(stoerungen)} Störfall/Störfälle in 24 Monaten")
    rapporte = s.scalars(select(Rapport).join(Servicefall, Rapport.servicefall_id == Servicefall.id)
                         .where(Servicefall.anlage_id == a.id, Rapport.ergebnis.in_(("nacharbeit", "ersatzteil")), Rapport.erstellt >= _dt(heute - timedelta(days=365)))).all()
    if rapporte:
        faktor *= 1.3
        treiber.append("Nacharbeit/Ersatzteil im letzten Jahr")
    if a.objekt.kunde.vertragsstatus == "wartungsvertrag":
        faktor *= 0.85
        treiber.append("Wartungsvertrag (−15 %)")
    return faktor, treiber


def _dt(d: date):
    from datetime import datetime, timezone
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def kalibrierung(s: Session, m: Mandant, heute: date) -> dict[str, float]:
    """Empirical-Bayes-Faktor je Anlagentyp: beobachtete Störfälle (24 Monate) gegen erwartete Ausfälle aus dem Prior."""
    anlagen = s.scalars(select(Anlage).where(Anlage.mandant_id == m.id)).all()
    seit = _dt(heute - timedelta(days=730))
    faktoren: dict[str, float] = {}
    for typ in {a.anlagentyp for a in anlagen}:
        gruppe = [a for a in anlagen if a.anlagentyp == typ]
        beobachtet = s.scalar(select(Servicefall.id).where(Servicefall.anlage_id.in_([a.id for a in gruppe]),
                                                            Servicefall.dringlichkeit.in_(("notdienst", "hoch")), Servicefall.erstellt >= seit).limit(1))
        n = len(s.scalars(select(Servicefall.id).where(Servicefall.anlage_id.in_([a.id for a in gruppe]),
                                                        Servicefall.dringlichkeit.in_(("notdienst", "hoch")), Servicefall.erstellt >= seit)).all()) if beobachtet else 0
        if n < 20:
            faktoren[typ] = 1.0
            continue
        beta, eta = WEIBULL.get(typ, STANDARD)
        erwartet = sum(ausfallwahrscheinlichkeit(_alter(a, heute), beta, eta, horizont_jahre=2.0) for a in gruppe)
        faktoren[typ] = min(2.0, max(0.5, n / erwartet)) if erwartet > 0 else 1.0
    return faktoren


def _alter(a: Anlage, heute: date) -> float:
    return float(heute.year - a.baujahr) if a.baujahr else 12.0


def bewerten(s: Session, a: Anlage, heute: date, kalib: dict[str, float] | None = None) -> dict[str, Any]:
    beta, eta = WEIBULL.get(a.anlagentyp, STANDARD)
    faktor, treiber = _kovariaten(s, a, heute)
    k = (kalib or {}).get(a.anlagentyp, 1.0)
    if k != 1.0:
        treiber.append(f"Kalibrierung aus eigenem Bestand ×{k:.2f}")
    alter = _alter(a, heute)
    if not a.baujahr:
        treiber.append("Baujahr unbekannt (Annahme 12 Jahre)")
    p = ausfallwahrscheinlichkeit(alter, beta, eta, faktor * k)
    p_heizperiode = ausfallwahrscheinlichkeit(alter, beta, eta, faktor * k, horizont_jahre=0.5)
    kl = klasse(p)
    typ = anlagentyp(a.anlagentyp)
    empfehlungen = []
    if kl in ("hoch", "kritisch") and alter >= 15 and a.anlagentyp in ("gas_brennwert", "oel"):
        empfehlungen.append("Ersatzangebot (Wärmepumpe/Brennwert) mit BEG-Förderung vorlegen")
    if wartungsstatus(a, heute) in ("ueberfaellig", "bald_faellig") or kl in ("hoch", "kritisch"):
        empfehlungen.append("Wartung vorziehen (vor der Heizperiode)")
    if a.objekt.kunde.vertragsstatus != "wartungsvertrag" and kl != "niedrig":
        empfehlungen.append("Wartungsvertrag anbieten")
    if kl in ("hoch", "kritisch") and a.anlagentyp in ERSATZTEILE:
        empfehlungen.append("Ersatzteile vorhalten: " + ", ".join(ERSATZTEILE[a.anlagentyp][:3]))
    faellig = naechste_wartung(a)
    return {"anlage_id": a.id, "anlagentyp": a.anlagentyp, "bezeichnung": typ.bezeichnung, "hersteller": a.hersteller, "modell": a.modell,
            "kunde_id": a.objekt.kunde.id, "kunde": a.objekt.kunde.name, "ort": a.objekt.ort, "alter_jahre": alter if a.baujahr else None,
            "p_12_monate": round(p, 3), "p_heizperiode": round(p_heizperiode, 3), "klasse": kl, "faktor": round(faktor * k, 2),
            "treiber": treiber, "empfehlungen": empfehlungen, "wartung": wartungsstatus(a, heute),
            "naechste_wartung": None if faellig in (None, date.min) else faellig.isoformat(),
            "modell_parameter": {"beta": beta, "eta_jahre": eta}}


def portfolio(s: Session, m: Mandant, heute: date, limit: int = 50) -> dict[str, Any]:
    kalib = kalibrierung(s, m, heute)
    anlagen = s.scalars(select(Anlage).where(Anlage.mandant_id == m.id)).all()
    bewertungen = sorted((bewerten(s, a, heute, kalib) for a in anlagen), key=lambda b: -b["p_12_monate"])
    je_klasse = {k: sum(1 for b in bewertungen if b["klasse"] == k) for _, k in KLASSEN}
    erwartet_heizperiode = sum(b["p_heizperiode"] for b in bewertungen)
    teile: dict[str, float] = {}
    for b in bewertungen:
        for t in ERSATZTEILE.get(b["anlagentyp"], [])[:3]:
            teile[t] = teile.get(t, 0.0) + b["p_heizperiode"]
    return {"anzahl": len(bewertungen), "je_klasse": je_klasse, "erwartete_ausfaelle_heizperiode": round(erwartet_heizperiode, 1),
            "erwartete_ausfaelle_12_monate": round(sum(b["p_12_monate"] for b in bewertungen), 1),
            "kalibrierung": {k: v for k, v in kalib.items() if v != 1.0},
            "ersatzteilvorhaltung": [{"teil": t, "erwarteter_bedarf": round(v, 1)} for t, v in sorted(teile.items(), key=lambda x: -x[1])[:8] if v >= 0.3],
            "anlagen": bewertungen[:limit]}
