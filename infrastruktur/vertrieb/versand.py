"""Versand-Agent: stellt Briefchargen zusammen, legt sie zur Freigabe vor und liest Freigabe und Rücklauf wieder ein.

Es wird nichts versendet. Eine Charge durchläuft die Zustände
    vorgeschlagen ──(Jonathan setzt „Freigabe = ja“)──▶ freigegeben ──(„Versandt am“ eingetragen)──▶ versandt
Der Freigabebogen ist zugleich Rückmeldebogen: Reaktionen (Antwort, Termin, Widerspruch …) werden in derselben Datei
eingetragen und mit `vertriebsagent.py einlesen` als Ereignisse übernommen.

Auswahl einer Charge (Größe N, Diversifikationsgrenze je Branche):
  1. fällige Nachfassbriefe (Kadenzpflicht),
  2. (1 − exploration) · N Plätze gierig nach Erwartungswert EV,
  3. Rest per Thompson-Sampling (EV mit gezogenem Segment-Lift), damit unterbeobachtete Segmente Daten erzeugen.
Ausgeschlossen sind gesperrte, aussortierte und nicht versandfähige Leads (Straße/PLZ fehlt), Leads in offenen
Chargen und Leads, deren letzter Brief jünger als die Sperrfrist ist.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import random
import re
from pathlib import Path

from vertrieb import scoring
from vertrieb.speicher import EREIGNISSE, ereignis

SPALTEN_WELLE = ["Klasse", "Score", "Quelle", "Firma", "Gewerk", "PLZ", "Ort", "Straße", "Telefon", "E-Mail", "Website", "Entscheider",
                 "Stellenanzeige", "Alter (Tage)", "Infrastruktur", "Probleme", "KUHN-Lösung", "Angebot", "Befund", "Status", "Quell-URL",
                 "Akut (Anzeige ≤14 T., verifiziert, KEIN/SCHWACH)", "Lead-ID"]
SPALTEN_BOGEN = ["Rang", "Lead-ID", "Art", "Freigabe (ja/nein)", "Versandt am", "Reaktion", "Reaktionsdatum", "Notiz",
                 "Firma", "Ansprechpartner", "Straße", "PLZ", "Ort", "Branche", "Signal", "Probleme", "Edition", "Zusatzmodule",
                 "Monat netto", "Einmalig netto", "p Reaktion", "EV je Brief", "Auswahl", "Begründung"]
EDITION_KUERZEL = {"basis": "T1", "pro": "T2", "premium": "T3"}


def versandfaehig(lead) -> bool:
    w = lead.werte
    return bool(w.get("strasse") and re.fullmatch(r"\d{5}", w.get("plz", "")) and w.get("ort")) and not re.search(r"postfach", w.get("strasse", ""), re.IGNORECASE)


def offene_chargen_leads(db) -> set[str]:
    return {r["lead_id"] for r in db.execute(
        "SELECT cl.lead_id FROM chargen_leads cl JOIN chargen c ON c.id=cl.charge_id WHERE c.status IN ('vorgeschlagen','freigegeben') "
        "AND cl.freigabe != 'nein'")}


def auswaehlen(kandidaten: list[dict], n: int, katalog: dict, modell: scoring.Modell, seed: int) -> list[dict]:
    """kandidaten: dicts mit lead, art, ev, branche; liefert die Charge in Rangfolge."""
    v = katalog["versand"]
    grenze = max(1, int(v["max_anteil_branche"] * n + 0.5))
    grenzwert = {"g": grenze}
    je_branche: dict[str, int] = {}
    charge: list[dict] = []

    def nimm(k: dict, auswahl: str) -> bool:
        if len(charge) >= n or any(c["lead"].id == k["lead"].id for c in charge):
            return False
        if je_branche.get(k["branche"], 0) >= grenzwert["g"] and auswahl != "Nachfass":
            return False
        je_branche[k["branche"]] = je_branche.get(k["branche"], 0) + 1
        charge.append(dict(k, auswahl=auswahl))
        return True

    for k in sorted((k for k in kandidaten if k["art"] == "Nachfassbrief"), key=lambda k: -k["ev"]):
        nimm(k, "Nachfass")
    erst = [k for k in kandidaten if k["art"] == "Erstbrief"]
    gierig_n = len(charge) + round((n - len(charge)) * (1 - katalog["modell"]["exploration"]))
    for k in sorted(erst, key=lambda k: -k["ev"]):
        if len(charge) >= gierig_n:
            break
        nimm(k, "gierig (EV)")
    rng = random.Random(seed)
    gezogen = []
    for k in erst:
        if any(c["lead"].id == k["lead"].id for c in charge):
            continue
        p = modell.p(k["lead"].id, rng)
        gezogen.append((scoring.erwartungswert(p, k["clv"], katalog), k))
    for _, k in sorted(gezogen, key=lambda x: -x[0]):
        if len(charge) >= n:
            break
        nimm(k, "Exploration (Thompson)")
    if len(charge) < n:                         # zu wenige Branchen im Pool: Grenze weich, Rest nach EV auffüllen
        grenzwert["g"] = n
        for k in sorted(erst, key=lambda k: -k["ev"]):
            if not any(c["lead"].id == k["lead"].id for c in charge):
                nimm(k, "gierig (EV, über Branchengrenze)")
    for i, k in enumerate(charge, 1):
        k["rang"] = i
    return charge


def charge_anlegen(db, charge: list[dict], heute: dt.date, parameter: dict) -> str:
    nr = db.execute("SELECT COUNT(*) FROM chargen").fetchone()[0] + 1
    cid = f"C{heute:%Y%m%d}-{nr:02d}"
    db.execute("INSERT INTO chargen VALUES (?,?,?,?,?)", (cid, heute.isoformat(), "vorgeschlagen", "Brief", json.dumps(parameter)))
    for k in charge:
        db.execute("INSERT INTO chargen_leads VALUES (?,?,?,?,?,?)", (cid, k["lead"].id, k["rang"], k["ev"], f"{k['art']} · {k['auswahl']}", ""))
    db.commit()
    return cid


def begruendung(k: dict) -> str:
    le, z = k["lead"], k["zuordnung"]
    teile = []
    if le.akut:
        teile.append("akut (frische Büroanzeige, verifiziert)")
    if le.werte.get("stelle"):
        teile.append(f"sucht {le.werte['stelle'][:60]}")
    if le.probleme:
        teile.append("belegt " + ", ".join(le.probleme))
    if z.module:
        teile.append("Upsell " + z.modul_codes)
    return "; ".join(teile)


def bogen_schreiben(pfad: Path, cid: str, charge: list[dict], ansprechpartner) -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Freigabe und Rücklauf"
    ws.append(SPALTEN_BOGEN)
    for k in charge:
        le, z = k["lead"], k["zuordnung"]
        w = le.werte
        ws.append([k["rang"], le.id, k["art"], "", "", "", "", "", le.firma, ansprechpartner(le), w.get("strasse", ""), w.get("plz", ""),
                   w.get("ort", ""), z.branche, k["signal"], ", ".join(le.probleme), z.edition.capitalize(), z.modul_codes,
                   z.monat_gesamt, z.einmalig_gesamt, round(k["p"], 4), round(k["ev"], 2), k["auswahl"], begruendung(k)])
    fett = Font(bold=True, color="FFFFFF")
    for c in ws[1]:
        c.font, c.fill, c.alignment = fett, PatternFill("solid", fgColor="1B6EC2"), Alignment(wrap_text=True, vertical="top")
    gelb = PatternFill("solid", fgColor="FFF4CC")
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=8):
        for c in row:
            c.fill = gelb
    dv_frei = DataValidation(type="list", formula1='"ja,nein"', allow_blank=True)
    dv_reakt = DataValidation(type="list", formula1='"' + ",".join(t for t in EREIGNISSE if not t.startswith("brief")) + '"', allow_blank=True)
    ws.add_data_validation(dv_frei)
    ws.add_data_validation(dv_reakt)
    dv_frei.add(f"D2:D{len(charge) + 1}")
    dv_reakt.add(f"F2:F{len(charge) + 1}")
    for col, breite in zip("ABCDEFGHIJKLMNOPQRSTUVWX", (5, 13, 13, 10, 12, 16, 13, 24, 34, 22, 24, 7, 16, 14, 15, 18, 9, 12, 10, 11, 9, 10, 20, 60)):
        ws.column_dimensions[col].width = breite
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = ws.dimensions
    hilfe = wb.create_sheet("Anleitung")
    for zeile in (
        [f"Charge {cid}: Freigabe- und Rückmeldebogen"],
        ["1. Spalte „Freigabe (ja/nein)“ ausfüllen. Nur Zeilen mit „ja“ gelten als freigegeben; es wird nichts automatisch versendet."],
        ["2. python infrastruktur/vertriebsagent.py einlesen <diese Datei> → Serienbrief-Datei und Paketliste für die freigegebenen Briefe."],
        ["3. Nach dem Einwurf „Versandt am“ (TT.MM.JJJJ) eintragen und erneut einlesen: startet die Nachfasskadenz (Brief 2 nach 14 Tagen)."],
        ["4. Reaktionen eintragen (Spalte „Reaktion“ + „Reaktionsdatum“) und einlesen: Rückkopplung in die Priorisierung aller Leads."],
        ["Reaktionswerte:"], *[[f"  {t}", b] for t, b in EREIGNISSE.items() if not t.startswith("brief")],
        ["Rechtsrahmen: Erstkontakt per Brief (§ 7 Abs. 1 UWG). Widerspruch oder „kein Interesse“ sperrt dauerhaft. Telefon/E-Mail erst nach Rückmeldung."],
    ):
        hilfe.append(zeile)
    hilfe.column_dimensions["A"].width = 30
    hilfe.column_dimensions["B"].width = 70
    pfad.parent.mkdir(parents=True, exist_ok=True)
    wb.save(pfad)


def paketliste_schreiben(pfad: Path, leads: list) -> None:
    """Leadliste im Schema der Wellen, direkt lesbar für paketagent.py (Brief, Angebot, Demo je Lead)."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(SPALTEN_WELLE)
    for le, z in leads:
        w = le.werte
        ws.append([w.get("klasse", ""), le.score, w.get("quelle", ""), le.firma, w.get("gewerk", ""), w.get("plz", ""), w.get("ort", ""),
                   w.get("strasse", ""), w.get("telefon", ""), w.get("email", ""), w.get("website", ""), w.get("entscheider", ""),
                   w.get("stelle", ""), w.get("alter", ""), w.get("infrastruktur", ""), ", ".join(le.probleme), "",
                   w.get("angebot") or EDITION_KUERZEL[z.edition], w.get("befund", ""), "freigegeben", w.get("quell_url", ""),
                   "AKUT" if le.akut else "", le.id])
    pfad.parent.mkdir(parents=True, exist_ok=True)
    wb.save(pfad)


def serienbrief_schreiben(pfad: Path, zeilen: list[dict]) -> None:
    """Serienbriefdatei (UTF-8 mit BOM, Semikolon) für Word/LibreOffice bzw. den Upload bei Briefdienstleistern."""
    felder = ["Lead-ID", "Firma", "Ansprechpartner", "Anrede", "Straße", "PLZ", "Ort", "Art", "Edition", "Zusatzmodule"]
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with pfad.open("w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=felder, delimiter=";")
        wr.writeheader()
        wr.writerows(zeilen)


def _datum(v) -> str:
    if v in (None, ""):
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    t = str(v).strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", t)
    if m:
        j = int(m.group(3))
        return dt.date(j + 2000 if j < 100 else j, int(m.group(2)), int(m.group(1))).isoformat()
    return dt.date.fromisoformat(t[:10]).isoformat()


def bogen_einlesen(db, pfad: Path, heute: dt.date) -> dict:
    """Übernimmt Freigabe, Versanddatum und Reaktionen. Idempotent (Ereignisse sind je Lead/Typ/Datum eindeutig)."""
    import openpyxl
    wb = openpyxl.load_workbook(pfad, data_only=True)
    ws = wb["Freigabe und Rücklauf"]
    kopf = [c.value for c in ws[1]]
    ix = {k: kopf.index(k) for k in SPALTEN_BOGEN if k in kopf}
    cid = re.search(r"C\d{8}-\d{2}", pfad.name)
    cid = cid.group(0) if cid else None
    bericht = {"charge": cid, "freigegeben": [], "abgelehnt": 0, "versandt": 0, "reaktionen": 0, "fehler": []}
    for r in ws.iter_rows(min_row=2, values_only=True):
        lid = r[ix["Lead-ID"]]
        if not lid:
            continue
        frei = str(r[ix["Freigabe (ja/nein)"]] or "").strip().lower()
        if cid and frei in ("ja", "nein"):
            db.execute("UPDATE chargen_leads SET freigabe=? WHERE charge_id=? AND lead_id=?", (frei, cid, lid))
        if frei == "ja":
            bericht["freigegeben"].append(lid)
        elif frei == "nein":
            bericht["abgelehnt"] += 1
        try:
            versandt = _datum(r[ix["Versandt am"]])
            if versandt:
                if frei != "ja":
                    bericht["fehler"].append(f"{lid}: Versanddatum ohne Freigabe – nicht übernommen")
                else:
                    typ = "brief_2" if str(r[ix["Art"]]).startswith("Nachfass") else "brief_1"
                    bericht["versandt"] += ereignis(db, lid, typ, versandt, quelle=pfad.name)
            reaktion = str(r[ix["Reaktion"]] or "").strip()
            if reaktion:
                datum = _datum(r[ix["Reaktionsdatum"]]) or heute.isoformat()
                bericht["reaktionen"] += ereignis(db, lid, reaktion, datum, str(r[ix["Notiz"]] or ""), pfad.name)
        except (ValueError, KeyError) as e:
            bericht["fehler"].append(f"{lid}: {e}")
    if cid:
        offen = db.execute("SELECT COUNT(*) FROM chargen_leads cl WHERE charge_id=? AND freigabe='ja' AND NOT EXISTS "
                           "(SELECT 1 FROM ereignisse e WHERE e.lead_id=cl.lead_id AND e.typ IN ('brief_1','brief_2') AND e.datum >= "
                           "(SELECT erstellt FROM chargen WHERE id=?))", (cid, cid)).fetchone()[0]
        status = "versandt" if bericht["freigegeben"] and not offen else ("freigegeben" if bericht["freigegeben"] else "vorgeschlagen")
        if not bericht["freigegeben"] and bericht["abgelehnt"]:
            status = "verworfen"
        db.execute("UPDATE chargen SET status=? WHERE id=?", (status, cid))
        bericht["status"] = status
    db.commit()
    return bericht
