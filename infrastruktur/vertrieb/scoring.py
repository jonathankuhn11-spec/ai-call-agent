"""Priorisierungs-Agent: Abschlusswahrscheinlichkeit und Erwartungswert je Lead mit Rückkopplung aus dem Tracking.

Modell
------
1. Prior je Lead aus dem Abschluss-Score der Leadlisten (0–100):
       p0 = basis + spanne · (score/100)^exponent,   bei akuten Leads · faktor_akut,   gekappt bei p_max.
   Konzern-/Gruppenbetriebe und Niederlassungen · faktor_filiale (zentrale Entscheidung). Die Konstanten sind Annahmen (Rücklaufquote B2B-Brief ca. 1–7 %) und werden durch eigene Daten überschrieben.
2. Empirical-Bayes-Korrektur je Segment in zwei Dimensionen (Branche, Signaltyp). Für jedes Segment s gilt
       θ_s ~ Beta(m·μ_s, m·(1−μ_s)),   μ_s = mittlerer Prior der Leads in s,   m = prior_staerke,
   beobachtet werden k_s positive Reaktionen bei n_s abgeschlossenen Kontaktversuchen. Der Posterior-Mittelwert
       θ̂_s = (m·μ_s + k_s) / (m + n_s)
   ergibt den Lift λ_s = θ̂_s / μ_s. Vorab kalibriert ein globaler Faktor g = (k + 0,05·m)/(E[k] + 0,05·m) das
   Gesamtniveau (beobachtete vs. erwartete Erfolge); die Segmente messen dann nur die Abweichung von μ_s·g.
   Je Lead p = p0 · g · √(λ_Branche · λ_Signal) (geometrisches Mittel, damit sich zwei Dimensionen nicht doppelt
   zählen). Ohne Daten ist g = λ = 1 und es gilt p = p0.
3. Rechtszensierung: Ein Kontakt zählt erst als Versuch, wenn ein Ergebnis vorliegt oder die Kadenz abgelaufen ist
   (Erstbrief + Nachfassfrist + Kadenzende). Laufende Kadenzen würden die Quote sonst systematisch drücken.
4. Erwartungswert je Brief: EV = p · p_abschluss_nach_reaktion · CLV − Kontaktkosten (Porto, Druck, Arbeitszeit).
5. Exploration: Ein Anteil der Charge wird per Thompson-Sampling vergeben (λ aus dem Posterior gezogen statt
   Mittelwert), damit auch unterbeobachtete Segmente Daten erzeugen (Exploration-Exploitation-Abwägung).
"""
from __future__ import annotations

import datetime as dt
import math
import random
import re
from dataclasses import dataclass

from vertrieb.speicher import KONTAKT, NEGATIV, POSITIV


def signaltyp(lead) -> str:
    if lead.werte.get("stelle") or "P8" in lead.probleme:
        return "Personalengpass"
    if "P1" in lead.probleme:
        return "ohne Website"
    if "P5" in lead.probleme or "P7" in lead.probleme:
        return "Erreichbarkeit"
    return "Website-Mängel"


def ist_filiale(lead) -> bool:
    """Konzern-/Gruppenbetrieb oder Niederlassung: Entscheidung fällt zentral, Brief an den Standort wirkt schwächer."""
    if lead.werte.get("konzern", "").lower().startswith("ja"):
        return True
    return bool(re.search(r"\b(NL|Niederlassung|Filiale)\b", lead.firma))


def prior(lead, modell: dict) -> float:
    p = modell["basis"] + modell["spanne"] * (lead.score / 100.0) ** modell["exponent"]
    if lead.akut:
        p *= modell["faktor_akut"]
    if ist_filiale(lead):
        p *= modell["faktor_filiale"]
    return min(p, modell["p_max"])


@dataclass
class Segment:
    mu: float = 0.0
    anzahl: int = 0
    n: int = 0
    k: int = 0

    def posterior(self, m: float, g: float = 1.0) -> tuple[float, float]:
        mu = min(self.mu * g, 0.999)
        return m * mu + self.k, m * (1 - mu) + self.n - self.k

    def lift(self, m: float, rng: random.Random | None = None, g: float = 1.0) -> float:
        """Lift relativ zur global kalibrierten Erwartung μ·g."""
        if self.mu <= 0:
            return 1.0
        a, b = self.posterior(m, g)
        theta = rng.betavariate(a, b) if rng else a / (a + b)
        return theta / min(self.mu * g, 0.999)


def ergebnis(ereignisse, heute: dt.date, kadenz: dict) -> tuple[bool, bool]:
    """(Versuch abgeschlossen, positiv) für die Historie eines Leads."""
    typen = {e["typ"] for e in ereignisse}
    if not typen & KONTAKT:
        return False, False
    if typen & POSITIV:
        return True, True
    if typen & NEGATIV:
        return True, False
    erster = min(dt.date.fromisoformat(e["datum"][:10]) for e in ereignisse if e["typ"] in KONTAKT)
    frist = kadenz["nachfassbrief_nach_tagen"] + kadenz["kadenzende_nach_tagen"]
    return (heute - erster).days >= frist, False


class Modell:
    def __init__(self, leads, historie: dict, katalog: dict, heute: dt.date, branchen: dict[str, str]):
        self.modell = katalog["modell"]
        self.kadenz = katalog["kadenz"]
        self.m = float(self.modell["prior_staerke"])
        self.p0 = {le.id: prior(le, self.modell) for le in leads}
        self.dim = {"branche": {}, "signal": {}}
        self.zuordnung_dim: dict[str, tuple[str, str]] = {}
        for le in leads:
            b, s = branchen.get(le.id, "unbekannt"), signaltyp(le)
            self.zuordnung_dim[le.id] = (b, s)
            for d, key in (("branche", b), ("signal", s)):
                seg = self.dim[d].setdefault(key, Segment())
                seg.mu += self.p0[le.id]
                seg.anzahl += 1
        for d in self.dim.values():
            for seg in d.values():
                seg.mu /= max(seg.anzahl, 1)
        self.versuche = self.erfolge = 0
        mu_versuche = 0.0
        for le in leads:
            fertig, positiv = ergebnis(historie.get(le.id, []), heute, self.kadenz)
            if not fertig:
                continue
            self.versuche += 1
            self.erfolge += positiv
            mu_versuche += self.p0[le.id]
            for d, key in zip(("branche", "signal"), self.zuordnung_dim[le.id]):
                seg = self.dim[d][key]
                seg.n += 1
                seg.k += positiv
        # Globale Kalibrierung g: beobachtete vs. erwartete Erfolge der kontaktierten Leads (Gamma-Poisson-Schrumpfung,
        # Priorstärke m erwartete Erfolge·m/n). Die Segment-Lifts messen dann Abweichungen relativ zu μ·g.
        erwartet = mu_versuche
        self.g = (self.erfolge + self.m * 0.05) / (erwartet + self.m * 0.05) if self.versuche else 1.0

    def p(self, lead_id: str, rng: random.Random | None = None) -> float:
        b, s = self.zuordnung_dim[lead_id]
        lift = math.sqrt(self.dim["branche"][b].lift(self.m, rng, self.g) * self.dim["signal"][s].lift(self.m, rng, self.g))
        return min(self.p0[lead_id] * self.g * lift, self.modell["p_max"])

    def segmente(self) -> list[dict]:
        aus = []
        for d, segs in self.dim.items():
            for key, seg in sorted(segs.items(), key=lambda x: -x[1].anzahl):
                a, b = seg.posterior(self.m, self.g)
                aus.append({"Dimension": d, "Segment": key, "Leads": seg.anzahl, "Prior μ": seg.mu, "Versuche n": seg.n,
                            "Erfolge k": seg.k, "Posterior θ̂": a / (a + b), "Lift λ": seg.lift(self.m, g=self.g),
                            "90%-Intervall": _beta_intervall(a, b)})
        return aus


def kontaktkosten(versand: dict) -> float:
    return versand["porto_standardbrief"] + versand["druck_kuvert"] + versand["minuten_je_brief"] / 60 * versand["stundensatz_intern"]


def erwartungswert(p: float, clv: float, katalog: dict) -> float:
    return p * katalog["modell"]["p_abschluss_nach_reaktion"] * clv - kontaktkosten(katalog["versand"])


def _beta_intervall(a: float, b: float, n: int = 4000) -> str:
    rng = random.Random(7)
    z = sorted(rng.betavariate(a, b) for _ in range(n))
    return f"{z[int(0.05 * n)]:.1%} – {z[int(0.95 * n)]:.1%}"
