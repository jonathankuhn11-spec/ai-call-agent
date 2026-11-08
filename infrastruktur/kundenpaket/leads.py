"""Lead-Agent: liest Leadlisten (Gesamtliste der Wellen, Agentenlisten), normalisiert Spalten und filtert.

Alle Listen des Projekts teilen das Schema der Wellen (Klasse, Score, Firma, Gewerk, PLZ, Ort, Straße, Telefon,
E-Mail, Website, Entscheider, Stellenanzeige, Alter (Tage), Infrastruktur, Probleme, KUHN-Lösung, Angebot, Befund,
Status, Quell-URL). Ältere Listen mit abweichenden Namen werden über Synonyme gelesen.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

SYNONYME = {
    "firma": ("Firma", "Firmenname", "Betrieb", "Name"),
    "gewerk": ("Gewerk", "Branche", "Segment"),
    "plz": ("PLZ",), "ort": ("Ort", "Stadt"), "strasse": ("Straße", "Strasse", "Adresse"),
    "telefon": ("Telefon", "Tel."), "email": ("E-Mail", "Email", "Mail"), "website": ("Website", "Webseite", "URL"),
    "entscheider": ("Entscheider", "Inhaber", "Geschäftsführung", "Ansprechpartner"),
    "stelle": ("Stellenanzeige",), "alter": ("Alter (Tage)",), "infrastruktur": ("Infrastruktur", "Infrastruktur-Engpass"),
    "probleme": ("Probleme", "Problemcodes"), "loesung": ("KUHN-Lösung", "KUHN-Ansatzpunkt"), "angebot": ("Angebot",),
    "befund": ("Befund",), "status": ("Status", "Status Kontakt"), "klasse": ("Klasse",), "score": ("Score", "Abschluss-Score"),
    "quelle": ("Quelle",), "quell_url": ("Quell-URL",), "welle": ("Welle",), "bundesland": ("Bundesland",),
    "akut": ("Akut (Anzeige ≤14 T., verifiziert, KEIN/SCHWACH)", "Akut"), "widerspruch": ("Widerspruch",),
}
BLATT_BEVORZUGT = ("Alle Wellen", "Leads", "Welle", "Leads Kfz", "Problem direkt belegt")
SPERRE = re.compile(r"kein interesse|widerspruch|nicht kontaktieren|ausgeschlossen", re.I)
CODE = re.compile(r"\bP(10|[1-9])\b")


@dataclass
class Lead:
    firma: str
    gewerk: str = ""
    plz: str = ""
    ort: str = ""
    strasse: str = ""
    telefon: str = ""
    email: str = ""
    website: str = ""
    entscheider: str = ""
    stelle: str = ""
    alter: str = ""
    infrastruktur: str = ""
    probleme: list[str] = field(default_factory=list)
    loesung: str = ""
    angebot: str = ""
    befund: str = ""
    status: str = ""
    klasse: str = ""
    score: str = ""
    quelle: str = ""
    quell_url: str = ""
    welle: str = ""
    bundesland: str = ""
    akut: bool = False
    gesperrt: bool = False
    herkunft: str = ""

    @property
    def schluessel(self) -> str:
        """Stabiler Verzeichnisname: Firma + PLZ, ASCII, Kleinbuchstaben."""
        return slug(f"{self.firma} {self.plz}")[:70].strip("-")


def slug(text: str) -> str:
    text = text.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("Ä", "ae").replace("Ö", "oe").replace("Ü", "ue")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _wert(zeile: dict[str, str], feld: str) -> str:
    for name in SYNONYME[feld]:
        v = zeile.get(name)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _zeilen(pfad: Path, blatt: str | None) -> list[dict[str, str]]:
    if pfad.suffix.lower() == ".csv":
        with pfad.open(encoding="utf-8-sig", newline="") as f:
            probe = f.read(4096)
            f.seek(0)
            return [dict(z) for z in csv.DictReader(f, dialect=csv.Sniffer().sniff(probe, delimiters=";,\t"))]
    import openpyxl
    mappe = openpyxl.load_workbook(pfad, read_only=True, data_only=True)
    name = blatt or next((b for b in BLATT_BEVORZUGT if b in mappe.sheetnames), None)
    ws = mappe[name] if name else max(mappe.worksheets, key=lambda w: w.max_row or 0)
    reihen = ws.iter_rows(values_only=True)
    kopf = [str(k or "").strip() for k in next(reihen)]
    return [{k: ("" if v is None else str(v)) for k, v in zip(kopf, r)} for r in reihen if any(r)]


def laden(pfad: Path, blatt: str | None = None) -> list[Lead]:
    leads = []
    for z in _zeilen(pfad, blatt):
        firma = _wert(z, "firma")
        if not firma:
            continue
        lead = Lead(firma=firma, **{f: _wert(z, f) for f in ("gewerk", "plz", "ort", "strasse", "telefon", "email", "website", "entscheider", "stelle",
                                                             "alter", "infrastruktur", "loesung", "angebot", "befund", "status", "klasse", "score",
                                                             "quelle", "quell_url", "welle", "bundesland")})
        lead.probleme = sorted(set("P" + c for c in CODE.findall(_wert(z, "probleme"))), key=lambda c: int(c[1:]))
        lead.akut = _wert(z, "akut").upper().startswith("AKUT")
        lead.gesperrt = bool(_wert(z, "widerspruch")) or bool(SPERRE.search(lead.status))
        lead.plz = lead.plz.split(".")[0].zfill(5) if lead.plz else ""
        lead.herkunft = pfad.name
        leads.append(lead)
    return leads


def filtern(leads: list[Lead], akut: bool = False, klassen: set[str] | None = None, firma: str | None = None,
            limit: int | None = None) -> list[Lead]:
    aus = []
    gesehen = set()
    for le in leads:
        if le.gesperrt or le.schluessel in gesehen:
            continue
        if akut and not le.akut:
            continue
        if klassen and le.klasse not in klassen:
            continue
        if firma and firma.lower() not in le.firma.lower():
            continue
        gesehen.add(le.schluessel)
        aus.append(le)
    aus.sort(key=lambda le: (not le.akut, -(float(le.score) if le.score.replace(".", "", 1).isdigit() else 0)))
    return aus[:limit] if limit else aus
