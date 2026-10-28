"""Selbstregistrierung: öffentliche Seite `/registrieren` und Endpunkt `POST /registrierung`.

Legt einen Mandanten mit Branche, Stammdaten und Testphase an (core.produkt), richtet ihn über core.einrichtung ein
(Leistungskatalog, Online-Buchung) und liefert die Zugangsdaten der Oberfläche. Nur aktiv mit REGISTRIERUNG_OFFEN=1.
Schutz: Honeypot-Feld `website`, Drosselung je Adresse (5 je Stunde), Pflichtfelder, E-Mail-Dublette je Name.
Mit OIDC (Keycloak) legt der Anbieter die Benutzer an; die Seite sagt das dann so."""
from __future__ import annotations

import html
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import branchen, einrichtung, produkt
from core.modelle import Mandant

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def pruefen(daten: dict[str, Any]) -> dict[str, Any]:
    name = (daten.get("name") or "").strip()
    email = (daten.get("email") or "").strip().lower()
    branche = (daten.get("branche") or "shk").strip().lower()
    if len(name) < 3:
        raise ValueError("Bitte den Namen des Betriebs angeben (mindestens 3 Zeichen)")
    if not EMAIL.match(email):
        raise ValueError("Bitte eine gültige E-Mail-Adresse angeben")
    if branche not in branchen.BRANCHEN:
        raise ValueError(f"Unbekannte Branche {branche!r}")
    if not daten.get("datenschutz"):
        raise ValueError("Bitte der Datenschutzerklärung und den Nutzungsbedingungen zustimmen")
    return {"name": name[:200], "email": email, "branche": branche, "telefon": (daten.get("telefon") or "").strip()[:40], "ort": (daten.get("ort") or "").strip()[:100],
            "inhaber": (daten.get("inhaber") or "").strip()[:200]}


def anlegen(s: Session, daten: dict[str, Any], heute: date, basis_url: str | None = None) -> dict[str, Any]:
    d = pruefen(daten)
    vorhanden = s.scalar(select(Mandant).where(Mandant.name == d["name"]))
    if vorhanden is not None and (vorhanden.stammdaten or {}).get("email") == d["email"]:
        raise ValueError("Dieser Betrieb ist bereits registriert; bitte anmelden oder den Support kontaktieren")
    m = Mandant(name=d["name"], gewerk=d["branche"].upper(), testphase_bis=heute + timedelta(days=produkt.TESTPHASE_TAGE))
    s.add(m)
    s.flush()
    stammdaten = {k: v for k, v in {"email": d["email"], "telefon": d["telefon"], "ort": d["ort"], "inhaber": d["inhaber"], "basis_url": basis_url,
                                    "registriert": heute.isoformat()}.items() if v}
    einrichtung.einrichten(s, m, {"branche": d["branche"], "stammdaten": stammdaten, "online_buchung": True}, "registrierung")
    st = produkt.status(m, heute)
    return {"mandant_id": m.id, "name": m.name, "branche": d["branche"], "edition": st["edition"], "testphase_bis": st["testphase_bis"],
            "app_url": f"{basis_url or ''}/app/#einrichtung", "hinweis": st["hinweis"]}


def seite(p: dict[str, Any], oidc: bool) -> str:
    e = html.escape
    branchen_optionen = "".join(f'<option value="{e(k)}">{e(v.bezeichnung)}</option>' for k, v in sorted(branchen.BRANCHEN.items(), key=lambda kv: kv[1].bezeichnung))
    farbe = p["farbe"] or "#0b5cad"
    editionen = "".join(f'<div class="ed"><h3>{e(v["titel"])}</h3><p class="preis">{v["preis_monat"]} € <small>/ Monat zzgl. USt</small></p>'
                        f'<ul>{"".join(f"<li>{e(f)}</li>" for f in v["features"][:8])}</ul><p class="klein">bis {v["limits"]["mitarbeiter"]} Mitarbeitende</p></div>'
                        for v in p["editionen"].values())
    zugang = ("Nach der Registrierung richtet der Anbieter Ihren Zugang (Benutzername und Passwort) ein und meldet sich per E-Mail." if oidc
              else "Sie erhalten sofort Ihre Zugangskennung und können loslegen.")
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(p["name"])} – kostenlos testen</title><meta name="robots" content="index,follow">
<style>:root{{--akzent:{e(farbe)};--text:#18212b;--hinter:#f4f6f9;--karte:#fff;--linie:#e4e8ee}}
@media (prefers-color-scheme: dark){{:root{{--text:#e8ecf1;--hinter:#0f141a;--karte:#18212b;--linie:#2b3540}}}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--text);background:var(--hinter)}}
header{{padding:28px 16px;text-align:center}}header h1{{margin:0;font-size:28px}}header p{{max-width:640px;margin:8px auto 0;color:#6b7785}}
main{{max-width:1040px;margin:0 auto;padding:0 16px 48px;display:grid;gap:24px;grid-template-columns:1fr}}@media (min-width:860px){{main{{grid-template-columns:3fr 2fr}}}}
.karte{{background:var(--karte);border:1px solid var(--linie);border-radius:14px;padding:24px;box-shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.04)}}label{{display:block;margin:10px 0 4px;font-weight:600;font-size:14px}}
input,select{{width:100%;padding:12px;font:inherit;border:1px solid var(--linie);border-radius:8px;background:var(--hinter);color:var(--text)}}
input:focus-visible,select:focus-visible{{outline:none;border-color:var(--akzent);box-shadow:0 0 0 3px color-mix(in srgb,var(--akzent) 30%,transparent)}}
button{{margin-top:16px;width:100%;padding:14px;font:inherit;font-weight:700;color:#fff;background:var(--akzent);border:0;border-radius:8px;cursor:pointer}}
.honig{{position:absolute;left:-9999px}}.ed{{border-top:1px solid var(--linie);padding:12px 0}}.ed h3{{margin:0}}.preis{{font-size:22px;margin:4px 0}}.klein,small{{color:#6b7785;font-size:13px}}
ul{{margin:6px 0;padding-left:18px;font-size:14px}}.erfolg{{background:#e7f6ec;border-color:#9ad3af}}.fehler{{color:#b42318}}code{{background:var(--hinter);padding:2px 6px;border-radius:4px}}
.logo{{max-height:48px}}</style></head><body>
<header>{f'<img class="logo" src="{e(p["logo"])}" alt="">' if p["logo"] else ""}<h1>{e(p["name"])} {p["testphase_tage"]} Tage kostenlos testen</h1>
<p>Telefonagent, Disposition, Kundenportal, Rechnungen und Buchhaltungsanbindung für Handwerk und Dienstleister. Keine Kreditkarte, keine Installation. {e(zugang)}</p></header>
<main><form class="karte" id="f" autocomplete="on"><h2>Betrieb registrieren</h2>
<label for="name">Name des Betriebs</label><input id="name" name="name" required minlength="3" placeholder="Muster Haustechnik GmbH">
<label for="branche">Branche</label><select id="branche" name="branche">{branchen_optionen}</select>
<label for="inhaber">Ansprechperson</label><input id="inhaber" name="inhaber" placeholder="Vor- und Nachname">
<label for="email">E-Mail</label><input id="email" name="email" type="email" required placeholder="info@betrieb.de">
<label for="telefon">Telefon</label><input id="telefon" name="telefon" type="tel" placeholder="0221 …">
<label for="ort">Ort</label><input id="ort" name="ort" placeholder="Köln">
<div class="honig" aria-hidden="true"><label for="website">Website</label><input id="website" name="website" tabindex="-1" autocomplete="off"></div>
<label><input type="checkbox" name="datenschutz" required style="width:auto"> Ich stimme den Nutzungsbedingungen und der Datenschutzerklärung zu.</label>
<button type="submit">Jetzt kostenlos testen</button><p class="fehler" id="fehler" role="alert"></p>
<p class="klein">Nach {p["testphase_tage"]} Tagen endet die Testphase automatisch; es entstehen keine Kosten, solange Sie keine Edition bestellen.</p></form>
<aside><div class="karte"><h2>Editionen</h2>{editionen}</div></aside></main>
<script src="/app/registrieren.js" data-oidc="{"1" if oidc else "0"}"></script></body></html>"""
