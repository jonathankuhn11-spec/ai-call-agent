"""Erzeugt Serienbriefe (Pilotprogramm) aus einer Leadliste als druckfertiges PDF.

    python infrastruktur/build.py                      # rendert zuerst dist-vorlagen/brief-pilot.html
    python infrastruktur/serienbrief.py LEADS.xlsx [--welle 2] [--region Paderborn] [--limit 30] [--pdf]

Die Leadliste bleibt außerhalb des Repositorys (personenbezogene Daten). Gelesen werden die Spalten
Firmenname, Inhaber, Straße, PLZ, Ort, Quelle, Notiz, Status Kontakt und Widerspruch (xlsx oder csv; erste
Tabelle bzw. Blatt "Leads"). Zeilen mit Widerspruch, Status "kein Interesse"/"Widerspruch" oder ohne Straße
werden übersprungen und gemeldet.

Ergebnis in infrastruktur/dist-briefe/ (nie veröffentlicht, nicht versioniert):
  briefe.html      alle Briefe, je Empfänger eine A4-Seite (DIN 5008 Form B, Fensterumschlag DL/C6/5)
  briefe.pdf       mit --pdf, über Chrome/Chromium headless
  versandliste.csv Firmenname, Ort, Versanddatum: Datum in Spalte "Art.-14-Info versendet am" der Leadliste übertragen
"""

import argparse
import csv
import datetime as dt
import html
import re
import sys
from pathlib import Path

from build import pdf_drucken

BASIS = Path(__file__).resolve().parent
VORLAGE = BASIS / "dist-vorlagen" / "brief-pilot.html"
FELD = re.compile(r"\[\[([a-z_]+)\]\]")
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]
DOPPELNAMEN = ("Rheda-Wiedenbrück", "Herzebrock-Clarholz", "Schloß Holte-Stukenbrock", "Hörstel-Riesenbeck")
SPERRE = re.compile(r"kein interesse|widerspruch|nicht kontaktieren", re.I)


def zeilen_laden(pfad: Path) -> list[dict[str, str]]:
    if pfad.suffix.lower() == ".csv":
        with pfad.open(encoding="utf-8-sig", newline="") as f:
            probe = f.read(4096)
            f.seek(0)
            dialekt = csv.Sniffer().sniff(probe, delimiters=";,\t")
            return [{k.strip(): (v or "").strip() for k, v in z.items() if k} for z in csv.DictReader(f, dialect=dialekt)]
    try:
        import openpyxl
    except ImportError:
        sys.exit("Für xlsx wird openpyxl gebraucht (pip install openpyxl) oder die Liste als CSV exportieren.")
    mappe = openpyxl.load_workbook(pfad, read_only=True, data_only=True)
    blatt = mappe["Leads"] if "Leads" in mappe.sheetnames else mappe.worksheets[0]
    reihen = blatt.iter_rows(values_only=True)
    kopf = [str(k or "").strip() for k in next(reihen)]
    return [{k: ("" if v is None else str(v)).strip() for k, v in zip(kopf, reihe)} for reihe in reihen if any(reihe)]


def wert(zeile: dict[str, str], *namen: str) -> str:
    for name in namen:
        if zeile.get(name):
            return zeile[name]
    return ""


def inhaber(zeile: dict[str, str]) -> str:
    """Name der Inhaberin bzw. des Inhabers ohne Funktionszusätze; Fallback: "Inh. …" aus dem Firmennamen."""
    roh = wert(zeile, "Inhaber")
    if not roh:
        treffer = re.search(r"Inh(?:\.|aber(?:in)?)\s+([A-ZÄÖÜ][^,()]+)", wert(zeile, "Firmenname"))
        roh = treffer.group(1) if treffer else ""
    roh = re.sub(r"\(.*?\)", "", roh).split(",")[0].split(";")[0].split("/")[0]
    roh = re.sub(r"\b(GF|Geschäftsführer(?:in)?|Inhaber(?:in)?|Inh\.|vertreten durch)\b", "", roh)
    roh = " ".join(roh.split())
    return roh if 1 < len(roh.split()) <= 4 else ""


def quelle(zeile: dict[str, str]) -> str:
    q = wert(zeile, "Quelle").lower()
    teile = []
    if "impressum" in q:
        teile.append("Impressum Ihrer Website")
    if "dasoertliche" in q or "örtliche" in q:
        teile.append("Das Örtliche")
    if "gelbeseiten" in q:
        teile.append("Gelbe Seiten")
    return ", ".join(teile) or "öffentliches Branchenverzeichnis"


def postort(ort: str) -> str:
    """Gemeindename für die Anschrift: Ortsteil nach Bindestrich entfällt (Paderborn-Wewer → Paderborn)."""
    for name in DOPPELNAMEN:
        if ort.startswith(name):
            return name
    return ort.split("-")[0].strip()


def datum_heute(heute: dt.date) -> str:
    return f"{heute.day}. {MONATE[heute.month - 1]} {heute.year}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("leads", type=Path, help="Leadliste (.xlsx oder .csv), liegt außerhalb des Repositorys")
    parser.add_argument("--welle", help="nur Zeilen, deren Notiz 'Welle N' enthält, z. B. 1 oder 2")
    parser.add_argument("--region", help="Teilzeichenkette in Region oder Ort, z. B. Paderborn")
    parser.add_argument("--limit", type=int, help="höchstens so viele Briefe (Versand in Tranchen)")
    parser.add_argument("--datum", help="Briefdatum als JJJJ-MM-TT (Standard: heute)")
    parser.add_argument("--pdf", action="store_true", help="zusätzlich briefe.pdf erzeugen")
    parser.add_argument("--out", type=Path, default=BASIS / "dist-briefe")
    args = parser.parse_args()

    if not VORLAGE.exists():
        sys.exit(f"{VORLAGE} fehlt: zuerst python infrastruktur/build.py ausführen.")
    vorlage = VORLAGE.read_text(encoding="utf-8")
    anfang, ende = vorlage.index("<!-- BRIEF-ANFANG -->"), vorlage.index("<!-- BRIEF-ENDE -->")
    kopf, block, fuss = vorlage[:anfang], vorlage[anfang:ende], vorlage[ende:]
    if "[OFFEN:" in vorlage:
        print("Warnung: stammdaten.toml ist unvollständig, die Briefe enthalten [OFFEN: …]-Marker (nur Probedruck).",
              file=sys.stderr)

    heute = dt.date.fromisoformat(args.datum) if args.datum else dt.date.today()
    briefe, versand, uebersprungen = [], [], []
    for zeile in zeilen_laden(args.leads):
        firma = wert(zeile, "Firmenname")
        if not firma:
            continue
        notiz = wert(zeile, "Notiz")
        if args.welle and f"Welle {args.welle}" not in notiz:
            continue
        if args.region and args.region.lower() not in (wert(zeile, "Region") + " " + wert(zeile, "Ort")).lower():
            continue
        if wert(zeile, "Widerspruch") or SPERRE.search(wert(zeile, "Status Kontakt") + " " + wert(zeile, "Ergebnis")):
            uebersprungen.append((firma, "Widerspruch bzw. kein Interesse"))
            continue
        strasse = wert(zeile, "Straße", "Strasse", "Anschrift")
        if not strasse or not wert(zeile, "PLZ"):
            uebersprungen.append((firma, "keine Straße/PLZ in der Liste"))
            continue
        name = inhaber(zeile)
        felder = {
            "firma": re.sub(r",?\s+Inh\.\s.*$", "", firma) if name else firma, "strasse": strasse, "plz": wert(zeile, "PLZ"), "ort": postort(wert(zeile, "Ort")),
            "datum": datum_heute(heute), "quelle": quelle(zeile),
            "anrede": f"Guten Tag {name}," if name else "Sehr geehrte Damen und Herren,",
        }
        escaped = {k: html.escape(v) for k, v in felder.items()}
        escaped["zu_haenden"] = f"z. Hd. {html.escape(name)}<br>" if name else ""
        briefe.append(FELD.sub(lambda m: escaped[m.group(1)], block))
        versand.append((firma, felder["ort"], heute.isoformat()))
        if args.limit and len(briefe) >= args.limit:
            break

    for firma, grund in uebersprungen:
        print(f"  übersprungen: {firma}: {grund}")
    if not briefe:
        print("Keine Briefe erzeugt (Filter prüfen).", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    ziel = args.out / "briefe.html"
    ziel.write_text(kopf + "".join(briefe) + fuss, encoding="utf-8")
    with (args.out / "versandliste.csv").open("w", encoding="utf-8", newline="") as f:
        schreiber = csv.writer(f, delimiter=";")
        schreiber.writerow(["Firmenname", "Ort", "Art.-14-Info versendet am"])
        schreiber.writerows(versand)
    if args.pdf:
        pdf_drucken(ziel, ziel.with_suffix(".pdf"))
    print(f"{len(briefe)} Briefe nach {args.out}" + (" (inkl. PDF)" if args.pdf else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
