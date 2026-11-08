"""Betriebswebsite je Mandant: serverseitig gerenderte, öffentliche Seite ohne JavaScript und ohne Build-Schritt.

Inhalt kommt aus den Daten, die der Betrieb ohnehin pflegt: Stammdaten (Anschrift, Telefon, Öffnungszeiten), Leistungskatalog
(Preise optional), Online-Buchung, veröffentlichte Kundenbewertungen, Team, Notdienst, Impressum und Datenschutz aus den
Stammdaten. Einstellungen liegen in `stammdaten["website"]` (Slug, Claim, Über uns, Farbe, Logo, Bereiche, eigene Domain).

Aufruf unter `/web/<slug>`; mit eigener Domain (CNAME auf den Server, Caddy on-demand TLS, Prüfendpunkt `/web/domain-pruefen`)
auch unter `https://www.ihr-betrieb.de/`. SEO: Title/Description, Open Graph, JSON-LD LocalBusiness mit AggregateRating,
sitemap.xml und robots.txt. Kontaktformular ohne JavaScript (Honeypot, Drosselung) → core/anfragen. Rechtstexte sind Entwürfe
aus den Stammdaten (§ 5 DDG, Art. 13 DSGVO) und vor Veröffentlichung juristisch zu prüfen.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import anfragen, branchen, buchung, hinweise, karriere, portal
from core.domaene import als_utc
from core.modelle import Artikel, Ereignis, Mandant, Ressource

BEREICHE = ("leistungen", "preise", "buchung", "bewertungen", "team", "notdienst", "anfahrt", "kontakt", "ablauf", "faq", "karriere", "fotos")
STANDARD = {"aktiv": False, "slug": None, "domain": None, "claim": None, "ueber_uns": None, "farbe": "#1b6ec2", "logo_url": None, "bild_url": None,
            "oeffnungszeiten": "Mo–Fr 7:30–17:00 Uhr", "einzugsgebiet": None, "notdienst_telefon": None, "notdienst_text": "24 Stunden erreichbar, auch am Wochenende.",
            "bereiche": {"leistungen": True, "preise": False, "buchung": True, "bewertungen": True, "team": False, "notdienst": True, "anfahrt": True, "kontakt": True, "ablauf": True, "faq": True,
                         "karriere": True, "fotos": True},
            "impressum_zusatz": None, "bewertungen_min_sterne": 4, "leistungen_text": {}, "faq": [], "stellen": []}
# Standardfragen, wenn der Betrieb keine eigenen pflegt; {ort}/{telefon} werden ersetzt
FAQ_STANDARD = [
    ("Wie schnell bekomme ich einen Termin?", "Notdienste am selben Tag, Wartungen meist innerhalb von zwei Wochen. Online sehen Sie freie Zeiten sofort und buchen mit zwei Klicks."),
    ("Was kostet die Anfahrt?", "Die Anfahrt im Einzugsgebiet ist in unseren Pauschalen enthalten oder wird vorab genannt – keine versteckten Kosten."),
    ("Bekomme ich vorher einen Festpreis?", "Für Wartungen und viele Standardarbeiten ja. Bei Reparaturen nennen wir nach der Diagnose den Preis, bevor wir weiterarbeiten."),
    ("Wie kann ich bezahlen?", "Per Überweisung (QR-Code auf der Rechnung), online im Kundenportal oder nach Absprache vor Ort."),
    ("Erinnern Sie mich an die nächste Wartung?", "Ja, auf Wunsch per E-Mail, SMS oder WhatsApp – Sie entscheiden, über welchen Weg."),
]
ABLAUF = [("Anfragen", "Online buchen, anrufen oder das Formular nutzen – auch außerhalb der Bürozeiten nimmt unser Telefonassistent Ihr Anliegen auf."),
          ("Termin bestätigen", "Sie bekommen einen Terminvorschlag und sehen in Ihrem Kundenportal jederzeit, wo Ihr Auftrag steht."),
          ("Erledigt und erklärt", "Wir arbeiten sauber, erklären, was gemacht wurde, und Sie erhalten Rapport und Rechnung digital.")]
FARBE = re.compile(r"^#[0-9a-fA-F]{6}$")
SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,58}[a-z0-9])?$")
DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_e = html.escape
MAX_FOTOS, MAX_FOTO = 3, 10 * 1024 * 1024


def foto_mime(inhalt: bytes) -> str | None:
    """MIME-Typ aus der Dateisignatur (JPEG, PNG, WebP, HEIC/HEIF vom iPhone); None bei allem anderen."""
    if inhalt.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if inhalt.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if inhalt[:4] == b"RIFF" and inhalt[8:12] == b"WEBP":
        return "image/webp"
    if inhalt[4:8] == b"ftyp" and inhalt[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic"
    return None


def slug_aus(name: str) -> str:
    s = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s or "betrieb")[:60]


def konfig(m: Mandant) -> dict[str, Any]:
    w = (m.stammdaten or {}).get("website") or {}
    k = {**STANDARD, **w, "bereiche": {**STANDARD["bereiche"], **(w.get("bereiche") or {})}}
    k["slug"] = k["slug"] or slug_aus(m.name)
    return k


def pruefen(daten: dict[str, Any], s: Session, m: Mandant, heute: date | None = None) -> dict[str, Any]:
    """Validiert Website-Einstellungen; liefert das zu speichernde Teilobjekt (nur bekannte Schlüssel)."""
    aus: dict[str, Any] = {}
    for k in ("aktiv",):
        if k in daten and daten[k] is not None:
            aus[k] = bool(daten[k])
    if daten.get("slug") is not None:
        slug = slug_aus(daten["slug"]) if not SLUG.match(daten["slug"]) else daten["slug"]
        if not SLUG.match(slug):
            raise ValueError("Slug: 3–60 Zeichen, Kleinbuchstaben, Ziffern, Bindestrich")
        anderer = mandant_nach_slug(s, slug)
        if anderer is not None and anderer.id != m.id:
            raise ValueError(f"Slug „{slug}“ ist bereits vergeben")
        aus["slug"] = slug
    if "domain" in daten:
        d = (daten["domain"] or "").strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
        if d and not DOMAIN.match(d):
            raise ValueError("Domain ungültig (z. B. www.ihr-betrieb.de)")
        if d:
            anderer = mandant_nach_domain(s, d)
            if anderer is not None and anderer.id != m.id:
                raise ValueError(f"Domain {d} ist bereits einem anderen Betrieb zugeordnet")
        aus["domain"] = d or None
    if daten.get("farbe"):
        if not FARBE.match(daten["farbe"]):
            raise ValueError("Farbe als Hexwert, z. B. #1b6ec2")
        aus["farbe"] = daten["farbe"].lower()
    for k, n in (("claim", 160), ("ueber_uns", 3000), ("oeffnungszeiten", 200), ("einzugsgebiet", 200), ("notdienst_telefon", 40), ("notdienst_text", 300), ("impressum_zusatz", 2000)):
        if k in daten:
            aus[k] = (daten[k] or "").strip()[:n] or None
    if "faq" in daten:
        faq = []
        for e in (daten["faq"] or [])[:12]:
            frage, antwort = (e.get("frage") or "").strip()[:200], (e.get("antwort") or "").strip()[:1000]
            if frage and antwort:
                faq.append({"frage": frage, "antwort": antwort})
        aus["faq"] = faq
    for k in ("logo_url", "bild_url"):
        if k in daten:
            u = (daten[k] or "").strip()
            if u and not (u.startswith("https://") or u.startswith("/")):
                raise ValueError(f"{k}: nur https-Adressen oder eigene Pfade")
            aus[k] = u[:500] or None
    if daten.get("bereiche") is not None:
        aus["bereiche"] = {k: bool(v) for k, v in daten["bereiche"].items() if k in BEREICHE}
    if daten.get("bewertungen_min_sterne") is not None:
        aus["bewertungen_min_sterne"] = max(1, min(5, int(daten["bewertungen_min_sterne"])))
    if "stellen" in daten:
        aus["stellen"] = karriere.stellen_pruefen(daten["stellen"], ((m.stammdaten or {}).get("website") or {}).get("stellen"), heute or date.today())
    if daten.get("leistungen_text") is not None:
        aus["leistungen_text"] = {str(k)[:40]: str(v)[:300] for k, v in dict(daten["leistungen_text"]).items()}
    return aus


def speichern(s: Session, m: Mandant, daten: dict[str, Any], heute: date | None = None) -> dict[str, Any]:
    neu = pruefen(daten, s, m, heute)
    alt = (m.stammdaten or {}).get("website") or {}
    if "bereiche" in neu:
        neu["bereiche"] = {**(alt.get("bereiche") or {}), **neu["bereiche"]}
    m.stammdaten = {**(m.stammdaten or {}), "website": {**alt, **neu}}
    s.flush()
    return konfig(m)


# --- Auflösung Slug/Domain (öffentlich, ohne Mandantenkopf) -----------------------------------------------
def _alle(s: Session) -> list[Mandant]:
    return s.scalars(select(Mandant)).all()


def mandant_nach_slug(s: Session, slug: str) -> Mandant | None:
    slug = slug.lower()
    for m in _alle(s):
        if konfig(m)["slug"] == slug:
            return m
    return None


def mandant_nach_domain(s: Session, host: str) -> Mandant | None:
    host = host.lower().split(":")[0]
    for m in _alle(s):
        d = konfig(m).get("domain")
        if d and (host == d or host == d.removeprefix("www.") or host == f"www.{d}"):
            return m
    return None


def basis(m: Mandant) -> str:
    """Öffentliche Adresse der Website (eigene Domain vor Pfad unter basis_url)."""
    k = konfig(m)
    if k.get("domain"):
        return f"https://{k['domain']}"
    b = ((m.stammdaten or {}).get("basis_url") or "").rstrip("/")
    return f"{b}/web/{k['slug']}" if b else f"/web/{k['slug']}"


def status(s: Session, m: Mandant) -> dict[str, Any]:
    k = konfig(m)
    st = m.stammdaten or {}
    b = branchen.branche(m)
    fehlend = [t for ok, t in ((st.get("strasse") and st.get("plz") and st.get("ort"), "Anschrift"), (st.get("telefon"), "Telefon"), (st.get("email"), "E-Mail"),
                               (st.get("inhaber"), "Inhaber/in (Impressum)"), (k.get("claim"), "Claim"), (k.get("ueber_uns"), "Über uns")) if not ok]
    return {"konfig": k, "url": basis(m), "pfad": f"/web/{k['slug']}", "vorschau": f"/web/{k['slug']}?vorschau=1", "fehlend": fehlend,
            "bewertungen": _bewertungen(s, m, k, alle=True), "leistungen": len(branchen.leistungen(s, m)) or len(b.leistungen), "buchung": bool(st.get("buchung_token")),
            "branche": b.bezeichnung, "domain_hinweis": "DNS: CNAME (oder A/AAAA) der Domain auf den Server; Caddy stellt das Zertifikat automatisch aus (on-demand TLS, HOSTING.md)."}


# --- Inhalte --------------------------------------------------------------------------------------------
def _bewertungen(s: Session, m: Mandant, k: dict[str, Any], alle: bool = False) -> dict[str, Any]:
    b = portal.bewertungen(s, m, 200)
    texte = []
    for e in s.scalars(select(Ereignis).where(Ereignis.mandant_id == m.id, Ereignis.typ == "bewertung.abgegeben").order_by(Ereignis.zeit.desc()).limit(200)):
        p = e.payload or {}
        if not p.get("text"):
            continue
        eintrag = {"id": e.id, "sterne": p.get("sterne"), "text": p["text"], "name": _kurzname(e.akteur_name), "zeit": als_utc(e.zeit).date().isoformat(), "veroeffentlicht": bool(p.get("veroeffentlicht"))}
        if alle or (eintrag["veroeffentlicht"] and (p.get("sterne") or 0) >= k["bewertungen_min_sterne"]):
            texte.append(eintrag)
    return {"anzahl": b["anzahl"], "durchschnitt": b["durchschnitt"], "texte": texte[:12 if not alle else 200]}


def _kurzname(name: str | None) -> str:
    if not name:
        return "Kunde"
    teile = name.split()
    return f"{teile[0]} {teile[-1][0]}." if len(teile) > 1 else teile[0]


def veroeffentlichen(s: Session, m: Mandant, ereignis_id: int, wert: bool) -> dict[str, Any]:
    e = s.get(Ereignis, ereignis_id)
    if e is None or e.mandant_id != m.id or e.typ != "bewertung.abgegeben":
        raise ValueError("Bewertung nicht gefunden")
    e.payload = {**(e.payload or {}), "veroeffentlicht": wert}
    s.flush()
    return {"id": e.id, "veroeffentlicht": wert}


def _brutto(a: Artikel) -> str:
    return f"{(Decimal(a.verkauf_netto) * (1 + Decimal(a.ust_satz) / 100)).quantize(Decimal('0.01')):.2f}".replace(".", ",")


def _quali(schluessel: str) -> str:
    from core.qualifikationen import QUALIFIKATIONEN
    e = QUALIFIKATIONEN.get(schluessel)
    return e[0].split(" (")[0] if e else schluessel.replace("_", " ").capitalize()


def daten(s: Session, m: Mandant, heute: date | None = None) -> dict[str, Any]:
    k, st, b = konfig(m), m.stammdaten or {}, branchen.branche(m)
    personen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv, Ressource.art == "person").order_by(Ressource.name)).all()
    texte = k.get("leistungen_text") or {}
    leistungen = [{"nummer": a.nummer, "bezeichnung": a.bezeichnung, "text": texte.get(a.nummer), "dauer": a.dauer_minuten,
                   "preis": _brutto(a) if k["bereiche"]["preise"] else None} for a in branchen.leistungen(s, m)]
    if not leistungen:   # noch kein Leistungskatalog angelegt → Standardkatalog der Branche (ohne Preise)
        leistungen = [{"nummer": le.nummer, "bezeichnung": le.bezeichnung, "text": texte.get(le.nummer), "dauer": le.dauer_minuten, "preis": None} for le in b.leistungen]
    return {"konfig": k, "branche": b, "stammdaten": st, "leistungen": leistungen, "bewertungen": _bewertungen(s, m, k),
            "team": [{"name": r.name.split(" (")[0], "qualifikationen": [_quali(q) for q in (r.qualifikationen or [])]} for r in personen],
            "whatsapp": portal.whatsapp_link(st.get("whatsapp_nummer"), f"Guten Tag, ich habe eine Frage an {m.name}."),
            "buchung_pfad": f"/buchung/#{st['buchung_token']}" if st.get("buchung_token") and k["bereiche"]["buchung"] and b.online_buchung else None,
            "basis": basis(m), "stellen": karriere.offene(m, heute or _heute()) if k["bereiche"].get("karriere", True) else []}


def _heute() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Berlin")).date()


# --- Rendering ------------------------------------------------------------------------------------------
def _css(farbe: str) -> str:
    return f"""
:root {{ --akzent: {farbe}; --tinte: #18212b; --leise: #5f6b78; --rand: #e4e8ee; --flaeche: #f4f6f9; --karte: #fff; --schatten: 0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.04); }}
@media (prefers-color-scheme: dark) {{ :root {{ --tinte: #e8edf2; --leise: #9aa8b5; --rand: #2c3a48; --flaeche: #0f1620; --karte: #182230; }} }}
* {{ box-sizing: border-box; }} html {{ scroll-behavior: smooth; }}
body {{ margin: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--tinte); background: var(--flaeche); line-height: 1.55; font-size: 17px; }}
a {{ color: var(--akzent); }} img {{ max-width: 100%; }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding: 0 20px; }}
header.kopf {{ background: var(--karte); border-bottom: 1px solid var(--rand); position: sticky; top: 0; z-index: 5; }}
header.kopf .wrap {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 64px; flex-wrap: wrap; }}
.marke {{ display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 1.15rem; text-decoration: none; color: var(--tinte); }}
.marke img {{ height: 40px; }}
nav.haupt a {{ margin-left: 16px; text-decoration: none; color: var(--tinte); font-weight: 500; }}
.knopf {{ display: inline-flex; align-items: center; gap: 8px; min-height: 46px; padding: 10px 20px; border-radius: 10px; background: var(--akzent); color: #fff !important; text-decoration: none; font-weight: 600; border: 1px solid var(--akzent); }}
.knopf.leise {{ background: transparent; color: var(--akzent) !important; }}
.hero {{ padding: 56px 0 40px; background: linear-gradient(180deg, color-mix(in srgb, var(--akzent) 10%, var(--flaeche)), var(--flaeche)); }}
.hero h1 {{ font-size: clamp(1.8rem, 4vw, 2.8rem); margin: 0 0 10px; line-height: 1.15; }}
.hero p.claim {{ font-size: 1.2rem; color: var(--leise); margin: 0 0 22px; max-width: 640px; }}
.hero .aktionen {{ display: flex; gap: 12px; flex-wrap: wrap; }}
.hero .bild {{ margin-top: 28px; border-radius: 16px; overflow: hidden; border: 1px solid var(--rand); }}
section {{ padding: 44px 0; }} section h2 {{ font-size: 1.6rem; margin: 0 0 18px; }}
.karten {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
.karte {{ background: var(--karte); border: 1px solid var(--rand); border-radius: 14px; padding: 20px; box-shadow: var(--schatten); transition: transform .12s, border-color .12s; }} .karte:hover {{ border-color: color-mix(in srgb, var(--akzent) 40%, var(--rand)); }}
.karte h3 {{ margin: 0 0 6px; font-size: 1.05rem; }} .karte p {{ margin: 0; color: var(--leise); }} .karte .preis {{ margin-top: 10px; font-weight: 600; color: var(--tinte); }}
.notdienst {{ background: color-mix(in srgb, var(--akzent) 12%, var(--karte)); border: 1px solid var(--akzent); border-radius: 14px; padding: 20px; display: flex; gap: 16px; align-items: center; justify-content: space-between; flex-wrap: wrap; }}
.sterne {{ color: #f2b01e; letter-spacing: 2px; }}
.bewertung {{ font-style: italic; }} .bewertung small {{ display: block; font-style: normal; color: var(--leise); margin-top: 8px; }}
form.kontakt {{ display: grid; gap: 12px; grid-template-columns: 1fr 1fr; }} form.kontakt label {{ display: flex; flex-direction: column; gap: 4px; font-size: .95rem; }}
form.kontakt label.breit {{ grid-column: 1 / -1; }} form.kontakt input, form.kontakt textarea {{ font: inherit; padding: 11px; border: 1px solid var(--rand); border-radius: 10px; background: var(--karte); color: var(--tinte); }}
.hinweis {{ padding: 12px 14px; border-radius: 10px; background: #e3f3ea; color: #1f7a4d; }}
.falle {{ position: absolute; left: -9999px; }}
footer {{ border-top: 1px solid var(--rand); padding: 24px 0 40px; color: var(--leise); font-size: .9rem; }} footer a {{ color: var(--leise); }}
.vorschau {{ background: #fff2e5; color: #b4541a; text-align: center; padding: 8px; font-size: .9rem; }}
.skip {{ position: absolute; left: -9999px; }} .skip:focus {{ left: 12px; top: 12px; background: var(--karte); padding: 8px 12px; z-index: 10; }}
a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible {{ outline: none; box-shadow: 0 0 0 3px color-mix(in srgb, var(--akzent) 30%, transparent); border-color: var(--akzent); }}
.schritte {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); counter-reset: s; }}
.schritte .karte {{ position: relative; padding-left: 64px; }} .schritte .karte::before {{ counter-increment: s; content: counter(s); position: absolute; left: 18px; top: 18px; width: 32px; height: 32px; border-radius: 50%; background: var(--akzent); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; }}
details.faq {{ background: var(--karte); border: 1px solid var(--rand); border-radius: 12px; padding: 4px 16px; margin-bottom: 8px; }} details.faq summary {{ cursor: pointer; font-weight: 600; padding: 10px 0; }} details.faq p {{ margin: 0 0 12px; color: var(--leise); }}
.knopf.wa {{ background: #25d366; border-color: #25d366; }} .kontaktwege {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }}
nav.haupt {{ display: inline; }} nav.haupt a {{ white-space: nowrap; }} button.knopf {{ font: inherit; font-weight: 600; cursor: pointer; }}
a.karte.stelle {{ display: block; color: inherit; text-decoration: none; }} a.karte.stelle:hover, a.karte.stelle:focus-visible {{ border-color: {farbe}; }}
.hinweis.notfall {{ background: #fff4e5; color: #7a3e00; }} input[type=file] {{ padding: 8px 0; }}
@media (max-width: 640px) {{ form.kontakt {{ grid-template-columns: 1fr; }} nav.haupt {{ display: none; }} }}
"""


def _kopf(d: dict[str, Any], titel: str, beschreibung: str, pfad_basis: str, json_ld: dict[str, Any] | None = None, noindex: bool = False,
          kanonisch: str = "/") -> str:
    k, st, m_name = d["konfig"], d["stammdaten"], d["name"]
    ld = f'<script type="application/ld+json">{json.dumps(json_ld, ensure_ascii=False)}</script>' if json_ld else ""
    logo = f'<img src="{_e(k["logo_url"])}" alt="">' if k.get("logo_url") else ""
    start = pfad_basis.rstrip("/") + "/"
    nav = "".join(f'<a href="{start}#{a}">{t}</a>' for a, t in (("leistungen", "Leistungen"), ("bewertungen", "Bewertungen"), ("kontakt", "Kontakt")) if k["bereiche"].get(a, True))
    if d.get("stellen"):
        nav += f'<a href="{start}karriere">Karriere</a>'

    knopf = f'<a class="knopf" href="{_e(d["buchung_pfad"])}">Termin buchen</a>' if d.get("buchung_pfad") else (f'<a class="knopf" href="tel:{_e(st["telefon"])}">Anrufen</a>' if st.get("telefon") else "")
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<title>{_e(titel)}</title><meta name="description" content="{_e(beschreibung)}">{'<meta name="robots" content="noindex">' if noindex else ''}
<meta property="og:title" content="{_e(titel)}"><meta property="og:description" content="{_e(beschreibung)}"><meta property="og:type" content="website"><meta property="og:url" content="{_e(d["basis"])}{_e(kanonisch)}">{f'<meta property="og:image" content="{_e(k["bild_url"])}">' if k.get("bild_url") else ''}
<link rel="canonical" href="{_e(d["basis"])}{_e(kanonisch)}"><link rel="icon" href="data:,"><style>{_css(k["farbe"])}</style>{ld}</head><body>
<a class="skip" href="#inhalt">Zum Inhalt springen</a>
<header class="kopf"><div class="wrap"><a class="marke" href="{pfad_basis}">{logo}{_e(m_name)}</a><div><nav class="haupt">{nav}</nav> {knopf}</div></div></header>"""


def _fuss(d: dict[str, Any], pfad_basis: str) -> str:
    st = d["stammdaten"]
    imp = st.get("impressum_url") or f"{pfad_basis}/impressum"
    ds = st.get("datenschutz_url") or f"{pfad_basis}/datenschutz"
    return f"""<footer><div class="wrap">© {datetime.now().year} {_e(d["name"])}{f" · {_e(st['strasse'])}, {_e(st.get('plz', ''))} {_e(st.get('ort', ''))}" if st.get("strasse") else ""} {f' · <a href="{pfad_basis}/karriere">Karriere</a>' if d.get("stellen") else ""} · <a href="{_e(imp)}">Impressum</a> · <a href="{_e(ds)}">Datenschutz</a></div></footer></body></html>"""


def _json_ld(d: dict[str, Any]) -> dict[str, Any]:
    st, k, bw = d["stammdaten"], d["konfig"], d["bewertungen"]
    ld: dict[str, Any] = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": d["name"], "url": f"{d['basis']}/", "telephone": st.get("telefon"), "email": st.get("email"),
                          "address": {"@type": "PostalAddress", "streetAddress": st.get("strasse"), "postalCode": st.get("plz"), "addressLocality": st.get("ort"), "addressCountry": "DE"},
                          "openingHours": k.get("oeffnungszeiten"), "areaServed": k.get("einzugsgebiet"), "image": k.get("bild_url") or k.get("logo_url"), "priceRange": "€€"}
    if st.get("lat") is not None and st.get("lon") is not None:
        ld["geo"] = {"@type": "GeoCoordinates", "latitude": st["lat"], "longitude": st["lon"]}
    if bw["anzahl"] and k["bereiche"]["bewertungen"]:
        ld["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": bw["durchschnitt"], "reviewCount": bw["anzahl"], "bestRating": 5}
    if d["leistungen"]:
        ld["makesOffer"] = [{"@type": "Offer", "itemOffered": {"@type": "Service", "name": le["bezeichnung"]}, **({"price": le["preis"].replace(",", "."), "priceCurrency": "EUR"} if le["preis"] else {})} for le in d["leistungen"][:20]]
    return {kk: v for kk, v in ld.items() if v not in (None, "", {})}


def startseite(s: Session, m: Mandant, gesendet: bool | int = False, vorschau: bool = False, fehler: str | None = None, heute: date | None = None) -> str:
    """gesendet: 1 = Anfrage eingegangen, 2 = als Notfall eingegangen (zeigt zusätzlich die Notdienstnummer)."""
    d = {**daten(s, m, heute), "name": m.name}
    k, st, b, bw = d["konfig"], d["stammdaten"], d["branche"], d["bewertungen"]
    pfad = "" if k.get("domain") else f"/web/{k['slug']}"
    gebiet = k.get("einzugsgebiet") or (f"in {st['ort']} und Umgebung" if st.get("ort") else "")
    claim = k.get("claim") or f"{b.bezeichnung} {gebiet}".strip()
    beschreibung = f"{m.name}: {claim}. " + (f"Online-Termin buchen oder anrufen: {st['telefon']}." if st.get("telefon") else "Jetzt Termin anfragen.")
    teile = [_kopf(d, f"{m.name} – {b.bezeichnung}{' ' + st['ort'] if st.get('ort') else ''}", beschreibung[:160], pfad or "/", _json_ld(d), noindex=vorschau or not k["aktiv"])]
    if vorschau or not k["aktiv"]:
        teile.append('<div class="vorschau">Vorschau – die Website ist noch nicht veröffentlicht (Ansicht „Website“ im Büro).</div>')
    aktionen = (f'<a class="knopf" href="{_e(d["buchung_pfad"])}">Online Termin buchen</a>' if d.get("buchung_pfad") else "") + \
               (f'<a class="knopf leise" href="tel:{_e(st["telefon"])}">{_e(st["telefon"])}</a>' if st.get("telefon") else "") + \
               (f'<a class="knopf wa" href="{_e(d["whatsapp"])}" rel="noopener">WhatsApp</a>' if d.get("whatsapp") else "") + f'<a class="knopf leise" href="{pfad}/#kontakt">Anfrage senden</a>'
    teile.append(f'<main id="inhalt"><section class="hero"><div class="wrap"><h1>{_e(m.name)}</h1><p class="claim">{_e(claim)}</p><div class="aktionen">{aktionen}</div>'
                 + (f'<div class="bild"><img src="{_e(k["bild_url"])}" alt="{_e(m.name)}"></div>' if k.get("bild_url") else "") + "</div></section>")
    if k["bereiche"]["notdienst"] and (k.get("notdienst_telefon") or st.get("telefon")):
        tel = k.get("notdienst_telefon") or st["telefon"]
        teile.append(f'<section class="wrap"><div class="notdienst"><div><strong>Notdienst</strong><br>{_e(k.get("notdienst_text") or "")}</div><a class="knopf" href="tel:{_e(tel)}">Notdienst anrufen: {_e(tel)}</a></div></section>')
    if d["stellen"]:
        karten = "".join(f'<a class="karte stelle" href="{pfad}/karriere/{_e(st_["id"])}"><h3>{_e(st_["titel"])}</h3><p>{_e(karriere.ARTEN[st_["art"]][0])}{" · " + _e(st_["ort"]) if st_.get("ort") else ""}</p></a>'
                         for st_ in d["stellen"][:3])
        teile.append(f'<section id="wir-stellen-ein"><div class="wrap"><h2>Wir stellen ein</h2><div class="karten">{karten}</div><p><a href="{pfad}/karriere">Alle Stellen und Kurzbewerbung in zwei Minuten</a></p></div></section>')
    if k["bereiche"]["leistungen"] and d["leistungen"]:
        karten = "".join(f'<div class="karte"><h3>{_e(le["bezeichnung"])}</h3><p>{_e(le["text"] or (f"ca. {le['dauer']} Minuten" if le["dauer"] else ""))}</p>{f"<div class=\'preis\'>ab {_e(le['preis'])} €</div>" if le["preis"] else ""}</div>' for le in d["leistungen"])
        teile.append(f'<section id="leistungen"><div class="wrap"><h2>Leistungen</h2><div class="karten">{karten}</div></div></section>')
    if k.get("ueber_uns"):
        absaetze = "".join(f"<p>{_e(a)}</p>" for a in k["ueber_uns"].split("\n") if a.strip())
        teile.append(f'<section id="ueber-uns"><div class="wrap"><h2>Über uns</h2>{absaetze}</div></section>')
    if k["bereiche"]["team"] and d["team"]:
        karten = "".join(f'<div class="karte"><h3>{_e(t["name"])}</h3><p>{_e(", ".join(t["qualifikationen"]))}</p></div>' for t in d["team"])
        teile.append(f'<section id="team"><div class="wrap"><h2>Unser Team</h2><div class="karten">{karten}</div></div></section>')
    if k["bereiche"]["bewertungen"] and bw["anzahl"]:
        sterne = "★" * round(bw["durchschnitt"]) + "☆" * (5 - round(bw["durchschnitt"]))
        karten = "".join(f'<div class="karte bewertung"><span class="sterne">{"★" * (t["sterne"] or 0)}</span><br>„{_e(t["text"])}“<small>{_e(t["name"])}, {t["zeit"][:7]}</small></div>' for t in bw["texte"])
        teile.append(f'<section id="bewertungen"><div class="wrap"><h2>Kundenstimmen</h2><p><span class="sterne">{sterne}</span> {bw["durchschnitt"]} von 5 aus {bw["anzahl"]} Bewertung{"en" if bw["anzahl"] != 1 else ""} – gesammelt über unser Kundenportal nach erledigten Aufträgen.</p><div class="karten">{karten}</div></div></section>')
    if k["bereiche"]["anfahrt"] and st.get("strasse"):
        teile.append(f'<section id="anfahrt"><div class="wrap"><h2>Öffnungszeiten und Anfahrt</h2><div class="karten"><div class="karte"><h3>Öffnungszeiten</h3><p>{_e(k.get("oeffnungszeiten") or "")}</p></div>'
                     f'<div class="karte"><h3>Adresse</h3><p>{_e(st["strasse"])}<br>{_e(st.get("plz", ""))} {_e(st.get("ort", ""))}</p></div>'
                     + (f'<div class="karte"><h3>Einzugsgebiet</h3><p>{_e(k["einzugsgebiet"])}</p></div>' if k.get("einzugsgebiet") else "") + "</div></div></section>")
    if k["bereiche"].get("ablauf", True):
        teile.append('<section id="ablauf"><div class="wrap"><h2>So läuft es ab</h2><div class="schritte">' + "".join(f'<div class="karte"><h3>{_e(t)}</h3><p>{_e(x)}</p></div>' for t, x in ABLAUF) + "</div></div></section>")
    if k["bereiche"].get("faq", True):
        fragen = [(f["frage"], f["antwort"]) for f in (k.get("faq") or [])] or FAQ_STANDARD
        teile.append('<section id="faq"><div class="wrap"><h2>Häufige Fragen</h2>' + "".join(f'<details class="faq"><summary>{_e(fr)}</summary><p>{_e(an)}</p></details>' for fr, an in fragen) + "</div></section>")
    if k["bereiche"]["kontakt"]:
        wege = (f'<a class="knopf leise" href="tel:{_e(st["telefon"])}">Anrufen: {_e(st["telefon"])}</a>' if st.get("telefon") else "") + \
               (f'<a class="knopf wa" href="{_e(d["whatsapp"])}" rel="noopener">Per WhatsApp schreiben</a>' if d.get("whatsapp") else "") + \
               (f'<a class="knopf leise" href="mailto:{_e(st["email"])}">E-Mail: {_e(st["email"])}</a>' if st.get("email") else "")
        not_tel = k.get("notdienst_telefon") or st.get("telefon")
        meldung = ('<p class="hinweis notfall">Ihre Notfallmeldung ist eingegangen und wurde sofort an unseren Bereitschaftsdienst weitergeleitet.'
                   + (f' Bei Gefahr für Personen oder Gebäude rufen Sie bitte zusätzlich direkt an: <a href="tel:{_e(not_tel)}">{_e(not_tel)}</a>.' if not_tel else "") + '</p>') if gesendet == 2 else \
                  '<p class="hinweis">Vielen Dank, Ihre Anfrage ist eingegangen. Wir melden uns schnellstmöglich.</p>' if gesendet else (f'<p class="hinweis" style="background:#fde8e8;color:#9b1c1c">{_e(fehler)}</p>' if fehler else "")
        teile.append(f'''<section id="kontakt"><div class="wrap"><h2>Anfrage senden</h2><p>Am schnellsten erreichen Sie uns so:</p><div class="kontaktwege">{wege}</div><p>Oder schreiben Sie uns hier – wir antworten in der Regel am selben Werktag.</p>{meldung}<form class="kontakt" method="post" action="{pfad}/anfrage" enctype="multipart/form-data">
<label>Name <input name="name" required maxlength="200"></label><label>Telefon <input name="telefon" type="tel" maxlength="40"></label>
<label>E-Mail <input name="email" type="email" maxlength="200"></label><label>Straße, PLZ, Ort <input name="adresse" maxlength="300"></label>
<label class="breit">Ihr Anliegen <textarea name="nachricht" rows="4" required maxlength="3000"></textarea></label>
{f'<label class="breit">Fotos (optional, bis zu {MAX_FOTOS}: Schaden, Typenschild, Einbausituation) <input name="fotos" type="file" accept="image/jpeg,image/png,image/webp,image/heic" multiple></label>' if k["bereiche"].get("fotos", True) else ""}
{'<label class="breit"><span><input type="checkbox" name="notfall" value="1"> Notfall (z. B. Wasserrohrbruch, Heizungsausfall bei Frost) – wir alarmieren sofort den Bereitschaftsdienst</span></label>' if k["bereiche"]["notdienst"] else ""}
<label class="falle" aria-hidden="true">Website <input name="website" tabindex="-1" autocomplete="off"></label>
<div class="breit"><button class="knopf" type="submit">Anfrage senden</button> <small>Mit dem Absenden stimmen Sie der Verarbeitung Ihrer Angaben zur Bearbeitung der Anfrage zu (<a href="{pfad}/datenschutz">Datenschutz</a>).</small></div></form></div></section>''')
    teile.append("</main>" + _fuss(d, pfad))
    return "".join(teile)


def impressum(s: Session, m: Mandant) -> str:
    d = {**daten(s, m), "name": m.name}
    k, st = d["konfig"], d["stammdaten"]
    pfad = "" if k.get("domain") else f"/web/{k['slug']}"
    zeilen = [f"<p><strong>{_e(m.name)}</strong><br>{_e(st.get('inhaber') or '[Inhaber/in eintragen]')}<br>{_e(st.get('strasse') or '[Straße]')}<br>{_e(st.get('plz') or '')} {_e(st.get('ort') or '[Ort]')}</p>",
              f"<p>Telefon: {_e(st.get('telefon') or '[Telefon]')}<br>E-Mail: {_e(st.get('email') or '[E-Mail]')}</p>"]
    if st.get("ust_id"):
        zeilen.append(f"<p>Umsatzsteuer-Identifikationsnummer gemäß § 27a UStG: {_e(st['ust_id'])}</p>")
    elif st.get("kleinunternehmer"):
        zeilen.append("<p>Kleinunternehmer nach § 19 UStG; es wird keine Umsatzsteuer ausgewiesen.</p>")
    if st.get("handwerksrolle"):
        zeilen.append(f"<p>Eintragung in der Handwerksrolle: {_e(st['handwerksrolle'])}</p>")
    if k.get("impressum_zusatz"):
        zeilen.append("".join(f"<p>{_e(z)}</p>" for z in k["impressum_zusatz"].split("\n") if z.strip()))
    zeilen.append("<p>Verbraucherstreitbeilegung: Wir sind nicht bereit und nicht verpflichtet, an Streitbeilegungsverfahren vor einer Verbraucherschlichtungsstelle teilzunehmen (§ 36 VSBG).</p>")
    inhalt = f'<section><div class="wrap"><h2>Impressum</h2><p><small>Angaben gemäß § 5 DDG. Entwurf aus den Stammdaten; vor Veröffentlichung prüfen.</small></p>{"".join(zeilen)}</div></section>'
    return _kopf(d, f"Impressum – {m.name}", f"Impressum von {m.name}", pfad or "/", noindex=True) + inhalt + _fuss(d, pfad)


def datenschutz(s: Session, m: Mandant) -> str:
    d = {**daten(s, m), "name": m.name}
    k, st = d["konfig"], d["stammdaten"]
    pfad = "" if k.get("domain") else f"/web/{k['slug']}"
    inhalt = f"""<section><div class="wrap"><h2>Datenschutzerklärung</h2><p><small>Entwurf nach Art. 13 DSGVO aus den Stammdaten; vor Veröffentlichung juristisch prüfen.</small></p>
<h3>Verantwortlicher</h3><p>{_e(m.name)}, {_e(st.get('inhaber') or '')}, {_e(st.get('strasse') or '')}, {_e(st.get('plz') or '')} {_e(st.get('ort') or '')}, E-Mail {_e(st.get('email') or '')}.</p>
<h3>Server-Logdaten</h3><p>Beim Aufruf dieser Website verarbeitet unser Server technisch notwendige Daten (IP-Adresse, Zeitpunkt, aufgerufene Seite, Browser) zur Auslieferung und Sicherheit (Art. 6 Abs. 1 lit. f DSGVO); Löschung nach spätestens 14 Tagen. Es werden keine Cookies zu Analyse- oder Werbezwecken gesetzt und keine Drittanbieter-Skripte geladen.</p>
<h3>Kontaktformular und Online-Terminbuchung</h3><p>Ihre Angaben (Name, Kontaktdaten, Anliegen, Adresse, gewünschter Termin) verarbeiten wir zur Bearbeitung Ihrer Anfrage und zur Vertragsdurchführung (Art. 6 Abs. 1 lit. b DSGVO). Sie werden in unserer Kundenverwaltung gespeichert und nach den gesetzlichen Aufbewahrungsfristen gelöscht.</p>
<h3>Kundenportal, Terminerinnerungen, Bewertungen</h3><p>Für bestehende Aufträge erhalten Sie einen persönlichen Portal-Link. Erinnerungen per E-Mail oder SMS und die Bitte um eine Bewertung versenden wir nur mit Ihrer Einwilligung (Art. 6 Abs. 1 lit. a DSGVO), die Sie jederzeit widerrufen können. Bewertungen veröffentlichen wir nur gekürzt mit Vorname und Initial.</p>
<h3 id="bewerbungen">Bewerbungen</h3><p>Angaben und Unterlagen aus Bewerbungen verarbeiten wir zur Durchführung des Bewerbungsverfahrens (Art. 6 Abs. 1 lit. b, Art. 88 DSGVO). Nach Abschluss des Verfahrens löschen wir sie nach sechs Monaten, sofern kein Arbeitsverhältnis zustande kommt (Nachweis nach dem AGG). Nur wenn Sie zusätzlich in die Aufnahme in unseren Talentpool einwilligen (Art. 6 Abs. 1 lit. a DSGVO), speichern wir sie bis zu zwei Jahre; die Einwilligung können Sie jederzeit widerrufen.</p>
<h3>Telefonassistent</h3><p>Anrufe kann ein KI-gestützter Telefonassistent entgegennehmen; er stellt sich zu Beginn als solcher vor. Eine Aufzeichnung erfolgt nur nach Ihrer ausdrücklichen Zustimmung im Gespräch.</p>
<h3>Auftragsverarbeitung und Hosting</h3><p>Hosting in Deutschland; Dienstleister sind durch Auftragsverarbeitungsverträge nach Art. 28 DSGVO gebunden.</p>
<h3>Ihre Rechte</h3><p>Auskunft, Berichtigung, Löschung, Einschränkung, Datenübertragbarkeit, Widerspruch (Art. 15–21 DSGVO) sowie Beschwerde bei einer Aufsichtsbehörde. Wenden Sie sich an die oben genannte Adresse.</p></div></section>"""
    return _kopf(d, f"Datenschutz – {m.name}", f"Datenschutzerklärung von {m.name}", pfad or "/", noindex=True) + inhalt + _fuss(d, pfad)


def sitemap(m: Mandant, heute: date | None = None) -> str:
    b = basis(m)
    pfade = ["/", "/impressum", "/datenschutz"]
    stellen = karriere.offene(m, heute or _heute()) if konfig(m)["bereiche"].get("karriere", True) else []
    if stellen:
        pfade += ["/karriere", *(f"/karriere/{st['id']}" for st in stellen)]
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{_e(b)}{p}</loc></url>" for p in pfade) + "</urlset>")


# --- Karriereseite (core.karriere) ------------------------------------------------------------------------
def _stelle_karte(pfad: str, st_: dict[str, Any]) -> str:
    zeile = " · ".join(x for x in (karriere.ARTEN[st_["art"]][0], st_.get("ort"), st_.get("stunden"), st_.get("start") and f"ab {st_['start']}") if x)
    gehalt = f'<div class="preis">{_e(st_["gehalt"])}</div>' if st_.get("gehalt") else ""
    return f'<a class="karte stelle" href="{pfad}/karriere/{_e(st_["id"])}"><h3>{_e(st_["titel"])}</h3><p>{_e(zeile)}</p>{gehalt}</a>'


def _bewerbungsformular(pfad: str, st_: dict[str, Any] | None, fehler: str | None) -> str:
    sid = f'<input type="hidden" name="stelle_id" value="{_e(st_["id"])}">' if st_ else ""
    return f'''<section id="bewerben"><div class="wrap"><h2>{"Jetzt bewerben" if st_ else "Initiativ bewerben"}</h2>
<p>Kein Anschreiben nötig: Name, Kontakt und ein paar Sätze reichen. Lebenslauf oder Zeugnisse können Sie anhängen, müssen aber nicht.</p>
{f'<p class="hinweis" style="background:#fde8e8;color:#9b1c1c">{_e(fehler)}</p>' if fehler else ""}
<form class="kontakt" method="post" action="{pfad}/karriere/bewerbung" enctype="multipart/form-data">{sid}
<label>Name <input name="name" required maxlength="200" autocomplete="name"></label><label>Telefon <input name="telefon" type="tel" maxlength="40" autocomplete="tel"></label>
<label>E-Mail <input name="email" type="email" maxlength="200" autocomplete="email"></label><label>Frühester Start <input name="start" maxlength="60" placeholder="z. B. sofort, 01.03."></label>
<label class="breit">Ein paar Sätze zu Ihnen (optional) <textarea name="nachricht" rows="3" maxlength="3000"></textarea></label>
<label class="breit">Unterlagen (optional, bis zu {karriere.MAX_UNTERLAGEN} Dateien als PDF, JPEG oder PNG) <input name="unterlagen" type="file" accept="application/pdf,image/jpeg,image/png" multiple></label>
<label class="falle" aria-hidden="true">Website <input name="website" tabindex="-1" autocomplete="off"></label>
<label class="breit"><span><input type="checkbox" name="datenschutz" value="1" required> Ich habe die <a href="{pfad}/datenschutz#bewerbungen">Datenschutzhinweise zur Bewerbung</a> gelesen.</span></label>
<label class="breit"><span><input type="checkbox" name="talentpool" value="1"> Freiwillig: Meine Bewerbung darf bis zu zwei Jahre für künftige Stellen gespeichert werden (jederzeit widerrufbar).</span></label>
<div class="breit"><button class="knopf" type="submit">Bewerbung absenden</button></div></form></div></section>'''


def karriere_seite(s: Session, m: Mandant, heute: date | None = None, gesendet: bool = False, fehler: str | None = None) -> str:
    d = {**daten(s, m, heute), "name": m.name}
    k, st = d["konfig"], d["stammdaten"]
    pfad = "" if k.get("domain") else f"/web/{k['slug']}"
    ort = st.get("ort") or ""
    teile = [_kopf(d, f"Karriere bei {m.name}{' in ' + ort if ort else ''}", f"Offene Stellen bei {m.name}{' in ' + ort if ort else ''}: "
                   + (", ".join(x["titel"] for x in d["stellen"][:4]) or "Initiativbewerbung") + ". Kurzbewerbung ohne Anschreiben.", pfad or "/",
                   noindex=not k["aktiv"], kanonisch="/karriere")]
    teile.append(f'<main id="inhalt"><section class="hero"><div class="wrap"><h1>Karriere bei {_e(m.name)}</h1><p class="claim">'
                 + ("Wir suchen Verstärkung für unser Team." if d["stellen"] else "Derzeit keine ausgeschriebenen Stellen – wir freuen uns über Initiativbewerbungen.") + "</p></div></section>")
    if gesendet:
        teile.append('<section class="wrap"><p class="hinweis">Vielen Dank für Ihre Bewerbung! Wir melden uns in der Regel innerhalb weniger Werktage.</p></section>')
    if d["stellen"]:
        teile.append(f'<section id="stellen"><div class="wrap"><h2>Offene Stellen</h2><div class="karten">{"".join(_stelle_karte(pfad, x) for x in d["stellen"])}</div></div></section>')
    teile.append(_bewerbungsformular(pfad, None, fehler))
    teile.append("</main>" + _fuss(d, pfad))
    return "".join(teile)


def stelle_seite(s: Session, m: Mandant, stelle_id: str, heute: date | None = None, fehler: str | None = None) -> str | None:
    d = {**daten(s, m, heute), "name": m.name}
    st_ = next((x for x in d["stellen"] if x["id"] == stelle_id), None)
    if st_ is None:
        return None
    k, st = d["konfig"], d["stammdaten"]
    pfad = "" if k.get("domain") else f"/web/{k['slug']}"
    ort = st_.get("ort") or st.get("ort") or ""
    zeile = " · ".join(x for x in (karriere.ARTEN[st_["art"]][0], ort, st_.get("stunden"), st_.get("start") and f"Start {st_['start']}", st_.get("gehalt")) if x)
    teile = [_kopf(d, f"{st_['titel']} ({karriere.ARTEN[st_['art']][0]}) – {m.name}{' ' + ort if ort else ''}",
                   f"{st_['titel']} bei {m.name}{' in ' + ort if ort else ''}. {(st_.get('text') or '')[:100]} Kurzbewerbung in zwei Minuten.", pfad or "/",
                   karriere.job_posting(d, st_), noindex=not k["aktiv"], kanonisch=f"/karriere/{st_['id']}")]
    teile.append(f'<main id="inhalt"><section class="hero"><div class="wrap"><p><a href="{pfad}/karriere">← Alle Stellen</a></p><h1>{_e(st_["titel"])}</h1><p class="claim">{_e(zeile)}</p>'
                 f'<div class="aktionen"><a class="knopf" href="#bewerben">Jetzt bewerben</a>'
                 + (f'<a class="knopf leise" href="tel:{_e(st["telefon"])}">Fragen? {_e(st["telefon"])}</a>' if st.get("telefon") else "") + "</div></div></section>")
    if st_.get("text"):
        teile.append(f'<section><div class="wrap">{"".join(f"<p>{_e(a)}</p>" for a in st_["text"].split(chr(10)) if a.strip())}</div></section>')
    for schl, titel in (("aufgaben", "Ihre Aufgaben"), ("profil", "Ihr Profil"), ("wir_bieten", "Das bieten wir")):
        if st_.get(schl):
            teile.append(f'<section><div class="wrap"><h2>{titel}</h2><ul>{"".join(f"<li>{_e(x)}</li>" for x in st_[schl])}</ul></div></section>')
    teile.append(_bewerbungsformular(pfad, st_, fehler))
    teile.append("</main>" + _fuss(d, pfad))
    return "".join(teile)


def robots(m: Mandant) -> str:
    k = konfig(m)
    return f"User-agent: *\n{'Allow' if k['aktiv'] else 'Disallow'}: /\nSitemap: {basis(m)}/sitemap.xml\n"


# --- Kontaktformular ----------------------------------------------------------------------------------------
def anfrage(s: Session, m: Mandant, form: dict[str, Any], jetzt: datetime, fotos: list[tuple[str, bytes]] | None = None) -> dict[str, Any]:
    """Kontaktformular: legt Anfrage/Servicefall an, hängt Fotos an den Vorgang und meldet, ob als Notfall eingestuft."""
    from core.einsatz import anhang_anlegen
    from core.modelle import Servicefall
    if form.get("website"):
        return {"status": "angenommen", "notdienst": False}   # Honeypot
    name, nachricht = (form.get("name") or "").strip(), (form.get("nachricht") or "").strip()
    if not name or len(nachricht) < 5:
        raise ValueError("Bitte Name und Anliegen angeben")
    telefon, email = (form.get("telefon") or "").strip() or None, (form.get("email") or "").strip() or None
    if not (telefon or email):
        raise ValueError("Bitte Telefon oder E-Mail angeben, damit wir antworten können")
    fotos = [(n, b) for n, b in (fotos or []) if b]
    if len(fotos) > MAX_FOTOS:
        raise ValueError(f"Bitte höchstens {MAX_FOTOS} Fotos anhängen")
    geprueft = []
    for n, b in fotos:
        mime = foto_mime(b)
        if mime is None:
            raise ValueError(f"{n or 'Datei'}: bitte nur Fotos (JPEG, PNG, WebP, HEIC)")
        if len(b) > MAX_FOTO:
            raise ValueError(f"{n or 'Foto'}: größer als {MAX_FOTO // 1024 // 1024} MB")
        geprueft.append((n or "foto", mime, b))
    notfall = bool(form.get("notfall")) and konfig(m)["bereiche"]["notdienst"]
    kennung = "website:" + hashlib.sha256(f"{m.id}|{name}|{telefon}|{email}|{nachricht}|{als_utc(jetzt).strftime('%Y-%m-%d %H')}".encode()).hexdigest()[:24]
    adresse = (form.get("adresse") or "").strip()
    strasse = plz = ort = None
    if adresse:
        t = re.match(r"^(.*?),?\s*(\d{5})\s+(.+)$", adresse)
        strasse, plz, ort = (t.group(1).strip(" ,"), t.group(2), t.group(3).strip()) if t else (adresse, None, None)
    a = anfragen.anfrage_verarbeiten(s, m, "web", kennung, name, telefon, email, nachricht[:120], nachricht, jetzt, strasse, plz, ort, notfall=notfall)
    fall = s.get(Servicefall, a.servicefall_id) if a is not None and a.servicefall_id else None
    if fall is not None:
        for n, mime, b in geprueft:
            anhang_anlegen(s, m, "servicefall", fall.id, "foto", n, mime, b, "kunde (Website)", "über das Kontaktformular")
    notdienst = bool(fall is not None and fall.dringlichkeit == "notdienst")
    if a is not None and not notdienst:   # Notfälle alarmiert bereits core.anfragen mit hoher Priorität
        hinweise.senden(s, m, "info", "Anfrage über die Website", f"{name}: {nachricht[:160]}" + (f" ({len(geprueft)} Foto(s))" if geprueft else ""),
                        f"website:{kennung}", f"vorgaenge/{fall.id}" if fall is not None else "anfragen")
    return {"status": "angenommen", "anfrage_id": a.id if a else None, "notdienst": notdienst, "fotos": len(geprueft) if fall is not None else 0}


def buchungslink(m: Mandant) -> str | None:
    return buchung.link(m)
