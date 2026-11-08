"""Instanz-Agent und Website-Agent: richten je Lead eine vorkonfigurierte KUHN-Instanz ein und exportieren die Betriebswebsite.

Die Instanz wird über die öffentliche API der Plattform angelegt (Selbstregistrierung, Stammdaten, Website, Rufumleitung,
Online-Buchung) – exakt der Weg, den ein Kunde selbst ginge. Ergebnis je Lead:

  kuhn-demo.db        SQLite-Datenbank mit dem Mandanten (Testphase 30 Tage, Branchenprofil, Leistungskatalog, Website,
                      Karriereseite mit der gesuchten Stelle). Starten:
                      DATABASE_URL=sqlite:///<pfad>/kuhn-demo.db python -m core serve   (im Verzeichnis plattform/)
                      → Website unter /web/<slug>?vorschau=1, Büro unter /app, Buchung unter dem Buchungslink.
  website/            statischer Export der Website (Start, Impressum, Datenschutz, Karriere) mit Entwurfshinweis,
                      noindex; Formular und Buchung verweisen auf die Live-Demo.
  agent.json          Konfiguration des Telefonagenten: Begrüßung, Rolle, Leistungen, Notdienst, Rufumleitungscodes.

Die Plattform liegt im Zweig plattform/backend-grundgeruest (bzw. dessen Nachfolgern); Pfad per --plattform oder
Umgebungsvariable KUHN_PLATTFORM. Es wird nichts im Internet abgerufen.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from kundenpaket.bedarf import FARBEN, Bedarf
from kundenpaket.leads import Lead, slug

ENTWURF = ('<div class="vorschau" style="background:#fff2e5;color:#7a3e00">Unverbindlicher Entwurf von KUHN für {firma} – nicht veröffentlicht, '
           'Texte und Bilder vorläufig.</div>')
_app_cache: dict[str, object] = {}


class PlattformFehlt(RuntimeError):
    pass


def plattform_laden(pfad: Path | None):
    """Importiert core.api der Plattform; gibt (app_erzeugen, Datenbank, TestClient) zurück."""
    kandidaten = [p for p in (pfad, Path(os.environ["KUHN_PLATTFORM"]) if os.environ.get("KUHN_PLATTFORM") else None,
                              Path(__file__).resolve().parents[2] / "plattform") if p]
    for p in kandidaten:
        if (p / "core" / "api.py").exists():
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
            os.environ.setdefault("REGISTRIERUNG_OFFEN", "1")
            from fastapi.testclient import TestClient

            from core.api import app_erzeugen
            from core.db import Datenbank
            return app_erzeugen, Datenbank, TestClient
    raise PlattformFehlt("Plattform nicht gefunden (--plattform <pfad>/plattform oder KUHN_PLATTFORM setzen)")


def _ok(r, schritt: str) -> dict:
    if r.status_code >= 400:
        raise RuntimeError(f"{schritt}: HTTP {r.status_code} {r.text[:300]}")
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


def _website_konfig(lead: Lead, bedarf: Bedarf, web_slug: str) -> dict:
    gewerk = lead.gewerk.split("(")[0].strip() or None
    ueber = (f"{lead.firma} – {gewerk or 'Ihr Fachbetrieb'} in {lead.ort}.\n"
             "Anfragen nehmen wir rund um die Uhr an: online, per Formular mit Fotos oder telefonisch über unseren digitalen Assistenten. "
             "So geht kein Anliegen verloren, und Sie wissen jederzeit, wie es weitergeht.")
    k = {"aktiv": False, "slug": web_slug, "farbe": FARBEN.get(bedarf.branche, "#1b6ec2"),
         "claim": f"{gewerk} in {lead.ort} und Umgebung" if gewerk else None, "ueber_uns": ueber,
         "oeffnungszeiten": "Anfragen rund um die Uhr – telefonisch über unseren Assistenten, online und per Formular",
         "einzugsgebiet": f"{lead.ort} und Umgebung" if lead.ort else None,
         # keine erfundenen Inhalte: Preise, Kundenstimmen, Team und Standard-FAQ bleiben aus, bis der Betrieb sie liefert
         "bereiche": {"preise": False, "bewertungen": False, "team": False, "faq": False, "notdienst": bedarf.notdienst,
                      "buchung": bedarf.edition != "basis", "karriere": True, "fotos": True},
         "notdienst_text": "Notfälle nimmt unser Telefonassistent rund um die Uhr an und verbindet mit dem Bereitschaftsdienst.",
         "impressum_zusatz": "Entwurf – Angaben vor Veröffentlichung durch den Betrieb prüfen und ergänzen."}
    if bedarf.stelle:
        k["stellen"] = [bedarf.stelle]
    return {kk: v for kk, v in k.items() if v is not None}


def _statisch(html: str, web_slug: str, firma: str, live: str | None) -> str:
    basis = f"/web/{web_slug}"
    ziel = live.rstrip("/") if live else None
    html = html.replace(f'href="{basis}/impressum"', 'href="impressum.html"').replace(f'href="{basis}/datenschutz"', 'href="datenschutz.html"')
    html = re.sub(rf'href="{re.escape(basis)}/karriere/([a-z0-9-]+)"', r'href="karriere.html#\1"', html)
    html = html.replace(f'href="{basis}/karriere"', 'href="karriere.html"').replace(f'href="{basis}/#', 'href="index.html#')
    html = html.replace(f'href="{basis}"', 'href="index.html"').replace(f'href="{basis}/"', 'href="index.html"')
    # Formular und Buchung funktionieren nur in der Live-Demo
    html = re.sub(r'action="[^"]*/anfrage"', f'action="{ziel}{basis}/anfrage"' if ziel else 'action="#" onsubmit="return false"', html)
    html = re.sub(r'href="/buchung/#[^"]*"', f'href="{ziel}/buchung/"' if ziel else 'href="index.html#kontakt"', html)
    html = re.sub(r'<div class="vorschau">.*?</div>', "", html, count=1, flags=re.S)
    return html.replace("<body>", "<body>" + ENTWURF.format(firma=firma), 1).replace("<head>", '<head><meta name="robots" content="noindex,nofollow">', 1)


def einrichten(lead: Lead, bedarf: Bedarf, ziel: Path, plattform: Path | None = None, live: str | None = None) -> dict:
    app_erzeugen, Datenbank, TestClient = plattform_laden(plattform)
    ziel.mkdir(parents=True, exist_ok=True)
    db_datei = ziel / "kuhn-demo.db"
    db_datei.unlink(missing_ok=True)
    db = Datenbank(f"sqlite:///{db_datei}")
    c = TestClient(app_erzeugen(db))
    email = lead.email if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", lead.email or "") else f"demo+{lead.schluessel}@kuhn.invalid"
    reg = _ok(c.post("/registrierung", json={"name": lead.firma, "email": email, "branche": bedarf.branche, "telefon": lead.telefon, "ort": lead.ort,
                                              "inhaber": lead.entscheider, "datenschutz": True}), "Registrierung")
    h = {"X-Mandant": reg["mandant_id"]}
    _ok(c.put("/betrieb/stammdaten", headers=h, json={"strasse": lead.strasse or None, "plz": lead.plz or None, "ort": lead.ort or None,
                                                      "telefon": lead.telefon or None, "email": email, "inhaber": lead.entscheider or None}), "Stammdaten")
    web_slug = slug(lead.firma)[:58].strip("-") or lead.schluessel
    _ok(c.put("/betrieb/website", headers=h, json=_website_konfig(lead, bedarf, web_slug)), "Website")
    if bedarf.edition != "basis":
        _ok(c.put("/betrieb/buchung?aktiv=true", headers=h), "Online-Buchung")
    rufumleitung = None
    if bedarf.rufumleitung or bedarf.module:
        rufumleitung = _ok(c.get("/betrieb/rufumleitung", headers=h), "Rufumleitung")
    kontext = _ok(c.get("/voice/kontext", headers=h), "Sprachagent-Kontext")

    seiten = {"index.html": f"/web/{web_slug}?vorschau=1", "impressum.html": f"/web/{web_slug}/impressum",
              "datenschutz.html": f"/web/{web_slug}/datenschutz", "karriere.html": f"/web/{web_slug}/karriere?vorschau=1"}
    web = ziel / "website"
    web.mkdir(exist_ok=True)
    erzeugt = []
    for datei, pfad in seiten.items():
        r = c.get(pfad)
        if r.status_code == 200:
            (web / datei).write_text(_statisch(r.text, web_slug, lead.firma, live), encoding="utf-8")
            erzeugt.append(datei)

    agent = {
        "betrieb": lead.firma, "branche": bedarf.branche, "edition": bedarf.edition,
        "begruessung": f"Guten Tag, Sie sind verbunden mit {lead.firma}. Ich bin der digitale Telefonassistent und nehme Ihr Anliegen gern auf.",
        "ki_hinweis": "Ansage weist auf den KI-Assistenten hin (Art. 50 KI-VO); Aufzeichnung nur mit Einwilligung.",
        "notdienst": {"aktiv": bedarf.notdienst, "weiterleitung_an": lead.telefon or None,
                      "regel": "Bei Gefahr (Gas, Wasser, Strom, Personen) sofort an die Bereitschaft weiterleiten, sonst Rückruf zusagen."},
        "rufumleitung": rufumleitung, "kontext": kontext,
        "demo_start": "cd plattform && DATABASE_URL=sqlite:///<Paketordner>/kuhn-demo.db python -m core serve  (Pfad absolut angeben)",
        "demo_pfade": {"website": f"/web/{web_slug}?vorschau=1", "buero": "/app/", "buchung": "/buchung/ (Link in der Website)"},
    }
    (ziel / "agent.json").write_text(json.dumps(agent, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"mandant_id": reg["mandant_id"], "slug": web_slug, "edition_testphase": reg.get("edition"), "testphase_bis": reg.get("testphase_bis"),
            "website_seiten": erzeugt, "db": db_datei.name, "leistungen": len(kontext.get("leistungen") or [])}
