"""Lernende Einsatzdauer-Schätzung aus Rapporten: hierarchisches Shrinkage-Modell (Empirical Bayes).

Jede Schätzung ist ein gewichtetes Mittel aus den beobachteten Arbeitszeiten der feinsten Ebene und dem Prior der
nächstgröberen Ebene: Ebene 1 (Anlagentyp × Kategorie) → Ebene 2 (Kategorie) → Ebene 3 (Betrieb gesamt) → Ebene 4
(Standardwert 90 min bzw. Leistungsdauer). Mit n Beobachtungen und Pseudozählung k gilt
  μ̂ = (n·x̄ + k·μ_prior) / (n + k), k = 4.
So dominieren eigene Daten erst, wenn genug davon vorliegen, und seltene Kombinationen fallen nicht auf einen
einzelnen Ausreißer zurück. Die Planungsdauer ist das 80-%-Quantil (μ̂ + 0,84·σ̂) in 15-Minuten-Raster, damit die
Disposition eher Luft lässt als Verspätung produziert; σ̂ wird ebenso geschrumpft (Prior-Streuung 30 %).

Die Kategorie wird deterministisch aus dem Anliegen abgeleitet (Schlüsselwörter: Wartung, Störung/Ausfall,
Installation/Tausch, Notdienst, Prüfung, Reinigung …); der Sprachagent und die Online-Buchung liefern dieselben
Begriffe. Jede Antwort nennt Ebene, Stichprobengröße und Konfidenz, damit das Büro die Zahl einordnen kann.
"""
from __future__ import annotations

import math

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Anlage, Mandant, Rapport, Servicefall

K = 4.0
STANDARD_MINUTEN = 90
PRIOR_STREUUNG = 0.3
KATEGORIEN: list[tuple[str, tuple[str, ...]]] = [
    ("notdienst", ("notdienst", "notfall", "gasgeruch", "wasserschaden", "rohrbruch")),
    ("stoerung", ("störung", "stoerung", "ausfall", "defekt", "geht nicht", "kalt", "fehler", "tropft", "leck", "undicht", "läuft nicht", "kein warm")),
    ("installation", ("installation", "einbau", "tausch", "austausch", "montage", "neu", "ersetzen", "umbau", "sanierung")),
    ("pruefung", ("prüfung", "pruefung", "abnahme", "messung", "check", "inspektion", "gutachten")),
    ("wartung", ("wartung", "inspektion", "service", "reinigung", "filter", "entlüft", "hydraulisch")),
]


def kategorie(anliegen: str | None, dringlichkeit: str | None = None) -> str:
    if dringlichkeit == "notdienst":
        return "notdienst"
    text = (anliegen or "").lower()
    for name, woerter in KATEGORIEN:
        if any(w in text for w in woerter):
            return name
    return "sonstiges"


def _stichproben(s: Session, m: Mandant) -> list[tuple[str | None, str, int]]:
    """(anlagentyp, kategorie, arbeitszeit_minuten) je Rapport mit Arbeitszeit > 0."""
    zeilen = s.execute(select(Rapport.arbeitszeit_minuten, Servicefall.anliegen, Servicefall.dringlichkeit, Anlage.anlagentyp)
                       .join(Servicefall, Rapport.servicefall_id == Servicefall.id)
                       .outerjoin(Anlage, Servicefall.anlage_id == Anlage.id)
                       .where(Rapport.mandant_id == m.id, Rapport.arbeitszeit_minuten > 0)).all()
    return [(typ, kategorie(anliegen, dringlichkeit), int(minuten)) for minuten, anliegen, dringlichkeit, typ in zeilen]


def _schrumpfen(werte: list[int], prior_mu: float, prior_sigma: float) -> tuple[float, float, int]:
    n = len(werte)
    if n == 0:
        return prior_mu, prior_sigma, 0
    mu = (sum(werte) + K * prior_mu) / (n + K)
    var = sum((w - mu) ** 2 for w in werte) / n if n > 1 else prior_sigma ** 2
    sigma = math.sqrt((n * var + K * prior_sigma ** 2) / (n + K))
    return mu, sigma, n


def schaetzen(s: Session, m: Mandant, anliegen: str | None, anlagentyp: str | None = None, dringlichkeit: str | None = None,
              standard_minuten: int | None = None) -> dict[str, Any]:
    kat = kategorie(anliegen, dringlichkeit)
    proben = _stichproben(s, m)
    basis = float(standard_minuten or STANDARD_MINUTEN)
    mu3, sig3, n3 = _schrumpfen([w for _, _, w in proben], basis, basis * PRIOR_STREUUNG)
    mu2, sig2, n2 = _schrumpfen([w for _, k, w in proben if k == kat], mu3, sig3)
    if anlagentyp:
        mu1, sig1, n1 = _schrumpfen([w for t, k, w in proben if k == kat and t == anlagentyp], mu2, sig2)
        ebene, mu, sig, n = ("anlagentyp×kategorie" if n1 else ("kategorie" if n2 else ("betrieb" if n3 else "standard"))), mu1, sig1, n1
    else:
        ebene, mu, sig, n = ("kategorie" if n2 else ("betrieb" if n3 else "standard")), mu2, sig2, n2
    planung = int(math.ceil((mu + 0.84 * sig) / 15.0) * 15) if n3 else int(basis)   # ohne eigene Daten gilt der Standardwert
    konfidenz = "hoch" if n >= 10 else ("mittel" if n >= 3 else "niedrig")
    return {"kategorie": kat, "anlagentyp": anlagentyp, "erwartung_minuten": round(mu), "streuung_minuten": round(sig), "planung_minuten": max(15, planung),
            "ebene": ebene, "stichprobe": n, "konfidenz": konfidenz,
            "erklaerung": f"{n} Rapporte auf Ebene „{ebene}“ (Kategorie {kat}), Prior {round(mu3)} min aus {n3} Rapporten des Betriebs; "
                          f"Planung mit 80-%-Quantil in 15-min-Raster."}


def uebersicht(s: Session, m: Mandant) -> list[dict[str, Any]]:
    proben = _stichproben(s, m)
    gruppen: dict[tuple[str | None, str], list[int]] = {}
    for t, k, w in proben:
        gruppen.setdefault((t, k), []).append(w)
    return [{"anlagentyp": t, "kategorie": k, "n": len(v), "mittel_minuten": round(sum(v) / len(v)), "min": min(v), "max": max(v),
             **{kk: vv for kk, vv in schaetzen(s, m, k, t).items() if kk in ("erwartung_minuten", "planung_minuten", "konfidenz")}}
            for (t, k), v in sorted(gruppen.items(), key=lambda x: -len(x[1]))]
