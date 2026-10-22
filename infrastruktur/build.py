"""Baut Landingpage und Einseiter aus Vorlagen und stammdaten.toml nach dist/.

    python infrastruktur/build.py              # Vorschau, offene Werte als [OFFEN: …]
    python infrastruktur/build.py --strict     # Deployment: bricht bei offenen Werten ab
    python infrastruktur/build.py --pdf        # zusätzlich einseiter.pdf (Chrome/Chromium headless)

site/ und einseiter/ landen in dist/ (wird veröffentlicht), vorlagen/ in dist-vorlagen/
(Signatur, E-Mail-Texte; wird nie veröffentlicht).

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
    args = parser.parse_args()

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
