"""Kalender-Abonnement (iCalendar, RFC 5545) je Monteur und für den ganzen Betrieb.

Der Monteur abonniert die Adresse auf dem Telefon (Apple Kalender, Google, Outlook, DAVx⁵); Termine, Abwesenheiten
und Bereitschaften erscheinen dort ohne App. Die Adresse trägt ein Token aus HMAC über die Ressourcen-ID, damit sie
ohne Anmeldung, aber nicht erratbar ist; ein Wechsel von `KALENDER_SCHLUESSEL` macht alle Adressen ungültig.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta
from typing import Any

from icalendar import Alarm, Calendar, Event
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.disposition import ZEITZONE
from core.domaene import als_utc
from core.modelle import Abwesenheit, Bereitschaft, Mandant, Ressource, Termin

ABWESENHEIT_TEXT = {"urlaub": "Urlaub", "krank": "Krank", "schulung": "Schulung", "sonstiges": "Abwesend"}


def _schluessel() -> bytes:
    return (os.environ.get("KALENDER_SCHLUESSEL") or os.environ.get("INTEGRATIONS_KEY") or os.environ.get("CORE_API_KEY") or "entwicklung").encode()


def token(kennung: str) -> str:
    return hmac.new(_schluessel(), kennung.encode(), hashlib.sha256).hexdigest()[:32]


def token_gueltig(kennung: str, wert: str | None) -> bool:
    return bool(wert) and hmac.compare_digest(token(kennung), wert)


def _uid(art: str, id_: str) -> str:
    return f"{art}-{id_}@shk-backoffice"


def _termin_ereignis(t: Termin, mit_kunde: bool, mit_monteur: bool) -> Event:
    f, k = t.servicefall, t.servicefall.kunde
    e = Event()
    e.add("uid", _uid("termin", t.id))
    e.add("dtstamp", als_utc(t.beginn))
    e.add("dtstart", als_utc(t.beginn).astimezone(ZEITZONE))
    e.add("dtend", als_utc(t.ende).astimezone(ZEITZONE))
    titel = f"{k.name}: {f.anliegen[:60]}" if mit_kunde else f.anliegen[:60]
    if t.status == "vorgeschlagen":
        titel = f"(vorgemerkt) {titel}"
    if mit_monteur:
        titel = f"{t.ressource.name.split(' (')[0]} · {titel}"
    e.add("summary", titel)
    adresse = ", ".join(x for x in (k.strasse, f"{k.plz or ''} {k.ort or ''}".strip()) if x)
    if adresse:
        e.add("location", adresse)
    if k.lat is not None and k.lon is not None:
        e.add("geo", (k.lat, k.lon))
    beschreibung = [f"Dringlichkeit: {f.dringlichkeit}", f"Status: {t.status}"]
    if k.telefon:
        beschreibung.append(f"Telefon: {k.telefon}")
    if f.symptom:
        beschreibung.append(f"Symptom: {f.symptom}")
    if f.anlage:
        beschreibung.append(f"Anlage: {f.anlage.anlagentyp} {f.anlage.hersteller or ''} {f.anlage.baujahr or ''}".strip())
    e.add("description", "\n".join(beschreibung))
    e.add("status", "TENTATIVE" if t.status == "vorgeschlagen" else "CONFIRMED")
    e.add("categories", [f.dringlichkeit])
    if f.dringlichkeit in ("notdienst", "hoch"):
        e.add("priority", 1)
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", "Abfahrt zum Einsatz")
    alarm.add("trigger", timedelta(minutes=-30))
    e.add_component(alarm)
    return e


def _ganztag(uid: str, titel: str, von, bis_einschliesslich, beschreibung: str | None = None) -> Event:
    e = Event()
    e.add("uid", uid)
    e.add("dtstamp", datetime.now(ZEITZONE))
    e.add("dtstart", von)
    e.add("dtend", bis_einschliesslich + timedelta(days=1))
    e.add("summary", titel)
    e.add("transp", "TRANSPARENT")
    if beschreibung:
        e.add("description", beschreibung)
    return e


def kalender(s: Session, m: Mandant, r: Ressource | None, jetzt_: datetime, wochen_zurueck: int = 4, wochen_voraus: int = 12) -> bytes:
    """ICS für einen Monteur (r) oder den Betrieb (r=None: alle Monteure, Termine mit Monteurname)."""
    von = als_utc(jetzt_) - timedelta(weeks=wochen_zurueck)
    bis = als_utc(jetzt_) + timedelta(weeks=wochen_voraus)
    cal = Calendar()
    cal.add("prodid", "-//SHK-Backoffice//Kalender//DE")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", f"{m.name} · {r.name.split(' (')[0]}" if r else f"{m.name} · Einsätze")
    cal.add("x-wr-timezone", "Europe/Berlin")
    cal.add("refresh-interval;value=duration", "PT30M")
    bedingung = [Termin.mandant_id == m.id, Termin.status != "abgesagt", Termin.beginn >= von, Termin.beginn < bis]
    if r:
        bedingung.append(Termin.ressource_id == r.id)
    for t in s.scalars(select(Termin).where(*bedingung).order_by(Termin.beginn)):
        cal.add_component(_termin_ereignis(t, mit_kunde=True, mit_monteur=r is None))
    bed_abw = [Abwesenheit.mandant_id == m.id, Abwesenheit.bis >= von.date(), Abwesenheit.von <= bis.date()]
    if r:
        bed_abw.append(Abwesenheit.ressource_id == r.id)
    for a in s.scalars(select(Abwesenheit).where(*bed_abw)):
        titel = ABWESENHEIT_TEXT.get(a.art, a.art) + ("" if r else f" · {a.ressource.name.split(' (')[0]}")
        cal.add_component(_ganztag(_uid("abwesenheit", a.id), titel, a.von, a.bis, a.notiz))
    bed_ber = [Bereitschaft.mandant_id == m.id, Bereitschaft.bis >= von, Bereitschaft.von < bis]
    if r:
        bed_ber.append(Bereitschaft.ressource_id == r.id)
    for b in s.scalars(select(Bereitschaft).where(*bed_ber)):
        e = Event()
        e.add("uid", _uid("bereitschaft", b.id))
        e.add("dtstamp", als_utc(b.von))
        e.add("dtstart", als_utc(b.von).astimezone(ZEITZONE))
        e.add("dtend", als_utc(b.bis).astimezone(ZEITZONE))
        e.add("summary", "Bereitschaft" + ("" if r else f" · {b.ressource.name.split(' (')[0]}"))
        e.add("description", f"Rufnummer: {b.telefon}" + (f"\n{b.notiz}" if b.notiz else ""))
        e.add("transp", "TRANSPARENT")
        cal.add_component(e)
    return cal.to_ical()


def links(m: Mandant, ressourcen: list[Ressource], basis_url: str) -> dict[str, Any]:
    basis = basis_url.rstrip("/")
    return {"betrieb": f"{basis}/kalender/betrieb/{m.id}.ics?token={token('betrieb:' + m.id)}",
            "monteure": [{"ressource_id": r.id, "name": r.name, "benutzer": r.benutzer,
                          "url": f"{basis}/kalender/{r.id}.ics?token={token('ressource:' + r.id)}"} for r in ressourcen]}
