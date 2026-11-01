"""Tagesplan-Optimierung: Vehicle-Routing mit Zeitfenstern, Qualifikationen und Arbeitszeiten über alle Monteure
eines Tages (VRPTW mit Skill-Constraints).

Modell. Jeder Termin ist ein Auftrag mit Dauer, benötigter Qualifikation, Kundenstandort und Zeitfenster: bestätigte
Termine dürfen nur innerhalb einer Toleranz um die zugesagte Uhrzeit liegen (Standard 0 min, d. h. fix; mit
`toleranz_minuten` = 60 wird aus „9:00“ das Fenster 8:00–10:00), vorgeschlagene Termine sind innerhalb der Arbeitszeit
frei. Kosten = Fahrzeit + 3·Verspätung + 5·Überstunden + 0,2·Wartezeit + Strafe je Monteurwechsel eines bestätigten
Termins. Qualifikationsverstöße sind hart.

Verfahren. Ausgangslösung ist der heutige Plan. Darauf arbeitet eine lokale Suche mit den Nachbarschaften Relocate
(Auftrag in anderen Monteur/andere Position), Swap (zwei Aufträge tauschen) und 2-opt (Teilroute umkehren), zuerst als
First-Improvement bis zum lokalen Optimum, dann als Simulated Annealing mit deterministischem Zufall (reproduzierbar).
Für die Tagesgröße eines Handwerksbetriebs (≤ 60 Aufträge) rechnet das in Millisekunden bis Sekunden; die Fahrzeiten
kommen aus `routing.Router` (OSRM oder Luftlinie). Kunden ohne Koordinaten gehen mit dem Puffer des Betriebs als
pauschaler Fahrzeit ein, damit der Plan nie ausfällt.

Ergebnis ist ein Vorschlag mit Begründung (Ersparnis in Minuten, Verspätungen, Überstunden, verschobene Termine);
`umsetzen()` schreibt ihn in die Termine und stößt für bestätigte Termine mit neuer Uhrzeit eine neue
Terminbestätigung an.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.dienstplan import arbeitszeit, dienstkarte
from core.qualifikationen import gueltig as qualifikation_gueltig
from core.disposition import ZEITZONE
from core.domaene import als_utc, outbox_schreiben, protokollieren
from core.modelle import Abwesenheit, Mandant, Ressource, Termin
from core.routing import Punkt, Router, _betrieb

GEWICHT = {"verspaetung": 3.0, "ueberstunden": 5.0, "wartezeit": 0.2, "monteurwechsel": 15.0, "unbekannte_fahrt": 1.0}
DRINGLICHKEIT_RANG = {"notdienst": 0, "hoch": 1, "normal": 2, "niedrig": 3}


@dataclass
class Auftrag:
    termin_id: str
    servicefall_id: str
    kunde: str
    dauer: int                      # Minuten
    qualifikation: str | None
    dringlichkeit: str
    status: str
    fenster_von: int                # frühester Beginn (Minuten ab Mitternacht, Ortszeit)
    fenster_bis: int                # spätester Beginn
    punkt: int | None               # Index in der Fahrzeitmatrix, None = ohne Koordinaten
    ressource_vorher: str
    beginn_vorher: int


@dataclass
class Fahrzeug:
    ressource_id: str
    name: str
    qualifikationen: set[str]
    arbeit_von: int
    arbeit_bis: int
    start: int | None               # Index des Betriebsstandorts


@dataclass
class Bewertung:
    kosten: float = math.inf
    fahrzeit: float = 0.0
    verspaetung: float = 0.0
    ueberstunden: float = 0.0
    wartezeit: float = 0.0
    monteurwechsel: int = 0
    zeiten: dict[str, tuple[int, int]] = field(default_factory=dict)   # termin_id → (beginn, ende)


class Problem:
    def __init__(self, auftraege: list[Auftrag], fahrzeuge: list[Fahrzeug], matrix: list[list[float]], pauschal: float, puffer: int):
        self.auftraege, self.fahrzeuge, self.matrix, self.pauschal, self.puffer = auftraege, fahrzeuge, matrix, pauschal, puffer
        self.index = {f.ressource_id: n for n, f in enumerate(fahrzeuge)}

    def fahrt(self, a: int | None, b: int | None) -> float:
        if a is None or b is None:
            return self.pauschal
        return self.matrix[a][b]

    def route_bewerten(self, f: Fahrzeug, route: list[int]) -> Bewertung | None:
        """Vorwärtsrechnung einer Route; None bei hartem Verstoß (Qualifikation)."""
        b = Bewertung(kosten=0.0)
        t, pos = f.arbeit_von, f.start
        for i in route:
            a = self.auftraege[i]
            if a.qualifikation and a.qualifikation not in f.qualifikationen:
                return None
            fahrt = math.ceil(self.fahrt(pos, a.punkt))
            t += max(fahrt, self.puffer) if pos is not None or a.punkt is not None else self.puffer
            beginn = max(int(-(-t // 5) * 5), a.fenster_von)   # Beginn im 5-Minuten-Raster
            b.wartezeit += beginn - t
            b.verspaetung += max(0, beginn - a.fenster_bis)
            b.fahrzeit += fahrt
            b.zeiten[a.termin_id] = (beginn, beginn + a.dauer)
            if a.status == "bestaetigt" and a.ressource_vorher != f.ressource_id:
                b.monteurwechsel += 1
            t, pos = beginn + a.dauer, a.punkt
        if route and f.start is not None:
            b.fahrzeit += math.ceil(self.fahrt(pos, f.start))   # Rückfahrt zum Betrieb
            t += math.ceil(self.fahrt(pos, f.start))
        b.ueberstunden = max(0, t - f.arbeit_bis) if route else 0
        b.kosten = (b.fahrzeit + GEWICHT["verspaetung"] * b.verspaetung + GEWICHT["ueberstunden"] * b.ueberstunden
                    + GEWICHT["wartezeit"] * b.wartezeit + GEWICHT["monteurwechsel"] * b.monteurwechsel)
        return b

    def bewerten(self, plan: list[list[int]]) -> tuple[float, list[Bewertung | None]]:
        teile = [self.route_bewerten(f, r) for f, r in zip(self.fahrzeuge, plan)]
        if any(t is None for t in teile):
            return math.inf, teile
        return sum(t.kosten for t in teile if t), teile

    # --- Nachbarschaften ---------------------------------------------------------------------------
    def nachbarn(self, plan: list[list[int]], zufall: random.Random | None = None):
        """Erzeugt Kandidatenpläne; mit `zufall` in zufälliger Reihenfolge (Annealing), sonst systematisch."""
        kandidaten = []
        n = len(plan)
        for v in range(n):
            for p in range(len(plan[v])):
                for w in range(n):
                    for q in range(len(plan[w]) + (0 if w == v else 1)):
                        if w == v and q in (p, p + 1):
                            continue
                        kandidaten.append(("relocate", v, p, w, q))
                for w in range(v, n):
                    for q in range(len(plan[w])):
                        if w == v and q <= p:
                            continue
                        kandidaten.append(("swap", v, p, w, q))
            for i in range(len(plan[v]) - 1):
                for j in range(i + 1, len(plan[v])):
                    kandidaten.append(("2opt", v, i, v, j))
        if zufall is not None:
            zufall.shuffle(kandidaten)
        for art, v, p, w, q in kandidaten:
            neu = [list(r) for r in plan]
            if art == "relocate":
                a = neu[v].pop(p)
                neu[w].insert(q, a)
            elif art == "swap":
                neu[v][p], neu[w][q] = neu[w][q], neu[v][p]
            else:
                neu[v][p:q + 1] = neu[v][p:q + 1][::-1]
            yield neu

    def loesen(self, start: list[list[int]], iterationen: int = 1500, seed: int = 7) -> tuple[list[list[int]], float]:
        plan, kosten = [list(r) for r in start], self.bewerten(start)[0]
        if kosten == math.inf:   # Ausgangsplan verletzt Qualifikationen: Aufträge notfalls neu verteilen
            plan, kosten = self._reparieren(plan)
        verbessert = True
        while verbessert:        # First-Improvement bis zum lokalen Optimum
            verbessert = False
            for kandidat in self.nachbarn(plan):
                k = self.bewerten(kandidat)[0]
                if k + 1e-6 < kosten:
                    plan, kosten, verbessert = kandidat, k, True
                    break
        zufall = random.Random(seed)
        beste, beste_kosten = plan, kosten
        temperatur = max(1.0, kosten * 0.05)
        aktuell, aktuell_kosten = plan, kosten
        for n, kandidat in enumerate(self.nachbarn(aktuell, zufall)):
            if n >= iterationen:
                break
            k = self.bewerten(kandidat)[0]
            if k == math.inf:
                continue
            if k < aktuell_kosten or zufall.random() < math.exp(-(k - aktuell_kosten) / temperatur):
                aktuell, aktuell_kosten = kandidat, k
                if k + 1e-6 < beste_kosten:
                    beste, beste_kosten = kandidat, k
            temperatur = max(0.05, temperatur * 0.995)
        # Nachbesserung aus dem besten Annealing-Zustand
        verbessert = True
        while verbessert:
            verbessert = False
            for kandidat in self.nachbarn(beste):
                k = self.bewerten(kandidat)[0]
                if k + 1e-6 < beste_kosten:
                    beste, beste_kosten, verbessert = kandidat, k, True
                    break
        return beste, beste_kosten

    def _reparieren(self, plan: list[list[int]]) -> tuple[list[list[int]], float]:
        frei = [i for r in plan for i in r]
        neu: list[list[int]] = [[] for _ in self.fahrzeuge]
        for i in sorted(frei, key=lambda i: (DRINGLICHKEIT_RANG.get(self.auftraege[i].dringlichkeit, 2), self.auftraege[i].fenster_von)):
            beste, beste_k = None, math.inf
            for v, f in enumerate(self.fahrzeuge):
                for q in range(len(neu[v]) + 1):
                    kandidat = [list(r) for r in neu]
                    kandidat[v].insert(q, i)
                    k = self.bewerten(kandidat)[0]
                    if k < beste_k:
                        beste, beste_k = kandidat, k
            if beste is not None:
                neu = beste
        return neu, self.bewerten(neu)[0]


# --- Anbindung an das Datenmodell ------------------------------------------------------------------
def _minuten(zeit: datetime, tag: date) -> int:
    lokal = als_utc(zeit).astimezone(ZEITZONE)
    return int((lokal - datetime.combine(tag, datetime.min.time(), ZEITZONE)).total_seconds() // 60)


def _zeitpunkt(tag: date, minuten: int) -> datetime:
    return datetime.combine(tag, datetime.min.time(), ZEITZONE) + timedelta(minutes=minuten)


def problem_laden(s: Session, m: Mandant, tag: date, router: Router, toleranz_minuten: int = 0) -> tuple[Problem, list[list[int]], str, list[dict[str, Any]]]:
    von = als_utc(_zeitpunkt(tag, 0))
    termine = s.scalars(select(Termin).where(Termin.mandant_id == m.id, Termin.beginn >= von, Termin.beginn < von + timedelta(days=1),
                                             Termin.status.in_(("vorgeschlagen", "bestaetigt"))).order_by(Termin.beginn)).all()
    abwesend = {a.ressource_id for a in s.scalars(select(Abwesenheit).where(Abwesenheit.mandant_id == m.id, Abwesenheit.von <= tag, Abwesenheit.bis >= tag))}
    dienste = dienstkarte(s, m.id, tag, tag)
    ressourcen = [r for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv, Ressource.art == "person").order_by(Ressource.name))
                  if arbeitszeit(r, tag, dienste, m.stammdaten) and r.id not in abwesend]
    zeiten = {r.id: arbeitszeit(r, tag, dienste, m.stammdaten) for r in ressourcen}
    betrieb = _betrieb(m)
    punkte: list[Punkt] = [betrieb] if betrieb else []
    index: dict[Punkt, int] = {betrieb: 0} if betrieb else {}
    auftraege: list[Auftrag] = []
    unplanbar: list[dict[str, Any]] = []
    for t in termine:
        k = t.servicefall.kunde
        p = None
        if k.lat is not None and k.lon is not None:
            p = index.setdefault((k.lat, k.lon), len(punkte))
            if p == len(punkte):
                punkte.append((k.lat, k.lon))
        beginn = _minuten(t.beginn, tag)
        dauer = max(5, int((als_utc(t.ende) - als_utc(t.beginn)).total_seconds() // 60))
        if t.status == "bestaetigt":
            fenster = (beginn - toleranz_minuten, beginn + toleranz_minuten)
        else:
            fenster = (0, 24 * 60)
        auftraege.append(Auftrag(t.id, t.servicefall_id, k.name, dauer, t.servicefall.benoetigte_qualifikation, t.servicefall.dringlichkeit,
                                 t.status, fenster[0], fenster[1], p, t.ressource_id, beginn))
    fahrzeuge = [Fahrzeug(r.id, r.name, {q for q in (r.qualifikationen or []) if qualifikation_gueltig(r, q, tag)}, zeiten[r.id][0].hour * 60 + zeiten[r.id][0].minute,
                          zeiten[r.id][1].hour * 60 + zeiten[r.id][1].minute, 0 if betrieb else None) for r in ressourcen]
    minuten, _, quelle = router.matrix(punkte) if len(punkte) >= 2 else ([[0.0] * len(punkte)] * len(punkte), None, "luftlinie")
    problem = Problem(auftraege, fahrzeuge, minuten, float(m.puffer_minuten), int(m.puffer_minuten))
    plan: list[list[int]] = [[] for _ in fahrzeuge]
    idx = {f.ressource_id: n for n, f in enumerate(fahrzeuge)}
    for i, a in enumerate(auftraege):
        if a.ressource_vorher in idx:
            plan[idx[a.ressource_vorher]].append(i)
        else:   # Monteur abwesend oder inaktiv: Auftrag muss umverteilt werden
            if fahrzeuge:
                plan[0].append(i)
            else:
                unplanbar.append({"termin_id": a.termin_id, "kunde": a.kunde, "grund": "kein Monteur am Tag"})
    return problem, plan, quelle, unplanbar


def vorschlag(s: Session, m: Mandant, tag: date, router: Router, toleranz_minuten: int = 0, iterationen: int = 1500) -> dict[str, Any]:
    problem, start, quelle, unplanbar = problem_laden(s, m, tag, router, toleranz_minuten)
    ist_kosten, ist_teile = problem.bewerten(start)
    if not problem.auftraege or not problem.fahrzeuge:
        return {"tag": tag.isoformat(), "quelle": quelle, "auftraege": len(problem.auftraege), "hinweis": "Keine Termine oder keine Monteure an diesem Tag",
                "routen": [], "aenderungen": [], "unplanbar": unplanbar}
    plan, kosten = problem.loesen(start, iterationen)
    _, teile = problem.bewerten(plan)

    def summe(teile_: list[Bewertung | None], feld: str) -> float:
        return sum(getattr(t, feld) for t in teile_ if t is not None)

    def kennzahlen(k: float, teile_: list[Bewertung | None]) -> dict[str, Any]:
        return {"kosten": None if k == math.inf else round(k, 1), "fahrzeit_minuten": round(summe(teile_, "fahrzeit")),
                "verspaetung_minuten": round(summe(teile_, "verspaetung")), "ueberstunden_minuten": round(summe(teile_, "ueberstunden")),
                "wartezeit_minuten": round(summe(teile_, "wartezeit")), "qualifikationsverstoesse": sum(1 for t in teile_ if t is None)}
    routen, aenderungen = [], []
    for f, route, bew in zip(problem.fahrzeuge, plan, teile):
        stopps = []
        for i in route:
            a = problem.auftraege[i]
            beginn, ende = bew.zeiten[a.termin_id]
            verschoben = beginn != a.beginn_vorher or f.ressource_id != a.ressource_vorher
            stopp = {"termin_id": a.termin_id, "servicefall_id": a.servicefall_id, "kunde": a.kunde, "status": a.status, "dringlichkeit": a.dringlichkeit,
                     "beginn": _zeitpunkt(tag, beginn).isoformat(), "ende": _zeitpunkt(tag, ende).isoformat(), "uhrzeit": f"{beginn // 60:02d}:{beginn % 60:02d}",
                     "verschoben": verschoben, "vorher": _zeitpunkt(tag, a.beginn_vorher).isoformat(), "ressource_vorher": a.ressource_vorher}
            stopps.append(stopp)
            if verschoben:
                aenderungen.append({**stopp, "ressource_id": f.ressource_id, "ressource": f.name, "differenz_minuten": beginn - a.beginn_vorher})
        routen.append({"ressource_id": f.ressource_id, "name": f.name, "stopps": stopps, "fahrzeit_minuten": round(bew.fahrzeit),
                       "ueberstunden_minuten": round(bew.ueberstunden), "verspaetung_minuten": round(bew.verspaetung)})
    ist, neu = kennzahlen(ist_kosten, ist_teile), kennzahlen(kosten, teile)
    return {"tag": tag.isoformat(), "quelle": quelle, "auftraege": len(problem.auftraege), "toleranz_minuten": toleranz_minuten,
            "ist": ist, "vorschlag": neu,
            "ersparnis_minuten": (ist["fahrzeit_minuten"] - neu["fahrzeit_minuten"]) if ist["kosten"] is not None else None,
            "routen": routen, "aenderungen": aenderungen, "unplanbar": unplanbar,
            "begruendung": _begruendung(ist, neu, aenderungen)}


def _begruendung(ist: dict[str, Any], neu: dict[str, Any], aenderungen: list[dict[str, Any]]) -> str:
    teile = []
    if ist["kosten"] is None:
        teile.append(f"Der aktuelle Plan verletzt {ist['qualifikationsverstoesse']} Qualifikationsanforderung(en); der Vorschlag behebt das.")
    d = ist["fahrzeit_minuten"] - neu["fahrzeit_minuten"]
    if d > 0:
        teile.append(f"{d} Minuten weniger Fahrzeit.")
    if ist["ueberstunden_minuten"] > neu["ueberstunden_minuten"]:
        teile.append(f"Überstunden von {ist['ueberstunden_minuten']} auf {neu['ueberstunden_minuten']} Minuten gesenkt.")
    if ist["verspaetung_minuten"] > neu["verspaetung_minuten"]:
        teile.append(f"Verspätungen gegenüber zugesagten Zeiten von {ist['verspaetung_minuten']} auf {neu['verspaetung_minuten']} Minuten gesenkt.")
    if not aenderungen:
        return "Der aktuelle Plan ist bereits optimal unter den gegebenen Zeitfenstern."
    teile.append(f"{len(aenderungen)} Termin(e) verschoben" + (", davon " + str(sum(1 for a in aenderungen if a['status'] == 'bestaetigt')) + " bestätigte (neue Terminbestätigung nötig)." if any(a["status"] == "bestaetigt" for a in aenderungen) else "."))
    return " ".join(teile)


def umsetzen(s: Session, m: Mandant, plan: dict[str, Any], akteur_name: str | None = None) -> dict[str, Any]:
    """Schreibt einen Vorschlag in die Termine; bestätigte Termine mit neuer Zeit oder neuem Monteur bekommen eine neue
    Terminbestätigung (Outbox endkunde), die Fachsoftware ein Upsert. Rückfahrten/Begründung sind nur Anzeige."""
    tag = date.fromisoformat(plan["tag"])
    geaendert, bestaetigungen = [], 0
    for route in plan.get("routen", []):
        r = s.get(Ressource, route["ressource_id"])
        if r is None or r.mandant_id != m.id:
            raise ValueError("Unbekannter Monteur im Plan")
        for stopp in route["stopps"]:
            t = s.get(Termin, stopp["termin_id"])
            if t is None or t.mandant_id != m.id or t.status == "abgesagt":
                raise ValueError(f"Termin {stopp['termin_id']} ist nicht planbar")
            if als_utc(t.beginn).astimezone(ZEITZONE).date() != tag:
                raise ValueError("Termin liegt nicht am Plantag")
            beginn, ende = datetime.fromisoformat(stopp["beginn"]), datetime.fromisoformat(stopp["ende"])
            if als_utc(t.beginn) != als_utc(beginn) or t.ressource_id != r.id:
                geaendert.append({"termin_id": t.id, "vorher": als_utc(t.beginn).isoformat(), "nachher": als_utc(beginn).isoformat(),
                                  "ressource_vorher": t.ressource_id, "ressource": r.id})
                t.beginn, t.ende, t.ressource_id = als_utc(beginn), als_utc(ende), r.id
                outbox_schreiben(s, m.id, "fachsoftware", "termin_upsert", {"termin_id": t.id}, f"termin_upsert:{t.id}:{als_utc(beginn).isoformat()}")
                if t.status == "bestaetigt":
                    outbox_schreiben(s, m.id, "endkunde", "terminbestaetigung", {"termin_id": t.id, "grund": "Neue Uhrzeit"},
                                     f"terminbestaetigung:{t.id}:{als_utc(beginn).isoformat()}")
                    bestaetigungen += 1
    protokollieren(s, m.id, "tagesplan.umgesetzt", "mandant", m.id, "mensch",
                   {"tag": plan["tag"], "verschoben": len(geaendert), "neue_bestaetigungen": bestaetigungen, "quelle": plan.get("quelle")}, akteur_name)
    return {"verschoben": geaendert, "neue_bestaetigungen": bestaetigungen}
