"""Erfassungs-Agent: liest alle Leadlisten des Projekts (Wellen, Segment-Agenten, Altlisten), normalisiert sie auf ein
gemeinsames Schema und führt Dubletten zu einem kanonischen Datensatz zusammen (Entity Resolution).

Die Listen sind über mehrere Wochen in unterschiedlichen Schemata entstanden (Firma/Firmenname, Score/Abschluss-Score,
Probleme/Belegte Probleme, Anschrift als Freitext). Der Kopf jedes Tabellenblatts wird daher über Synonyme erkannt.
Blätter mit Ausschlüssen (Ausgeschlossen, Aussortiert) werden nicht als Leads, sondern als Ausschlussliste gelesen.

Dublettenschlüssel (Union-Find über alle Datensätze):
  1. Rufnummer ohne Vorwahl-Präfix +49/0 und ohne Trennzeichen (mindestens 7 Ziffern),
  2. normalisierter Firmenname (ohne Rechtsform, Inh., Umlaute aufgelöst) + PLZ,
  3. normalisierter Firmenname + Ort, wenn keine PLZ vorliegt.
Beim Zusammenführen gewinnt der jeweils jüngste nichtleere Wert; Problemcodes und Herkünfte werden vereinigt, der
Score als Maximum übernommen.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

FELDER = ("firma", "gewerk", "plz", "ort", "strasse", "telefon", "email", "website", "entscheider", "stelle", "alter",
          "infrastruktur", "probleme", "angebot", "befund", "status", "klasse", "score", "quelle", "quell_url", "akut",
          "widerspruch", "anschrift", "telefontyp", "region", "bundesland", "website_status", "grund", "konzern")

# Spaltenkopf (klein, ohne Klammerzusatz) -> Feld. Reihenfolge innerhalb eines Feldes = Vorrang.
SYNONYME: dict[str, tuple[str, ...]] = {
    "firma": ("firma", "firmenname", "betrieb", "unternehmen"),
    "gewerk": ("gewerk", "branche", "segment", "kategorie"),
    "plz": ("plz",),
    "ort": ("ort", "stadt"),
    "strasse": ("straße", "strasse"),
    "anschrift": ("anschrift", "adresse"),
    "telefon": ("telefon", "tel."),
    "telefontyp": ("telefontyp", "mobilnummer"),
    "email": ("e-mail", "email", "e-mail-hinweis"),
    "website": ("website", "webseite"),
    "entscheider": ("entscheider", "inhaber/gf", "inhaber/firmierung", "inhaber", "geschäftsführung", "ansprechpartner"),
    "stelle": ("stellenanzeige", "belegter bedarf"),
    "alter": ("alter",),
    "infrastruktur": ("infrastruktur", "infrastruktur-engpass", "belegter infrastruktur-engpass"),
    "probleme": ("probleme", "belegte probleme", "problemcodes"),
    "angebot": ("angebot", "baustein"),
    "befund": ("befund", "konkreter bedarf", "nachfragesignal", "signale"),
    "status": ("status", "status kontakt", "ergebnis"),
    "klasse": ("klasse", "prio"),
    "score": ("score", "abschluss-score"),
    "quelle": ("quelle", "signalquelle", "herkunft"),
    "quell_url": ("quell-url",),
    "akut": ("akut",),
    "widerspruch": ("widerspruch", "widerspruch am"),
    "region": ("region",),
    "grund": ("grund", "ausschlussgrund"),
    "konzern": ("konzern/gruppe", "konzern"),
    "website_status": ("website-status", "website-hinweis", "website-websuche", "websuche_ergebnis"),
    "bundesland": ("bundesland",),
}
AUSSCHLUSS_BLATT = re.compile(r"ausgeschlossen|aussortiert", re.IGNORECASE)
IGNORIERT_BLATT = re.compile(r"übersicht|katalog|methodik|recht|leitfaden|risiko|wirtschaftlich|serienbrief|einwilligung|"
                             r"kontaktstrecke|kanäle|gesprächs", re.IGNORECASE)
SPERRE = re.compile(r"kein interesse|widerspruch|nicht kontaktieren|werbesperre|abgelehnt", re.IGNORECASE)
CODE = re.compile(r"\bP(10|[1-9])\b")
RECHTSFORM = re.compile(r"\b(gmbh\s*&\s*co\.?\s*kg|gmbh|ug|haftungsbeschränkt|kg|ohg|gbr|e\.?\s?k\.?|ek|ag|mbh|co|inh|inhaber|in|"
                        r"meisterbetrieb|e\.?v\.?)\b\.?", re.IGNORECASE)
PLZ_ORT = re.compile(r"(\d{5})\s+([^,]+)$")


@dataclass
class Datensatz:
    werte: dict[str, str]
    herkunft: str                  # Datei | Blatt
    stand: str                     # Datum der Liste (aus Dateiname), für "jüngster Wert gewinnt"


@dataclass
class Lead:
    id: str
    firma: str
    werte: dict[str, str] = field(default_factory=dict)
    probleme: list[str] = field(default_factory=list)
    score: float = 0.0
    akut: bool = False
    gesperrt: bool = False
    sperrgrund: str = ""
    ausgeschlossen: str = ""
    herkunft: list[str] = field(default_factory=list)
    erstmals: str = ""

    def __getattr__(self, name: str) -> str:   # bequemer Feldzugriff lead.ort, lead.plz …
        werte = self.__dict__.get("werte", {})
        if name in FELDER:
            return werte.get(name, "")
        raise AttributeError(name)


def ascii_klein(text: str) -> str:
    text = (text or "").replace("ß", "ss")
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "ae"), ("Ö", "oe"), ("Ü", "ue")):
        text = text.replace(a, b)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def firma_norm(firma: str) -> str:
    t = re.sub(r"\s*[–—,-]+\s*Inh(\.|aber(in)?)\s.*$", "", firma or "", flags=re.IGNORECASE)   # „Autofuchs – Inh. A. Galster“ -> „Autofuchs“
    t = ascii_klein(t)
    t = RECHTSFORM.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(w for w in t.split() if len(w) > 1)


def telefon_norm(tel: str) -> str:
    erste = re.split(r"[,;]| oder | und |\s/\s*(?=0)", (tel or "").replace("(0)", ""))[0]   # „0231 / 12345“ bleibt eine Nummer
    ziffern = re.sub(r"\D", "", erste)
    if ziffern.startswith("49") and len(ziffern) > 10:
        ziffern = ziffern[2:]
    ziffern = ziffern.lstrip("0")
    return ziffern if len(ziffern) >= 7 else ""


def ist_mobil(tel: str) -> bool:
    return bool(re.match(r"1[5-7]\d", telefon_norm(tel)))


def lead_id(firma: str, plz: str, tel: str) -> str:
    basis = f"{firma_norm(firma)}|{plz}|{telefon_norm(tel)}"
    return "L" + hashlib.sha1(basis.encode()).hexdigest()[:10]


def _kopf_feld(kopf: str) -> str | None:
    k = re.sub(r"\s*\(.*?\)\s*", " ", kopf or "").strip().lower()
    k = re.sub(r"\s+", " ", k)
    for feld, namen in SYNONYME.items():
        if k in namen:
            return feld
    if k.startswith("akut"):
        return "akut"
    return None


def _stand(pfad: Path) -> str:
    m = re.search(r"(20\d\d-\d\d-\d\d)(?:_(\d{2})(\d{2}))?", str(pfad))
    if not m:
        return "0000-00-00"
    return m.group(1) + (f"T{m.group(2)}:{m.group(3)}" if m.group(2) else "")


def _zelle(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    return str(v).strip()


def _blatt_lesen(zeilen: list[tuple], herkunft: str, stand: str) -> tuple[list[Datensatz], bool]:
    """Erkennt die Kopfzeile in den ersten fünf Zeilen; liefert Datensätze und ob ein Kopf gefunden wurde."""
    for i, kopf in enumerate(zeilen[:5]):
        abbildung = {j: _kopf_feld(str(k or "")) for j, k in enumerate(kopf)}
        felder = {f for f in abbildung.values() if f}
        if "firma" in felder and felder & {"telefon", "ort", "plz", "anschrift"}:
            daten = []
            for r in zeilen[i + 1:]:
                w: dict[str, str] = {}
                for j, f in abbildung.items():
                    if f and j < len(r):
                        v = _zelle(r[j])
                        if v and not w.get(f):
                            w[f] = v
                if "website" in felder:
                    w["_website_spalte"] = "1"
                if w.get("firma"):
                    daten.append(Datensatz(w, herkunft, stand))
            return daten, True
    return [], False


def datei_lesen(pfad: Path) -> tuple[list[Datensatz], list[Datensatz]]:
    """Liefert (Leads, Ausschlüsse) einer xlsx/csv/json-Datei."""
    stand = _stand(pfad)
    leads: list[Datensatz] = []
    aus: list[Datensatz] = []
    if pfad.suffix.lower() == ".json":
        roh = json.loads(pfad.read_text(encoding="utf-8"))
        roh = roh if isinstance(roh, list) else next((v for v in roh.values() if isinstance(v, list)), [])
        for e in roh:
            if isinstance(e, dict) and e.get("firma"):
                w = {k: _zelle(e.get(k)) for k in FELDER if e.get(k) not in (None, "", "unbekannt")}
                w.setdefault("stelle", _zelle(e.get("stelle")))
                w["website_status"] = _zelle(e.get("websuche_ergebnis"))
                if "website" in e:
                    w["_website_spalte"] = "1"
                leads.append(Datensatz(w, pfad.name, stand))
        return leads, aus
    if pfad.suffix.lower() == ".csv":
        with pfad.open(encoding="utf-8-sig", newline="") as f:
            probe = f.read(4096)
            f.seek(0)
            zeilen = [tuple(z) for z in csv.reader(f, dialect=csv.Sniffer().sniff(probe, delimiters=";,\t"))]
        d, _ = _blatt_lesen(zeilen, pfad.name, stand)
        return d, aus
    import openpyxl
    mappe = openpyxl.load_workbook(pfad, read_only=True, data_only=True)
    for ws in mappe.worksheets:
        if IGNORIERT_BLATT.search(ws.title) and not AUSSCHLUSS_BLATT.search(ws.title):
            continue
        zeilen = list(ws.iter_rows(values_only=True))
        d, ok = _blatt_lesen(zeilen, f"{pfad.name} | {ws.title}", stand)
        if not ok:
            continue
        (aus if AUSSCHLUSS_BLATT.search(ws.title) else leads).extend(d)
    mappe.close()
    return leads, aus


def _anschrift_zerlegen(w: dict[str, str]) -> None:
    a = w.get("anschrift", "")
    if a and not w.get("plz"):
        m = PLZ_ORT.search(a)
        if m:
            w["plz"] = m.group(1)
            w.setdefault("ort", m.group(2).strip())
            w.setdefault("strasse", a[:m.start()].strip(" ,"))
    if w.get("plz"):
        w["plz"] = re.sub(r"\D", "", w["plz"].split(".")[0]).zfill(5)[:5]
        if w["plz"] == "00000":
            w["plz"] = ""
    if w.get("ort"):
        w["ort"] = w["ort"].split(",")[0].strip()


def _score(w: dict[str, str]) -> float:
    s = w.get("score", "").replace(",", ".")
    try:
        return max(0.0, min(100.0, float(s)))
    except ValueError:
        pass
    # Altlisten führen nur eine Prio/Klasse: A+ 95, A 85, B 65, C 45 (Mittelwerte der Klassen in den Wellen)
    return {"A+": 95.0, "A": 85.0, "B": 65.0, "C": 45.0}.get(w.get("klasse", "").strip().upper(), 50.0)


class _UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _schluessel(w: dict[str, str]) -> list[str]:
    s = []
    t = telefon_norm(w.get("telefon", ""))
    if t:
        s.append("t:" + t)
    f = firma_norm(w.get("firma", ""))
    if f and w.get("plz"):
        s.append(f"f:{f}|{w['plz']}")
    elif f and w.get("ort"):
        s.append(f"o:{f}|{ascii_klein(w['ort'])}")
    return s


def zusammenfuehren(datensaetze: list[Datensatz], ausschluesse: list[Datensatz] | None = None) -> list[Lead]:
    for d in datensaetze:
        _anschrift_zerlegen(d.werte)
    uf = _UnionFind(len(datensaetze))
    erster: dict[str, int] = {}
    for i, d in enumerate(datensaetze):
        for k in _schluessel(d.werte):
            if k in erster:
                uf.union(i, erster[k])
            else:
                erster[k] = i
    gruppen: dict[int, list[Datensatz]] = {}
    for i, d in enumerate(datensaetze):
        gruppen.setdefault(uf.find(i), []).append(d)

    aus_index: dict[str, str] = {}
    for d in ausschluesse or []:
        _anschrift_zerlegen(d.werte)
        grund = next((d.werte.get(k) for k in ("grund", "status", "befund") if d.werte.get(k)), "") or d.herkunft
        if re.search(r"dublette|doppelt|bereits (in|im) bestand", grund, re.IGNORECASE):
            continue                        # Dubletten führt die Zusammenführung ohnehin zusammen; kein Ausschluss
        for k in _schluessel(d.werte):
            aus_index.setdefault(k, grund)

    leads = []
    for gruppe in gruppen.values():
        gruppe.sort(key=lambda d: d.stand)                 # ältester zuerst, jüngere überschreiben
        w: dict[str, str] = {}
        codes: set[str] = set()
        score = 0.0
        akut = False
        sperre = ""
        for d in gruppe:
            for k, v in d.werte.items():
                if v:
                    w[k] = v
            codes |= {"P" + c for c in CODE.findall(d.werte.get("probleme", ""))}
            score = max(score, _score(d.werte))
            akut = akut or d.werte.get("akut", "").upper().startswith("AKUT")
            text = f"{d.werte.get('status', '')} {d.werte.get('widerspruch', '')}"
            if d.werte.get("widerspruch") or SPERRE.search(text):
                sperre = sperre or (d.werte.get("widerspruch") and f"Widerspruch {d.werte['widerspruch']}") or d.werte.get("status", "")
        # P1 nur bei ausdrücklichem Beleg (Websuche/Verzeichnis negativ, „ohne Website“-Liste): eine leere Website-Spalte
        # in Altlisten heißt oft „nicht recherchiert“; ein falscher P1-Befund im Brief wäre peinlich.
        negativ = re.search(r"keine", f"{w.get('website', '')} {w.get('website_status', '')}", re.IGNORECASE)
        if re.search(r"keine|unbekannt|nicht geprüft|^-$", w.get("website", ""), re.IGNORECASE):
            w["website"] = ""
        w.pop("_website_spalte", "")
        if not w.get("website") and (negativ or any("ohne_website" in d.herkunft.lower() for d in gruppe)):
            codes.add("P1")
        elif w.get("website"):
            codes.discard("P1")
        if re.search(r"firmierung|gmbh|\bkg\b|e\.\s?k\.|unbekannt|^-$", w.get("entscheider", ""), re.IGNORECASE):
            w["entscheider"] = ""            # Firmierungsangabe statt Person: kein persönlicher Ansprechpartner
        if (w.get("telefontyp", "").lower().startswith(("mobil", "ja")) or ist_mobil(w.get("telefon", ""))):
            codes.add("P7")
        if w.get("stelle") and re.search(r"büro|bürokraft|verwaltung|disponent|telefon|empfang|kaufm|service", w["stelle"], re.IGNORECASE):
            codes.add("P8")
        lead = Lead(id=lead_id(w["firma"], w.get("plz", ""), w.get("telefon", "")), firma=w["firma"], werte=w,
                    probleme=sorted(codes, key=lambda c: int(c[1:])), score=score, akut=akut, gesperrt=bool(sperre),
                    sperrgrund=sperre, herkunft=sorted({d.herkunft for d in gruppe}), erstmals=gruppe[0].stand)
        lead.ausgeschlossen = next((aus_index[k] for k in _schluessel(w) if k in aus_index), "")
        leads.append(lead)
    return leads


STANDARD_MUSTER = ("wellen/Gesamtliste_Wellen.xlsx", "agents/*.xlsx", "Leads_Problemkatalog_v6_*.xlsx", "Leads_*Telefonakquise*.xlsx",
                   "SHK_Leads_*.xlsx", "Leads_Top_*.xlsx", "Leads_Problem_belegt_*.xlsx")


def listenfamilie(stem: str) -> str:
    """„Leads_Problemkatalog_v6_2026-11-08“ und „Leads_Top_Infrastruktur_Engpass_v4_2026-11-07“ -> „Leads_Top“."""
    f = re.sub(r"_20\d\d-\d\d-\d\d.*$", "", stem)
    f = re.sub(r"_(v\d+|mit)(_[A-Za-z]+)*$", "", f)
    f = re.sub(r"_v\d+(_[A-Za-z]+)*", "", f)
    return re.sub(r"^Leads_(Problem_belegt|Problemkatalog|Top_[A-Za-z_]+)$", "Leads_Top", f)


def _fassung(p: Path) -> tuple[str, int, float]:
    """Ordnung der Fassungen einer Familie: Datum im Namen, dann Versionsnummer, dann Änderungszeit."""
    v = re.search(r"_v(\d+)", p.stem)
    return _stand(p), int(v.group(1)) if v else 1, p.stat().st_mtime


def quellen_finden(wurzel: Path, muster: tuple[str, ...] = STANDARD_MUSTER) -> list[Path]:
    """Neueste Fassung je Listenfamilie: Versionen (_v2, _v3 …) einer Liste werden auf die jüngste reduziert,
    weil ältere Fassungen Teilmengen sind und nur Rechenzeit kosten."""
    gefunden: dict[str, Path] = {}
    for m in muster:
        for p in sorted(wurzel.glob(m)):
            familie = listenfamilie(p.stem)
            alt = gefunden.get(familie)
            if alt is None or _fassung(p) > _fassung(alt):
                gefunden[familie] = p
    return sorted(gefunden.values())


def alle_lesen(pfade: list[Path]) -> tuple[list[Lead], dict]:
    datensaetze: list[Datensatz] = []
    aus: list[Datensatz] = []
    protokoll = {"dateien": {}}
    for p in pfade:
        d, a = datei_lesen(p)
        datensaetze += d
        aus += a
        protokoll["dateien"][p.name] = {"leads": len(d), "ausschluesse": len(a)}
    leads = zusammenfuehren(datensaetze, aus)
    protokoll.update(datensaetze=len(datensaetze), leads=len(leads), dubletten=len(datensaetze) - len(leads),
                     gesperrt=sum(le.gesperrt for le in leads), ausgeschlossen=sum(bool(le.ausgeschlossen) for le in leads))
    return leads, protokoll
