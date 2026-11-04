"""Prüft vor und nach dem Go-live, ob Website, DNS und Postfach-Einträge stimmen.

    python infrastruktur/golive_check.py                 # Domain aus stammdaten.toml
    python infrastruktur/golive_check.py --domain kuhn-beispiel.de --hoster github

Geprüft wird: (1) Stammdaten vollständig, (2) DNS über DNS-over-HTTPS (A/AAAA für GitHub Pages bzw.
beliebig bei eigenem Hoster, CNAME www, MX, SPF, DMARC), (3) HTTPS mit gültigem Zertifikat, Umleitung von
http, Erreichbarkeit von Startseite, Pilotseite, Impressum und Datenschutz ohne [OFFEN: …]-Marker, eigene
404-Seite. Nur Standardbibliothek. Exit-Code 0 nur, wenn alle Pflichtprüfungen bestehen.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from build import stammdaten_laden

BASIS = Path(__file__).resolve().parent
GITHUB_A = {"185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"}
DOH = "https://dns.google/resolve"
SEITEN = ["/", "/pilot.html", "/impressum.html", "/datenschutz.html"]
ergebnisse: list[tuple[bool, bool, str, str]] = []  # (ok, pflicht, prüfung, hinweis)


def melden(ok: bool, pruefung: str, hinweis: str = "", pflicht: bool = True) -> None:
    ergebnisse.append((ok, pflicht, pruefung, hinweis))


def dns(name: str, typ: str) -> list[str]:
    url = f"{DOH}?{urllib.parse.urlencode({'name': name, 'type': typ})}"
    try:
        with urllib.request.urlopen(url, timeout=10) as antwort:
            daten = json.load(antwort)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    return [a["data"].strip('"').rstrip(".") for a in daten.get("Answer", []) if a.get("type") in (1, 5, 15, 16, 28)]


def abrufen(url: str, folgen: bool = True) -> tuple[int, str, str]:
    """Liefert (Status, End-URL bzw. Location, Text). Status 0 bei Netzwerk- oder Zertifikatsfehler."""
    class KeinFolgen(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    oeffner = urllib.request.build_opener() if folgen else urllib.request.build_opener(KeinFolgen)
    anfrage = urllib.request.Request(url, headers={"User-Agent": "golive-check"})
    try:
        with oeffner.open(anfrage, timeout=15) as antwort:
            return antwort.status, antwort.url, antwort.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as fehler:
        return fehler.code, fehler.headers.get("Location", ""), fehler.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as fehler:
        return 0, "", str(fehler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", help="Standard: auftritt.domain aus stammdaten.toml")
    parser.add_argument("--hoster", choices=["github", "eigener"], default="github",
                        help="github: A-Records müssen auf GitHub Pages zeigen; eigener: beliebige A-Records")
    parser.add_argument("--stammdaten", type=Path, default=BASIS / "stammdaten.toml")
    args = parser.parse_args()

    daten = stammdaten_laden(args.stammdaten)
    offen = sorted(k for k, v in daten.items() if not v)
    melden(not offen, "Stammdaten vollständig", "offen: " + ", ".join(offen) if offen else "")
    domain = (args.domain or daten.get("auftritt.domain", "")).strip().lower()
    if not domain:
        melden(False, "Domain bekannt", "auftritt.domain in stammdaten.toml eintragen oder --domain angeben")
        return bericht()

    a = set(dns(domain, "A"))
    if args.hoster == "github":
        melden(a == GITHUB_A, "A-Records auf GitHub Pages",
               f"gefunden: {', '.join(sorted(a)) or 'keine'}; erwartet: {', '.join(sorted(GITHUB_A))}")
        melden(len(dns(domain, "AAAA")) == 4, "AAAA-Records (IPv6) auf GitHub Pages", "vier 2606:50c0:800x::153", pflicht=False)
    else:
        melden(bool(a), "A-Record vorhanden", f"gefunden: {', '.join(sorted(a)) or 'keine'}")
    www = dns(f"www.{domain}", "CNAME") or dns(f"www.{domain}", "A")
    melden(bool(www), "www-Subdomain aufgelöst", ", ".join(www) or "CNAME www → Pages-Adresse bzw. Hoster setzen")
    mx = dns(domain, "MX")
    melden(bool(mx), "MX-Record (Postfach)", ", ".join(mx) or "MX laut Postfach-Anbieter setzen")
    txt = dns(domain, "TXT")
    spf = [t for t in txt if t.startswith("v=spf1")]
    melden(len(spf) == 1, "SPF genau einmal", spf[0] if spf else "TXT v=spf1 include:<anbieter> ~all setzen")
    dmarc = [t for t in dns(f"_dmarc.{domain}", "TXT") if t.startswith("v=DMARC1")]
    melden(bool(dmarc), "DMARC", dmarc[0] if dmarc else "TXT _dmarc: v=DMARC1; p=quarantine; rua=mailto:…")
    melden(False, "DKIM", "Selektor ist anbieterabhängig: per Testmail an Gmail prüfen (Original anzeigen → DKIM: PASS)", pflicht=False)

    status, ziel, _ = abrufen(f"http://{domain}/", folgen=False)
    melden(status in (301, 308) and ziel.startswith("https://"), "http leitet auf https um",
           f"Status {status} → {ziel or '–'}; bei GitHub Pages 'Enforce HTTPS' aktivieren")
    for pfad in SEITEN:
        status, _, text = abrufen(f"https://{domain}{pfad}")
        fehler = "Zertifikat oder Verbindung fehlerhaft" if status == 0 else f"Status {status}"
        ok = status == 200 and "[OFFEN:" not in text and "OFFEN-" not in text
        melden(ok, f"https://{domain}{pfad}", "" if ok else (fehler if status != 200 else "enthält [OFFEN: …]-Marker"))
    status, _, text = abrufen(f"https://{domain}/gibt-es-nicht-{id(args)}")
    melden(status == 404 and "nicht" in text, "eigene 404-Seite", f"Status {status}", pflicht=False)
    return bericht()


def bericht() -> int:
    breite = max(len(p) for _, _, p, _ in ergebnisse)
    for ok, pflicht, pruefung, hinweis in ergebnisse:
        zeichen = "✓" if ok else ("✗" if pflicht else "!")
        print(f"{zeichen} {pruefung.ljust(breite)}  {hinweis if not ok else ''}".rstrip())
    fehler = sum(1 for ok, pflicht, _, _ in ergebnisse if pflicht and not ok)
    print(f"\n{'Go-live bereit.' if not fehler else f'{fehler} Pflichtprüfung(en) offen.'}")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
