"""Orchestrierung der Vertriebs-Agenten: Erfassung → Upsell → Priorisierung → Kadenz → Charge → Abrufbudget → Cockpit."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from kundenpaket import bedarf as bedarf_agent

from vertrieb import budget, erfassung, kadenz, scoring, speicher, upsell, versand


@dataclass
class Ergebnis:
    protokoll: dict
    zeilen: list[dict] = field(default_factory=list)       # je Lead: lead, zuordnung, p, ev, stand, signal
    aufgaben: list = field(default_factory=list)
    charge_id: str | None = None
    charge: list[dict] = field(default_factory=list)
    modell: scoring.Modell | None = None
    budget: dict | None = None
    upsell: list[dict] = field(default_factory=list)
    trichter: dict = field(default_factory=dict)


def _db_leads(db) -> list[erfassung.Lead]:
    aus = []
    for r in db.execute("SELECT * FROM leads"):
        le = erfassung.Lead(id=r["id"], firma=r["firma"], werte=json.loads(r["werte"]), probleme=[p for p in r["probleme"].split(",") if p],
                            score=r["score"] or 0.0, akut=bool(r["akut"]), gesperrt=bool(r["gesperrt"]), sperrgrund=r["sperrgrund"] or "",
                            ausgeschlossen=r["ausgeschlossen"] or "", herkunft=json.loads(r["herkunft"] or "[]"), erstmals=r["erstmals"] or "")
        aus.append(le)
    return aus


def ansprechpartner(le) -> str:
    return bedarf_agent.ansprechpartner(upsell._kp_lead(le))


def lauf(wurzel: Path, db_pfad: Path, heute: dt.date, charge_groesse: int | None = None, neue_charge: bool = True,
         katalog: dict | None = None, seed: int | None = None, quellen: list[Path] | None = None) -> Ergebnis:
    katalog = katalog or upsell.katalog_laden()
    preise = upsell.preise_laden()
    db = speicher.oeffnen(db_pfad)
    pfade = quellen if quellen is not None else erfassung.quellen_finden(wurzel)
    neu, protokoll = erfassung.alle_lesen(pfade)
    speicher.leads_schreiben(db, neu, heute.isoformat())
    speicher.sperre_auf_rufnummer(db)
    historie = speicher.historie(db)
    # Nur Leads des aktuellen Bestands; ältere IDs (z. B. nach korrigierter Firmierung/PLZ) bleiben nur, wenn sie Historie haben
    aktuell = {le.id for le in neu}
    leads = [le for le in _db_leads(db) if le.id in aktuell or le.id in historie]

    zuordnung = {le.id: upsell.zuordnen(le, katalog, preise) for le in leads}
    modell = scoring.Modell(leads, historie, katalog, heute, {i: z.branche for i, z in zuordnung.items()})
    erg = Ergebnis(protokoll=protokoll, modell=modell)
    staende = {}
    for le in leads:
        st = kadenz.stand(historie.get(le.id, []), le.gesperrt, katalog["kadenz"], heute, le.sperrgrund)
        staende[le.id] = st
        z = zuordnung[le.id]
        p = modell.p(le.id)
        erg.zeilen.append({"lead": le, "zuordnung": z, "p": p, "ev": scoring.erwartungswert(p, z.clv, katalog), "stand": st,
                           "signal": scoring.signaltyp(le), "branche": z.branche, "clv": z.clv})
    erg.zeilen.sort(key=lambda r: -r["ev"])
    erg.aufgaben = kadenz.aufgaben(staende, heute)

    # Trichter
    for r in erg.zeilen:
        erg.trichter[r["stand"].status] = erg.trichter.get(r["stand"].status, 0) + 1

    # Upsell-Potenzial je Modul (nur aktive, versandfähige Leads)
    pot: dict[str, dict] = {}
    for r in erg.zeilen:
        if r["lead"].gesperrt or r["lead"].ausgeschlossen:
            continue
        for m in r["zuordnung"].module:
            e = pot.setdefault(m["code"], {"Code": m["code"], "Modul": m["name"], "Leads": 0, "MRR-Potenzial": 0.0, "Einmal-Potenzial": 0.0,
                                           "erwarteter MRR (p-gewichtet)": 0.0, "Status": m.get("status", ""), "Begründung": m.get("grund", "")})
            e["Leads"] += 1
            e["MRR-Potenzial"] += m.get("monat", 0)
            e["Einmal-Potenzial"] += m.get("einmalig", 0)
            e["erwarteter MRR (p-gewichtet)"] += m.get("monat", 0) * r["p"] * katalog["modell"]["p_abschluss_nach_reaktion"]
    erg.upsell = sorted(pot.values(), key=lambda e: -e["MRR-Potenzial"])

    # Charge
    offen = versand.offene_chargen_leads(db)
    if neue_charge and not offen:
        sperr = dt.timedelta(days=katalog["versand"]["sperrfrist_tage"])
        kandidaten = []
        for r in erg.zeilen:
            le, st = r["lead"], r["stand"]
            if le.gesperrt or le.ausgeschlossen or not versand.versandfaehig(le) or le.id in offen:
                continue
            if st.status == "neu":
                art = "Erstbrief"
            elif st.status == "Nachfassbrief fällig":
                art = "Nachfassbrief"
            elif st.status == "Kadenz beendet" and st.faellig and st.faellig <= heute:
                art = "Erstbrief"
            else:
                continue
            letzte = [dt.date.fromisoformat(e["datum"][:10]) for e in historie.get(le.id, []) if e["typ"] in speicher.KONTAKT]
            if art == "Erstbrief" and letzte and heute - max(letzte) < sperr:
                continue
            kandidaten.append(dict(r, art=art))
        n = charge_groesse or katalog["versand"]["chargengroesse"]
        charge = versand.auswaehlen(kandidaten, n, katalog, modell, seed if seed is not None else int(heute.strftime("%Y%m%d")))
        if charge:
            erg.charge_id = versand.charge_anlegen(db, charge, heute, {"n": n, "exploration": katalog["modell"]["exploration"]})
            erg.charge = charge

    # Abrufbudget
    index: dict[str, tuple[float, float]] = {}
    vorbestand: set[str] = set()
    for r in erg.zeilen:
        le = r["lead"]
        keys = set()
        t = erfassung.telefon_norm(le.werte.get("telefon", ""))
        if t:
            keys.add("t:" + t)
        keys.add("f:" + erfassung.firma_norm(le.firma) + "|" + (le.werte.get("plz") or le.werte.get("ort") or ""))
        for k in keys:
            index[k] = (r["p"], r["clv"])
        index.setdefault("n:" + erfassung.firma_norm(le.firma), (r["p"], r["clv"]))
        # Bestand vor den Rohdaten der Wellen/Segment-Agenten = alle Leads, die (auch) aus einer Altliste stammen
        if any(not h.startswith(("Gesamtliste_Wellen", "Agent_", "Kfz_", "SHK_Leads_ausserhalb")) for h in le.herkunft):
            vorbestand |= keys

    def wert(keys):
        return next((index[k] for k in sorted(keys, key=lambda k: k.startswith("n:")) if k in index), None)

    def branche_von(gewerk, firma):
        return bedarf_agent.branche_bestimmen(upsell.kp_leads.Lead(firma=firma, gewerk=gewerk))[0]

    erg.budget = budget.auswerten(wurzel, wert, branche_von, vorbestand)
    db.close()
    return erg
