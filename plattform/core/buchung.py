"""Online-Terminbuchung für Endkunden (Friseur, Kosmetik, Massage, Werkstatt, SHK-Wartung): Leistung wählen,
freie Zeiten sehen, buchen – ohne Konto, über einen Betriebslink mit Token (`stammdaten.buchung_token`).

Die Buchung läuft über dieselbe Disposition wie Telefon und Büro: Dauer und Qualifikation kommen aus der Leistung
(Artikel der Kategorie „leistung“), der Termin entsteht als Vorschlag und wird per Freigabe „termin_bestaetigen“
(Klasse B) verbindlich; mit Policy `termin_bestaetigen: auto` sofort. Der Kunde erhält die Terminbestätigung mit
Portal-Link (bestätigen/absagen). Drosselung je Token, Honeypot gegen Bots, Dubletten über die Rufnummer.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import branchen
from core.disposition import ZEITZONE, freie_zeitfenster
from core.domaene import als_utc
from core.modelle import Artikel, Kunde, Mandant, Ressource
from core.regeln import telefon_normalisieren


def token_erzeugen() -> str:
    return secrets.token_urlsafe(18)


def link(m: Mandant) -> str | None:
    basis, token = (m.stammdaten or {}).get("basis_url"), (m.stammdaten or {}).get("buchung_token")
    return f"{basis.rstrip('/')}/buchung/#{token}" if basis and token else None


def leistung_laden(s: Session, m: Mandant, leistung_id: str) -> Artikel:
    a = s.get(Artikel, leistung_id)
    if a is None or a.mandant_id != m.id or a.kategorie != "leistung" or not a.aktiv:
        raise ValueError("Leistung nicht buchbar")
    return a


def angebot(s: Session, m: Mandant) -> dict[str, Any]:
    """Daten der Buchungsseite: Betrieb, Leistungen, wählbare Mitarbeiter, Hinweise."""
    b = branchen.branche(m)
    st = m.stammdaten or {}
    ressourcen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv).order_by(Ressource.name)).all()
    return {"betrieb": {"name": m.name, "telefon": st.get("telefon"), "strasse": st.get("strasse"), "plz": st.get("plz"), "ort": st.get("ort"),
                        "impressum_url": st.get("impressum_url"), "datenschutz_url": st.get("datenschutz_url")},
            "branche": {"schluessel": b.schluessel, "bezeichnung": b.bezeichnung, "ressource": b.begriffe["ressource"], "hinweise": b.hinweise_kunde,
                        "adresse_noetig": bool(b.begriffe.get("anlagen"))},  # Handwerk: Einsatz beim Kunden, Adresse nötig
            "leistungen": [{"id": a.id, "nummer": a.nummer, "bezeichnung": a.bezeichnung, "dauer_minuten": a.dauer_minuten or 60, "qualifikation": a.qualifikation,
                            "preis_brutto": _brutto(a)} for a in branchen.leistungen(s, m)],
            "mitarbeiter": [{"id": r.id, "name": r.name.split(" (")[0], "qualifikationen": r.qualifikationen or []} for r in ressourcen],
            "vorlauf_tage": int(st.get("buchung_vorlauf_tage", 14) or 14)}


def _brutto(a: Artikel) -> str:
    from decimal import Decimal
    return f"{(Decimal(a.verkauf_netto) * (1 + Decimal(a.ust_satz) / 100)).quantize(Decimal('0.01')):.2f}"


def zeiten(s: Session, m: Mandant, leistung: Artikel, ab: datetime, ressource_id: str | None = None, tage: int | None = None,
           je_tag: int = 6) -> dict[str, Any]:
    """Freie Zeiten je Tag; Mindestvorlauf 2 h, höchstens `vorlauf_tage` voraus."""
    st = m.stammdaten or {}
    tage = tage or int(st.get("buchung_vorlauf_tage", 14) or 14)
    ab = max(als_utc(ab) + timedelta(hours=int(st.get("buchung_vorlauf_stunden", 2) or 2)), als_utc(ab))
    fenster = freie_zeitfenster(s, m, ab, tage=tage, dauer_minuten=leistung.dauer_minuten or 60, qualifikation=leistung.qualifikation,
                                anzahl=tage * 60, je_ressource=tage * 30)
    je_tag_liste: dict[str, list[dict[str, Any]]] = {}
    gesehen: set[tuple[str, datetime]] = set()
    for z in fenster:
        if ressource_id and z.ressource_id != ressource_id:
            continue
        tag = z.beginn.astimezone(ZEITZONE).date().isoformat()
        if (tag, z.beginn) in gesehen and not ressource_id:
            continue
        gesehen.add((tag, z.beginn))
        je_tag_liste.setdefault(tag, []).append({"beginn": z.beginn.isoformat(), "ende": z.ende.isoformat(), "ressource_id": z.ressource_id,
                                                 "name": z.ressource_name.split(" (")[0], "uhrzeit": z.beginn.astimezone(ZEITZONE).strftime("%H:%M")})
    tage_aus = []
    for t, liste in sorted(je_tag_liste.items()):
        schritt = max(1, len(liste) // je_tag)        # über den Tag verteilt statt nur die frühesten Fenster
        tage_aus.append({"tag": t, "zeiten": liste[::schritt][:je_tag]})
    return {"leistung": {"id": leistung.id, "bezeichnung": leistung.bezeichnung, "dauer_minuten": leistung.dauer_minuten or 60}, "tage": tage_aus}


def kunde_finden_oder_anlegen(s: Session, m: Mandant, name: str, telefon: str | None, email: str | None, strasse: str | None = None,
                              plz: str | None = None, ort: str | None = None, einwilligung_email: bool = False) -> tuple[Kunde, bool]:
    tel = telefon_normalisieren(telefon) if telefon else None
    k = None
    if tel:
        k = s.scalar(select(Kunde).where(Kunde.mandant_id == m.id, Kunde.telefon == tel))
    if k is None and email:
        k = s.scalar(select(Kunde).where(Kunde.mandant_id == m.id, Kunde.email == email.strip().lower()))
    neu = k is None
    if neu:
        k = Kunde(mandant_id=m.id, name=name.strip()[:200], telefon=tel, email=(email or "").strip().lower() or None, strasse=strasse, plz=plz, ort=ort,
                  einwilligungen={"email": True, "einwilligung_email_am": datetime.now().isoformat()} if einwilligung_email and email else {})
        s.add(k)
        s.flush()
    else:
        if email and not k.email:
            k.email = email.strip().lower()
        if einwilligung_email and email and not (k.einwilligungen or {}).get("email"):
            k.einwilligungen = {**(k.einwilligungen or {}), "email": True}
    return k, neu
