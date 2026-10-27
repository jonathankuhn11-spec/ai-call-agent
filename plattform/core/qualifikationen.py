"""Qualifikationsmatrix: Wer darf was, und wann läuft es ab?

Befähigungen sind in `ressource.qualifikationen` hinterlegt; `ressource.qualifikationen_ablauf` trägt je Schlüssel
das Ablauf- bzw. Nachschulungsdatum. Der Katalog nennt übliche Nachschulungs- und Unterweisungsintervalle als
Orientierung (DGUV, DVGW, TRGS, ChemKlimaschutzV); maßgeblich sind die jeweils geltenden Vorschriften und
Herstellervorgaben. Eine abgelaufene Qualifikation zählt in Disposition, Warteliste und Optimierer nicht mehr
(`gueltig()`); die Matrix zeigt gültig / läuft ab (≤ 90 Tage) / abgelaufen / fehlt, und der Tageslauf meldet
Ablaufende an den Inhaber.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Mandant, Ressource

# schluessel: (Bezeichnung, Nachschulung in Monaten oder None = unbefristet, Grundlage)
QUALIFIKATIONEN: dict[str, tuple[str, int | None, str]] = {
    "gas": ("Gasinstallation (Eintragung Installateurverzeichnis / TRGI-Schulung)", 60, "DVGW G 600 (TRGI), Netzbetreiber-Verzeichnis; Fortbildung alle 5 Jahre üblich"),
    "trinkwasser": ("Trinkwasserinstallation / Hygiene", 60, "VDI/DVGW 6023 Kat. A; Auffrischung alle 5 Jahre"),
    "elektro": ("Elektrofachkraft für festgelegte Tätigkeiten", 12, "DGUV Vorschrift 3, jährliche Unterweisung"),
    "kaeltetechnik": ("Sachkunde Kältemittel Kategorie I (Kälteschein)", None, "ChemKlimaschutzV § 5, Zertifikat unbefristet; Unterweisung zu F-Gas-VO 2024/573 empfohlen"),
    "oel": ("Ölfeuerung / Fachbetrieb nach WHG", 24, "AwSV § 62 Fachbetrieb, Nachweis alle 2 Jahre"),
    "solar": ("Solarthermie / PV-Montage", None, "Herstellerschulung"),
    "waermepumpe": ("Wärmepumpen-Fachhandwerker", 36, "BWP-/Hersteller-Zertifikat, Auffrischung 3 Jahre"),
    "asbest": ("Asbest-Sachkunde (Nachtfaser, Nachtspeicher, Dichtschnüre)", 72, "TRGS 519 Anlage 4C/5, Fortbildung alle 6 Jahre"),
    "erste_hilfe": ("Ersthelfer", 24, "DGUV Vorschrift 1 § 26, Fortbildung alle 2 Jahre"),
    "leitern_geruest": ("Unterweisung Leitern/Tritte/Absturz", 12, "DGUV Information 208-016, jährlich"),
    "psaga": ("PSA gegen Absturz", 12, "DGUV Regel 112-198, jährliche Unterweisung mit Übung"),
    "fuehrerschein": ("Führerscheinkontrolle", 6, "Halterhaftung § 21 StVG, halbjährliche Kontrolle"),
    "ladungssicherung": ("Ladungssicherung", 36, "VDI 2700a"),
    "hochvolt": ("Hochvolt-Fachkundige Person (Kfz)", 12, "DGUV Information 209-093, jährliche Unterweisung"),
    "au_berechtigung": ("AU-/HU-Vorbereitung verantwortliche Person", 36, "AU-Schulung alle 36 Monate"),
    "hygiene_salon": ("Hygieneschulung (Kosmetik/Tattoo/Fußpflege)", 24, "Landeshygieneverordnungen, § 43 IfSG (Belehrung)"),
    "meister": ("Meisterbrief / Betriebsleiter", None, "HwO § 7"),
}
VORLAUF_TAGE = 90


def _ablauf(r: Ressource, q: str) -> date | None:
    wert = (r.qualifikationen_ablauf or {}).get(q)
    try:
        return date.fromisoformat(wert) if wert else None
    except ValueError:
        return None


def gueltig(r: Ressource, q: str, heute: date) -> bool:
    if q not in (r.qualifikationen or []):
        return False
    ablauf = _ablauf(r, q)
    return ablauf is None or ablauf >= heute


def status(r: Ressource, q: str, heute: date) -> dict[str, Any]:
    if q not in (r.qualifikationen or []):
        return {"status": "fehlt", "ablauf": None}
    ablauf = _ablauf(r, q)
    if ablauf is None:
        return {"status": "unbefristet" if QUALIFIKATIONEN.get(q, ("", None, ""))[1] is None else "ohne_datum", "ablauf": None}
    if ablauf < heute:
        return {"status": "abgelaufen", "ablauf": ablauf.isoformat(), "tage": (ablauf - heute).days}
    if ablauf <= heute + timedelta(days=VORLAUF_TAGE):
        return {"status": "laeuft_ab", "ablauf": ablauf.isoformat(), "tage": (ablauf - heute).days}
    return {"status": "gueltig", "ablauf": ablauf.isoformat(), "tage": (ablauf - heute).days}


def matrix(s: Session, m: Mandant, heute: date) -> dict[str, Any]:
    ressourcen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv, Ressource.art == "person").order_by(Ressource.name)).all()
    pflicht = list((m.stammdaten or {}).get("pflichtqualifikationen") or ["erste_hilfe", "fuehrerschein", "leitern_geruest"])
    spalten: list[str] = []
    for q in pflicht + [q for r in ressourcen for q in (r.qualifikationen or [])]:
        if q not in spalten:
            spalten.append(q)
    zeilen, warnungen = [], []
    for r in ressourcen:
        zellen = {}
        for q in spalten:
            st = status(r, q, heute)
            zellen[q] = st
            if st["status"] in ("abgelaufen", "laeuft_ab"):
                warnungen.append({"ressource_id": r.id, "name": r.name, "qualifikation": q, "bezeichnung": QUALIFIKATIONEN.get(q, (q,))[0], **st})
            elif st["status"] == "fehlt" and q in pflicht:
                warnungen.append({"ressource_id": r.id, "name": r.name, "qualifikation": q, "bezeichnung": QUALIFIKATIONEN.get(q, (q,))[0], "status": "fehlt", "ablauf": None})
        zeilen.append({"ressource_id": r.id, "name": r.name, "zellen": zellen})
    abdeckung = {q: sum(1 for r in ressourcen if gueltig(r, q, heute)) for q in spalten}
    luecken = [q for q, n in abdeckung.items() if n == 0 and q not in pflicht]
    einzel = [q for q, n in abdeckung.items() if n == 1 and q not in pflicht]
    warnungen.sort(key=lambda w: (w["status"] != "abgelaufen", w.get("tage", 10**6), w["name"]))
    return {"heute": heute.isoformat(), "spalten": [{"schluessel": q, "bezeichnung": QUALIFIKATIONEN.get(q, (q, None, ""))[0],
                                                     "nachschulung_monate": QUALIFIKATIONEN.get(q, (q, None, ""))[1], "grundlage": QUALIFIKATIONEN.get(q, (q, None, ""))[2],
                                                     "pflicht": q in pflicht} for q in spalten],
            "zeilen": zeilen, "abdeckung": abdeckung, "warnungen": warnungen,
            "risiken": [f"Niemand ist für „{QUALIFIKATIONEN.get(q, (q,))[0]}“ gültig qualifiziert." for q in luecken]
                       + [f"Nur eine Person für „{QUALIFIKATIONEN.get(q, (q,))[0]}“ – bei Urlaub/Krankheit entfällt die Leistung (Schlüsselpersonenrisiko)." for q in einzel]}


def ablaufende(s: Session, m: Mandant, heute: date) -> list[dict[str, Any]]:
    return [w for w in matrix(s, m, heute)["warnungen"] if w["status"] in ("abgelaufen", "laeuft_ab")]


def vorschlag_ablauf(q: str, ab: date) -> date | None:
    monate = QUALIFIKATIONEN.get(q, (q, None, ""))[1]
    if monate is None:
        return None
    jahr, monat = divmod(ab.month - 1 + monate, 12)
    return date(ab.year + jahr, monat + 1, min(ab.day, 28))
