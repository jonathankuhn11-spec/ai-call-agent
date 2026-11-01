"""Gewährleistung und Herstellergarantie je Vorgang – „zahlt das der Kunde, der Hersteller oder wir?“.

Gewährleistung aus eigenen Leistungen (Rapporte): Nach § 634a BGB verjähren Mängelansprüche bei Arbeiten an einem
Bauwerk in fünf Jahren, bei sonstigen Werkleistungen (Reparatur, Wartung) in zwei Jahren; bei Vereinbarung der VOB/B
gelten vier Jahre für Bauleistungen (§ 13 Abs. 4 VOB/B), zwei Jahre für feuerungstechnische Anlagen ohne
Wartungsvertrag. Die Zuordnung erfolgt über die Kategorie des Anliegens (schaetzung.kategorie): „installation“ gilt
als Bauwerksleistung, alles andere als sonstige Werkleistung; `stammdaten.vob` schaltet auf VOB-Fristen, Material
aus eigener Lieferung folgt § 438 BGB (zwei Jahre). Herstellergarantie: Baujahr der Anlage + stammdaten.garantie_jahre
(Standard 2; je Hersteller überschreibbar über stammdaten.garantie_hersteller {"Vaillant": 5}) oder eine Frist der
Art „garantie“ auf die Anlage. Ergebnis sind Hinweise im Vorgang, keine Rechtsprüfung.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Anlage, Frist, Kunde, Mandant, Rapport, Servicefall
from core.schaetzung import kategorie


def _plus_jahre(d: date, jahre: int) -> date:
    try:
        return d.replace(year=d.year + jahre)
    except ValueError:
        return d.replace(year=d.year + jahre, day=28)


def gewaehrleistungsjahre(m: Mandant, kat: str) -> tuple[int, str]:
    vob = bool((m.stammdaten or {}).get("vob"))
    if kat == "installation":
        return (4, "§ 13 Abs. 4 Nr. 1 VOB/B") if vob else (5, "§ 634a Abs. 1 Nr. 2 BGB (Bauwerk)")
    return (2, "§ 13 Abs. 4 Nr. 2 VOB/B") if vob else (2, "§ 634a Abs. 1 Nr. 1 BGB (Werkleistung)")


def herstellergarantie(s: Session, m: Mandant, a: Anlage, heute: date) -> dict[str, Any] | None:
    frist = s.scalar(select(Frist).where(Frist.mandant_id == m.id, Frist.bezug_typ == "anlage", Frist.bezug_id == a.id, Frist.art == "garantie").order_by(Frist.faellig.desc()).limit(1))
    if frist:
        ende, quelle = frist.faellig, frist.bezeichnung
    elif a.baujahr:
        st = m.stammdaten or {}
        jahre = int((st.get("garantie_hersteller") or {}).get(a.hersteller or "", st.get("garantie_jahre", 2)))
        ende, quelle = _plus_jahre(date(a.baujahr, 1, 1), jahre), f"Baujahr {a.baujahr} + {jahre} Jahre (Annahme, Garantieurkunde prüfen)"
    else:
        return None
    return {"anlage_id": a.id, "hersteller": a.hersteller, "modell": a.modell, "ende": ende.isoformat(), "aktiv": ende >= heute,
            "resttage": (ende - heute).days, "quelle": quelle}


def eigene_leistungen(s: Session, m: Mandant, kunde: Kunde, heute: date, anlage_id: str | None = None) -> list[dict[str, Any]]:
    """Rapporte des Kunden mit laufender Gewährleistungsfrist."""
    abfrage = select(Rapport).join(Servicefall, Servicefall.id == Rapport.servicefall_id).where(Rapport.mandant_id == m.id, Servicefall.kunde_id == kunde.id)
    if anlage_id:
        abfrage = abfrage.where(Servicefall.anlage_id == anlage_id)
    ergebnis = []
    for r in s.scalars(abfrage.order_by(Rapport.erstellt.desc())):
        if r.ergebnis == "abgebrochen":
            continue
        kat = kategorie(r.servicefall.anliegen)
        jahre, grundlage = gewaehrleistungsjahre(m, kat)
        beginn = r.erstellt.date()
        ende = _plus_jahre(beginn, jahre)
        if ende < heute:
            continue
        ergebnis.append({"rapport_id": r.id, "nummer": r.nummer, "servicefall_id": r.servicefall_id, "anlage_id": r.servicefall.anlage_id,
                         "datum": beginn.isoformat(), "kategorie": kat, "taetigkeiten": (r.taetigkeiten or "")[:160], "material": [x.get("bezeichnung") for x in (r.material or [])][:5],
                         "jahre": jahre, "grundlage": grundlage, "ende": ende.isoformat(), "resttage": (ende - heute).days})
    return ergebnis


def pruefen(s: Session, m: Mandant, f: Servicefall, heute: date) -> dict[str, Any]:
    hinweise: list[str] = []
    garantie = None
    if f.anlage_id:
        a = s.get(Anlage, f.anlage_id)
        if a:
            garantie = herstellergarantie(s, m, a, heute)
            if garantie and garantie["aktiv"]:
                hinweise.append(f"Herstellergarantie {a.hersteller or ''} läuft voraussichtlich bis {date.fromisoformat(garantie['ende']).strftime('%d.%m.%Y')} – Ersatzteile/Arbeit beim Hersteller geltend machen, Seriennummer und Kaufbeleg bereithalten.")
    leistungen = eigene_leistungen(s, m, f.kunde, heute, f.anlage_id) if f.anlage_id else eigene_leistungen(s, m, f.kunde, heute)
    kat = kategorie(f.anliegen, f.dringlichkeit)
    relevant = [x for x in leistungen if x["servicefall_id"] != f.id]
    if relevant and kat in ("stoerung", "notdienst", "sonstiges"):
        j = relevant[0]
        hinweise.append(f"Eigene Leistung Rapport {j['nummer']} vom {date.fromisoformat(j['datum']).strftime('%d.%m.%Y')} ({j['kategorie']}) steht bis {date.fromisoformat(j['ende']).strftime('%d.%m.%Y')} in der Gewährleistung ({j['grundlage']}). "
                        "Hängt die Störung damit zusammen, ist die Nachbesserung kostenfrei – vor der Rechnung prüfen und im Rapport vermerken.")
    return {"servicefall_id": f.id, "kategorie": kat, "herstellergarantie": garantie, "gewaehrleistung": relevant, "hinweise": hinweise,
            "moeglicher_gewaehrleistungsfall": bool(hinweise)}
