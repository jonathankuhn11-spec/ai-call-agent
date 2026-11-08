"""Karriereseite der Betriebswebsite: Stellenangebote mit JobPosting-Auszeichnung (Google for Jobs) und Kurzbewerbung.

Adressiert die Lücke „Personalsuche ohne Karriereseite“ (Problemcode P10): Viele Betriebe schalten Anzeigen in der
Jobbörse der Bundesagentur, haben auf der eigenen Website aber keine Stellen und kein Bewerbungsformular. Stellen werden
in `stammdaten["website"]["stellen"]` gepflegt (Ansicht „Website“), öffentlich unter `/web/<slug>/karriere` bzw.
`https://www.ihr-betrieb.de/karriere` ausgeliefert und in sitemap.xml aufgenommen. Die Kurzbewerbung verzichtet bewusst
auf Anschreiben (Name, Kontakt, frühester Start, optional bis zu drei Unterlagen als PDF/JPEG/PNG), weil Handwerksbewerber
überwiegend mobil und ohne vollständige Mappe bewerben.

Datenschutz: Verarbeitung zur Durchführung des Bewerbungsverfahrens (Art. 6 Abs. 1 lit. b, Art. 88 DSGVO); Löschung nach
Abschluss des Verfahrens mit Frist `bewerbung_tage` (Standard 180 Tage: zwei Monate Geltendmachung nach § 15 Abs. 4 AGG,
drei Monate Klagefrist nach § 61b ArbGG, Zustellpuffer); Talentpool nur mit gesonderter Einwilligung (Art. 6 Abs. 1 lit. a),
Frist `talentpool_tage` (Standard 730). Umgesetzt in core.loeschung.loeschlauf.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene, hinweise
from core.domaene import als_utc
from core.einsatz import anhang_anlegen
from core.modelle import Anhang, Bewerbung, Mandant

ARTEN = {"vollzeit": ("Vollzeit", "FULL_TIME"), "teilzeit": ("Teilzeit", "PART_TIME"), "minijob": ("Minijob", "PART_TIME"),
         "ausbildung": ("Ausbildung", "OTHER"), "praktikum": ("Praktikum", "INTERN"), "aushilfe": ("Aushilfe", "TEMPORARY")}
STATUS = ("neu", "in_pruefung", "gespraech", "zusage", "eingestellt", "absage", "zurueckgezogen")
ABGESCHLOSSEN = ("eingestellt", "absage", "zurueckgezogen")
STATUS_TEXT = {"neu": "neu", "in_pruefung": "in Prüfung", "gespraech": "Gespräch vereinbart", "zusage": "Zusage", "eingestellt": "eingestellt",
               "absage": "Absage", "zurueckgezogen": "zurückgezogen"}
MAX_UNTERLAGEN, MAX_DATEI, MAX_GESAMT = 3, 10 * 1024 * 1024, 15 * 1024 * 1024
MAX_STELLEN = 20
STELLE_ID = re.compile(r"^[a-z0-9-]{3,40}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[a-z]{2,24}$", re.I)
# Dateisignaturen: nur PDF und gängige Bildformate; alles andere (Office-Makros, Archive, ausführbare Dateien) wird abgewiesen
SIGNATUREN = ((b"%PDF-", "application/pdf"), (b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


def _liste(wert: Any, n: int = 12, laenge: int = 200) -> list[str]:
    if isinstance(wert, str):
        wert = wert.split("\n")
    return [str(z).strip(" -•\t")[:laenge] for z in (wert or []) if str(z).strip(" -•\t")][:n]


def _slug(titel: str) -> str:
    from core.website import slug_aus
    return slug_aus(titel)[:32].strip("-") or "stelle"


def stellen_pruefen(roh: list[dict[str, Any]] | None, alt: list[dict[str, Any]] | None, heute: date) -> list[dict[str, Any]]:
    """Validiert die Stellenliste der Website-Einstellungen. Bestehende Stellen behalten ID und Veröffentlichungsdatum."""
    alt_je_id = {s["id"]: s for s in (alt or []) if s.get("id")}
    aus, ids = [], set()
    for e in (roh or [])[:MAX_STELLEN]:
        titel = (e.get("titel") or "").strip()[:120]
        if not titel:
            continue
        art = (e.get("art") or "vollzeit").strip().lower()
        if art not in ARTEN:
            raise ValueError(f"Stellenart {art!r} unbekannt ({', '.join(ARTEN)})")
        sid = (e.get("id") or "").strip().lower()
        if sid and not STELLE_ID.match(sid):
            raise ValueError("Stellen-ID: 3–40 Zeichen, Kleinbuchstaben, Ziffern, Bindestrich")
        if not sid:
            sid = f"{_slug(titel)}-{secrets.token_hex(2)}"
        if sid in ids:
            raise ValueError(f"Stellen-ID {sid} doppelt")
        ids.add(sid)
        gueltig_bis = e.get("gueltig_bis") or None
        if gueltig_bis:
            try:
                gueltig_bis = date.fromisoformat(str(gueltig_bis)[:10]).isoformat()
            except ValueError as fehler:
                raise ValueError("gueltig_bis als Datum JJJJ-MM-TT") from fehler
        vorher = alt_je_id.get(sid, {})
        aus.append({"id": sid, "titel": titel, "art": art, "aktiv": bool(e.get("aktiv", True)),
                    "ort": (e.get("ort") or "").strip()[:120] or None, "start": (e.get("start") or "").strip()[:60] or None,
                    "stunden": (e.get("stunden") or "").strip()[:60] or None, "gehalt": (e.get("gehalt") or "").strip()[:120] or None,
                    "text": (e.get("text") or "").strip()[:2000] or None, "aufgaben": _liste(e.get("aufgaben")), "profil": _liste(e.get("profil")),
                    "wir_bieten": _liste(e.get("wir_bieten")), "gueltig_bis": gueltig_bis,
                    "veroeffentlicht": vorher.get("veroeffentlicht") or (e.get("veroeffentlicht") or heute.isoformat())[:10]})
    return aus


def offene(m: Mandant, heute: date) -> list[dict[str, Any]]:
    """Aktive, nicht abgelaufene Stellen (abgelaufene JobPostings wertet Google als Qualitätsmangel)."""
    from core.website import konfig
    return [s for s in konfig(m).get("stellen") or [] if s.get("aktiv") and not (s.get("gueltig_bis") and s["gueltig_bis"] < heute.isoformat())]


def stelle(m: Mandant, stelle_id: str, heute: date) -> dict[str, Any] | None:
    return next((s for s in offene(m, heute) if s["id"] == stelle_id), None)


def job_posting(d: dict[str, Any], s: dict[str, Any]) -> dict[str, Any]:
    """schema.org/JobPosting mit den Pflichtfeldern für Google for Jobs (title, description, datePosted, hiringOrganization, jobLocation)."""
    st = d["stammdaten"]
    beschreibung = "".join([f"<p>{_e(s['text'])}</p>" if s.get("text") else "",
                            *(f"<p><strong>{t}</strong></p><ul>{''.join(f'<li>{_e(x)}</li>' for x in s[k])}</ul>"
                              for k, t in (("aufgaben", "Ihre Aufgaben"), ("profil", "Ihr Profil"), ("wir_bieten", "Wir bieten")) if s.get(k))]) \
        or f"<p>{_e(s['titel'])} bei {_e(d['name'])}</p>"
    ld: dict[str, Any] = {"@context": "https://schema.org", "@type": "JobPosting", "title": s["titel"], "description": beschreibung,
                          "datePosted": s["veroeffentlicht"], "employmentType": ARTEN[s["art"]][1], "directApply": True,
                          "hiringOrganization": {"@type": "Organization", "name": d["name"], "sameAs": f"{d['basis']}/"},
                          "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "streetAddress": st.get("strasse"), "postalCode": st.get("plz"),
                                                                        "addressLocality": s.get("ort") or st.get("ort"), "addressCountry": "DE"}},
                          "identifier": {"@type": "PropertyValue", "name": d["name"], "value": s["id"]}, "url": f"{d['basis']}/karriere/{s['id']}"}
    if s.get("gueltig_bis"):
        ld["validThrough"] = f"{s['gueltig_bis']}T23:59:59+01:00"
    ld["jobLocation"]["address"] = {k: v for k, v in ld["jobLocation"]["address"].items() if v}
    return ld


def _e(x: Any) -> str:
    import html
    return html.escape(str(x))


# --- Bewerbungseingang ------------------------------------------------------------------------------------
def unterlage_pruefen(name: str, inhalt: bytes) -> str:
    """Liefert den MIME-Typ aus der Dateisignatur oder wirft ValueError (Endung und Content-Type sind fälschbar)."""
    if not inhalt:
        raise ValueError(f"{name}: leere Datei")
    if len(inhalt) > MAX_DATEI:
        raise ValueError(f"{name}: größer als {MAX_DATEI // 1024 // 1024} MB")
    for kopf, mime in SIGNATUREN:
        if inhalt.startswith(kopf):
            return mime
    raise ValueError(f"{name}: nur PDF, JPEG oder PNG")


def bewerbung_annehmen(s: Session, m: Mandant, form: dict[str, str], dateien: list[tuple[str, bytes]], jetzt: datetime) -> Bewerbung | None:
    """Kurzbewerbung von der Karriereseite. Idempotent je (Stelle, Kontakt, Stunde); Honeypot `website` → None."""
    if form.get("website"):
        return None
    name = (form.get("name") or "").strip()[:200]
    email = (form.get("email") or "").strip().lower()[:200] or None
    telefon = (form.get("telefon") or "").strip()[:40] or None
    if len(name) < 2:
        raise ValueError("Bitte Ihren Namen angeben")
    if not (email or telefon):
        raise ValueError("Bitte E-Mail oder Telefon angeben, damit wir antworten können")
    if email and not EMAIL.match(email):
        raise ValueError("Die E-Mail-Adresse ist ungültig")
    if not form.get("datenschutz"):
        raise ValueError("Bitte bestätigen Sie die Datenschutzhinweise zur Bewerbung")
    sid = (form.get("stelle_id") or "").strip() or None
    st = stelle(m, sid, jetzt.date()) if sid else None
    if sid and st is None:
        raise ValueError("Diese Stelle ist nicht mehr ausgeschrieben – gern als Initiativbewerbung")
    dateien = [(n, b) for n, b in dateien if b][:MAX_UNTERLAGEN + 1]
    if len(dateien) > MAX_UNTERLAGEN:
        raise ValueError(f"Höchstens {MAX_UNTERLAGEN} Dateien")
    if sum(len(b) for _, b in dateien) > MAX_GESAMT:
        raise ValueError(f"Unterlagen zusammen höchstens {MAX_GESAMT // 1024 // 1024} MB")
    geprueft = [(n, unterlage_pruefen(n, b), b) for n, b in dateien]
    kennung = hashlib.sha256(f"{m.id}|{sid}|{name.lower()}|{email}|{telefon}|{als_utc(jetzt).strftime('%Y-%m-%d %H')}".encode()).hexdigest()[:40]
    if s.scalar(select(Bewerbung).where(Bewerbung.mandant_id == m.id, Bewerbung.externe_id == kennung)):
        return None
    b = Bewerbung(mandant_id=m.id, stelle_id=sid, stelle_titel=st["titel"] if st else "Initiativbewerbung", name=name, telefon=telefon, email=email,
                  nachricht=(form.get("nachricht") or "").strip()[:3000] or None, fruehester_start=(form.get("start") or "").strip()[:60] or None,
                  talentpool=bool(form.get("talentpool")), externe_id=kennung, eingegangen=jetzt)
    s.add(b)
    s.flush()
    for n, mime, inhalt in geprueft:
        anhang_anlegen(s, m, "bewerbung", b.id, "dokument", n or "unterlage", mime, inhalt, "bewerber")
    domaene.protokollieren(s, m.id, "bewerbung.eingegangen", "bewerbung", b.id, "system", {"stelle": b.stelle_titel, "unterlagen": len(geprueft)})
    hinweise.senden(s, m, "info", "Neue Bewerbung", f"{name} – {b.stelle_titel}", f"bewerbung:{b.id}", "bewerbungen")
    return b


def als_dict(s: Session, b: Bewerbung) -> dict[str, Any]:
    unterlagen = s.scalars(select(Anhang).where(Anhang.mandant_id == b.mandant_id, Anhang.bezug_typ == "bewerbung", Anhang.bezug_id == b.id)).all()
    return {"id": b.id, "stelle_id": b.stelle_id, "stelle_titel": b.stelle_titel, "name": b.name, "telefon": b.telefon, "email": b.email,
            "nachricht": b.nachricht, "fruehester_start": b.fruehester_start, "status": b.status, "status_text": STATUS_TEXT.get(b.status, b.status),
            "talentpool": b.talentpool, "notiz": b.notiz, "eingegangen": als_utc(b.eingegangen).isoformat(),
            "abgeschlossen": als_utc(b.abgeschlossen).isoformat() if b.abgeschlossen else None,
            "unterlagen": [{"id": a.id, "name": a.name, "mime": a.mime, "groesse": a.groesse} for a in unterlagen]}


def liste(s: Session, m: Mandant, status: str | None = None) -> list[dict[str, Any]]:
    q = select(Bewerbung).where(Bewerbung.mandant_id == m.id)
    if status:
        q = q.where(Bewerbung.status == status)
    return [als_dict(s, b) for b in s.scalars(q.order_by(Bewerbung.eingegangen.desc()))]


def aendern(s: Session, m: Mandant, b: Bewerbung, daten: dict[str, Any], jetzt: datetime, wer: str | None) -> Bewerbung:
    if daten.get("status") is not None:
        if daten["status"] not in STATUS:
            raise ValueError(f"Status {daten['status']!r} unbekannt ({', '.join(STATUS)})")
        alt, b.status = b.status, daten["status"]
        b.abgeschlossen = (b.abgeschlossen or jetzt) if b.status in ABGESCHLOSSEN else None
        if alt != b.status:
            domaene.protokollieren(s, m.id, "bewerbung.status", "bewerbung", b.id, "mensch", {"von": alt, "nach": b.status}, wer)
    if "notiz" in daten:
        b.notiz = (daten["notiz"] or "").strip()[:3000] or None
    if daten.get("talentpool") is False:   # Widerruf der Einwilligung; Erteilen nur durch die bewerbende Person selbst
        b.talentpool = False
    s.flush()
    return b


def loeschen(s: Session, b: Bewerbung) -> None:
    for a in s.scalars(select(Anhang).where(Anhang.mandant_id == b.mandant_id, Anhang.bezug_typ == "bewerbung", Anhang.bezug_id == b.id)):
        s.delete(a)
    s.delete(b)


def faellige(s: Session, m: Mandant, jetzt: datetime, tage: int, talentpool_tage: int, offen_tage: int = 365) -> list[Bewerbung]:
    """Abgeschlossene Verfahren nach Frist; nie bearbeitete Eingänge spätestens nach `offen_tage`."""
    vor = lambda t: jetzt - timedelta(days=t)  # noqa: E731
    alle = s.scalars(select(Bewerbung).where(Bewerbung.mandant_id == m.id)).all()
    aus = []
    for b in alle:
        if b.status in ABGESCHLOSSEN and b.abgeschlossen and als_utc(b.abgeschlossen) < vor(talentpool_tage if b.talentpool else tage):
            aus.append(b)
        elif b.status not in ABGESCHLOSSEN and als_utc(b.eingegangen) < vor(max(offen_tage, talentpool_tage if b.talentpool else 0)):
            aus.append(b)
    return aus
