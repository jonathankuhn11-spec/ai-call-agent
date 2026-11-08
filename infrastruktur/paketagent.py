"""Paket-Agenten: erzeugt je Lead ein vollständiges Kundenpaket passend zum belegten Bedarf.

    python infrastruktur/paketagent.py LEADS.xlsx [LEADS2.xlsx …] --plattform ../plattform [--akut] [--klasse A+ A]
           [--firma TEXT] [--limit N] [--parallel 4] [--neu] [--ohne-pdf] [--demo-url https://demo.example.de] [--out DIR]

Pipeline je Lead (jeder Schritt ein eigener Agent, Module unter kundenpaket/):
  1. Lead-Agent       liest und normalisiert die Leadliste, überspringt Widersprüche und Dubletten
  2. Bedarfs-Agent    Problemcodes P1–P10, Infrastruktur, Stellenanzeige → Branchenprofil, Edition, Website-Modus, Module
  3. Instanz-Agent    legt über die Plattform-API eine KUHN-Instanz an (SQLite, Testphase, Branche, Leistungen, Buchung)
  4. Website-Agent    konfiguriert und exportiert die Betriebswebsite (inkl. Karriereseite mit der gesuchten Stelle)
  5. Angebots-Agent   Kundendatei kunde.toml + Angebot (HTML/PDF) mit Beträgen aus preise.toml
  6. Brief-Agent      personalisierter Brief (DIN 5008, 2 Seiten: Bedarfe, Website-Entwurf, Konditionen)
  7. Gesprächs-Agent  Gesprächsleitfaden mit Bedarfsfragen, Vorführablauf und Einwänden
Ergebnis: <out>/<firma-plz>/ je Lead (index.html als Einstieg) sowie <out>/Pakete_Uebersicht.xlsx und index.html.

Leaddaten und Pakete bleiben außerhalb des Repositorys (Standard-Ausgabe dist-pakete/ ist per .gitignore ausgeschlossen).
Es werden keine Webseiten abgerufen; das gemeinsame Abrufkontingent der Lead-Agenten bleibt unberührt.
Bereits erzeugte Pakete werden übersprungen (inkrementell nach jeder Leadwelle), --neu erzeugt sie erneut.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

BASIS = Path(__file__).resolve().parent
sys.path.insert(0, str(BASIS))

from kundenpaket import bedarf as bedarf_agent  # noqa: E402
from kundenpaket import dokumente, instanz  # noqa: E402
from kundenpaket import leads as lead_agent  # noqa: E402


def paket_erzeugen(lead: lead_agent.Lead, out: Path, nummer: str, heute: dt.date, plattform: Path | None, pdf: bool,
                   demo_url: str | None) -> dict:
    ziel = out / lead.schluessel
    ziel.mkdir(parents=True, exist_ok=True)
    b = bedarf_agent.analysieren(lead)
    ergebnis: dict = {"firma": lead.firma, "ort": lead.ort, "schluessel": lead.schluessel, "akut": lead.akut, "klasse": lead.klasse,
                      "score": lead.score, "branche": b.branche, "naeherung": b.branche_naeherung, "edition": b.edition,
                      "website_modus": b.website_modus, "codes": [m.code for m in b.module if m.code != "–"], "hinweise": list(b.hinweise),
                      "herkunft": lead.herkunft, "erzeugt": heute.isoformat(), "fehler": []}
    inst = None
    try:
        live = f"{demo_url.rstrip('/')}" if demo_url else None
        inst = instanz.einrichten(lead, b, ziel, plattform, live)
        ergebnis["instanz"] = inst
        dokumente.bildschirmfoto(ziel / "website" / "index.html", ziel / "website-desktop.png", 1280, 800)
        dokumente.bildschirmfoto(ziel / "website" / "index.html", ziel / "website-mobil.png", 390, 760)
    except instanz.PlattformFehlt as e:
        ergebnis["fehler"].append(str(e))
    except Exception as e:  # ein Lead darf die Welle nicht abbrechen
        ergebnis["fehler"].append(f"Instanz/Website: {e}")
        traceback.print_exc()
    konditionen = dokumente.angebot(lead, b, ziel, nummer, heute, pdf)
    ergebnis.update({"paket": konditionen.get("paket.name"), "monat": konditionen.get("paket.monat_netto"),
                     "einrichtung": konditionen.get("paket.einrichtung_netto"), "angebot_offen": konditionen.get("offen")})
    url = f"{demo_url.rstrip('/')}/web/{inst['slug']}?vorschau=1" if demo_url and inst else None
    dokumente.brief(lead, b, ziel, konditionen, heute, pdf, url)
    dokumente.gespraech(lead, b, ziel, inst, konditionen)
    dokumente.uebersicht(lead, b, ziel, ergebnis)
    return ergebnis


def gesamtuebersicht(out: Path, ergebnisse: list[dict]) -> None:
    alle = []
    for datei in sorted(out.glob("*/paket.json")):
        alle.append(json.loads(datei.read_text(encoding="utf-8")))
    alle.sort(key=lambda e: (not e.get("akut"), -(float(e.get("score") or 0))))
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Pakete"
        kopf = ["Akut", "Klasse", "Score", "Firma", "Ort", "Branchenprofil", "Edition", "Monat netto", "Einrichtung netto", "Website", "Problemcodes",
                "Paketordner", "Hinweise", "Fehler", "Erzeugt", "Quelle"]
        ws.append(kopf)
        for e in alle:
            ws.append(["AKUT" if e.get("akut") else "", e.get("klasse"), float(e["score"]) if str(e.get("score") or "").replace(".", "", 1).isdigit() else e.get("score"),
                       e["firma"], e.get("ort"), e["branche"] + (" (Näherung)" if e.get("naeherung") else ""), e.get("paket"), e.get("monat"), e.get("einrichtung"),
                       e.get("website_modus"), ", ".join(e.get("codes", [])), e["schluessel"], " | ".join(e.get("hinweise", [])), " | ".join(e.get("fehler", [])),
                       e.get("erzeugt"), e.get("herkunft")])
        for z in ws[1]:
            z.font = Font(bold=True, color="FFFFFF")
            z.fill = PatternFill("solid", fgColor="B25A26")
        for spalte, breite in zip("ABCDEFGHIJKLMNOP", (7, 7, 7, 42, 16, 22, 10, 12, 14, 12, 18, 42, 60, 30, 11, 34)):
            ws.column_dimensions[spalte].width = breite
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        wb.save(out / "Pakete_Uebersicht.xlsx")
    except ImportError:
        pass
    zeilen = "".join(f'<tr><td>{"AKUT" if e.get("akut") else ""}</td><td><a href="{html.escape(e["schluessel"])}/index.html">{html.escape(e["firma"])}</a></td>'
                     f'<td>{html.escape(e.get("ort") or "")}</td><td>{html.escape(e["branche"])}</td><td>{html.escape(e.get("paket") or "")}</td>'
                     f'<td>{html.escape(", ".join(e.get("codes", [])))}</td><td>{html.escape(" | ".join(e.get("fehler", [])))}</td></tr>' for e in alle)
    (out / "index.html").write_text(f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Kundenpakete</title><style>body{{font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;color:#1b2129;background:#fff}}
@media (prefers-color-scheme:dark){{body{{color:#e8edf2;background:#121a24}}}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #8884;padding:6px;text-align:left}}a{{color:#b25a26}}</style></head>
<body><h1>Kundenpakete ({len(alle)})</h1><table><tr><th></th><th>Firma</th><th>Ort</th><th>Branche</th><th>Edition</th><th>Bedarf</th><th>Fehler</th></tr>{zeilen}</table></body></html>""",
                                     encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("listen", nargs="+", type=Path)
    ap.add_argument("--blatt", help="Tabellenblatt (Standard: Alle Wellen/Leads/Welle …)")
    ap.add_argument("--plattform", type=Path, help="Verzeichnis plattform/ (Zweig plattform/backend-grundgeruest)")
    ap.add_argument("--akut", action="store_true", help="nur Leads mit Akut-Kennzeichen")
    ap.add_argument("--klasse", nargs="*", help="nur diese Klassen, z. B. A+ A")
    ap.add_argument("--firma", help="nur Firmen, deren Name den Text enthält")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--parallel", type=int, default=1, help="Anzahl gleichzeitiger Paket-Agenten")
    ap.add_argument("--neu", action="store_true", help="vorhandene Pakete neu erzeugen")
    ap.add_argument("--ohne-pdf", action="store_true")
    ap.add_argument("--demo-url", help="Basis-URL einer laufenden Demo-Plattform (sonst Hinweis auf Vorführung im Brief)")
    ap.add_argument("--out", type=Path, default=BASIS / "dist-pakete")
    ap.add_argument("--datum", type=dt.date.fromisoformat, default=dt.date.today())
    args = ap.parse_args()

    alle = [le for pfad in args.listen for le in lead_agent.laden(pfad, args.blatt)]
    auswahl = lead_agent.filtern(alle, args.akut, set(args.klasse) if args.klasse else None, args.firma, None)
    if not args.neu:
        auswahl = [le for le in auswahl if not (args.out / le.schluessel / "paket.json").exists()]
    if args.limit:
        auswahl = auswahl[:args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"{len(alle)} Leads gelesen, {len(auswahl)} Pakete zu erzeugen → {args.out}")
    stamm = args.datum.strftime("%Y%m%d")
    vorhandene = len(list(args.out.glob("*/paket.json")))
    auftraege = [(le, f"A-{stamm}-{vorhandene + i:03d}") for i, le in enumerate(auswahl, 1)]
    ergebnisse = []
    plattform = args.plattform.resolve() if args.plattform else None
    if args.parallel > 1:
        with ProcessPoolExecutor(args.parallel) as pool:
            laeufe = {pool.submit(paket_erzeugen, le, args.out, nr, args.datum, plattform, not args.ohne_pdf, args.demo_url): le for le, nr in auftraege}
            for f in as_completed(laeufe):
                e = f.result()
                ergebnisse.append(e)
                print(f"  ✓ {e['firma']}: {e['paket']} · {e['branche']} · {', '.join(e['codes'])}" + (f"  ⚠ {'; '.join(e['fehler'])}" if e["fehler"] else ""))
    else:
        for le, nr in auftraege:
            e = paket_erzeugen(le, args.out, nr, args.datum, plattform, not args.ohne_pdf, args.demo_url)
            ergebnisse.append(e)
            print(f"  ✓ {e['firma']}: {e['paket']} · {e['branche']} · {', '.join(e['codes'])}" + (f"  ⚠ {'; '.join(e['fehler'])}" if e["fehler"] else ""))
    gesamtuebersicht(args.out, ergebnisse)
    fehler = sum(1 for e in ergebnisse if e["fehler"])
    print(f"Fertig: {len(ergebnisse)} Pakete, {fehler} mit Hinweisen. Übersicht: {args.out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
