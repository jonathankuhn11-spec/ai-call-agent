"""Website-Build (Pilotseite, SEO-Dateien) und Serienbrief aus einer Leadliste."""

import csv
import subprocess
import sys
from pathlib import Path

INFRA = Path(__file__).resolve().parent.parent / "infrastruktur"


def _build(tmp_path: Path) -> Path:
    out = tmp_path / "dist"
    subprocess.run([sys.executable, str(INFRA / "build.py"), "--out", str(out),
                    "--vorlagen-out", str(tmp_path / "dist-vorlagen")], check=True, capture_output=True)
    return out


def test_build_erzeugt_pilotseite_und_seo_dateien(tmp_path):
    out = _build(tmp_path)
    for name in ["index.html", "pilot.html", "404.html", "robots.txt", "sitemap.xml", ".htaccess"]:
        assert (out / name).exists(), name
    pilot = (out / "pilot.html").read_text(encoding="utf-8")
    assert "{{" not in pilot and "Gründungspartner" in pilot
    assert "Köln" not in (out / "index.html").read_text(encoding="utf-8")


def test_serienbrief_filtert_und_fuellt(tmp_path, monkeypatch):
    vorlagen = tmp_path / "dist-vorlagen"
    _build(tmp_path)
    leads = tmp_path / "leads.csv"
    with leads.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Region", "Firmenname", "Inhaber", "PLZ", "Ort", "Quelle", "Notiz", "Widerspruch", "Straße"])
        w.writerow(["Hamm", "Bernd Schmitz e.K. Inh. Nils Holtsträter", "", "59073", "Hamm-Frielick",
                    "https://www.dasoertliche.de/x", "Priorität Welle 2", "", "Frielicker Weg 112"])
        w.writerow(["Gütersloh", "Wortmeier GmbH & Co. KG", "", "33378", "Rheda-Wiedenbrück", "", "Priorität Welle 2", "",
                    "Rietberger Str. 65"])
        w.writerow(["Hamm", "Gesperrt GmbH", "", "59065", "Hamm", "", "Priorität Welle 2", "2026-11-01", "Weg 1"])
        w.writerow(["Hamm", "Ohne Straße GmbH", "", "59065", "Hamm", "", "Priorität Welle 2", "", ""])
        w.writerow(["Hamm", "Welle drei GmbH", "", "59065", "Hamm", "", "Priorität Welle 3", "", "Weg 2"])
    sys.path.insert(0, str(INFRA))
    import serienbrief
    monkeypatch.setattr(serienbrief, "VORLAGE", vorlagen / "brief-pilot.html")
    monkeypatch.setattr(sys, "argv", ["serienbrief", str(leads), "--welle", "2", "--out", str(tmp_path / "briefe"),
                                      "--datum", "2026-11-05"])
    assert serienbrief.main() == 0
    text = (tmp_path / "briefe" / "briefe.html").read_text(encoding="utf-8")
    assert text.count('class="seite"') == 2
    assert "Guten Tag Nils Holtsträter," in text and "z. Hd. Nils Holtsträter" in text
    assert "59073 Hamm<" in text and "33378 Rheda-Wiedenbrück<" in text
    assert "Sehr geehrte Damen und Herren," in text and "5. November 2026" in text
    assert "Gesperrt" not in text and "Welle drei" not in text and "[[firma]]" not in text and "[[anrede]]" not in text
