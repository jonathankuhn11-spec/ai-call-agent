"""Lebenszyklus je Anlage: Bauteile mit Nutzungsdauer und wiederkehrende Prüfpflichten – „wann muss was erneuert
oder geprüft werden“ – als Erneuerungskalender je Kunde mit Kostenschätzung und Rücklage.

Katalog. Je Anlagentyp Verschleiß- und Hauptbauteile mit rechnerischer Nutzungsdauer (Orientierung VDI 2067 Blatt 1,
Herstellerangaben) und Kostenschätzung (brutto, Erfahrungswerte, in den Stammdaten überschreibbar) sowie Prüfpflichten
mit Intervall und Rechtsgrundlage (z. B. Gasleitungs-Dichtheitsprüfung nach TRGI 2018 alle 12 Jahre, Legionellen-
untersuchung nach TrinkwV § 31 alle 3 Jahre bei Großanlagen in vermieteten Gebäuden, Öltankprüfung nach AwSV,
Dichtheitsprüfung von Kältekreisen nach F-Gas-Verordnung (EU) 2024/573 je nach CO₂-Äquivalent, Haupt- und
Abgasuntersuchung nach § 29 StVZO). Die Rechtsgrundlagen sind Hinweise für den Betrieb, keine Rechtsberatung.

Rechnung. Fälligkeit eines Bauteils = letzte Erneuerung (sonst Einbau, sonst Baujahr) + Nutzungsdauer; einer Prüfung
= letzte Prüfung + Intervall (ohne Datum: sofort fällig). Rücklage je Jahr = Σ Kosten / Nutzungsdauer über alle
Bauteile (lineare Abschreibung). Der Kalender zeigt je Jahr, was ansteht und was es kostet; „fällig“ liefert alles,
was innerhalb des Vorlaufs ansteht, und der Tageslauf erinnert den Kunden (Freigabe B, nur mit Einwilligung) und
legt auf Wunsch einen Vorgang an.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene
from core.modelle import Anlage, Bauteil, Ereignis, Kunde, Mandant, Objekt


@dataclass(frozen=True)
class Eintrag:
    art: str
    bezeichnung: str
    pflicht: bool
    nutzungsdauer_jahre: int | None
    intervall_monate: int | None
    kosten: str | None
    rechtsgrundlage: str | None = None


def _b(art, bez, jahre, kosten): return Eintrag(art, bez, False, jahre, None, kosten)
def _p(art, bez, monate, kosten, grund): return Eintrag(art, bez, True, None, monate, kosten, grund)


KATALOG: dict[str, list[Eintrag]] = {
    "gas_brennwert": [_b("waermeerzeuger", "Gas-Brennwertgerät (Austausch)", 18, "7500.00"), _b("umwaelzpumpe", "Umwälzpumpe", 10, "450.00"),
                      _b("ausdehnungsgefaess", "Ausdehnungsgefäß", 12, "350.00"), _b("regelung", "Regelung/Steuerung", 15, "900.00"),
                      _b("elektroden", "Zünd-/Ionisationselektroden", 5, "180.00"), _b("abgasanlage", "Abgasleitung", 20, "1800.00"),
                      _p("abgaswegepruefung", "Abgaswegeüberprüfung (Schornsteinfeger)", 24, None, "KÜO Anlage 1 (Brennwert: alle 2 Jahre)"),
                      _p("gas_dichtheit", "Gasleitung Dichtheits-/Gebrauchsfähigkeitsprüfung", 144, "250.00", "TRGI 2018 (DVGW G 600) alle 12 Jahre"),
                      _p("hydraulischer_abgleich", "Hydraulischer Abgleich prüfen", 120, "600.00", "GEG § 60c (Mehrfamilienhäuser mit Gaszentralheizung)")],
    "oel": [_b("waermeerzeuger", "Öl-Brennwertkessel (Austausch)", 20, "9500.00"), _b("brenner", "Ölbrenner/Düse/Filter", 8, "900.00"), _b("umwaelzpumpe", "Umwälzpumpe", 10, "450.00"),
            _b("oeltank", "Öltank", 30, "6000.00"), _b("ausdehnungsgefaess", "Ausdehnungsgefäß", 12, "350.00"),
            _p("tankpruefung", "Öltankprüfung durch Sachverständigen", 60, "300.00", "AwSV § 46 (über 1 000 l unterirdisch bzw. nach Landesrecht)"),
            _p("abgaswegepruefung", "Abgaswegeüberprüfung (Schornsteinfeger)", 12, None, "KÜO Anlage 1")],
    "waermepumpe": [_b("verdichter", "Verdichter", 15, "2800.00"), _b("ventilator", "Ventilator Außeneinheit", 12, "600.00"), _b("umwaelzpumpe", "Umwälzpumpe", 10, "450.00"),
                    _b("regelung", "Regelung/Platine", 15, "900.00"), _b("pufferspeicher", "Pufferspeicher", 25, "2500.00"),
                    _p("kaeltekreis_dichtheit", "Dichtheitsprüfung Kältekreis", 12, "180.00", "F-Gas-VO (EU) 2024/573 Art. 5 (ab 5 t CO₂-Äq., hermetisch ab 10 t; sonst Herstellerintervall)")],
    "solarthermie": [_b("kollektoren", "Kollektoren", 25, "6000.00"), _b("solarfluessigkeit", "Solarflüssigkeit", 5, "250.00"), _b("solarpumpe", "Solarpumpe/Station", 12, "700.00"),
                     _b("ausdehnungsgefaess", "Ausdehnungsgefäß Solar", 10, "300.00"), _p("frostschutz", "Frostschutz und pH-Wert prüfen", 24, "80.00", "Herstellerangabe")],
    "klima": [_b("splitgeraet", "Klimagerät (Austausch)", 13, "3500.00"), _b("kondensatpumpe", "Kondensatpumpe", 6, "250.00"), _b("filter", "Filter", 1, "40.00"),
              _p("kaeltekreis_dichtheit", "Dichtheitsprüfung Kältekreis", 12, "150.00", "F-Gas-VO (EU) 2024/573 Art. 5 (abhängig vom CO₂-Äquivalent)"),
              _p("hygiene", "Hygieneinspektion", 24, "200.00", "VDI 6022 (RLT-Anlagen)")],
    "trinkwasser": [_b("filter", "Trinkwasserfilter (Filterkerze)", 1, "60.00"), _b("druckminderer", "Druckminderer", 15, "350.00"), _b("sicherungsarmatur", "Rückflussverhinderer/Sicherungsarmatur", 10, "250.00"),
                    _p("legionellen", "Legionellenuntersuchung", 36, "250.00", "TrinkwV 2023 § 31 (Großanlagen in vermieteten Gebäuden, alle 3 Jahre)"),
                    _p("filter_rueckspuelung", "Filter rückspülen", 2, None, "DIN 1988-200 / DIN EN 806-5 (alle 2 Monate)")],
    "warmwasser": [_b("speicher", "Warmwasserspeicher", 20, "2200.00"), _b("opferanode", "Opferanode", 2, "180.00"), _b("heizflansch", "Heizflansch/Elektroheizstab", 10, "350.00"),
                   _b("sicherheitsventil", "Sicherheitsventil", 8, "90.00"), _p("legionellen_schaltung", "Legionellenschaltung prüfen", 12, None, "DVGW W 551")],
    "sanitaer": [_b("armaturen", "Armaturen", 15, "600.00"), _b("spuelkasten", "Spülkasten-Technik", 12, "150.00"), _b("silikonfugen", "Silikonfugen", 8, "250.00")],
    "fahrzeug": [_b("reifen", "Reifen (Alter)", 6, "500.00"), _b("bremsfluessigkeit", "Bremsflüssigkeit", 2, "90.00"), _b("batterie", "Starterbatterie", 5, "200.00"),
                 _p("hu", "Hauptuntersuchung", 24, "140.00", "§ 29 StVZO"), _p("oelwechsel", "Ölwechsel/Inspektion", 12, "250.00", "Herstellerintervall")],
    "fahrrad": [_b("kette", "Kette", 1, "40.00"), _b("bremsbelaege", "Bremsbeläge", 1, "40.00"), _b("akku", "E-Bike-Akku", 5, "600.00"), _p("inspektion", "Inspektion", 12, "80.00", None)],
}
STANDARD_ANLAGE = [_b("hauptkomponente", "Hauptkomponente", 15, None)]


def katalog(anlagentyp: str) -> list[Eintrag]:
    return KATALOG.get(anlagentyp, STANDARD_ANLAGE)


def bauteile_anlegen(s: Session, m: Mandant, a: Anlage, akteur_name: str | None = None) -> list[Bauteil]:
    """Katalog-Bauteile an die Anlage hängen (idempotent nach `art`); Einbau = Baujahr, letzte Prüfung = letzte Wartung."""
    vorhanden = {b.art for b in s.scalars(select(Bauteil).where(Bauteil.anlage_id == a.id))}
    neu = []
    einbau = date(a.baujahr, 1, 1) if a.baujahr else None
    for e in katalog(a.anlagentyp):
        if e.art in vorhanden:
            continue
        b = Bauteil(mandant_id=m.id, anlage_id=a.id, art=e.art, bezeichnung=e.bezeichnung, pflicht=e.pflicht, nutzungsdauer_jahre=e.nutzungsdauer_jahre,
                    intervall_monate=e.intervall_monate, einbau=einbau, letzte_erneuerung=(a.letzte_wartung if e.pflicht else None),
                    kosten_schaetzung=_kosten(m, e), rechtsgrundlage=e.rechtsgrundlage)
        s.add(b)
        neu.append(b)
    s.flush()
    if neu:
        domaene.protokollieren(s, m.id, "bauteile.angelegt", "anlage", a.id, "mensch" if akteur_name else "system", {"anzahl": len(neu)}, akteur_name)
    return neu


def _kosten(m: Mandant, e: Eintrag) -> str | None:
    eigene = ((m.stammdaten or {}).get("bauteilkosten") or {}).get(e.art)
    return str(eigene) if eigene else e.kosten


def faelligkeit(b: Bauteil) -> date | None:
    if b.pflicht:
        if not b.intervall_monate:
            return None
        if b.letzte_erneuerung is None:
            return date.min
        return _plus_monate(b.letzte_erneuerung, b.intervall_monate)
    if not b.nutzungsdauer_jahre:
        return None
    basis = b.letzte_erneuerung or b.einbau
    if basis is None:
        return None
    return date(basis.year + b.nutzungsdauer_jahre, basis.month, min(basis.day, 28))


def _plus_monate(d: date, monate: int) -> date:
    monat = d.month - 1 + monate
    return date(d.year + monat // 12, monat % 12 + 1, min(d.day, 28))


def status(b: Bauteil, heute: date) -> str:
    f = faelligkeit(b)
    if f is None:
        return "ohne_termin"
    if f <= heute:
        return "faellig"
    if f <= heute + timedelta(days=365):
        return "bald"
    return "aktuell"


def _eintrag(b: Bauteil, heute: date) -> dict[str, Any]:
    f = faelligkeit(b)
    return {"id": b.id, "anlage_id": b.anlage_id, "art": b.art, "bezeichnung": b.bezeichnung, "pflicht": b.pflicht, "nutzungsdauer_jahre": b.nutzungsdauer_jahre,
            "intervall_monate": b.intervall_monate, "einbau": b.einbau.isoformat() if b.einbau else None,
            "letzte_erneuerung": b.letzte_erneuerung.isoformat() if b.letzte_erneuerung else None,
            "faellig": None if f in (None, date.min) else f.isoformat(), "status": status(b, heute), "kosten_schaetzung": b.kosten_schaetzung,
            "rechtsgrundlage": b.rechtsgrundlage, "notiz": b.notiz,
            "restnutzung_jahre": None if f in (None, date.min) or b.pflicht else round((f - heute).days / 365.25, 1)}


def anlage_uebersicht(s: Session, m: Mandant, a: Anlage, heute: date) -> dict[str, Any]:
    bauteile = s.scalars(select(Bauteil).where(Bauteil.anlage_id == a.id, Bauteil.status == "aktiv").order_by(Bauteil.pflicht, Bauteil.bezeichnung)).all()
    eintraege = [_eintrag(b, heute) for b in bauteile]
    ruecklage = sum((Decimal(b.kosten_schaetzung) / b.nutzungsdauer_jahre for b in bauteile if not b.pflicht and b.kosten_schaetzung and b.nutzungsdauer_jahre), Decimal(0))
    from core.ontologie import ANLAGENTYPEN
    typ = ANLAGENTYPEN.get(a.anlagentyp)
    return {"anlage_id": a.id, "anlagentyp": a.anlagentyp, "anlage": {"bezeichnung": getattr(typ, "bezeichnung", a.anlagentyp), "hersteller": a.hersteller, "modell": a.modell, "baujahr": a.baujahr},
            "bauteile": eintraege, "faellig": [e for e in eintraege if e["status"] == "faellig"],
            "ruecklage_jahr": f"{ruecklage:.2f}", "ruecklage_monat": f"{ruecklage / 12:.2f}"}


def kalender(s: Session, m: Mandant, kunde: Kunde, heute: date, jahre: int = 10) -> dict[str, Any]:
    """Erneuerungskalender je Kunde über alle Anlagen: je Jahr die fälligen Erneuerungen/Prüfungen mit Kosten; Rücklage."""
    anlagen = s.scalars(select(Anlage).join(Objekt, Anlage.objekt_id == Objekt.id).where(Objekt.kunde_id == kunde.id, Anlage.mandant_id == m.id)).all()
    jahre_aus: dict[int, list[dict[str, Any]]] = {heute.year + i: [] for i in range(jahre)}
    ueberfaellig, ruecklage, gesamt = [], Decimal(0), Decimal(0)
    for a in anlagen:
        for b in s.scalars(select(Bauteil).where(Bauteil.anlage_id == a.id, Bauteil.status == "aktiv")):
            e = {**_eintrag(b, heute), "anlage": a.anlagentyp, "objekt": a.objekt.bezeichnung}
            if not b.pflicht and b.kosten_schaetzung and b.nutzungsdauer_jahre:
                ruecklage += Decimal(b.kosten_schaetzung) / b.nutzungsdauer_jahre
            f = faelligkeit(b)
            if f is None:
                continue
            if f <= heute:
                ueberfaellig.append(e)
                f = heute
            # Prüfungen wiederholen sich im Kalenderhorizont
            while f.year <= heute.year + jahre - 1:
                if f.year in jahre_aus:
                    jahre_aus[f.year].append({**e, "faellig": f.isoformat()})
                    if b.kosten_schaetzung:
                        gesamt += Decimal(b.kosten_schaetzung)
                if b.pflicht and b.intervall_monate:
                    f = _plus_monate(f, b.intervall_monate)
                elif b.nutzungsdauer_jahre:
                    f = date(f.year + b.nutzungsdauer_jahre, f.month, f.day)
                else:
                    break
    return {"kunde_id": kunde.id, "kunde": kunde.name, "anlagen": len(anlagen), "ruecklage_jahr": f"{ruecklage:.2f}", "ruecklage_monat": f"{ruecklage / 12:.2f}",
            "kosten_horizont": f"{gesamt:.2f}", "ueberfaellig": ueberfaellig,
            "jahre": [{"jahr": j, "eintraege": sorted(v, key=lambda x: x["faellig"]), "kosten": f"{sum((Decimal(x['kosten_schaetzung']) for x in v if x['kosten_schaetzung']), Decimal(0)):.2f}"}
                      for j, v in jahre_aus.items()]}


def erneuern(s: Session, m: Mandant, b: Bauteil, am: date, kosten: str | None = None, notiz: str | None = None, akteur_name: str | None = None) -> Bauteil:
    b.letzte_erneuerung = am
    if kosten:
        b.kosten_schaetzung = kosten
    if notiz:
        b.notiz = notiz
    domaene.protokollieren(s, m.id, "bauteil.geprueft" if b.pflicht else "bauteil.erneuert", "anlage", b.anlage_id, "mensch", {"bauteil_id": b.id, "art": b.art, "am": am.isoformat()}, akteur_name)
    return b


def faellig(s: Session, m: Mandant, heute: date, vorlauf_tage: int = 90) -> list[dict[str, Any]]:
    grenze = heute + timedelta(days=vorlauf_tage)
    ergebnis = []
    for b in s.scalars(select(Bauteil).where(Bauteil.mandant_id == m.id, Bauteil.status == "aktiv")):
        f = faelligkeit(b)
        if f is not None and f <= grenze:
            a = b.anlage
            ergebnis.append({**_eintrag(b, heute), "anlagentyp": a.anlagentyp, "kunde_id": a.objekt.kunde_id, "kunde": a.objekt.kunde.name, "ort": a.objekt.ort})
    ergebnis.sort(key=lambda e: (e["faellig"] or "", e["kunde"]))
    return ergebnis


def erinnerungen(s: Session, m: Mandant, heute: date, vorlauf_tage: int = 60) -> int:
    """Tageslauf: Kunden mit fälligen Erneuerungen/Prüfungen einmal je Bauteil und Fälligkeit erinnern (Freigabe B, nur mit Einwilligung)."""
    n = 0
    je_kunde: dict[str, list[dict[str, Any]]] = {}
    for e in faellig(s, m, heute, vorlauf_tage):
        je_kunde.setdefault(e["kunde_id"], []).append(e)
    for kunde_id, eintraege in je_kunde.items():
        k = s.get(Kunde, kunde_id)
        ew = k.einwilligungen or {}
        if not (ew.get("email") or ew.get("sms")) or not (k.email or k.telefon):
            continue
        schluessel = ",".join(sorted(f"{e['id']}:{e['faellig']}" for e in eintraege))
        schon = s.scalar(select(Ereignis).where(Ereignis.mandant_id == m.id, Ereignis.typ == "lebenszyklus.erinnert", Ereignis.entitaet_id == kunde_id,
                                                Ereignis.payload["schluessel"].as_string() == schluessel))
        if schon is not None:
            continue
        zeilen = "\n".join(f"- {e['bezeichnung']} ({'Prüfung' if e['pflicht'] else 'Erneuerung'}{', fällig ' + e['faellig'] if e['faellig'] else ', überfällig'}"
                           + (f", ca. {e['kosten_schaetzung'].replace('.', ',')} €" if e['kosten_schaetzung'] else "") + ")" for e in eintraege)
        text = (f"Guten Tag {k.name},\n\nfür Ihre Haustechnik stehen demnächst an:\n{zeilen}\n\nGern planen wir das mit Ihnen; antworten Sie kurz oder rufen Sie uns an: "
                f"{(m.stammdaten or {}).get('telefon', '')}\n\nMit freundlichen Grüßen\n{m.name}")
        domaene.aktion_beantragen(s, m, "nachricht_an_endkunde", {"kunde_id": k.id, "betreff": "Was bei Ihrer Haustechnik ansteht", "text": text,
                                                                   "kurz": f"{m.name}: {len(eintraege)} Erneuerung(en)/Prüfung(en) stehen an. Wir melden uns."},
                                  "system", None, "Erneuerungskalender")
        domaene.protokollieren(s, m.id, "lebenszyklus.erinnert", "kunde", k.id, "system", {"schluessel": schluessel, "anzahl": len(eintraege)})
        n += 1
    return n
