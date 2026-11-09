"""Upsell-Agent: ordnet jedem Lead Edition und Zusatzmodule zu und beziffert den erwarteten Kundenwert.

Edition und Branchenprofil übernimmt er vom Bedarfs-Agenten der Kundenpakete (kundenpaket/bedarf.py), damit Brief,
Angebot und Priorisierung dieselbe Zuordnung verwenden. Zusatzmodule folgen den Auslöseregeln in katalog.toml
(Problemcodes P1–P10, Branche, Schlagwörter der Stellenanzeige). Ein Modul, das in der gewählten Edition bereits
enthalten ist, erscheint als Argument, nicht als Mehrumsatz.

Kundenwert (Customer Lifetime Value, netto, Deckungsbeitrag):
    CLV = Einrichtung + Σ Modul-Einmalbeträge + (Monat + Σ Modul-Monatsbeträge) · Vertragsmonate · DB-Quote
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import tomllib
from kundenpaket import bedarf as bedarf_agent
from kundenpaket import leads as kp_leads

BASIS = Path(__file__).resolve().parent
EDITION_RANG = {"basis": 0, "pro": 1, "premium": 2}


def katalog_laden(pfad: Path | None = None) -> dict:
    with (pfad or BASIS / "katalog.toml").open("rb") as f:
        return tomllib.load(f)


def preise_laden(pfad: Path | None = None) -> dict:
    with (pfad or BASIS.parent / "unterlagen" / "preise.toml").open("rb") as f:
        return tomllib.load(f)


@dataclass
class Zuordnung:
    branche: str
    edition: str
    monat: float
    einrichtung: float
    module: list[dict] = field(default_factory=list)       # vorgeschlagene Zusatzmodule (Mehrumsatz)
    enthalten: list[dict] = field(default_factory=list)    # in der Edition enthaltene Argumente
    clv: float = 0.0

    @property
    def modul_codes(self) -> str:
        return ", ".join(m["code"] for m in self.module)

    @property
    def monat_gesamt(self) -> float:
        return self.monat + sum(m.get("monat", 0) for m in self.module)

    @property
    def einmalig_gesamt(self) -> float:
        return self.einrichtung + sum(m.get("einmalig", 0) for m in self.module)


def _kp_lead(lead) -> kp_leads.Lead:
    w = lead.werte
    return kp_leads.Lead(firma=lead.firma, gewerk=w.get("gewerk", ""), plz=w.get("plz", ""), ort=w.get("ort", ""),
                         strasse=w.get("strasse", ""), telefon=w.get("telefon", ""), email=w.get("email", ""),
                         website=w.get("website", ""), entscheider=w.get("entscheider", ""), stelle=w.get("stelle", ""),
                         infrastruktur=w.get("infrastruktur", ""), probleme=list(lead.probleme), angebot=w.get("angebot", ""),
                         befund=w.get("befund", ""), status=w.get("status", ""), klasse=w.get("klasse", ""), score=str(lead.score),
                         akut=lead.akut)


def zuordnen(lead, katalog: dict, preise: dict) -> Zuordnung:
    b = bedarf_agent.analysieren(_kp_lead(lead))
    text = f"{lead.werte.get('stelle', '')} {lead.werte.get('befund', '')} {lead.werte.get('gewerk', '')}".lower()
    codes = set(lead.probleme)
    edition = b.edition
    module, enthalten = [], []
    for m in katalog.get("modul", []):
        greift = (codes & set(m.get("codes", ()))) or (b.branche in m.get("branchen", ())) or \
                 any(re.search(re.escape(s), text) for s in m.get("schlagworte", ()))
        if not greift:
            continue
        if m["code"] == "DI":                   # Disposition = Upgrade auf Premium
            if edition != "premium":
                module.append(dict(m))
            continue
        if m["code"] == "W3" and "P1" in codes:
            continue                            # ohne Website kein Relaunch
        if m["code"] == "W1" and b.website_modus != "neu":
            continue
        ed = m.get("in_edition")
        if ed and EDITION_RANG[edition] >= EDITION_RANG[ed]:
            enthalten.append(dict(m))
        elif ed:                                # Modul verlangt höhere Edition: als Argument für das Upgrade führen
            enthalten.append(dict(m, hinweis=f"ab Edition {ed}"))
        else:
            module.append(dict(m))
    paket = preise["pakete"][edition]
    z = Zuordnung(branche=b.branche, edition=edition, monat=float(paket["monat_netto"]), einrichtung=float(paket["einrichtung_netto"]),
                  module=module, enthalten=enthalten)
    mod = katalog["modell"]
    z.clv = round(z.einmalig_gesamt + z.monat_gesamt * mod["vertragsmonate"] * mod["db_quote"], 2)
    return z
