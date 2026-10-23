"""Kundenkarte, Geokodierung und Vollständigkeit der Kundendaten.

Koordinaten (WGS 84) liegen an Kunde und Objekt. Sie entstehen auf drei Wegen: manuell (API/Oberfläche),
per Geokodierung über einen Nominatim-kompatiblen Dienst (Standard: nominatim.openstreetmap.org, Nutzungs-
bedingungen: höchstens eine Anfrage je Sekunde, aussagekräftiger User-Agent, kein Massenabgleich) oder aus
den Demodaten. Die Karte in der Oberfläche zeichnet die Punkte selbst (Web-Mercator) und holt nur die
Hintergrundkacheln von einem konfigurierbaren Kachelserver; Fremdcode wird dafür nicht eingebettet.
"""
from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.disposition import ZEITZONE
from core.domaene import als_utc
from core.modelle import Kunde, Mandant, Objekt, Servicefall, Termin
from core.regeln import telefon_gueltig
from core.twin import OFFENE_STATUS, wartungsstatus

Geocoder = Callable[[str], tuple[float, float] | None]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
KACHELN_STANDARD = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
KACHELN_HINWEIS = "© OpenStreetMap-Mitwirkende (ODbL)"
# Felder, die ein Kundendatensatz für den vollautomatischen Betrieb braucht; Reihenfolge = Anzeige
PFLICHTFELDER = (
    ("telefon", "gültige Rufnummer (Erkennung am Telefon, Rückruf, SMS)"),
    ("email", "E-Mail (Terminbestätigung, Angebote, Rechnungen)"),
    ("adresse", "Anschrift (Straße, PLZ, Ort)"),
    ("geo", "Geokoordinaten (Kundenkarte, Anfahrt)"),
    ("objekt", "Objekt (Gebäude, an dem gearbeitet wird)"),
    ("anlage", "mindestens eine Anlage (Wartungsplanung, Qualifikation)"),
    ("einwilligung", "Einwilligung zur Kontaktaufnahme (E-Mail oder SMS)"),
)


def adresse_text(strasse: str | None, plz: str | None, ort: str | None) -> str | None:
    teile = [t.strip() for t in (strasse, f"{plz or ''} {ort or ''}") if t and t.strip()]
    return ", ".join(teile) + ", Deutschland" if strasse and ort else None


def nominatim(url: str | None = None, email: str | None = None, pause: float = 1.0, client: httpx.Client | None = None) -> Geocoder:
    """Geocoder gegen einen Nominatim-kompatiblen Dienst, mit Mindestabstand zwischen Anfragen."""
    url = url or os.environ.get("GEOCODER_URL") or NOMINATIM_URL
    email = email or os.environ.get("GEOCODER_EMAIL") or ""
    kopf = {"User-Agent": f"shk-backoffice/0.2 ({email or 'kein Kontakt hinterlegt'})", "Accept-Language": "de"}
    zuletzt = [0.0]

    def suchen(adresse: str) -> tuple[float, float] | None:
        warte = pause - (time.monotonic() - zuletzt[0])
        if warte > 0:
            time.sleep(warte)
        zuletzt[0] = time.monotonic()
        params = {"q": adresse, "format": "jsonv2", "limit": 1, "countrycodes": "de,at,ch"}
        if email:
            params["email"] = email
        c = client or httpx.Client(timeout=10.0)
        try:
            r = c.get(url, params=params, headers=kopf)
            r.raise_for_status()
            treffer = r.json()
        finally:
            if client is None:
                c.close()
        if not treffer:
            return None
        return float(treffer[0]["lat"]), float(treffer[0]["lon"])

    return suchen


def geokodieren(s: Session, m: Mandant, geocoder: Geocoder, jetzt_: datetime, limit: int = 50,
                nur_fehlende: bool = True) -> dict[str, Any]:
    """Setzt Koordinaten an Kunden und Objekten ohne Koordinaten (oder allen), Adresse → Punkt, mit Cache je Adresse."""
    ergebnis = {"geprueft": 0, "gesetzt": 0, "ohne_treffer": 0, "fehler": 0, "uebrig": 0}
    cache: dict[str, tuple[float, float] | None] = {}
    kandidaten: list[Kunde | Objekt] = []
    for modell in (Kunde, Objekt):
        abfrage = select(modell).where(modell.mandant_id == m.id)
        if nur_fehlende:
            abfrage = abfrage.where(modell.lat.is_(None))
        kandidaten.extend(s.scalars(abfrage.order_by(modell.id)).all())
    for eintrag in kandidaten:
        adresse = adresse_text(eintrag.strasse, eintrag.plz, eintrag.ort)
        if not adresse:
            continue
        if ergebnis["geprueft"] >= limit and adresse not in cache:
            ergebnis["uebrig"] += 1
            continue
        if adresse not in cache:
            ergebnis["geprueft"] += 1
            try:
                cache[adresse] = geocoder(adresse)
            except (httpx.HTTPError, ValueError, KeyError):
                ergebnis["fehler"] += 1
                cache[adresse] = None
                continue
        punkt = cache[adresse]
        if punkt is None:
            ergebnis["ohne_treffer"] += 1
            continue
        eintrag.lat, eintrag.lon = punkt
        eintrag.geo_quelle, eintrag.geo_stand = "nominatim", jetzt_
        ergebnis["gesetzt"] += 1
    # Betriebsstandort aus den Stammdaten, falls noch ohne Koordinaten
    st = dict(m.stammdaten or {})
    adresse = adresse_text(st.get("strasse"), st.get("plz"), st.get("ort"))
    if adresse and (not nur_fehlende or st.get("lat") is None) and ergebnis["geprueft"] < limit:
        try:
            punkt = cache.get(adresse) or geocoder(adresse)
            ergebnis["geprueft"] += 1
            if punkt:
                m.stammdaten = {**st, "lat": punkt[0], "lon": punkt[1]}
                ergebnis["gesetzt"] += 1
        except (httpx.HTTPError, ValueError, KeyError):
            ergebnis["fehler"] += 1
    return ergebnis


def vollstaendigkeit(k: Kunde) -> dict[str, Any]:
    """Prüft, ob alles für den automatischen Betrieb am Kunden sitzt; liefert fehlende Felder und eine Quote."""
    objekte = list(k.objekte)
    anlagen = [a for o in objekte for a in o.anlagen]
    einwilligungen = {sch for sch, wert in (k.einwilligungen or {}).items() if wert}
    vorhanden = {
        "telefon": telefon_gueltig(k.telefon),
        "email": bool(k.email),
        "adresse": bool(k.strasse and k.plz and k.ort),
        "geo": k.lat is not None and k.lon is not None,
        "objekt": bool(objekte),
        "anlage": bool(anlagen),
        "einwilligung": bool(einwilligungen & {"email", "sms", "kontakt"}),
    }
    fehlend = [{"feld": feld, "text": text} for feld, text in PFLICHTFELDER if not vorhanden[feld]]
    return {"quote": round(sum(vorhanden.values()) / len(PFLICHTFELDER), 2), "fehlend": fehlend,
            "vollstaendig": not fehlend}


def vollstaendigkeit_betrieb(s: Session, m: Mandant) -> dict[str, Any]:
    kunden = s.scalars(select(Kunde).where(Kunde.mandant_id == m.id).order_by(Kunde.name)).all()
    zeilen, je_feld = [], Counter()
    for k in kunden:
        v = vollstaendigkeit(k)
        for f in v["fehlend"]:
            je_feld[f["feld"]] += 1
        if not v["vollstaendig"]:
            zeilen.append({"id": k.id, "name": k.name, "ort": k.ort, "quote": v["quote"], "fehlend": [f["feld"] for f in v["fehlend"]]})
    return {"kunden": len(kunden), "vollstaendig": len(kunden) - len(zeilen), "unvollstaendig": zeilen,
            "je_feld": [{"feld": feld, "text": text, "anzahl": je_feld.get(feld, 0)} for feld, text in PFLICHTFELDER]}


def kundenkarte(s: Session, m: Mandant, jetzt_: datetime) -> dict[str, Any]:
    """Alles, was das Cockpit für die Karte braucht: Kundenpunkte, heutige Einsätze, Betrieb, Verteilung je Ort."""
    heute = als_utc(jetzt_).astimezone(ZEITZONE).date()
    von = datetime.combine(heute, datetime.min.time(), ZEITZONE)
    kunden = s.scalars(select(Kunde).where(Kunde.mandant_id == m.id).order_by(Kunde.name)).all()
    offene = Counter()
    dringend: dict[str, str] = {}
    for f in s.scalars(select(Servicefall).where(Servicefall.mandant_id == m.id, Servicefall.status.in_(OFFENE_STATUS))):
        offene[f.kunde_id] += 1
        if f.dringlichkeit in ("notdienst", "hoch") and dringend.get(f.kunde_id) != "notdienst":
            dringend[f.kunde_id] = f.dringlichkeit
    punkte, ohne_geo, je_ort, plz_je_ort = [], [], Counter(), {}
    for k in kunden:
        ort = (k.ort or "unbekannt").split("-")[0].strip()   # "Köln-Porz" zählt zu Köln
        je_ort[ort] += 1
        if k.plz:
            plz_je_ort.setdefault(ort, set()).add(k.plz)
        wartung = "aktuell"
        for o in k.objekte:
            for a in o.anlagen:
                st = wartungsstatus(a, heute)
                if st == "ueberfaellig" or (st == "bald_faellig" and wartung == "aktuell"):
                    wartung = st
        if k.lat is None or k.lon is None:
            ohne_geo.append({"id": k.id, "name": k.name, "ort": k.ort, "adresse": bool(adresse_text(k.strasse, k.plz, k.ort))})
            continue
        punkte.append({"id": k.id, "name": k.name, "lat": k.lat, "lon": k.lon, "plz": k.plz, "ort": k.ort,
                       "strasse": k.strasse, "vertragsstatus": k.vertragsstatus, "offene_vorgaenge": offene.get(k.id, 0),
                       "dringlichkeit": dringend.get(k.id), "wartung": wartung, "geo_quelle": k.geo_quelle})
    termine = s.scalars(select(Termin).where(Termin.mandant_id == m.id, Termin.beginn >= von,
                                             Termin.beginn < von + timedelta(days=1), Termin.status != "abgesagt")
                        .order_by(Termin.beginn)).all()
    einsaetze = [{"termin_id": t.id, "beginn": t.beginn, "ende": t.ende, "status": t.status, "ressource": t.ressource.name,
                  "ressource_id": t.ressource_id, "servicefall_id": t.servicefall_id, "anliegen": t.servicefall.anliegen,
                  "kunde_id": t.servicefall.kunde_id, "kunde": t.servicefall.kunde.name,
                  "lat": t.servicefall.kunde.lat, "lon": t.servicefall.kunde.lon} for t in termine]
    st = m.stammdaten or {}
    betrieb = {"name": m.name, "lat": st.get("lat"), "lon": st.get("lon"), "ort": st.get("ort")}
    return {"betrieb": betrieb, "kunden": punkte, "ohne_koordinaten": ohne_geo, "einsaetze_heute": einsaetze,
            "je_ort": [{"ort": ort, "anzahl": n, "plz": sorted(plz_je_ort.get(ort, ()))}
                       for ort, n in sorted(je_ort.items(), key=lambda e: (-e[1], e[0]))],
            "kacheln": {"url": st.get("karte_kacheln") or KACHELN_STANDARD, "hinweis": st.get("karte_hinweis") or KACHELN_HINWEIS},
            "stand": jetzt_}
