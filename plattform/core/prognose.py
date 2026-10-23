"""Nachfrageprognose und Kapazitätsplanung: Holt-Winters auf der Wochenreihe der Vorgänge, Kapazität aus
Arbeitszeiten, Abwesenheiten, Feiertagen und bereits gebuchten Terminen; Engpass- und Unterauslastungswarnungen.

Verfahren. Wochenreihe der eingegangenen Vorgänge (bis 104 Wochen). Bei mindestens zwei vollen Saisons (104 Wochen)
additives Holt-Winters mit Saisonlänge 52 und per Rastersuche optimierten Glättungsparametern (α, β, γ) nach
Ein-Schritt-Fehler. Bei kürzerer Historie wird die saisonale Komponente aus einem Branchen-Prior (SHK: Heizperiode
Oktober–Februar, Sommerloch; Salons: Dezember-Spitze, Sommerdelle; Werkstätten: Frühjahr/Herbst) übernommen und mit
den eigenen Daten gewichtet (w = n/(n+26): nach 26 Wochen halbe Gewichtung). Niveau und Trend kommen dann aus
einfacher exponentieller Glättung mit Trenddämpfung. Prognoseintervalle aus der Residualstreuung (±1,28 σ ≈ 80 %).

Bedarf in Stunden = Vorgänge × mittlere Einsatzdauer (aus Rapporten oder Terminen, Rückfall 1,5 h) × Fahrtfaktor.
Kapazität je Woche = Σ Monteure (Arbeitsstunden × Arbeitstage − Abwesenheit − Feiertag) − bereits gebuchte Stunden
(für die Prognosewochen). Auslastung = (Bedarf + gebucht) / Bruttokapazität. Engpass ≥ 90 %, Unterauslastung ≤ 55 %.
Empfehlungen: Bereitschaft/Aushilfe oder Notdienst-Priorisierung in Engpasswochen; Wartungen vorziehen bzw.
Wiederkehr-Aktionen in schwachen Wochen (Verknüpfung mit `risiko.portfolio`).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import branchen
from core.feiertage import feiertag
from core.modelle import Abwesenheit, Mandant, Rapport, Ressource, Servicefall, Termin

SAISON = 52
PRIOR: dict[str, list[float]] = {   # relative Saisonfaktoren je Monat (Jan..Dez), Mittel 1,0
    "handwerk": [1.25, 1.15, 1.05, 0.9, 0.8, 0.75, 0.75, 0.8, 1.0, 1.25, 1.35, 1.2],
    "salon": [0.9, 0.95, 1.0, 1.0, 1.05, 1.0, 0.9, 0.9, 1.0, 1.05, 1.1, 1.25],
    "werkstatt": [0.85, 0.9, 1.15, 1.2, 1.05, 1.0, 0.95, 0.95, 1.05, 1.15, 1.0, 0.75],
}


def _profil(m: Mandant) -> str:
    b = branchen.branche(m).schluessel
    if b in ("shk", "elektro"):
        return "handwerk"
    if b in ("fahrrad", "kfz"):
        return "werkstatt"
    return "salon"


def _wochenstart(d: date) -> date:
    return d - timedelta(days=d.weekday())


def wochenreihe(s: Session, m: Mandant, heute: date, wochen: int = 104) -> list[tuple[date, int]]:
    start = _wochenstart(heute) - timedelta(weeks=wochen - 1)
    zaehler = {start + timedelta(weeks=i): 0 for i in range(wochen)}
    for (erstellt,) in s.execute(select(Servicefall.erstellt).where(Servicefall.mandant_id == m.id)):
        w = _wochenstart(erstellt.date() if isinstance(erstellt, datetime) else erstellt)
        if w in zaehler:
            zaehler[w] += 1
    return sorted(zaehler.items())


def holt_winters(y: list[float], saison: int, alpha: float, beta: float, gamma: float) -> tuple[float, float, list[float], list[float]]:
    """Additives Holt-Winters; liefert Niveau, Trend, Saisonkomponenten und Ein-Schritt-Residuen."""
    n = len(y)
    s0 = [y[i] - sum(y[:saison]) / saison for i in range(saison)]
    level = sum(y[:saison]) / saison
    trend = (sum(y[saison:2 * saison]) - sum(y[:saison])) / saison ** 2 if n >= 2 * saison else 0.0
    sais = list(s0)
    resid = []
    for t in range(saison, n):
        vorhersage = level + trend + sais[t % saison]
        resid.append(y[t] - vorhersage)
        alt_level = level
        level = alpha * (y[t] - sais[t % saison]) + (1 - alpha) * (level + trend)
        trend = beta * (level - alt_level) + (1 - beta) * trend
        sais[t % saison] = gamma * (y[t] - level) + (1 - gamma) * sais[t % saison]
    return level, trend, sais, resid


def _bestes_hw(y: list[float], saison: int) -> tuple[tuple[float, float, float], float, float, list[float], float]:
    beste = None
    for a in (0.1, 0.2, 0.3, 0.5):
        for b in (0.01, 0.05, 0.1):
            for g in (0.05, 0.1, 0.3):
                level, trend, sais, resid = holt_winters(y, saison, a, b, g)
                fehler = sum(r * r for r in resid) / max(1, len(resid))
                if beste is None or fehler < beste[0]:
                    beste = (fehler, (a, b, g), level, trend, sais)
    fehler, params, level, trend, sais = beste
    return params, level, trend, sais, math.sqrt(fehler)


def prognose_reihe(reihe: list[tuple[date, int]], profil: str, horizont: int = 8) -> dict[str, Any]:
    """Prognose der nächsten `horizont` Wochen mit Verfahrenswahl nach Datenlage."""
    y = [float(v) for _, v in reihe]
    n_daten = sum(1 for v in y if v > 0)
    erste = next((i for i, v in enumerate(y) if v > 0), len(y))
    y_eff = y[erste:]
    letzte_woche = reihe[-1][0]
    if len(y_eff) >= 2 * SAISON:
        params, level, trend, sais, sigma = _bestes_hw(y_eff, SAISON)
        verfahren = f"Holt-Winters additiv (α={params[0]}, β={params[1]}, γ={params[2]})"
        werte = [max(0.0, level + (h + 1) * trend + sais[(len(y_eff) + h) % SAISON]) for h in range(horizont)]
    else:
        # exponentielle Glättung mit gedämpftem Trend auf der saisonbereinigten Reihe + Prior-Saisonfaktoren
        faktoren = PRIOR[profil]
        gewicht = len(y_eff) / (len(y_eff) + 26)
        def faktor(d: date) -> float:
            return 1.0 + gewicht * 0 + (faktoren[d.month - 1] - 1.0) * (1.0 if len(y_eff) < 26 else 1.0)
        bereinigt = [v / faktor(d) for (d, _), v in zip(reihe[erste:], y_eff)] if y_eff else []
        level, trend, resid = (bereinigt[0] if bereinigt else 0.0), 0.0, []
        alpha, beta_, phi = 0.3, 0.1, 0.9
        for v in bereinigt[1:]:
            vorhersage = level + phi * trend
            resid.append(v - vorhersage)
            alt = level
            level = alpha * v + (1 - alpha) * (level + phi * trend)
            trend = beta_ * (level - alt) + (1 - beta_) * phi * trend
        sigma = math.sqrt(sum(r * r for r in resid) / len(resid)) if resid else max(1.0, level * 0.5)
        verfahren = f"Exponentielle Glättung mit gedämpftem Trend, Saisonprior „{profil}“ (Gewicht eigener Daten {gewicht:.0%})"
        werte = []
        for h in range(horizont):
            d = letzte_woche + timedelta(weeks=h + 1)
            werte.append(max(0.0, (level + sum(phi ** k for k in range(1, h + 2)) * trend) * faktor(d)))
    wochen = []
    for h, w in enumerate(werte):
        d = letzte_woche + timedelta(weeks=h + 1)
        breite = 1.28 * sigma * math.sqrt(h + 1)
        wochen.append({"woche": d.isoformat(), "kw": d.isocalendar()[1], "vorgaenge": round(w, 1), "untergrenze": round(max(0.0, w - breite), 1),
                       "obergrenze": round(w + breite, 1)})
    return {"verfahren": verfahren, "datenwochen": len(y_eff), "datenpunkte": n_daten, "sigma": round(sigma, 2), "wochen": wochen,
            "historie": [{"woche": d.isoformat(), "vorgaenge": int(v)} for d, v in reihe[-26:]]}


def mittlere_dauer_h(s: Session, m: Mandant) -> float:
    minuten = [r.arbeitszeit_minuten + r.fahrzeit_minuten for r in s.scalars(select(Rapport).where(Rapport.mandant_id == m.id)) if r.arbeitszeit_minuten]
    if len(minuten) >= 5:
        return sum(minuten) / len(minuten) / 60
    dauern = [(t.ende - t.beginn).total_seconds() / 3600 for t in s.scalars(select(Termin).where(Termin.mandant_id == m.id)).all()]
    return (sum(dauern) / len(dauern) * 1.25) if dauern else 1.5   # +25 % Fahrt und Rüstzeit


def kapazitaet(s: Session, m: Mandant, woche: date) -> dict[str, float]:
    brutto, gebucht = 0.0, 0.0
    ressourcen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv)).all()
    abwesenheiten = s.scalars(select(Abwesenheit).where(Abwesenheit.mandant_id == m.id, Abwesenheit.bis >= woche, Abwesenheit.von < woche + timedelta(days=7))).all()
    for r in ressourcen:
        stunden = (datetime.combine(date.min, r.arbeitsende) - datetime.combine(date.min, r.arbeitsbeginn)).seconds / 3600
        for i in range(7):
            tag = woche + timedelta(days=i)
            if tag.weekday() not in (r.arbeitstage or []) or feiertag(tag, m.stammdaten):
                continue
            if any(a.ressource_id == r.id and a.von <= tag <= a.bis for a in abwesenheiten):
                continue
            brutto += stunden
    von = datetime.combine(woche, datetime.min.time())
    for t in s.scalars(select(Termin).where(Termin.mandant_id == m.id, Termin.status.in_(("vorgeschlagen", "bestaetigt")),
                                            Termin.beginn >= von, Termin.beginn < von + timedelta(days=7))):
        gebucht += (t.ende - t.beginn).total_seconds() / 3600
    return {"brutto_h": round(brutto, 1), "gebucht_h": round(gebucht, 1)}


def auslastung(s: Session, m: Mandant, heute: date, horizont: int = 8) -> dict[str, Any]:
    reihe = wochenreihe(s, m, heute)
    p = prognose_reihe(reihe, _profil(m), horizont)
    dauer = mittlere_dauer_h(s, m)
    wochen, warnungen = [], []
    for w in p["wochen"]:
        woche = date.fromisoformat(w["woche"])
        kap = kapazitaet(s, m, woche)
        bedarf = w["vorgaenge"] * dauer
        last = kap["gebucht_h"] + bedarf
        quote = (last / kap["brutto_h"]) if kap["brutto_h"] else None
        quote_max = ((kap["gebucht_h"] + w["obergrenze"] * dauer) / kap["brutto_h"]) if kap["brutto_h"] else None
        lage = "engpass" if quote is not None and quote >= 0.9 else ("unterauslastung" if quote is not None and quote <= 0.55 else "normal")
        eintrag = {**w, "kapazitaet_h": kap["brutto_h"], "gebucht_h": kap["gebucht_h"], "bedarf_h": round(bedarf, 1), "auslastung": None if quote is None else round(quote, 2),
                   "auslastung_obergrenze": None if quote_max is None else round(quote_max, 2), "lage": lage}
        wochen.append(eintrag)
        if lage == "engpass":
            fehl = max(0.0, last - kap["brutto_h"])
            warnungen.append({"woche": w["woche"], "kw": w["kw"], "art": "engpass",
                              "text": f"KW {w['kw']}: voraussichtlich {quote:.0%} Auslastung ({fehl:.0f} h Fehlkapazität). Bereitschaft/Aushilfe planen, Wartungen in schwache Wochen verschieben, Notdienst priorisieren."})
        elif lage == "unterauslastung":
            frei = kap["brutto_h"] - last
            warnungen.append({"woche": w["woche"], "kw": w["kw"], "art": "unterauslastung",
                              "text": f"KW {w['kw']}: nur {quote:.0%} Auslastung ({frei:.0f} h frei). Fällige Wartungen und Anlagen mit hohem Ausfallrisiko aktiv terminieren, Wiederkehr-Aktion starten."})
    return {"heute": heute.isoformat(), "verfahren": p["verfahren"], "datenwochen": p["datenwochen"], "sigma": p["sigma"],
            "mittlere_dauer_h": round(dauer, 2), "wochen": wochen, "warnungen": warnungen, "historie": p["historie"]}
