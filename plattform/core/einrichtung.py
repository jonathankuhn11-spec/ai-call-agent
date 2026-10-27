"""Einrichtung in Minuten: Assistent (Branche, Betrieb, Team, Sachmittel, Leistungen, Online-Buchung), Fortschrittsanzeige
mit den nächsten Schritten und Kundenimport aus CSV-Exporten der bisherigen Software.

Der Assistent ist idempotent: ein zweiter Lauf legt nichts doppelt an (Mitarbeiter und Sachmittel nach Name, Leistungen
nach Nummer). Der CSV-Import erkennt Trennzeichen und Spalten selbst (Synonyme deutsch/englisch, Vor- und Nachname
werden zusammengeführt), normalisiert Rufnummern (E.164) und überspringt Dubletten (Rufnummer, sonst E-Mail). Eine
Spalte „Kennzeichen“ legt je Kunde ein Fahrzeug (Anlage fahrzeug mit Anlagenkartei) an – der Werkstatt-Bestand ist
damit nach dem ersten Import vollständig. `pruefen=True` zeigt Spaltenzuordnung und Vorschau, ohne zu schreiben.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import branchen, buchung, domaene, lebenszyklus
from core.modelle import Anlage, Artikel, Kunde, Mandant, Objekt, Ressource
from core.regeln import telefon_normalisieren

SYNONYME: dict[str, tuple[str, ...]] = {
    "name": ("name", "kunde", "kundenname", "firma", "firmenname", "company", "customer", "bezeichnung", "anzeigename"),
    "vorname": ("vorname", "firstname", "first name", "first_name", "given name"),
    "nachname": ("nachname", "name2", "lastname", "last name", "last_name", "familienname", "surname"),
    "telefon": ("telefon", "tel", "tel.", "telefonnummer", "phone", "rufnummer", "festnetz", "telefon1", "phone number"),
    "mobil": ("mobil", "handy", "mobile", "mobiltelefon", "telefon2", "cell"),
    "email": ("email", "e-mail", "mail", "e_mail", "emailadresse", "e-mail-adresse"),
    "strasse": ("strasse", "straße", "str", "str.", "adresse", "address", "street", "anschrift", "strasse hausnummer"),
    "plz": ("plz", "postleitzahl", "zip", "zipcode", "postal code", "postcode"),
    "ort": ("ort", "stadt", "city", "wohnort", "town"),
    "notiz": ("notiz", "notizen", "bemerkung", "bemerkungen", "hinweis", "notes", "memo", "kommentar"),
    "kundennummer": ("kundennummer", "kundennr", "kunden-nr", "kdnr", "kd-nr", "nummer", "customer id", "id"),
    "kennzeichen": ("kennzeichen", "kfz-kennzeichen", "amtliches kennzeichen", "plate", "license plate", "nummernschild"),
    "fahrzeug": ("fahrzeug", "modell", "fahrzeugmodell", "marke modell", "vehicle", "typ"),
    "hersteller": ("hersteller", "marke", "make", "fabrikat"),
    "fin": ("fin", "fahrgestellnummer", "vin", "fahrzeug-identifizierungsnummer"),
    "hu_bis": ("hu", "hu bis", "hu/au", "tüv", "tuev", "hauptuntersuchung", "hu_bis", "tüv bis"),
    "kilometerstand": ("km", "kilometerstand", "km-stand", "laufleistung", "mileage", "odometer"),
}


# --- Fortschritt ------------------------------------------------------------------------------
def status(s: Session, m: Mandant) -> dict[str, Any]:
    st = m.stammdaten or {}
    b = branchen.branche(m)
    personen = s.scalar(select(func.count()).select_from(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv, Ressource.art == "person")) or 0
    mittel = s.scalar(select(func.count()).select_from(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv, Ressource.art == "sachmittel")) or 0
    leistungen = s.scalar(select(func.count()).select_from(Artikel).where(Artikel.mandant_id == m.id, Artikel.kategorie == "leistung")) or 0
    kunden = s.scalar(select(func.count()).select_from(Kunde).where(Kunde.mandant_id == m.id)) or 0
    schritte = [
        {"schluessel": "branche", "titel": "Branche gewählt", "erledigt": bool(m.gewerk), "hinweis": f"{b.bezeichnung}" if m.gewerk else "Branche bestimmt Begriffe, Leistungen und Kartei.", "pfad": "einrichtung"},
        {"schluessel": "betrieb", "titel": "Betriebsdaten (Anschrift, Telefon, E-Mail)", "erledigt": all(st.get(k) for k in ("strasse", "plz", "ort", "telefon", "email")),
         "hinweis": "Erscheint auf Rechnungen, Angeboten, im Portal und in der Buchung.", "pfad": "einstellungen"},
        {"schluessel": "iban", "titel": "Bankverbindung (IBAN) für GiroCode auf Rechnungen", "erledigt": bool(st.get("iban")), "hinweis": "Kunden zahlen per QR-Code aus der Banking-App.", "pfad": "einstellungen"},
        {"schluessel": "team", "titel": f"{b.begriffe.get('ressourcen', 'Team')} angelegt", "erledigt": personen > 0, "hinweis": f"{personen} aktiv", "pfad": "einrichtung"},
        {"schluessel": "sachmittel", "titel": "Sachmittel angelegt (Hebebühne, Arbeitsplatz, Raum)", "erledigt": mittel > 0 or not b.sachmittel, "hinweis": f"{mittel} angelegt" if b.sachmittel else "für diese Branche nicht nötig", "pfad": "einrichtung", "optional": True},
        {"schluessel": "leistungen", "titel": "Leistungskatalog mit Dauer und Preis", "erledigt": leistungen > 0 or not b.leistungen, "hinweis": f"{leistungen} Leistungen", "pfad": "lager"},
        {"schluessel": "kunden", "titel": "Kunden importiert oder angelegt", "erledigt": kunden > 0, "hinweis": f"{kunden} Kunden", "pfad": "einrichtung"},
        {"schluessel": "basis_url", "titel": "Öffentliche Adresse (Portal-Links, Buchung, Kalender-Abo)", "erledigt": bool(st.get("basis_url")), "hinweis": "z. B. https://backoffice.ihr-betrieb.de", "pfad": "einstellungen"},
        {"schluessel": "buchung", "titel": "Online-Buchung aktiv", "erledigt": bool(st.get("buchung_token")) or not b.online_buchung, "hinweis": "Kunden buchen freie Zeiten selbst." if b.online_buchung else "für diese Branche nicht vorgesehen", "pfad": "einstellungen", "optional": True},
        {"schluessel": "einwilligungen", "titel": "Datenschutz: Impressum und Datenschutzerklärung verlinkt", "erledigt": bool(st.get("impressum_url") and st.get("datenschutz_url")), "hinweis": "Pflicht im Portal und auf der Buchungsseite.", "pfad": "einstellungen"},
        {"schluessel": "telefonie", "titel": "Telefonassistent verbunden (SIP-Trunk, Sprachdienst)", "erledigt": bool(st.get("telefonie_aktiv")), "hinweis": "Siehe TELEFONIE.md; bis dahin Anrufsimulator.", "pfad": "anruf", "optional": True},
    ]
    pflicht = [x for x in schritte if not x.get("optional")]
    erledigt = sum(1 for x in pflicht if x["erledigt"])
    naechste = [x for x in schritte if not x["erledigt"]][:3]
    return {"branche": b.schluessel, "bezeichnung": b.bezeichnung, "fortschritt": round(erledigt / len(pflicht), 2), "erledigt": erledigt, "pflicht": len(pflicht),
            "schritte": schritte, "naechste": naechste, "fertig": erledigt == len(pflicht)}


# --- Assistent -------------------------------------------------------------------------------
def einrichten(s: Session, m: Mandant, daten: dict[str, Any], akteur_name: str | None = None) -> dict[str, Any]:
    ergebnis: dict[str, Any] = {}
    if daten.get("branche"):
        ergebnis["branche"] = branchen.umstellen(s, m, daten["branche"], daten.get("leistungen_anlegen", True), akteur_name)
    if daten.get("name"):
        m.name = daten["name"].strip()[:200]
    if daten.get("stammdaten"):
        m.stammdaten = {**(m.stammdaten or {}), **{k: v for k, v in daten["stammdaten"].items() if v not in (None, "")}}
    vorhandene = {r.name.lower(): r for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m.id))}
    angelegt_p, angelegt_s = 0, 0
    for p in daten.get("mitarbeiter") or []:
        name = (p.get("name") or "").strip()
        if not name or name.lower() in vorhandene:
            continue
        r = Ressource(mandant_id=m.id, name=name[:200], qualifikationen=list(p.get("qualifikationen") or []), arbeitstage=list(p.get("arbeitstage") or [0, 1, 2, 3, 4]),
                      arbeitsbeginn=time.fromisoformat(p.get("arbeitsbeginn") or "07:30"), arbeitsende=time.fromisoformat(p.get("arbeitsende") or "16:30"),
                      benutzer=p.get("benutzer"), wochenstunden=float(p.get("wochenstunden") or 40))
        s.add(r)
        vorhandene[name.lower()] = r
        angelegt_p += 1
    for sm in daten.get("sachmittel") or []:
        name, typ = (sm.get("name") or "").strip(), sm.get("typ")
        if not name or typ not in branchen.SACHMITTEL:
            raise ValueError(f"Sachmittel braucht Name und Typ aus {sorted(branchen.SACHMITTEL)}")
        if name.lower() in vorhandene:
            continue
        r = Ressource(mandant_id=m.id, name=name[:200], art="sachmittel", sachmittel_typ=typ, arbeitstage=[0, 1, 2, 3, 4, 5, 6], arbeitsbeginn=time(0, 0), arbeitsende=time(23, 59))
        s.add(r)
        vorhandene[name.lower()] = r
        angelegt_s += 1
    if daten.get("online_buchung") and not (m.stammdaten or {}).get("buchung_token"):
        m.stammdaten = {**(m.stammdaten or {}), "buchung_token": buchung.token_erzeugen()}
    s.flush()
    domaene.protokollieren(s, m.id, "betrieb.eingerichtet", "mandant", m.id, "mensch", {"mitarbeiter": angelegt_p, "sachmittel": angelegt_s, "branche": m.gewerk}, akteur_name)
    ergebnis.update({"mitarbeiter_angelegt": angelegt_p, "sachmittel_angelegt": angelegt_s, "status": status(s, m)})
    return ergebnis


# --- CSV-Import -------------------------------------------------------------------------------
def _spalten_zuordnen(kopf: list[str]) -> dict[str, int]:
    zuordnung: dict[str, int] = {}
    for i, roh in enumerate(kopf):
        k = roh.strip().lower().replace("_", " ")
        for ziel, namen in SYNONYME.items():
            if ziel in zuordnung:
                continue
            if k in namen or k.replace(" ", "_") in namen:
                zuordnung[ziel] = i
                break
    if "name" not in zuordnung and ("vorname" in zuordnung or "nachname" in zuordnung):
        zuordnung["name"] = -1   # aus Vor- und Nachname
    return zuordnung


def _lesen(text: str, trennzeichen: str | None = None) -> tuple[list[str], list[list[str]], str]:
    text = text.lstrip("﻿")
    probe = text[:4096]
    if trennzeichen is None:
        try:
            trennzeichen = csv.Sniffer().sniff(probe, delimiters=";,\t|").delimiter
        except csv.Error:
            trennzeichen = ";" if probe.count(";") >= probe.count(",") else ","
    zeilen = [z for z in csv.reader(io.StringIO(text), delimiter=trennzeichen) if any(c.strip() for c in z)]
    if not zeilen:
        raise ValueError("Die Datei enthält keine Zeilen")
    return zeilen[0], zeilen[1:], trennzeichen


def _wert(zeile: list[str], zuordnung: dict[str, int], feld: str) -> str | None:
    i = zuordnung.get(feld)
    if i is None or i < 0 or i >= len(zeile):
        return None
    w = zeile[i].strip()
    return w or None


def _datum(wert: str | None) -> str | None:
    if not wert:
        return None
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%m/%Y", "%d.%m.%y", "%Y-%m"):
        try:
            d = datetime.strptime(wert.strip(), f).date()
            if f in ("%m/%Y", "%Y-%m"):   # HU-Plakette: Monat → Monatsende
                d = date(d.year + (d.month == 12), d.month % 12 + 1, 1) - __import__("datetime").timedelta(days=1)
            return d.isoformat()
        except ValueError:
            continue
    return None


def kunden_import(s: Session, m: Mandant, text: str, trennzeichen: str | None = None, pruefen: bool = False, heute: date | None = None,
                  akteur_name: str | None = None) -> dict[str, Any]:
    kopf, zeilen, trenn = _lesen(text, trennzeichen)
    zuordnung = _spalten_zuordnen(kopf)
    if "name" not in zuordnung:
        raise ValueError(f"Keine Namensspalte erkannt (Spalten: {', '.join(kopf)}). Erwartet z. B. Name, Kunde, Firma oder Vorname/Nachname.")
    heute = heute or date.today()
    telefone = {k.telefon for k in s.scalars(select(Kunde).where(Kunde.mandant_id == m.id)) if k.telefon}
    emails = {k.email.lower() for k in s.scalars(select(Kunde).where(Kunde.mandant_id == m.id)) if k.email}
    angelegt, uebersprungen, fehler, fahrzeuge, vorschau = 0, 0, [], 0, []
    for n, z in enumerate(zeilen, start=2):
        name = _wert(z, zuordnung, "name") if zuordnung["name"] >= 0 else " ".join(x for x in (_wert(z, zuordnung, "vorname"), _wert(z, zuordnung, "nachname")) if x)
        if not name:
            fehler.append({"zeile": n, "grund": "Name fehlt"})
            continue
        tel = telefon_normalisieren(_wert(z, zuordnung, "telefon") or _wert(z, zuordnung, "mobil"))
        email = (_wert(z, zuordnung, "email") or "").lower() or None
        if (tel and tel in telefone) or (email and email in emails):
            uebersprungen += 1
            continue
        eintrag = {"name": name[:200], "telefon": tel, "email": email, "strasse": _wert(z, zuordnung, "strasse"), "plz": _wert(z, zuordnung, "plz"), "ort": _wert(z, zuordnung, "ort"),
                   "notiz": _wert(z, zuordnung, "notiz"), "kennzeichen": _wert(z, zuordnung, "kennzeichen")}
        if len(vorschau) < 5:
            vorschau.append(eintrag)
        if tel:
            telefone.add(tel)
        if email:
            emails.add(email)
        if pruefen:
            angelegt += 1
            fahrzeuge += 1 if eintrag["kennzeichen"] else 0
            continue
        k = Kunde(mandant_id=m.id, name=eintrag["name"], telefon=tel, email=email, strasse=eintrag["strasse"], plz=eintrag["plz"], ort=eintrag["ort"],
                  notiz=(f"Import: Kundennr. {_wert(z, zuordnung, 'kundennummer')}. " if _wert(z, zuordnung, "kundennummer") else "") + (eintrag["notiz"] or "") or None)
        s.add(k)
        s.flush()
        angelegt += 1
        if eintrag["kennzeichen"]:
            o = Objekt(mandant_id=m.id, kunde_id=k.id, bezeichnung="Fahrzeuge", strasse=eintrag["strasse"], plz=eintrag["plz"], ort=eintrag["ort"])
            s.add(o)
            s.flush()
            merk = branchen.merkmale_pruefen("fahrzeug", {"kennzeichen": eintrag["kennzeichen"].upper(), "fin": _wert(z, zuordnung, "fin"),
                                                          "hu_bis": _datum(_wert(z, zuordnung, "hu_bis")), "kilometerstand": _wert(z, zuordnung, "kilometerstand")})
            a = Anlage(mandant_id=m.id, objekt_id=o.id, anlagentyp="fahrzeug", hersteller=_wert(z, zuordnung, "hersteller"), modell=_wert(z, zuordnung, "fahrzeug"),
                       wartungsintervall_monate=12, merkmale=merk)
            s.add(a)
            s.flush()
            lebenszyklus.bauteile_anlegen(s, m, a)
            lebenszyklus.merkmale_uebernehmen(s, m, a, heute)
            fahrzeuge += 1
    if not pruefen:
        domaene.protokollieren(s, m.id, "kunden.importiert", "mandant", m.id, "mensch", {"angelegt": angelegt, "uebersprungen": uebersprungen, "fahrzeuge": fahrzeuge, "fehler": len(fehler)}, akteur_name)
    return {"pruefen": pruefen, "trennzeichen": trenn, "spalten": kopf, "zuordnung": {k: (kopf[i] if i >= 0 else "Vorname + Nachname") for k, i in zuordnung.items()},
            "zeilen": len(zeilen), "angelegt": angelegt, "uebersprungen": uebersprungen, "fahrzeuge": fahrzeuge, "fehler": fehler[:20], "vorschau": vorschau}
