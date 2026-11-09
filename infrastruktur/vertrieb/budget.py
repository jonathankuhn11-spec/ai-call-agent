"""Abrufbudget-Agent: verteilt das gemeinsame Kontingent der Lead-Agenten (ca. 100 Webseitenabrufe je Stunde) auf die
Quellen mit dem höchsten erwarteten Vertriebswert je Abruf.

Grundlage sind die Rohdaten der Wellen und Segment-Agenten (wellen/roh/*.json, agents/*roh/*.json). Je Quelle × Branche:
    Abrufe      ≈ Anzahl verschiedener Quell-URLs (eine Ergebnisseite = ein Abruf; Untergrenze, Prüfabrufe kommen hinzu)
    Neuquote    = Anteil der Datensätze, die beim Eintreffen noch in keiner früheren Liste standen (Sättigungsmaß)
    Ertrag      = neue Leads je Abruf
    Wert/Abruf  = Ertrag · mittleres p (Priorisierungsmodell, inkl. Rückkopplung) · p_abschluss · mittlerer CLV
Ertrag je Abruf wird zum Gesamtmittel geschrumpft (3 Pseudo-Abrufe), Zellen unter 5 Datensätzen werden je Quelle
zusammengefasst, und ein gedämpfter Upper-Confidence-Bound-Term (0,5·√(ln N/n)) verhindert, dass kleine Stichproben
vorschnell verworfen werden. Das Ergebnis wird als JSON geschrieben, das die Wellen-Agenten vor ihrer Quellenwahl lesen können.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from vertrieb.erfassung import firma_norm, telefon_norm


def _quelle(datei: str, e: dict) -> str:
    url = e.get("quelle_url") or e.get("quell_url") or ""
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0] or "unbekannt"
    if datei.startswith("jobboerse"):
        return "Jobbörse BA (Büro-/Serviceanzeigen)"
    if datei.startswith("ohne_website"):
        return f"Verzeichnis ohne Website ({host})"
    if datei.startswith("pruefung"):
        return ""
    return f"Segment-Agent {Path(datei).stem}"


def _schluessel(e: dict) -> set[str]:
    s = set()
    t = telefon_norm(str(e.get("telefon") or ""))
    if t:
        s.add("t:" + t)
    f = firma_norm(str(e.get("firma") or ""))
    if f:
        s.add("f:" + f + "|" + str(e.get("plz") or e.get("ort") or ""))
        s.add("n:" + f)              # Rückfall ohne PLZ/Ort (Segment-Agenten liefern teils „unbekannt“)
    return s


def rohdaten(wurzel: Path) -> list[tuple[str, dict]]:
    dateien = sorted(list((wurzel / "wellen" / "roh").glob("*.json")) + list((wurzel / "agents").glob("*roh/*.json")),
                     key=lambda p: (p.stat().st_mtime, p.name))
    aus = []
    for p in dateien:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        d = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
        aus += [(p.name, e) for e in d if isinstance(e, dict) and e.get("firma")]
    return aus


def auswerten(wurzel: Path, wert_je_lead, branche_von, bestand_vor: set[str] | None = None, kontingent: int = 100) -> dict:
    """wert_je_lead(schluessel_set) -> (p, clv) des gematchten Leads oder None; branche_von(gewerk, firma) -> Branche."""
    gesehen: set[str] = set(bestand_vor or ())
    stat: dict[tuple[str, str], dict] = {}
    for datei, e in rohdaten(wurzel):
        q = _quelle(datei, e)
        if not q:
            continue
        b = branche_von(str(e.get("gewerk") or ""), str(e.get("firma") or ""))
        s = stat.setdefault((q, b), {"datensaetze": 0, "neu": 0, "urls": set(), "wert": 0.0, "p": 0.0, "bewertet": 0})
        keys = _schluessel(e)
        s["datensaetze"] += 1
        if not keys & gesehen:
            s["neu"] += 1
        gesehen |= keys
        url = e.get("quelle_url") or e.get("quell_url") or f"{datei}#{s['datensaetze'] // 20}"
        s["urls"].add(url)
        wv = wert_je_lead(keys)
        if wv:
            s["p"] += wv[0]
            s["wert"] += wv[0] * wv[1]
            s["bewertet"] += 1
    # Kleine Zellen (< 5 Datensätze) je Quelle zu „übrige Branchen“ zusammenfassen
    for (q, b) in [k for k, v in stat.items() if v["datensaetze"] < 5]:
        klein = stat.pop((q, b))
        z = stat.setdefault((q, "übrige Branchen"), {"datensaetze": 0, "neu": 0, "urls": set(), "wert": 0.0, "p": 0.0, "bewertet": 0})
        for k in ("datensaetze", "neu", "wert", "p", "bewertet"):
            z[k] += klein[k]
        z["urls"] |= klein["urls"]
    zeilen = []
    gesamt_abrufe = sum(len(s["urls"]) for s in stat.values()) or 1
    # Schrumpfung des Ertrags je Abruf zum Gesamtmittel (k0 Pseudo-Abrufe), damit Einzelfunde nicht dominieren
    k0 = 3.0
    global_ertrag = sum(s["neu"] for s in stat.values()) / gesamt_abrufe
    for (q, b), s in stat.items():
        abrufe = max(len(s["urls"]), 1)
        ertrag = (s["neu"] + k0 * global_ertrag) / (abrufe + k0)
        wert_lead = s["wert"] / s["bewertet"] if s["bewertet"] else 0.0
        zeilen.append({"Quelle": q, "Branche": b, "Datensätze": s["datensaetze"], "neu": s["neu"], "Abrufe (≈ URLs)": abrufe,
                       "Neuquote": s["neu"] / s["datensaetze"], "neue Leads je Abruf (geschrumpft)": ertrag,
                       "mittleres p": s["p"] / s["bewertet"] if s["bewertet"] else 0.0,
                       "Wert je Abruf (€, vor p_abschluss)": ertrag * wert_lead,
                       "UCB": ertrag * wert_lead * (1 + 0.5 * math.sqrt(math.log(gesamt_abrufe) / abrufe))})
    zeilen.sort(key=lambda z: -z["UCB"])
    summe = sum(z["UCB"] for z in zeilen) or 1
    for z in zeilen:
        z["Empfehlung Abrufe/h"] = round(kontingent * z["UCB"] / summe)
    return {"kontingent_je_stunde": kontingent, "quellen": zeilen}


def schreiben(pfad: Path, ergebnis: dict) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    kurz = {"kontingent_je_stunde": ergebnis["kontingent_je_stunde"],
            "hinweis": "Von den Wellen-Agenten vor der Quellenwahl zu lesen; Quellen mit 0 Abrufen/h pausieren (gesättigt).",
            "quellen": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in z.items()} for z in ergebnis["quellen"]]}
    pfad.write_text(json.dumps(kurz, ensure_ascii=False, indent=1), encoding="utf-8")
