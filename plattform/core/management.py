"""Management-Funktionen: Wartungsplanung, Kennzahlen, Plantafel und SPARQL über den Zwilling.

Wartungsplanung macht aus dem Digital Twin Umsatz: Fällige Anlagen werden zu Servicefällen
(Klasse A, intern), die Kundenansprache dazu läuft als Klasse B über die Freigabe-Inbox.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from rdflib import Graph
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import domaene
from core.disposition import ZEITZONE
from core.domaene import als_utc
from core.modelle import Abwesenheit, Anfrage, Beleg, Rapport, Rechnung, Zahlung, Zeiteintrag, Anlage, Ereignis, Freigabe, Kommunikation, Mandant, OutboxEintrag, Ressource, Servicefall, Termin
from core.ontologie import ANLAGENTYPEN, exportieren
from core.twin import OFFENE_STATUS, naechste_wartung

WARTUNG_PRAEFIX = "Wartung"


def wartung_planen(s: Session, mandant: Mandant, heute: date, vorlauf_tage: int = 60,
                   kunden_anschreiben: bool = True) -> list[dict[str, Any]]:
    """Legt für jede bis `heute + vorlauf_tage` fällige Anlage ohne offenen Wartungsfall einen an."""
    grenze = heute + timedelta(days=vorlauf_tage)
    offen = set(s.scalars(select(Servicefall.anlage_id).where(
        Servicefall.mandant_id == mandant.id, Servicefall.status.in_(OFFENE_STATUS),
        Servicefall.anliegen.startswith(WARTUNG_PRAEFIX))))
    neu = []
    for a in s.scalars(select(Anlage).where(Anlage.mandant_id == mandant.id)):
        faellig = naechste_wartung(a)
        if faellig is None or faellig > grenze or a.id in offen:
            continue
        typ = ANLAGENTYPEN[a.anlagentyp]
        kunde = a.objekt.kunde
        f = Servicefall(mandant_id=mandant.id, kunde_id=kunde.id, anlage_id=a.id,
                        anliegen=f"{WARTUNG_PRAEFIX} {typ.bezeichnung}" + (f" {a.hersteller}" if a.hersteller else ""),
                        dringlichkeit="niedrig", quelle="manuell", benoetigte_qualifikation=typ.qualifikation,
                        dauer_minuten=60)
        s.add(f)
        s.flush()
        domaene.protokollieren(s, mandant.id, "servicefall.eingegangen", "servicefall", f.id, "system",
                               {"quelle": "wartungsplanung", "faellig": None if faellig == date.min else faellig.isoformat()})
        if kunden_anschreiben:
            domaene.aktion_beantragen(s, mandant, "nachricht_an_endkunde",
                                      {"kunde_id": kunde.id, "vorlage": "wartungserinnerung", "servicefall_id": f.id},
                                      "system", f.id, f"Wartungserinnerung an {kunde.name}")
        neu.append({"servicefall_id": f.id, "kunde": kunde.name, "anlage": typ.bezeichnung,
                    "faellig": "nie gewartet" if faellig == date.min else faellig.isoformat()})
    return neu


def kennzahlen(s: Session, mandant: Mandant, jetzt_: datetime, tage: int = 30) -> dict[str, Any]:
    m = mandant.id
    ab = als_utc(jetzt_) - timedelta(days=tage)
    quellen = dict(s.execute(select(Servicefall.quelle, func.count()).where(
        Servicefall.mandant_id == m, Servicefall.erstellt >= ab).group_by(Servicefall.quelle)).all())
    anrufe = s.scalar(select(func.count()).select_from(Kommunikation).where(
        Kommunikation.mandant_id == m, Kommunikation.kanal == "telefon", Kommunikation.zeit >= ab))
    entscheidungen = s.execute(select(Freigabe.entschieden_von, func.count()).where(
        Freigabe.mandant_id == m, Freigabe.status != "offen", Freigabe.erstellt >= ab)
        .group_by(Freigabe.entschieden_von)).all()
    automatisch = sum(n for wer, n in entscheidungen if wer == "policy")
    gesamt = sum(n for _, n in entscheidungen)

    # Reaktionszeit: Eingang bis Einplanung, aus dem Event-Log
    eingang: dict[str, datetime] = {}
    dauern: list[float] = []
    for e in s.scalars(select(Ereignis).where(Ereignis.mandant_id == m, Ereignis.zeit >= ab,
                                              Ereignis.typ.in_(("servicefall.eingegangen", "servicefall.eingeplant")))
                       .order_by(Ereignis.id)):
        if e.typ == "servicefall.eingegangen":
            eingang[e.entitaet_id] = als_utc(e.zeit)
        elif e.entitaet_id in eingang:
            dauern.append((als_utc(e.zeit) - eingang[e.entitaet_id]).total_seconds() / 3600)
    return {
        "zeitraum_tage": tage,
        "neue_vorgaenge_je_quelle": quellen,
        "anrufe_erfasst": anrufe,
        "offene_vorgaenge": s.scalar(select(func.count()).select_from(Servicefall).where(
            Servicefall.mandant_id == m, Servicefall.status.in_(OFFENE_STATUS))),
        "offene_freigaben": s.scalar(select(func.count()).select_from(Freigabe).where(
            Freigabe.mandant_id == m, Freigabe.status == "offen")),
        "automatisierungsquote": round(automatisch / gesamt, 2) if gesamt else None,
        "median_stunden_bis_einplanung": round(median(dauern), 1) if dauern else None,
        "outbox_ausstehend": s.scalar(select(func.count()).select_from(OutboxEintrag).where(
            OutboxEintrag.mandant_id == m, OutboxEintrag.status == "ausstehend")),
        "outbox_tot": s.scalar(select(func.count()).select_from(OutboxEintrag).where(
            OutboxEintrag.mandant_id == m, OutboxEintrag.status == "tot")),
    }


def plantafel(s: Session, mandant: Mandant, tag: date, tage: int = 5, nur_ressourcen: list[str] | None = None) -> dict[str, Any]:
    beginn = datetime.combine(tag, datetime.min.time(), ZEITZONE)
    ende = beginn + timedelta(days=tage)
    zeilen = []
    abfrage = select(Ressource).where(Ressource.mandant_id == mandant.id, Ressource.aktiv).order_by(Ressource.name)
    if nur_ressourcen is not None:
        abfrage = abfrage.where(Ressource.id.in_(nur_ressourcen))
    for r in s.scalars(abfrage):
        termine = s.scalars(select(Termin).where(Termin.ressource_id == r.id, Termin.status != "abgesagt",
                                                 Termin.beginn >= als_utc(beginn), Termin.beginn < als_utc(ende))
                            .order_by(Termin.beginn)).all()
        abwesend = s.scalars(select(Abwesenheit).where(Abwesenheit.ressource_id == r.id, Abwesenheit.bis >= beginn.date(),
                                                       Abwesenheit.von < ende.date())).all()
        zeilen.append({
            "abwesenheiten": [{"id": a.id, "von": a.von.isoformat(), "bis": a.bis.isoformat(), "art": a.art} for a in abwesend],
            "ressource_id": r.id, "name": r.name, "qualifikationen": r.qualifikationen,
            "arbeitsbeginn": r.arbeitsbeginn.strftime("%H:%M"), "arbeitsende": r.arbeitsende.strftime("%H:%M"),
            "termine": [{"id": t.id, "beginn": als_utc(t.beginn).astimezone(ZEITZONE).isoformat(),
                         "ende": als_utc(t.ende).astimezone(ZEITZONE).isoformat(), "status": t.status,
                         "servicefall_id": t.servicefall_id, "anliegen": t.servicefall.anliegen,
                         "dringlichkeit": t.servicefall.dringlichkeit, "kunde": t.servicefall.kunde.name,
                         "ort": t.servicefall.kunde.ort} for t in termine],
        })
    return {"von": beginn.date().isoformat(), "tage": tage, "ressourcen": zeilen}


def sparql(s: Session, mandant: Mandant, abfrage: str) -> dict[str, Any]:
    """Nur SELECT/ASK über TBox und Instanzdaten des Mandanten; der Graph ist je Anfrage isoliert."""
    kopf = abfrage.strip().upper()
    if not any(w in kopf for w in ("SELECT", "ASK")) or any(w in kopf for w in ("INSERT", "DELETE", "LOAD", "CLEAR", "SERVICE")):
        raise ValueError("Nur SELECT- und ASK-Abfragen sind erlaubt")
    g: Graph = exportieren(s, mandant, mit_tbox=True)
    ergebnis = g.query(abfrage)
    if ergebnis.type == "ASK":
        return {"typ": "ask", "ergebnis": bool(ergebnis.askAnswer)}
    variablen = [str(v) for v in ergebnis.vars]
    return {"typ": "select", "variablen": variablen,
            "zeilen": [[None if w is None else str(w) for w in zeile] for zeile in ergebnis][:500]}



def auswertungen(s: Session, mandant: Mandant, jetzt_: datetime, monate: int = 12) -> dict[str, Any]:
    """Betriebswirtschaftliche Sicht: Umsatz und Zahlungseingang je Monat, offene Posten, Angebotsquote, Deckungsbeitrag
    je Monteur (aus Rapport-Zeiten zu internen Kosten und zugeordneten Belegen), Anfragen je Kanal, Vertragsbestand."""
    from decimal import Decimal

    from core import angebote, forderungen, vertraege
    from core.einsatz import STANDARD_SAETZE

    m = mandant.id
    heute = als_utc(jetzt_).date()
    ab = date(heute.year, heute.month, 1)
    for _ in range(monate - 1):
        ab = (ab.replace(day=1) - timedelta(days=1)).replace(day=1)
    monatsreihe: dict[str, dict[str, Decimal]] = {}
    tag = ab
    while tag <= heute:
        monatsreihe[tag.strftime("%Y-%m")] = {"umsatz_netto": Decimal("0"), "zahlungen": Decimal("0"), "material_netto": Decimal("0")}
        tag = (tag.replace(day=28) + timedelta(days=4)).replace(day=1)
    for r in s.scalars(select(Rechnung).where(Rechnung.mandant_id == m, Rechnung.datum >= ab, Rechnung.status != "storniert")):
        schl = r.datum.strftime("%Y-%m")
        if schl in monatsreihe:
            monatsreihe[schl]["umsatz_netto"] += Decimal(r.netto)
    for z in s.scalars(select(Zahlung).where(Zahlung.mandant_id == m, Zahlung.datum >= ab)):
        schl = z.datum.strftime("%Y-%m")
        if schl in monatsreihe:
            monatsreihe[schl]["zahlungen"] += Decimal(z.betrag)
    for b in s.scalars(select(Beleg).where(Beleg.mandant_id == m, Beleg.datum >= ab)):
        schl = b.datum.strftime("%Y-%m")
        if schl in monatsreihe:
            monatsreihe[schl]["material_netto"] += Decimal(b.netto)
    posten = forderungen.offene_posten(s, mandant, heute)
    satz_kosten = Decimal(str(mandant.stammdaten.get("stundensatz_kosten", STANDARD_SAETZE["stundensatz_kosten"])))
    je_monteur: dict[str, dict[str, Any]] = {}
    ab_dt = datetime.combine(ab, datetime.min.time(), ZEITZONE)
    for rp in s.scalars(select(Rapport).where(Rapport.mandant_id == m, Rapport.erstellt >= als_utc(ab_dt))):
        e = je_monteur.setdefault(rp.ressource_id, {"name": rp.ressource.name.split(" (")[0], "rapporte": 0, "stunden": Decimal("0"),
                                                    "umsatz_netto": Decimal("0"), "material_netto": Decimal("0")})
        e["rapporte"] += 1
        e["stunden"] += Decimal(rp.arbeitszeit_minuten + rp.fahrzeit_minuten) / 60
        e["umsatz_netto"] += sum((Decimal(r.netto) for r in s.scalars(select(Rechnung).where(Rechnung.servicefall_id == rp.servicefall_id,
                                                                                          Rechnung.status != "storniert"))), Decimal("0"))
        e["material_netto"] += sum((Decimal(b.netto) for b in s.scalars(select(Beleg).where(Beleg.servicefall_id == rp.servicefall_id))), Decimal("0"))
    monteure = []
    for rid, e in je_monteur.items():
        lohn = (e["stunden"] * satz_kosten).quantize(Decimal("0.01"))
        db = (e["umsatz_netto"] - e["material_netto"] - lohn).quantize(Decimal("0.01"))
        monteure.append({"ressource_id": rid, "name": e["name"], "rapporte": e["rapporte"], "stunden": f"{e['stunden']:.1f}",
                         "umsatz_netto": f"{e['umsatz_netto']:.2f}", "material_netto": f"{e['material_netto']:.2f}", "lohnkosten": str(lohn),
                         "deckungsbeitrag": str(db)})
    anfragen = dict(s.execute(select(Anfrage.kanal, func.count()).where(Anfrage.mandant_id == m, Anfrage.eingegangen >= als_utc(ab_dt))
                              .group_by(Anfrage.kanal)).all())
    stunden_gesamt = s.scalar(select(func.count()).select_from(Zeiteintrag).where(Zeiteintrag.mandant_id == m, Zeiteintrag.beginn >= als_utc(ab_dt)))
    return {
        "von": ab.isoformat(), "bis": heute.isoformat(),
        "monate": [{"monat": k, **{f: str(v.quantize(Decimal("0.01"))) for f, v in w.items()}} for k, w in monatsreihe.items()],
        "umsatz_netto_gesamt": str(sum((w["umsatz_netto"] for w in monatsreihe.values()), Decimal("0")).quantize(Decimal("0.01"))),
        "offene_posten": {"anzahl": len(posten), "summe": str(sum((Decimal(p["offen"]) for p in posten), Decimal("0"))),
                          "ueberfaellig": str(sum((Decimal(p["offen"]) for p in posten if p["tage_ueberfaellig"] > 0), Decimal("0"))),
                          "gemahnt": sum(1 for p in posten if p["mahnstufe"] > 0)},
        "angebote": angebote.quote(s, mandant, ab),
        "vertraege": vertraege.bestand(s, mandant),
        "monteure": sorted(monteure, key=lambda x: x["name"]),
        "anfragen_je_kanal": anfragen,
        "zeiteintraege": stunden_gesamt,
    }
