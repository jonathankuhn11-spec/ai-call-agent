"""Großhandelsanbindung: DATANORM-Artikelimport, IDS-Connect-Warenkorb, Lagerbestand und Bestellvorschlag.

DATANORM (Datenaustausch im Baugewerbe, Version 4 als ';'-getrennte Satzarten in CP850, Version 5 als XML) ist das
Format, in dem der SHK-Großhandel (GC-Gruppe, Richter+Frenzel, Pfeiffer & May, Reisser …) seine Preislisten
bereitstellt. Verarbeitet werden hier die Satzarten A (Artikel-Hauptsatz) und P (Preissatz) nach DATANORM 4.0 sowie
die Elemente ARTIKEL/ARTNR/KTEXT1/KTEXT2/PREIS/MEINH/WG aus DATANORM 5; herstellerspezifische Abweichungen (Preis in
Cent oder mit Komma, Preiseinheit) werden tolerant gelesen. Vor dem ersten Import einer neuen Quelle: Trockenlauf.

IDS-Connect (ITEK, Version 2.5) ist die Schnittstelle zwischen Handwerkersoftware und Großhandels-Shop: der Warenkorb
wird als Formular (POST) an die Shop-URL übergeben, der Monteur bzw. das Büro landet im angemeldeten Shop mit gefülltem
Warenkorb und bestellt dort. Die Feldnamen (version, action, warenkorb) und das XML entsprechen unserem Kenntnisstand
der Spezifikation; vor Inbetriebnahme gegen die IDS-Dokumentation des jeweiligen Großhändlers prüfen.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene
from core.modelle import Artikel, Bestand, Mandant, Rapport, Ressource

PREISEINHEIT = {"0": Decimal(1), "1": Decimal(10), "2": Decimal(100), "3": Decimal(1000), "": Decimal(1)}
MENGENEINHEIT = {"STCK": "Stk", "ST": "Stk", "STK": "Stk", "M": "m", "MTR": "m", "LFM": "m", "M2": "m²", "QM": "m²", "KG": "kg",
                 "L": "l", "LTR": "l", "PAAR": "Paar", "PK": "Pack", "PCK": "Pack", "SET": "Set", "ROL": "Rolle", "KRT": "Karton"}


def _preis(roh: str, einheit: str = "0", nachkomma_implizit: bool = True) -> Decimal | None:
    """DATANORM 4 führt Preise als Ganzzahl mit zwei impliziten Nachkommastellen; manche Lieferanten mit Komma."""
    roh = (roh or "").strip()
    if not roh:
        return None
    try:
        if "," in roh or "." in roh:
            wert = Decimal(roh.replace(".", "").replace(",", ".")) if "," in roh else Decimal(roh)
        else:
            wert = Decimal(roh) / (100 if nachkomma_implizit else 1)
    except InvalidOperation:
        return None
    return (wert / PREISEINHEIT.get(einheit.strip(), Decimal(1))).quantize(Decimal("0.01"))


def _einheit(roh: str) -> str:
    roh = (roh or "").strip()
    return MENGENEINHEIT.get(roh.upper(), roh[:10] or "Stk")


def datanorm_lesen(inhalt: bytes) -> list[dict[str, Any]]:
    """Liefert Artikelzeilen {nummer, bezeichnung, einheit, einkauf_netto|None, listenpreis|None, warengruppe, kennzeichen}."""
    text = inhalt.decode("utf-8-sig") if inhalt.lstrip().startswith(b"<") else inhalt.decode("cp850", errors="replace")
    if text.lstrip().startswith("<"):
        return _datanorm5(text)
    artikel: dict[str, dict[str, Any]] = {}
    reihenfolge: list[str] = []
    for zeile in text.splitlines():
        if not zeile or zeile[0] not in "APB" or len(zeile) < 2 or zeile[1] != ";":
            continue
        felder = zeile.split(";")
        satz = felder[0]
        if satz == "A" and len(felder) >= 10:
            nummer = felder[2].strip()
            if not nummer:
                continue
            preis = _preis(felder[9], felder[7] if len(felder) > 7 else "0")
            kennzeichen = (felder[6] or "").strip()  # 1 = Bruttopreis (Listenpreis), 2 = Nettopreis
            eintrag = artikel.setdefault(nummer, {"nummer": nummer, "bezeichnung": "", "einheit": "Stk", "einkauf_netto": None,
                                                  "listenpreis": None, "warengruppe": "", "kennzeichen": (felder[1] or "N").strip()})
            eintrag["bezeichnung"] = " ".join(t for t in (felder[4].strip(), felder[5].strip()) if t)[:200] or nummer
            eintrag["einheit"] = _einheit(felder[8])
            eintrag["warengruppe"] = (felder[11] if len(felder) > 11 else "").strip()
            if preis is not None:
                eintrag["einkauf_netto" if kennzeichen == "2" else "listenpreis"] = preis
            if nummer not in reihenfolge:
                reihenfolge.append(nummer)
        elif satz == "P" and len(felder) >= 5:
            # P;Kennz;ArtNr;Preiskennz;Preis;Rabatt;ArtNr;Preiskennz;Preis;Rabatt;… (bis drei Gruppen)
            gruppen = felder[2:]
            for i in range(0, len(gruppen) - 2, 4):
                nummer, kennz, preis = gruppen[i].strip(), gruppen[i + 1].strip(), _preis(gruppen[i + 2])
                if not nummer or preis is None:
                    continue
                eintrag = artikel.setdefault(nummer, {"nummer": nummer, "bezeichnung": nummer, "einheit": "Stk", "einkauf_netto": None,
                                                      "listenpreis": None, "warengruppe": "", "kennzeichen": "N"})
                eintrag["einkauf_netto" if kennz == "2" else "listenpreis"] = preis
                if nummer not in reihenfolge:
                    reihenfolge.append(nummer)
        elif satz == "B" and len(felder) >= 4:  # Zusatzsatz: Matchcode, EAN, Herstellernummer
            nummer = felder[2].strip()
            if nummer in artikel:
                artikel[nummer]["matchcode"] = felder[3].strip()[:40]
    return [artikel[n] for n in reihenfolge]


def _datanorm5(text: str) -> list[dict[str, Any]]:
    wurzel = ET.fromstring(text)
    ergebnis = []
    for el in wurzel.iter():
        if el.tag.split("}")[-1].upper() != "ARTIKEL":
            continue
        kinder = {k.tag.split("}")[-1].upper(): (k.text or "").strip() for k in el}
        nummer = kinder.get("ARTNR") or kinder.get("ARTIKELNUMMER") or ""
        if not nummer:
            continue
        preis = _preis(kinder.get("PREIS", ""), kinder.get("PREISEINHEIT", "0"), nachkomma_implizit=False)
        ergebnis.append({"nummer": nummer, "bezeichnung": " ".join(t for t in (kinder.get("KTEXT1", ""), kinder.get("KTEXT2", "")) if t)[:200] or nummer,
                         "einheit": _einheit(kinder.get("MEINH", "")), "warengruppe": kinder.get("WG", ""), "kennzeichen": kinder.get("VKZ", "N") or "N",
                         "einkauf_netto": preis if kinder.get("PREISKZ", "2") == "2" else None,
                         "listenpreis": preis if kinder.get("PREISKZ", "2") == "1" else None})
    return ergebnis


def datanorm_importieren(s: Session, m: Mandant, inhalt: bytes, lieferant: str, aufschlag_prozent: Decimal | None = None,
                         trocken: bool = False, akteur_name: str | None = None) -> dict[str, Any]:
    """Upsert in den Artikelstamm je (Mandant, Nummer). Verkaufspreis: Einkauf × (1 + Aufschlag), sonst Listenpreis;
    bestehende Verkaufspreise bleiben bei Preisänderung nur dann stehen, wenn kein Aufschlag übergeben wurde."""
    zeilen = datanorm_lesen(inhalt)
    aufschlag = aufschlag_prozent if aufschlag_prozent is not None else Decimal(str(m.stammdaten.get("materialaufschlag_prozent", 0) or 0))
    vorhanden = {a.nummer: a for a in s.scalars(select(Artikel).where(Artikel.mandant_id == m.id))}
    neu = geaendert = geloescht = uebersprungen = 0
    beispiele: list[dict[str, Any]] = []
    for z in zeilen:
        a = vorhanden.get(z["nummer"])
        if z["kennzeichen"] == "L":
            if a is not None and a.aktiv:
                geloescht += 1
                if not trocken:
                    a.aktiv = False
            continue
        einkauf = z.get("einkauf_netto")
        liste = z.get("listenpreis")
        verkauf = (einkauf * (1 + aufschlag / 100)).quantize(Decimal("0.01")) if einkauf is not None else liste
        if a is None:
            if verkauf is None:
                uebersprungen += 1
                continue
            neu += 1
            if not trocken:
                a = Artikel(mandant_id=m.id, nummer=z["nummer"][:40], bezeichnung=z["bezeichnung"], kategorie="material", einheit=z["einheit"],
                            einkauf_netto=f"{einkauf:.2f}" if einkauf is not None else None, verkauf_netto=f"{verkauf:.2f}", lieferant=lieferant[:200])
                s.add(a)
                vorhanden[a.nummer] = a
        else:
            aenderung = {}
            if einkauf is not None and a.einkauf_netto != f"{einkauf:.2f}":
                aenderung["einkauf_netto"] = f"{einkauf:.2f}"
                if aufschlag_prozent is not None or not a.verkauf_netto:
                    aenderung["verkauf_netto"] = f"{verkauf:.2f}"
            if z["bezeichnung"] and z["bezeichnung"] != z["nummer"] and a.bezeichnung != z["bezeichnung"]:
                aenderung["bezeichnung"] = z["bezeichnung"]
            if not aenderung:
                continue
            geaendert += 1
            if not trocken:
                for k, v in aenderung.items():
                    setattr(a, k, v)
                a.lieferant = a.lieferant or lieferant[:200]
        if len(beispiele) < 10:
            beispiele.append({"nummer": z["nummer"], "bezeichnung": z["bezeichnung"], "einkauf_netto": float(einkauf) if einkauf is not None else None,
                              "verkauf_netto": float(verkauf) if verkauf is not None else None})
    ergebnis = {"gelesen": len(zeilen), "neu": neu, "geaendert": geaendert, "deaktiviert": geloescht, "uebersprungen": uebersprungen,
                "trocken": trocken, "lieferant": lieferant, "aufschlag_prozent": float(aufschlag), "beispiele": beispiele}
    if not trocken:
        domaene.protokollieren(s, m.id, "artikel.datanorm_import", "mandant", m.id, "mensch", {k: v for k, v in ergebnis.items() if k != "beispiele"}, akteur_name)
    return ergebnis


# --- IDS-Connect ------------------------------------------------------------------------------
def ids_warenkorb_xml(positionen: Iterable[dict[str, Any]], kennung: str, version: str = "2.5") -> str:
    zeilen = ['<?xml version="1.0" encoding="UTF-8"?>', "<WARENKORB>", f"<VERSION>{escape(version)}</VERSION>",
              f"<WARENKORBINFO><KUNDEN_WARENKORB_ID>{escape(kennung)}</KUNDEN_WARENKORB_ID></WARENKORBINFO>"]
    for i, p in enumerate(positionen, 1):
        zeilen.append("<POSITION>"
                      f"<POSNR>{i}</POSNR><ARTIKEL_NR>{escape(str(p.get('nummer', '')))}</ARTIKEL_NR>"
                      f"<MENGE>{escape(str(p.get('menge', 1)).replace('.', ','))}</MENGE>"
                      f"<MENGENEINHEIT>{escape(str(p.get('einheit', 'Stk')))}</MENGENEINHEIT>"
                      f"<BEZEICHNUNG>{escape(str(p.get('bezeichnung', '')))}</BEZEICHNUNG>"
                      f"<KOMMISSION>{escape(str(p.get('kommission', '')))}</KOMMISSION>"
                      "</POSITION>")
    zeilen.append("</WARENKORB>")
    return "".join(zeilen)


def ids_formular(shop_url: str, positionen: list[dict[str, Any]], kennung: str, version: str = "2.5",
                 zusatzfelder: dict[str, str] | None = None) -> str:
    """HTML-Seite, die den Warenkorb per POST an den Shop übergibt (Browser des Nutzers, Shop-Login bleibt dort)."""
    felder = {"version": version, "action": "WARENKORB", "warenkorb": ids_warenkorb_xml(positionen, kennung, version), **(zusatzfelder or {})}
    inputs = "".join(f'<input type="hidden" name="{escape(k)}" value="{escape(v, {chr(34): "&quot;"})}">' for k, v in felder.items())
    return ("<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\"><title>Warenkorb an den Großhandel übergeben</title></head><body>"
            f"<p>Der Warenkorb mit {len(positionen)} Position(en) wird an den Shop übergeben …</p>"
            f'<form id="ids" method="post" action="{escape(shop_url, {chr(34): "&quot;"})}" accept-charset="UTF-8">{inputs}'
            '<button type="submit">Weiter zum Shop</button></form><script>document.getElementById("ids").submit()</script></body></html>')


# --- Lager -------------------------------------------------------------------------------------
def _bestand(s: Session, m: Mandant, artikel_id: str, lager: str, anlegen: bool = True) -> Bestand | None:
    b = s.scalar(select(Bestand).where(Bestand.mandant_id == m.id, Bestand.artikel_id == artikel_id, Bestand.lager == lager))
    if b is None and anlegen:
        b = Bestand(mandant_id=m.id, artikel_id=artikel_id, lager=lager, menge=0, mindestbestand=0)
        s.add(b)
        s.flush()
    return b


def lagername(s: Session, m: Mandant, lager: str) -> str:
    if lager == "zentral":
        return "Zentrallager"
    r = s.get(Ressource, lager)
    return f"Fahrzeug {r.name}" if r is not None and r.mandant_id == m.id else lager


def buchen(s: Session, m: Mandant, artikel_id: str, lager: str, menge: float, grund: str, mindestbestand: float | None = None,
           akteur_name: str | None = None, bezug_id: str | None = None) -> Bestand:
    """Zugang (positiv) oder Abgang (negativ); Umbuchung = zwei Buchungen. Negative Bestände sind erlaubt und werden
    im Bestellvorschlag als Fehlmenge geführt (der Monteur verbaut, was er hat; das Lager lernt nach)."""
    a = s.get(Artikel, artikel_id)
    if a is None or a.mandant_id != m.id:
        raise ValueError("Artikel nicht gefunden")
    if lager != "zentral":
        r = s.get(Ressource, lager)
        if r is None or r.mandant_id != m.id:
            raise ValueError("Lagerort muss 'zentral' oder eine Ressourcen-ID (Fahrzeug) sein")
    b = _bestand(s, m, artikel_id, lager)
    b.menge = round((b.menge or 0) + float(menge), 3)
    if mindestbestand is not None:
        b.mindestbestand = float(mindestbestand)
    b.geaendert = datetime.now(timezone.utc)
    domaene.protokollieren(s, m.id, "lager.gebucht", "artikel", artikel_id, "mensch" if akteur_name else "system",
                           {"lager": lager, "menge": menge, "neu": b.menge, "grund": grund, "bezug_id": bezug_id}, akteur_name)
    return b


def bestand_listen(s: Session, m: Mandant, lager: str | None = None) -> list[dict[str, Any]]:
    abfrage = select(Bestand).where(Bestand.mandant_id == m.id)
    if lager:
        abfrage = abfrage.where(Bestand.lager == lager)
    zeilen = []
    for b in s.scalars(abfrage):
        a = b.artikel
        zeilen.append({"id": b.id, "artikel_id": b.artikel_id, "nummer": a.nummer, "bezeichnung": a.bezeichnung, "einheit": a.einheit,
                       "lieferant": a.lieferant, "einkauf_netto": a.einkauf_netto, "lager": b.lager, "lagername": lagername(s, m, b.lager), "menge": b.menge,
                       "mindestbestand": b.mindestbestand, "unter_minimum": b.menge < b.mindestbestand, "geaendert": b.geaendert})
    zeilen.sort(key=lambda z: (z["lagername"], z["nummer"]))
    return zeilen


def bestellvorschlag(s: Session, m: Mandant) -> dict[str, Any]:
    """Je Artikel: Fehlmenge = Σ max(0, Mindestbestand − Bestand) über alle Lagerorte; gruppiert nach Lieferant,
    damit der Warenkorb je Großhändler per IDS-Connect übergeben werden kann."""
    je_artikel: dict[str, dict[str, Any]] = {}
    for b in s.scalars(select(Bestand).where(Bestand.mandant_id == m.id)):
        fehl = max(0.0, (b.mindestbestand or 0) - (b.menge or 0))
        if fehl <= 0:
            continue
        a = b.artikel
        e = je_artikel.setdefault(a.id, {"artikel_id": a.id, "nummer": a.nummer, "bezeichnung": a.bezeichnung, "einheit": a.einheit,
                                         "lieferant": a.lieferant or "ohne Lieferant", "einkauf_netto": a.einkauf_netto, "menge": 0.0, "lager": []})
        e["menge"] = round(e["menge"] + fehl, 3)
        e["lager"].append({"lager": b.lager, "lagername": lagername(s, m, b.lager), "fehlmenge": fehl})
    je_lieferant: dict[str, list[dict[str, Any]]] = {}
    for e in je_artikel.values():
        je_lieferant.setdefault(e["lieferant"], []).append(e)
    gruppen = [{"lieferant": lieferant, "positionen": sorted(pos, key=lambda p: p["nummer"]),
                "summe_einkauf": float(sum((Decimal(p["einkauf_netto"] or 0) * Decimal(str(p["menge"])) for p in pos), Decimal(0)))}
               for lieferant, pos in sorted(je_lieferant.items())]
    return {"gruppen": gruppen, "positionen": sum(len(g["positionen"]) for g in gruppen)}


def rapport_abbuchen(s: Session, m: Mandant, rapport: Rapport, akteur_name: str | None = None) -> list[dict[str, Any]]:
    """Material mit Artikelnummer bucht vom Fahrzeuglager des Monteurs ab (Fallback Zentrallager, wenn das Fahrzeug
    keinen Bestandssatz für den Artikel führt). Freitextmaterial ohne Nummer bleibt unberührt."""
    gebucht = []
    for pos in rapport.material or []:
        nummer = (pos.get("nummer") or pos.get("artikelnummer") or "").strip()
        a = None
        if pos.get("artikel_id"):
            a = s.get(Artikel, pos["artikel_id"])
        elif nummer:
            a = s.scalar(select(Artikel).where(Artikel.mandant_id == m.id, Artikel.nummer == nummer))
        if a is None or a.mandant_id != m.id:
            continue
        try:
            menge = float(str(pos.get("menge", 1)).replace(",", "."))
        except ValueError:
            continue
        lager = rapport.ressource_id if _bestand(s, m, a.id, rapport.ressource_id, anlegen=False) is not None else "zentral"
        b = buchen(s, m, a.id, lager, -menge, f"Rapport {rapport.nummer}", akteur_name=akteur_name, bezug_id=rapport.id)
        gebucht.append({"artikel_id": a.id, "nummer": a.nummer, "lager": lager, "menge": -menge, "bestand": b.menge})
    return gebucht


def bestand_csv(s: Session, m: Mandant) -> bytes:
    puffer = io.StringIO()
    w = csv.writer(puffer, delimiter=";", lineterminator="\r\n")
    w.writerow(["Lager", "Artikelnummer", "Bezeichnung", "Einheit", "Bestand", "Mindestbestand", "Lieferant", "Einkauf netto"])
    for z in bestand_listen(s, m):
        w.writerow([z["lagername"], z["nummer"], z["bezeichnung"], z["einheit"], str(z["menge"]).replace(".", ","),
                    str(z["mindestbestand"]).replace(".", ","), z["lieferant"] or "", (z.get("einkauf_netto") or "").replace(".", ",")])
    return puffer.getvalue().encode("utf-8-sig")

