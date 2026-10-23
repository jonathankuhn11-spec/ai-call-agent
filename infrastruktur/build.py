"""Baut Landingpage und Einseiter aus Vorlagen und stammdaten.toml nach dist/.

    python infrastruktur/build.py              # Vorschau, offene Werte als [OFFEN: …]
    python infrastruktur/build.py --strict     # Deployment: bricht bei offenen Werten ab
    python infrastruktur/build.py --pdf        # zusätzlich einseiter.pdf (Chrome/Chromium headless)

site/ und einseiter/ landen in dist/ (wird veröffentlicht), vorlagen/ in dist-vorlagen/
(Signatur, E-Mail-Texte; wird nie veröffentlicht).

    python infrastruktur/build.py --unterlagen [--kunde unterlagen/kunden/<betrieb>.toml] [--pdf]

rendert zusätzlich die Kundenunterlagen (Verträge, Angebot, Rechnung, Onboarding, Betrieb) nach
dist-unterlagen/<kunde>/. Dafür werden neben stammdaten.toml auch unterlagen/geschaeftsdaten.toml
(Bank, Steuer; nicht versioniert, Vorlage: geschaeftsdaten.vorlage.toml), unterlagen/preise.toml und
die Kundendatei geladen. Ohne --kunde dient unterlagen/kunden/vorlage.toml, alle Kundenwerte bleiben offen.
Unterlagen werden nie veröffentlicht und nie mit --strict geprüft.

Platzhalter in den Vorlagen haben die Form {{abschnitt.schluessel}}. Ein Platzhalter,
der in stammdaten.toml nicht existiert, ist immer ein Fehler (Tippfehler-Schutz).
"""

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

BASIS = Path(__file__).resolve().parent
PLATZHALTER = re.compile(r"\{\{\s*([a-z_]+\.[a-z0-9_]+)\s*\}\}")
QUELLEN = ["site", "einseiter"]
TEXTFORMATE = {".html", ".txt", ".md", ".csv"}
CHROME_KANDIDATEN = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


def stammdaten_laden(pfad: Path) -> dict[str, str]:
    with pfad.open("rb") as f:
        roh = tomllib.load(f)
    return {f"{abschnitt}.{k}": str(v).strip() for abschnitt, werte in roh.items() for k, v in werte.items()}


def rendern(text: str, daten: dict[str, str], offen: set[str], unbekannt: set[str], markup: bool = True) -> str:
    def ersetzen(m: re.Match) -> str:
        schluessel = m.group(1)
        if schluessel not in daten:
            unbekannt.add(schluessel)
            return m.group(0)
        if not daten[schluessel]:
            offen.add(schluessel)
            if not markup:
                return f"[OFFEN: {schluessel}]"
            davor = m.string[: m.start()]
            if davor.rfind("<") > davor.rfind(">"):  # innerhalb eines Tags (Attribut): kein Markup
                return f"OFFEN-{schluessel}"
            return f'<mark class="offen">[OFFEN: {schluessel}]</mark>'
        return html.escape(daten[schluessel]) if markup else daten[schluessel]

    return PLATZHALTER.sub(ersetzen, text)


def toml_laden(pfad: Path) -> dict:
    with pfad.open("rb") as f:
        return tomllib.load(f)


def flach(roh: dict) -> dict[str, str]:
    return {f"{abschnitt}.{k}": ("" if v is None else str(v)).strip()
            for abschnitt, werte in roh.items() if isinstance(werte, dict)
            for k, v in werte.items() if not isinstance(v, dict)}


def euro(betrag: float) -> str:
    text = f"{betrag:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def zahl(wert: str) -> float | None:
    try:
        return float(str(wert).replace(",", "."))
    except ValueError:
        return None


def abgeleitet(daten: dict[str, str], preise: dict) -> dict[str, str]:
    """Berechnet Paket-, Angebots- und Rechnungsbeträge; was nicht berechenbar ist, bleibt offen ("")."""
    ust = float(preise["allgemein"]["umsatzsteuer_prozent"]) / 100
    minute = float(preise["allgemein"]["mehrminute_netto"])
    paket = preise["pakete"].get(daten.get("kunde.paket", "").lower())
    leer = {k: "" for k in ("paket.name", "paket.monat_netto", "paket.einrichtung_netto", "paket.minuten",
                            "angebot.einrichtung_netto", "angebot.einrichtung_ust", "angebot.einrichtung_brutto",
                            "angebot.monat_ust", "angebot.monat_brutto", "angebot.nachlass",
                            "rechnung.pos_monat", "rechnung.pos_mehrminuten", "rechnung.pos_einrichtung",
                            "rechnung.netto", "rechnung.ust", "rechnung.brutto")}
    if not paket:
        return leer
    einrichtung = float(paket["einrichtung_netto"])
    nachlass = zahl(daten.get("angebot.nachlass_einrichtung_prozent", "")) or 0.0
    einrichtung_angebot = round(einrichtung * (1 - nachlass / 100), 2)
    monat = float(paket["monat_netto"])
    werte = {
        "paket.name": paket["name"], "paket.monat_netto": euro(monat),
        "paket.einrichtung_netto": euro(einrichtung), "paket.minuten": str(paket["minuten"]),
        "angebot.nachlass": f"{nachlass:g} %".replace(".", ","),
        "angebot.einrichtung_netto": euro(einrichtung_angebot),
        "angebot.einrichtung_ust": euro(einrichtung_angebot * ust),
        "angebot.einrichtung_brutto": euro(einrichtung_angebot * (1 + ust)),
        "angebot.monat_ust": euro(monat * ust), "angebot.monat_brutto": euro(monat * (1 + ust)),
    }
    mehrminuten = zahl(daten.get("rechnung.mehrminuten", "")) or 0.0
    mit_einrichtung = daten.get("rechnung.mit_einrichtung", "").lower() in ("true", "ja", "1")
    netto = monat + mehrminuten * minute + (einrichtung_angebot if mit_einrichtung else 0.0)
    werte.update({
        "rechnung.pos_monat": euro(monat),
        "rechnung.pos_mehrminuten": f"{mehrminuten:g} × {euro(minute)} = {euro(mehrminuten * minute)}",
        "rechnung.pos_einrichtung": euro(einrichtung_angebot) if mit_einrichtung else "entfällt",
        "rechnung.netto": euro(netto), "rechnung.ust": euro(netto * ust), "rechnung.brutto": euro(netto * (1 + ust)),
    })
    return {**leer, **werte}


def unterlagen_bauen(args, daten: dict[str, str]) -> int:
    quelle = BASIS / "unterlagen"
    geschaeft = quelle / "geschaeftsdaten.toml"
    if not geschaeft.exists():
        geschaeft = quelle / "geschaeftsdaten.vorlage.toml"
    kunde_datei = args.kunde or quelle / "kunden" / "vorlage.toml"
    preise = toml_laden(quelle / "preise.toml")
    daten = {**daten, **flach(toml_laden(geschaeft)), **flach(toml_laden(kunde_datei))}
    daten.update(abgeleitet(daten, preise))
    ziel = args.unterlagen_out / kunde_datei.stem
    if ziel.exists():
        shutil.rmtree(ziel)
    offen: set[str] = set()
    unbekannt: set[str] = set()
    html_dateien = []
    for datei in sorted(quelle.rglob("*")):
        rel = datei.relative_to(quelle)
        if datei.is_dir() or rel.parts[0] == "kunden" or datei.suffix == ".toml":
            continue
        ausgabe = ziel / rel
        ausgabe.parent.mkdir(parents=True, exist_ok=True)
        if datei.suffix in TEXTFORMATE:
            text = rendern(datei.read_text(encoding="utf-8"), daten, offen, unbekannt, markup=datei.suffix == ".html")
            ausgabe.write_text(text, encoding="utf-8")
            if datei.suffix == ".html":
                html_dateien.append(ausgabe)
        else:
            shutil.copy2(datei, ausgabe)
    if unbekannt:
        print("Unbekannte Platzhalter in unterlagen/ (Tippfehler?): " + ", ".join(sorted(unbekannt)), file=sys.stderr)
        return 2
    if args.pdf:
        for html_datei in html_dateien:
            pdf_drucken(html_datei, html_datei.with_suffix(".pdf"))
    if offen:
        print(f"Unterlagen: {len(offen)} offene Werte (erscheinen als [OFFEN: …]): " + ", ".join(sorted(offen)))
    print(f"Unterlagen gebaut nach {ziel}")
    return 0


def chrome_finden() -> str | None:
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    return next((p for p in map(shutil.which, CHROME_KANDIDATEN) if p), None)


def pdf_drucken(html_datei: Path, pdf_datei: Path) -> None:
    chrome = chrome_finden()
    if not chrome:
        sys.exit("Kein Chrome/Chromium gefunden (Umgebungsvariable CHROME setzen).")
    subprocess.run(
        [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_datei}", html_datei.as_uri()],
        check=True, capture_output=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true", help="offene Stammdaten sind ein Fehler")
    parser.add_argument("--pdf", action="store_true", help="einseiter.pdf erzeugen")
    parser.add_argument("--stammdaten", type=Path, default=BASIS / "stammdaten.toml")
    parser.add_argument("--out", type=Path, default=BASIS / "dist")
    parser.add_argument("--vorlagen-out", type=Path, default=BASIS / "dist-vorlagen")
    parser.add_argument("--unterlagen", action="store_true", help="zusätzlich Kundenunterlagen rendern (nie veröffentlicht)")
    parser.add_argument("--kunde", type=Path, help="Kundendatei unter unterlagen/kunden/ (Standard: vorlage.toml)")
    parser.add_argument("--unterlagen-out", type=Path, default=BASIS / "dist-unterlagen")
    args = parser.parse_args()
    if (args.kunde or args.unterlagen) and args.strict:
        parser.error("--unterlagen und --kunde sind mit --strict nicht kombinierbar (Unterlagen werden nie veröffentlicht)")

    daten = stammdaten_laden(args.stammdaten)
    offen: set[str] = set()
    unbekannt: set[str] = set()
    ziele = [(verzeichnis, args.out) for verzeichnis in QUELLEN] + [("vorlagen", args.vorlagen_out)]
    for ausgabe in {ziel for _, ziel in ziele}:
        if ausgabe.exists():
            shutil.rmtree(ausgabe)
        ausgabe.mkdir(parents=True)

    for verzeichnis, ausgabe in ziele:
        for quelle in sorted((BASIS / verzeichnis).iterdir()):
            ziel = ausgabe / quelle.name
            if quelle.suffix in TEXTFORMATE:
                text = rendern(quelle.read_text(encoding="utf-8"), daten, offen, unbekannt, markup=quelle.suffix == ".html")
                ziel.write_text(text, encoding="utf-8")
            else:
                shutil.copy2(quelle, ziel)

    if unbekannt:
        print("Unbekannte Platzhalter (Tippfehler?): " + ", ".join(sorted(unbekannt)), file=sys.stderr)
        return 2
    if offen:
        meldung = "Offene Stammdaten: " + ", ".join(sorted(offen))
        if args.strict:
            print(meldung + "\nDeployment abgebrochen: stammdaten.toml vollständig ausfüllen.", file=sys.stderr)
            return 1
        # GitHub-Actions-Annotation, lokal einfach eine Zeile
        print(f"::warning title=Stammdaten unvollständig::{meldung}")

    if args.pdf:
        pdf_drucken(args.out / "einseiter.html", args.out / "einseiter.pdf")

    print(f"Gebaut nach {args.out} ({len(list(args.out.iterdir()))} Dateien)")
    if args.unterlagen or args.kunde:
        return unterlagen_bauen(args, daten)
    return 0


if __name__ == "__main__":
    sys.exit(main())
