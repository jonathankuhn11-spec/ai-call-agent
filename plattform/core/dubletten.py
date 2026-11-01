"""Dublettenauflösung (Entity Resolution) im Kundenstamm: probabilistisches Matching mit Blocking und sichere
Zusammenführung mit Audit-Snapshot.

Merkmale. Namen werden normalisiert (Umlaute, Anrede/Firmenzusätze, Reihenfolge Vorname/Nachname) und phonetisch mit
der Kölner Phonetik verglichen (für deutsche Namen besser als Soundex: „Meyer/Maier/Mayr“ → 67). Zusätzlich
Jaro-Winkler-Ähnlichkeit (Tippfehler), Rufnummer in E.164 (exakt), E-Mail (exakt, Kleinschreibung), Adresse mit
normalisierter Straße (str./straße, Hausnummer getrennt) und PLZ.

Score (0–1): Fellegi-Sunter-artig als gewichtete Summe von Übereinstimmungsgewichten; Rufnummer 0,45, E-Mail 0,4,
Adresse 0,3, Name phonetisch 0,25, Name Jaro-Winkler ≥ 0,92 0,2; Widersprüche (verschiedene PLZ, verschiedene
Rufnummern bei beiden gesetzt) ziehen ab. Vorschlag ab 0,6, sicher ab 0,85. Blocking über phonetischen Code des
Nachnamens, Rufnummer und PLZ, damit auch große Bestände nicht quadratisch verglichen werden.

Zusammenführung. Alle Fremdschlüssel auf `kunde.id` werden generisch aus den SQLAlchemy-Metadaten ermittelt und auf den
Zielkunden umgehängt (Vorgänge, Objekte, Rechnungen, Angebote, Verträge, Gutscheine, Anfragen …); fehlende Stammfelder
werden aufgefüllt, Einwilligungen vereinigt (frühester Zeitstempel), Kartei zusammengeführt, No-Show-Zähler addiert.
Der Quellkunde wird gelöscht; sein vollständiger Stand liegt im Ereignis `kunde.zusammengefuehrt` (Audit, Art. 5 DSGVO).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.domaene import protokollieren
from core.modelle import Basis, Kunde, Mandant

UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
ZUSAETZE = {"gmbh", "gbr", "kg", "ohg", "ug", "ag", "e.k.", "ek", "herr", "frau", "fam", "familie", "dr", "prof", "dipl", "ing", "inh", "co", "und", "&"}


def normal(text: str | None) -> str:
    t = (text or "").lower().translate(UMLAUTE)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", t).strip()


def namensteile(name: str | None) -> list[str]:
    return [w for w in normal(name).replace(".", " ").split() if w not in ZUSAETZE and len(w) > 1]


def koelner_phonetik(wort: str) -> str:
    """Kölner Phonetik (Postel 1969) für ein einzelnes Wort."""
    w = normal(wort).replace(" ", "")
    if not w:
        return ""
    w = w.replace("ph", "f")
    codes = []
    for i, c in enumerate(w):
        prev = w[i - 1] if i > 0 else ""
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if c in "aeijouy":
            code = "0"
        elif c == "h":
            code = "-"
        elif c in "bp":
            code = "1"
        elif c in "dt":
            code = "8" if nxt and nxt in "csz" else "2"
        elif c in "fvw":
            code = "3"
        elif c in "gkq":
            code = "4"
        elif c == "c":
            if i == 0:
                code = "4" if nxt and nxt in "ahkloqrux" else "8"
            elif prev and prev in "sz":
                code = "8"
            else:
                code = "4" if nxt and nxt in "ahkoqux" else "8"
        elif c == "x":
            code = "8" if prev and prev in "ckq" else "48"
        elif c == "l":
            code = "5"
        elif c in "mn":
            code = "6"
        elif c == "r":
            code = "7"
        elif c in "sz":
            code = "8"
        else:
            continue
        codes.append(code)
    raw = "".join(codes)
    out = []
    for i, ch in enumerate(raw):
        if ch == "-":
            continue
        if i > 0 and raw[i - 1] == ch:
            continue
        out.append(ch)
    ergebnis = "".join(out)
    return ergebnis[0] + ergebnis[1:].replace("0", "") if ergebnis else ""


def jaro_winkler(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    fenster = max(len(a), len(b)) // 2 - 1
    ma, mb = [False] * len(a), [False] * len(b)
    treffer = 0
    for i, ca in enumerate(a):
        for j in range(max(0, i - fenster), min(len(b), i + fenster + 1)):
            if not mb[j] and b[j] == ca:
                ma[i] = mb[j] = True
                treffer += 1
                break
    if not treffer:
        return 0.0
    sa = [c for c, f in zip(a, ma) if f]
    sb = [c for c, f in zip(b, mb) if f]
    transpos = sum(1 for x, y in zip(sa, sb) if x != y) / 2
    jaro = (treffer / len(a) + treffer / len(b) + (treffer - transpos) / treffer) / 3
    praefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        praefix += 1
    return jaro + praefix * 0.1 * (1 - jaro)


def strasse_normal(strasse: str | None) -> tuple[str, str]:
    t = normal(strasse)
    t = re.sub(r"\b(str|strasse|st)\b\.?", "strasse", t)
    t = t.replace("strasse", "str")
    m = re.match(r"^(.*?)[\s]*(\d+\s*[a-z]?)?$", t)
    name = (m.group(1) if m else t).replace(" ", "")
    nummer = (m.group(2) or "").replace(" ", "") if m else ""
    return name, nummer


def vergleichen(a: Kunde, b: Kunde) -> tuple[float, list[str]]:
    score, gruende = 0.0, []
    if a.telefon and b.telefon:
        if a.telefon == b.telefon:
            score += 0.45
            gruende.append("gleiche Rufnummer")
        else:
            score -= 0.15
    if a.email and b.email:
        if a.email.strip().lower() == b.email.strip().lower():
            score += 0.4
            gruende.append("gleiche E-Mail")
        else:
            score -= 0.1
    ta, tb = namensteile(a.name), namensteile(b.name)
    if ta and tb:
        pa, pb = {koelner_phonetik(w) for w in ta}, {koelner_phonetik(w) for w in tb}
        gemeinsame = pa & pb - {""}
        if len(gemeinsame) >= max(1, min(len(pa), len(pb)) - (1 if min(len(pa), len(pb)) > 1 else 0)):
            score += 0.25
            gruende.append("Name phonetisch gleich")
        jw = jaro_winkler(" ".join(sorted(ta)), " ".join(sorted(tb)))
        if jw >= 0.92:
            score += 0.2
            gruende.append(f"Name ähnlich (Jaro-Winkler {jw:.2f})")
        elif jw < 0.7 and not gemeinsame:
            score -= 0.2
    if a.plz and b.plz and a.plz != b.plz:
        score -= 0.2
    sa, sb = strasse_normal(a.strasse), strasse_normal(b.strasse)
    if sa[0] and sb[0] and a.plz and a.plz == b.plz:
        if sa[0] == sb[0] and sa[1] == sb[1]:
            score += 0.3
            gruende.append("gleiche Adresse")
        elif jaro_winkler(sa[0], sb[0]) >= 0.9 and sa[1] == sb[1]:
            score += 0.2
            gruende.append("Adresse ähnlich")
    return max(0.0, min(1.0, score)), gruende


def _bloecke(k: Kunde) -> set[str]:
    teile = namensteile(k.name)
    b = {f"ph:{koelner_phonetik(teile[-1])}"} if teile else set()
    if k.telefon:
        b.add(f"tel:{k.telefon}")
    if k.email:
        b.add(f"mail:{k.email.strip().lower()}")
    if k.plz:
        b.add(f"plz:{k.plz}")
    return b


def kandidaten(s: Session, m: Mandant, schwelle: float = 0.6, limit: int = 100) -> list[dict[str, Any]]:
    kunden = s.scalars(select(Kunde).where(Kunde.mandant_id == m.id)).all()
    index: dict[str, list[Kunde]] = {}
    for k in kunden:
        for b in _bloecke(k):
            index.setdefault(b, []).append(k)
    gesehen: set[tuple[str, str]] = set()
    treffer = []
    for gruppe in index.values():
        if len(gruppe) > 200:   # degenerierter Block (z. B. Sammel-PLZ) überspringen
            continue
        for i in range(len(gruppe)):
            for j in range(i + 1, len(gruppe)):
                a, b = sorted((gruppe[i], gruppe[j]), key=lambda k: k.erstellt or k.id)
                paar = (a.id, b.id)
                if paar in gesehen:
                    continue
                gesehen.add(paar)
                score, gruende = vergleichen(a, b)
                if score >= schwelle:
                    treffer.append({"score": round(score, 2), "sicher": score >= 0.85, "gruende": gruende,
                                    "ziel": _kurz(a), "quelle": _kurz(b)})
    treffer.sort(key=lambda t: -t["score"])
    return treffer[:limit]


def _kurz(k: Kunde) -> dict[str, Any]:
    return {"id": k.id, "name": k.name, "telefon": k.telefon, "email": k.email, "strasse": k.strasse, "plz": k.plz, "ort": k.ort,
            "erstellt": k.erstellt.isoformat() if k.erstellt else None}


def fremdschluessel() -> list[tuple[Any, Any]]:
    """Alle (Tabelle, Spalte), die auf kunde.id zeigen – aus den Metadaten, damit neue Entitäten automatisch mitgehen."""
    ergebnis = []
    for tabelle in Basis.metadata.sorted_tables:
        for spalte in tabelle.columns:
            for fk in spalte.foreign_keys:
                if fk.column.table.name == "kunde" and fk.column.name == "id":
                    ergebnis.append((tabelle, spalte))
    return ergebnis


def zusammenfuehren(s: Session, m: Mandant, ziel: Kunde, quelle: Kunde, akteur_name: str | None = None) -> dict[str, Any]:
    if ziel.id == quelle.id:
        raise ValueError("Ziel und Quelle sind identisch")
    if ziel.mandant_id != m.id or quelle.mandant_id != m.id:
        raise ValueError("Kunden gehören nicht zum Betrieb")
    schnappschuss = {spalte.name: _json(getattr(quelle, spalte.name)) for spalte in Kunde.__table__.columns}
    umgehaengt: dict[str, int] = {}
    for tabelle, spalte in fremdschluessel():
        r = s.execute(update(tabelle).where(spalte == quelle.id).values({spalte.name: ziel.id}))
        if r.rowcount:
            umgehaengt[f"{tabelle.name}.{spalte.name}"] = r.rowcount
    for feld in ("telefon", "email", "strasse", "plz", "ort", "notiz", "lat", "lon", "geo_quelle", "geo_stand"):
        if getattr(ziel, feld) in (None, "") and getattr(quelle, feld) not in (None, ""):
            setattr(ziel, feld, getattr(quelle, feld))
    if quelle.vertragsstatus == "wartungsvertrag":
        ziel.vertragsstatus = "wartungsvertrag"
    ew = dict(quelle.einwilligungen or {})
    for k_, v in (ziel.einwilligungen or {}).items():
        ew[k_] = v if k_ not in ew or not isinstance(v, str) or not isinstance(ew[k_], str) else min(v, ew[k_])
    ziel.einwilligungen = ew
    ziel.kartei = {**(quelle.kartei or {}), **(ziel.kartei or {})}
    ziel.nicht_erschienen = (ziel.nicht_erschienen or 0) + (quelle.nicht_erschienen or 0)
    s.delete(quelle)
    s.flush()
    protokollieren(s, m.id, "kunde.zusammengefuehrt", "kunde", ziel.id, "mensch",
                   {"quelle_id": quelle.id, "quelle": schnappschuss, "umgehaengt": umgehaengt}, akteur_name)
    return {"ziel_id": ziel.id, "quelle_id": quelle.id, "umgehaengt": umgehaengt}


def _json(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v
