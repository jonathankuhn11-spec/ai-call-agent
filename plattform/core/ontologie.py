"""Ontologie-Anbindung: Anlagentypen als kontrolliertes Vokabular und Turtle-Export der Instanzdaten.

Die TBox liegt in ontologie/shk.ttl, die ABox erzeugt `exportieren()` aus der Datenbank. Beides
zusammen lässt sich in einen Triple Store (Apache Jena Fuseki, Profil „ontologie“ im
docker-compose) laden und per SPARQL abfragen, z. B. „alle Wärmepumpen mit überfälliger
Wartung im PLZ-Bereich 50…, deren Betreuer keinen Kälteschein hat“.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rdflib import RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Anlage, Kunde, Mandant, Objekt, Ressource, Servicefall, Termin

SHK = Namespace("https://w3id.org/shk-backoffice/ont#")
TBOX = Path(__file__).resolve().parent.parent / "ontologie" / "shk.ttl"


@dataclass(frozen=True)
class Anlagentyp:
    klasse: str
    bezeichnung: str
    qualifikation: str | None
    wartungsintervall_monate: int | None


ANLAGENTYPEN: dict[str, Anlagentyp] = {
    "gas_brennwert": Anlagentyp("GasBrennwertkessel", "Gas-Brennwertkessel", "gas", 12),
    "oel": Anlagentyp("Oelheizkessel", "Öl-Heizkessel", "oel", 12),
    "waermepumpe": Anlagentyp("Waermepumpe", "Wärmepumpe", "kaeltetechnik", 12),
    "solarthermie": Anlagentyp("Solarthermieanlage", "Solarthermieanlage", None, 24),
    "klima": Anlagentyp("Klimaanlage", "Klimaanlage", "kaeltetechnik", 12),
    "trinkwasser": Anlagentyp("Trinkwasseranlage", "Trinkwasseranlage", "trinkwasser", 12),
    "warmwasser": Anlagentyp("Warmwasserbereiter", "Warmwasserbereiter", None, 24),
    "sanitaer": Anlagentyp("Sanitaeranlage", "Sanitäranlage", None, None),
}


def anlagentyp(schluessel: str) -> Anlagentyp:
    if schluessel not in ANLAGENTYPEN:
        raise ValueError(f"Unbekannter Anlagentyp {schluessel!r}; erlaubt: {sorted(ANLAGENTYPEN)}")
    return ANLAGENTYPEN[schluessel]


def _iri(mandant_id: str, art: str, id_: str) -> URIRef:
    return URIRef(f"urn:shk:{mandant_id}:{art}:{id_}")


def exportieren(s: Session, mandant: Mandant, mit_tbox: bool = False) -> Graph:
    g = Graph()
    g.bind("shk", SHK)
    if mit_tbox:
        g.parse(TBOX)
    m = mandant.id
    betrieb = _iri(m, "betrieb", m)
    g.add((betrieb, RDF.type, SHK.Betrieb))
    g.add((betrieb, SHK.name, Literal(mandant.name)))

    for k in s.scalars(select(Kunde).where(Kunde.mandant_id == m)):
        ki = _iri(m, "kunde", k.id)
        g.add((ki, RDF.type, SHK.Kunde))
        g.add((betrieb, SHK.betreut, ki))
        g.add((ki, SHK.name, Literal(k.name)))
        if k.telefon:
            g.add((ki, SHK.telefon, Literal(k.telefon)))
    for o in s.scalars(select(Objekt).where(Objekt.mandant_id == m)):
        oi = _iri(m, "objekt", o.id)
        g.add((oi, RDF.type, SHK.Objekt))
        g.add((_iri(m, "kunde", o.kunde_id), SHK.besitztObjekt, oi))
        if o.ort:
            g.add((oi, SHK.ort, Literal(o.ort)))
    for a in s.scalars(select(Anlage).where(Anlage.mandant_id == m)):
        ai = _iri(m, "anlage", a.id)
        g.add((ai, RDF.type, SHK[anlagentyp(a.anlagentyp).klasse]))
        g.add((_iri(m, "objekt", a.objekt_id), SHK.hatAnlage, ai))
        if a.hersteller:
            g.add((ai, SHK.hersteller, Literal(a.hersteller)))
        if a.baujahr:
            g.add((ai, SHK.baujahr, Literal(str(a.baujahr), datatype=XSD.gYear)))
        if a.letzte_wartung:
            g.add((ai, SHK.letzteWartung, Literal(a.letzte_wartung, datatype=XSD.date)))
    for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m)):
        ri = _iri(m, "ressource", r.id)
        g.add((ri, RDF.type, SHK.Ressource))
        g.add((ri, RDFS.label, Literal(r.name)))
        for q in r.qualifikationen or []:
            g.add((ri, SHK.hatQualifikation, SHK[f"Q_{q}"]))
    for f in s.scalars(select(Servicefall).where(Servicefall.mandant_id == m)):
        fi = _iri(m, "servicefall", f.id)
        g.add((fi, RDF.type, SHK.Servicefall))
        g.add((fi, SHK.betrifftKunde, _iri(m, "kunde", f.kunde_id)))
        g.add((fi, SHK.hatStatus, SHK[f"S_{f.status}"]))
        g.add((fi, SHK.dringlichkeit, Literal(f.dringlichkeit)))
        g.add((fi, SHK.anliegen, Literal(f.anliegen)))
        if f.anlage_id:
            g.add((fi, SHK.betrifftAnlage, _iri(m, "anlage", f.anlage_id)))
        if f.benoetigte_qualifikation:
            g.add((fi, SHK.erfordertQualifikation, SHK[f"Q_{f.benoetigte_qualifikation}"]))
    for t in s.scalars(select(Termin).where(Termin.mandant_id == m)):
        ti = _iri(m, "termin", t.id)
        g.add((ti, RDF.type, SHK.Termin))
        g.add((_iri(m, "servicefall", t.servicefall_id), SHK.hatTermin, ti))
        g.add((ti, SHK.durchgefuehrtVon, _iri(m, "ressource", t.ressource_id)))
        g.add((ti, SHK.beginn, Literal(t.beginn, datatype=XSD.dateTime)))
        g.add((ti, SHK.ende, Literal(t.ende, datatype=XSD.dateTime)))
    return g
