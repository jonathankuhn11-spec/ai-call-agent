"""Offene Posten, Zahlungen, Bankimport und Mahnwesen.

Zahlungen werden je Rechnung gebucht; der Rechnungsstatus folgt (gestellt → teilbezahlt → bezahlt). Der Bankimport
liest CSV-Umsätze (Sparkasse/VR/CAMT-CSV-Exporte, Spalten werden an Überschriften erkannt) und ordnet sie über die
Rechnungsnummer im Verwendungszweck zu. Das Mahnwesen schlägt je überfälliger Rechnung die nächste Stufe vor:
Stufe 1 Zahlungserinnerung (Klasse B), Stufe 2 und 3 Mahnung mit Gebühr (Klasse C, Inhaber). Verzugszinsen nach
§ 288 BGB werden ausgewiesen, nicht berechnet; Inkasso oder Mahnbescheid bleiben Entscheidung des Betriebs.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene
from core.modelle import Mahnung, Mandant, Rechnung, Zahlung
from core.rechnung import dokument_pdf, geld, girocode

MAHNSTUFEN = {1: ("Zahlungserinnerung", "zahlungserinnerung_senden"), 2: ("Mahnung", "mahnung_senden"), 3: ("Letzte Mahnung", "mahnung_senden")}
STANDARD = {"mahn_karenz_tage": 7, "mahn_frist_tage": 10, "mahngebuehr_stufe2": "5.00", "mahngebuehr_stufe3": "10.00"}


def einstellung(m: Mandant, schluessel: str) -> Any:
    return m.stammdaten.get(schluessel, STANDARD[schluessel])


def gezahlt(s: Session, r: Rechnung) -> Decimal:
    return sum((Decimal(z.betrag) for z in s.scalars(select(Zahlung).where(Zahlung.rechnung_id == r.id))), Decimal("0"))


def offen(s: Session, r: Rechnung) -> Decimal:
    return geld(Decimal(r.brutto) - gezahlt(s, r))


def status_nachfuehren(s: Session, r: Rechnung) -> str:
    if r.status == "storniert" or r.art == "storno":
        return r.status
    rest = offen(s, r)
    r.status = "bezahlt" if rest <= 0 else ("teilbezahlt" if gezahlt(s, r) > 0 else "gestellt")
    return r.status


def zahlung_buchen(s: Session, m: Mandant, r: Rechnung, betrag: Any, datum: date, art: str = "ueberweisung", referenz: str | None = None,
                   quelle: str = "manuell", akteur_name: str | None = None) -> Zahlung:
    if r.mandant_id != m.id or r.art != "rechnung":
        raise ValueError("Zahlungen werden nur auf eigene Rechnungen gebucht")
    if r.status == "storniert":
        raise ValueError(f"Rechnung {r.nummer} ist storniert")
    betrag = geld(betrag)
    if betrag == 0:
        raise ValueError("Betrag 0")
    z = Zahlung(mandant_id=m.id, rechnung_id=r.id, datum=datum, betrag=str(betrag), art=art, referenz=referenz, quelle=quelle)
    s.add(z)
    s.flush()
    status = status_nachfuehren(s, r)
    domaene.protokollieren(s, m.id, "rechnung.zahlung", "rechnung", r.id, "mensch" if quelle == "manuell" else "system",
                           {"nummer": r.nummer, "betrag": str(betrag), "art": art, "status": status}, akteur_name)
    return z


def offene_posten(s: Session, m: Mandant, heute: date) -> list[dict[str, Any]]:
    """Alle nicht vollständig bezahlten Rechnungen mit Restbetrag, Überfälligkeit und letzter Mahnstufe."""
    ergebnis = []
    for r in s.scalars(select(Rechnung).where(Rechnung.mandant_id == m.id, Rechnung.art == "rechnung",
                                              Rechnung.status.in_(("gestellt", "teilbezahlt"))).order_by(Rechnung.faellig)):
        rest = offen(s, r)
        if rest <= 0:
            continue
        letzte = s.scalar(select(Mahnung).where(Mahnung.rechnung_id == r.id).order_by(Mahnung.stufe.desc()).limit(1))
        ergebnis.append({"rechnung_id": r.id, "nummer": r.nummer, "kunde_id": r.kunde_id, "kunde": r.empfaenger.get("name"), "datum": r.datum,
                         "faellig": r.faellig, "brutto": r.brutto, "offen": str(rest), "tage_ueberfaellig": max(0, (heute - r.faellig).days),
                         "mahnstufe": letzte.stufe if letzte else 0, "mahnfrist": letzte.frist if letzte else None, "status": r.status})
    return ergebnis


def naechste_stufe(s: Session, r: Rechnung, heute: date, m: Mandant) -> int | None:
    """Welche Mahnstufe heute fällig ist; None, wenn Karenz oder Mahnfrist noch läuft oder Stufe 3 erreicht ist."""
    letzte = s.scalar(select(Mahnung).where(Mahnung.rechnung_id == r.id).order_by(Mahnung.stufe.desc()).limit(1))
    if letzte is None:
        return 1 if heute > r.faellig + timedelta(days=int(einstellung(m, "mahn_karenz_tage"))) else None
    if letzte.stufe >= 3 or heute <= letzte.frist:
        return None
    return letzte.stufe + 1


def mahnlauf(s: Session, m: Mandant, heute: date, akteur: str = "system") -> list[dict[str, Any]]:
    """Legt je fälliger Stufe eine Freigabe an (B für die Erinnerung, C für Mahnungen); nie doppelt je Rechnung und Stufe."""
    vorschlaege = []
    offene_freigaben = {(f.payload.get("rechnung_id"), f.payload.get("stufe")) for f in s.scalars(
        select(domaene.Freigabe).where(domaene.Freigabe.mandant_id == m.id, domaene.Freigabe.status == "offen",
                                       domaene.Freigabe.aktion.in_(("zahlungserinnerung_senden", "mahnung_senden"))))}
    for posten in offene_posten(s, m, heute):
        r = s.get(Rechnung, posten["rechnung_id"])
        stufe = naechste_stufe(s, r, heute, m)
        if stufe is None or (r.id, stufe) in offene_freigaben:
            continue
        titel, aktion = MAHNSTUFEN[stufe]
        gebuehr = "0.00" if stufe == 1 else str(geld(einstellung(m, f"mahngebuehr_stufe{stufe}")))
        f = domaene.aktion_beantragen(s, m, aktion, {"rechnung_id": r.id, "kunde_id": r.kunde_id, "stufe": stufe, "gebuehr": gebuehr, "offen": posten["offen"]},
                                      akteur, r.servicefall_id, f"{titel} zu {r.nummer}: {posten['offen'].replace('.', ',')} € offen, "
                                                                 f"{posten['tage_ueberfaellig']} Tage überfällig")
        vorschlaege.append({"rechnung": r.nummer, "stufe": stufe, "freigabe_id": f.id, "offen": posten["offen"]})
    return vorschlaege


def mahnung_erzeugen(s: Session, m: Mandant, r: Rechnung, stufe: int, heute: date, gebuehr: str) -> Mahnung:
    """Wirkung der Freigabe: Mahnung festhalten und per Outbox zustellen."""
    mahnung = Mahnung(mandant_id=m.id, rechnung_id=r.id, stufe=stufe, datum=heute, frist=heute + timedelta(days=int(einstellung(m, "mahn_frist_tage"))),
                      gebuehr=gebuehr, offen=str(offen(s, r)))
    s.add(mahnung)
    s.flush()
    domaene.protokollieren(s, m.id, "rechnung.gemahnt", "rechnung", r.id, "system", {"nummer": r.nummer, "stufe": stufe, "gebuehr": gebuehr, "frist": mahnung.frist.isoformat()})
    domaene.outbox_schreiben(s, m.id, "endkunde", "mahnung_zustellen", {"mahnung_id": mahnung.id, "rechnung_id": r.id, "kunde_id": r.kunde_id},
                             f"mahnung_zustellen:{mahnung.id}")
    return mahnung


def mahnung_pdf(mh: Mahnung, m: Mandant) -> bytes:
    r = mh.rechnung
    titel = MAHNSTUFEN[mh.stufe][0]
    st = m.stammdaten
    gesamt = geld(Decimal(mh.offen) + Decimal(mh.gebuehr))
    vortext = ["Sehr geehrte Damen und Herren,", "",
               f"zu unserer Rechnung {r.nummer} vom {r.datum.strftime('%d.%m.%Y')}, fällig am {r.faellig.strftime('%d.%m.%Y')}, konnten wir",
               f"bislang keinen vollständigen Zahlungseingang feststellen. Offen sind {mh.offen.replace('.', ',')} €."]
    if mh.stufe == 1:
        vortext += ["Sicher haben Sie die Zahlung nur übersehen. Bitte überweisen Sie den Betrag bis zum", f"{mh.frist.strftime('%d.%m.%Y')}."]
    else:
        vortext += [f"Wir bitten um Zahlung bis zum {mh.frist.strftime('%d.%m.%Y')} einschließlich Mahngebühr von {mh.gebuehr.replace('.', ',')} €,",
                    f"insgesamt {str(gesamt).replace('.', ',')} €. Seit Fälligkeit befinden Sie sich in Verzug (§ 286 BGB); wir behalten uns",
                    "Verzugszinsen nach § 288 BGB vor." + (" Nach fruchtlosem Ablauf übergeben wir die Forderung dem gerichtlichen Mahnverfahren." if mh.stufe == 3 else "")]
    vortext += ["", "Sollte sich Ihre Zahlung mit diesem Schreiben überschnitten haben, betrachten Sie es bitte als gegenstandslos."]
    schluss = [f"Zahlung bitte auf IBAN {st.get('iban', '')} ({st.get('bank', '')}), Verwendungszweck {r.nummer}."]
    return dokument_pdf(m, titel, r.nummer, mh.datum, r.empfaenger, [], Decimal(mh.offen), gesamt,
                        [f"Zur Rechnung {r.nummer}", f"Zahlungsfrist: {mh.frist.strftime('%d.%m.%Y')}"], vortext, schluss,
                        qr=girocode(m, gesamt, f"{r.nummer} {titel}"))


# --- Bankimport ---------------------------------------------------------------------------------
SPALTEN = {"datum": ("buchungstag", "buchungsdatum", "valutadatum", "datum", "wertstellung"),
           "betrag": ("betrag", "umsatz", "amount"),
           "zweck": ("verwendungszweck", "buchungstext", "verwendungszweck/buchungstext", "beschreibung", "purpose"),
           "name": ("beguenstigter/zahlungspflichtiger", "name zahlungsbeteiligter", "auftraggeber/empfänger", "name", "zahlungspflichtiger")}


def _spalte(kopf: list[str], kandidaten: tuple[str, ...]) -> int | None:
    norm = [k.strip().strip('"').lower() for k in kopf]
    for kand in kandidaten:
        for i, k in enumerate(norm):
            if k == kand or k.startswith(kand):
                return i
    return None


def _betrag(wert: str) -> Decimal | None:
    w = wert.strip().replace("€", "").replace(" ", "")
    if "," in w and "." in w:
        w = w.replace(".", "")
    w = w.replace(",", ".")
    try:
        return geld(Decimal(w))
    except InvalidOperation:
        return None


def _datum(wert: str) -> date | None:
    w = wert.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(w, fmt).date()
        except ValueError:
            continue
    return None


def bankimport(s: Session, m: Mandant, inhalt: bytes, heute: date, buchen: bool = True, akteur_name: str | None = None) -> dict[str, Any]:
    """CSV-Umsätze lesen, Rechnungsnummer im Verwendungszweck suchen, Zahlungen buchen; Rest zur Nacharbeit melden."""
    text = inhalt.decode("utf-8-sig", errors="replace") if b"\xef\xbb\xbf" in inhalt[:3] or inhalt[:1] != b"\xff" else inhalt.decode("cp1252", errors="replace")
    if "�" in text:
        text = inhalt.decode("cp1252", errors="replace")
    trenner = ";" if text.count(";") >= text.count(",") else ","
    zeilen = list(csv.reader(io.StringIO(text), delimiter=trenner))
    if not zeilen:
        raise ValueError("Leere Datei")
    kopf = zeilen[0]
    ix = {k: _spalte(kopf, v) for k, v in SPALTEN.items()}
    if ix["betrag"] is None or ix["zweck"] is None:
        raise ValueError(f"Spalten Betrag/Verwendungszweck nicht erkannt (Überschriften: {', '.join(kopf[:8])})")
    praefix = re.escape(m.stammdaten.get("rechnungsnummer_praefix", "RE"))
    muster = re.compile(praefix + r"-?\s?(\d{4})-?\s?(\d{5})", re.I)
    nummern = {r.nummer: r for r in s.scalars(select(Rechnung).where(Rechnung.mandant_id == m.id, Rechnung.art == "rechnung"))}
    zugeordnet, unklar, uebersprungen = [], [], 0
    vorhandene = {(z.referenz or "") for z in s.scalars(select(Zahlung).where(Zahlung.mandant_id == m.id, Zahlung.quelle == "bankimport"))}
    for n, zeile in enumerate(zeilen[1:], 2):
        if len(zeile) <= max(ix["betrag"], ix["zweck"]):
            continue
        betrag = _betrag(zeile[ix["betrag"]])
        if betrag is None or betrag <= 0:
            uebersprungen += 1  # Abgänge und Leerzeilen
            continue
        zweck = zeile[ix["zweck"]]
        datum = _datum(zeile[ix["datum"]]) if ix["datum"] is not None else None
        name = zeile[ix["name"]] if ix["name"] is not None else ""
        referenz = f"bank:{datum or heute}:{betrag}:{zweck[:60]}"
        treffer = muster.search(zweck)
        nummer = f"{m.stammdaten.get('rechnungsnummer_praefix', 'RE')}-{treffer.group(1)}-{treffer.group(2)}" if treffer else None
        r = nummern.get(nummer or "")
        if r is None or r.status in ("bezahlt", "storniert"):
            unklar.append({"zeile": n, "datum": datum, "betrag": str(betrag), "name": name, "zweck": zweck[:120], "nummer": nummer,
                           "grund": "keine offene Rechnung zur Nummer" if nummer else "keine Rechnungsnummer im Verwendungszweck"})
            continue
        if referenz in vorhandene:
            uebersprungen += 1
            continue
        if buchen:
            zahlung_buchen(s, m, r, betrag, datum or heute, "ueberweisung", referenz, "bankimport", akteur_name)
        zugeordnet.append({"zeile": n, "nummer": r.nummer, "betrag": str(betrag), "datum": datum, "name": name, "status": r.status})
    return {"zugeordnet": zugeordnet, "unklar": unklar, "uebersprungen": uebersprungen, "gebucht": buchen}
