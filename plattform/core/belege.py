"""Belegeingang: Lieferantenrechnungen und Quittungen erfassen, strukturiert auslesen und dem Auftrag zuordnen.

Stufen der Erkennung (quelle):
  xrechnung  XML nach EN 16931 (UBL): Felder werden ohne Texterkennung übernommen
  zugferd    PDF mit eingebettetem CII-XML (ZUGFeRD/Factur-X), wie es Großhändler liefern
  ocr        PDF-Text oder Texterkennung (tesseract, falls installiert) plus Heuristiken; Konfidenz niedrig,
             der Beleg bleibt im Status "erfasst", bis das Büro ihn prüft
  manuell    Felder von Hand eingegeben (Foto als Anhang)

Grenze (§ 6 Nr. 3 StBerG): keine Kontierung. Der Beleg erhält eine Kategorie (Material, Fremdleistung,
Fahrzeug, Sonstiges) für die Nachkalkulation; Konten vergibt die Kanzlei.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree as ET

ZUGFERD_NAMEN = ("factur-x.xml", "zugferd-invoice.xml", "ZUGFeRD-invoice.xml", "xrechnung.xml", "order-x.xml")
MAX_BYTES = 10 * 1024 * 1024


class Erkennung(dict):
    """Ergebnis der Extraktion: Felder plus quelle, konfidenz (0..1) und hinweise."""


# --- XML (UBL und CII) -------------------------------------------------------------------------
def _lokal(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _finde(el: ET.Element, *pfad: str) -> ET.Element | None:
    """Sucht entlang lokaler Elementnamen (namespace-unabhängig, weil UBL/CII-Versionen variieren)."""
    aktuell: list[ET.Element] = [el]
    for name in pfad:
        naechste = [k for e in aktuell for k in e if _lokal(k.tag) == name]
        if not naechste:
            return None
        aktuell = naechste
    return aktuell[0]


def _text(el: ET.Element | None, *pfad: str) -> str | None:
    if el is None:
        return None
    ziel = _finde(el, *pfad) if pfad else el
    return (ziel.text or "").strip() if ziel is not None and ziel.text else None


def _satz(wert: str | None) -> str | None:
    """Steuersatz als ganze Zahl ("19.00" → "19"), wie ihn Beleg.ust_satz erwartet."""
    d = _dezimal(wert)
    return None if d is None else str(int(d))


def _dezimal(wert: str | None) -> Decimal | None:
    if wert is None:
        return None
    try:
        return Decimal(wert.replace(",", ".")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _datum(wert: str | None) -> date | None:
    if not wert:
        return None
    wert = wert.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(wert[:10] if fmt == "%Y-%m-%d" else wert, fmt).date()
        except ValueError:
            continue
    return None


def aus_ubl(wurzel: ET.Element) -> Erkennung:
    lieferant = _finde(wurzel, "AccountingSupplierParty", "Party")
    name = (_text(lieferant, "PartyName", "Name") or _text(lieferant, "PartyLegalEntity", "RegistrationName") or "Unbekannt")
    summe = _finde(wurzel, "LegalMonetaryTotal")
    steuer = _finde(wurzel, "TaxTotal")
    positionen = []
    for zeile in (k for k in wurzel if _lokal(k.tag) in ("InvoiceLine", "CreditNoteLine")):
        menge_el = _finde(zeile, "InvoicedQuantity")
        if menge_el is None:
            menge_el = _finde(zeile, "CreditedQuantity")
        positionen.append({"bezeichnung": _text(zeile, "Item", "Name") or "", "menge": _text(menge_el) or "1",
                           "einheit": (menge_el.get("unitCode") if menge_el is not None else None) or "C62",
                           "netto": _text(zeile, "LineExtensionAmount") or "0"})
    return Erkennung(quelle="xrechnung", konfidenz=1.0, lieferant=name, nummer=_text(wurzel, "ID"),
                     datum=_datum(_text(wurzel, "IssueDate")), faellig=_datum(_text(wurzel, "DueDate")),
                     netto=_dezimal(_text(summe, "TaxExclusiveAmount")), brutto=_dezimal(_text(summe, "PayableAmount") or _text(summe, "TaxInclusiveAmount")),
                     umsatzsteuer=_dezimal(_text(steuer, "TaxAmount")), ust_satz=_satz(_text(steuer, "TaxSubtotal", "TaxCategory", "Percent")),
                     positionen=positionen, hinweise=[])


def aus_cii(wurzel: ET.Element) -> Erkennung:
    dok = _finde(wurzel, "ExchangedDocument")
    trans = _finde(wurzel, "SupplyChainTradeTransaction")
    verkaeufer = _finde(trans, "ApplicableHeaderTradeAgreement", "SellerTradeParty") if trans is not None else None
    abrechnung = _finde(trans, "ApplicableHeaderTradeSettlement") if trans is not None else None
    summe = _finde(abrechnung, "SpecifiedTradeSettlementHeaderMonetarySummation") if abrechnung is not None else None
    positionen = []
    if trans is not None:
        for zeile in (k for k in trans if _lokal(k.tag) == "IncludedSupplyChainTradeLineItem"):
            positionen.append({"bezeichnung": _text(zeile, "SpecifiedTradeProduct", "Name") or "",
                               "menge": _text(zeile, "SpecifiedLineTradeDelivery", "BilledQuantity") or "1", "einheit": "C62",
                               "netto": _text(zeile, "SpecifiedLineTradeSettlement", "SpecifiedTradeSettlementLineMonetarySummation", "LineTotalAmount") or "0"})
    return Erkennung(quelle="zugferd", konfidenz=1.0, lieferant=_text(verkaeufer, "Name") or "Unbekannt", nummer=_text(dok, "ID"),
                     datum=_datum(_text(dok, "IssueDateTime", "DateTimeString")),
                     faellig=_datum(_text(abrechnung, "SpecifiedTradePaymentTerms", "DueDateDateTime", "DateTimeString")),
                     netto=_dezimal(_text(summe, "TaxBasisTotalAmount")), brutto=_dezimal(_text(summe, "DuePayableAmount") or _text(summe, "GrandTotalAmount")),
                     umsatzsteuer=_dezimal(_text(summe, "TaxTotalAmount")), ust_satz=_satz(_text(abrechnung, "ApplicableTradeTax", "RateApplicablePercent")),
                     positionen=positionen, hinweise=[])


def aus_xml(inhalt: bytes) -> Erkennung | None:
    try:
        wurzel = ET.fromstring(inhalt)
    except ET.ParseError:
        return None
    name = _lokal(wurzel.tag)
    if name in ("Invoice", "CreditNote"):
        return aus_ubl(wurzel)
    if name == "CrossIndustryInvoice":
        return aus_cii(wurzel)
    return None


# --- PDF und Bilder ------------------------------------------------------------------------------
def pdf_eingebettetes_xml(inhalt: bytes) -> bytes | None:
    from pypdf import PdfReader

    try:
        leser = PdfReader(io.BytesIO(inhalt))
        anhaenge = leser.attachments
    except Exception:  # noqa: BLE001  beschädigtes PDF: dann Textweg
        return None
    for name in ZUGFERD_NAMEN:
        if name in anhaenge:
            return anhaenge[name][0]
    for name, daten in anhaenge.items():
        if name.lower().endswith(".xml"):
            return daten[0]
    return None


def pdf_text(inhalt: bytes) -> str:
    from pypdf import PdfReader

    try:
        return "\n".join((seite.extract_text() or "") for seite in PdfReader(io.BytesIO(inhalt)).pages[:5])
    except Exception:  # noqa: BLE001
        return ""


def ocr_text(inhalt: bytes, endung: str) -> str:
    """Texterkennung über die tesseract-CLI (Debian-Paket tesseract-ocr-deu); ohne Installation leer."""
    if not shutil.which("tesseract"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=endung, delete=True) as f:
        f.write(inhalt)
        f.flush()
        try:
            r = subprocess.run(["tesseract", f.name, "stdout", "-l", "deu+eng", "--psm", "6"], capture_output=True,
                               text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return r.stdout if r.returncode == 0 else ""


# --- Heuristiken über Text ---------------------------------------------------------------------
BETRAG = r"(-?\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})|-?\d+(?:[.,]\d{2}))"
MUSTER_NUMMER = re.compile(r"(?:Rechnungs?-?\s?(?:Nr\.?|Nummer)|Beleg-?\s?(?:Nr\.?|Nummer)|Invoice\s?(?:No\.?|Number))\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/._]{2,})", re.I)
MUSTER_DATUM = re.compile(r"(?:Rechnungsdatum|Belegdatum|Datum)\s*[:]?\s*(\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{2}-\d{2})", re.I)
MUSTER_IRGENDEIN_DATUM = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b")
MUSTER_BRUTTO = re.compile(r"(?:Rechnungsbetrag|Gesamtbetrag|Endbetrag|Zu zahlen|Zahlbetrag|Gesamt\s*brutto|Brutto|Summe)\D{0,25}?" + BETRAG, re.I)
MUSTER_NETTO = re.compile(r"(?:Netto(?:betrag|summe)?|Zwischensumme|Summe netto)\D{0,25}?" + BETRAG, re.I)
MUSTER_UST = re.compile(r"(?:USt|MwSt|Umsatzsteuer|Mehrwertsteuer)\.?\s*(?:\(?\s*)?(19|7|0)\s*%", re.I)
MUSTER_KOMMISSION = re.compile(r"(?:Kommission|Komm\.|Auftrag(?:s-?Nr\.?)?|Bauvorhaben|BV|Projekt)\s*[:#]?\s*([^\n]{3,60})", re.I)
MUSTER_FAELLIG = re.compile(r"(?:zahlbar bis|fällig(?: am)?|Zahlungsziel)\s*[:]?\s*(\d{1,2}\.\d{1,2}\.\d{2,4})", re.I)
MUSTER_SKONTO = re.compile(r"(\d(?:[.,]\d)?)\s*%\s*Skonto", re.I)


def _zahl(text: str) -> Decimal | None:
    text = text.replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def aus_text(text: str) -> Erkennung:
    zeilen = [z.strip() for z in text.splitlines() if z.strip()]
    hinweise: list[str] = []
    lieferant = next((z for z in zeilen[:8] if len(z) > 3 and not re.search(r"\d{5}|Rechnung|Datum", z, re.I)), None)
    if not lieferant:
        lieferant, _ = "Unbekannt", hinweise.append("Lieferant nicht erkannt")
    nummer = (m.group(1) if (m := MUSTER_NUMMER.search(text)) else None)
    datum = _datum(m.group(1)) if (m := MUSTER_DATUM.search(text)) else None
    if datum is None and (m := MUSTER_IRGENDEIN_DATUM.search(text)):
        datum = _datum(m.group(1))
        hinweise.append("Datum geraten (erstes Datum im Text)")
    brutto = _zahl(m.group(1)) if (m := MUSTER_BRUTTO.search(text)) else None
    netto = _zahl(m.group(1)) if (m := MUSTER_NETTO.search(text)) else None
    satz = m.group(1) if (m := MUSTER_UST.search(text)) else "19"
    if brutto is None and netto is not None:
        brutto = (netto * (1 + Decimal(satz) / 100)).quantize(Decimal("0.01"))
        hinweise.append("Brutto aus Netto berechnet")
    if brutto is not None and netto is None:
        netto = (brutto / (1 + Decimal(satz) / 100)).quantize(Decimal("0.01"))
        hinweise.append("Netto aus Brutto berechnet")
    if brutto is None:
        hinweise.append("Betrag nicht erkannt")
    treffer = sum(x is not None for x in (nummer, datum, brutto)) + (lieferant != "Unbekannt")
    return Erkennung(quelle="ocr", konfidenz=round(treffer / 4, 2), lieferant=lieferant[:200], nummer=nummer, datum=datum,
                     faellig=_datum(m.group(1)) if (m := MUSTER_FAELLIG.search(text)) else None,
                     netto=netto, brutto=brutto, umsatzsteuer=(brutto - netto) if brutto is not None and netto is not None else None,
                     ust_satz=satz, positionen=[], hinweise=hinweise,
                     kommission=(m.group(1).strip() if (m := MUSTER_KOMMISSION.search(text)) else None),
                     skonto_prozent=(m.group(1).replace(",", ".") if (m := MUSTER_SKONTO.search(text)) else None))


# --- Einstieg ----------------------------------------------------------------------------------
def erkennen(inhalt: bytes, name: str, mime: str) -> Erkennung:
    """Wählt den Erkennungsweg nach Dateityp; liefert immer ein Ergebnis, notfalls mit Hinweisen."""
    endung = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if mime in ("application/xml", "text/xml") or endung == ".xml":
        return aus_xml(inhalt) or Erkennung(quelle="ocr", konfidenz=0.0, lieferant="Unbekannt", hinweise=["XML ist keine Rechnung nach EN 16931"])
    if mime == "application/pdf" or endung == ".pdf":
        if (xml := pdf_eingebettetes_xml(inhalt)) and (e := aus_xml(xml)):
            e["quelle"] = "zugferd"
            return e
        text = pdf_text(inhalt)
        if len(text.strip()) < 40:  # gescanntes PDF ohne Textebene
            text = ocr_text(inhalt, ".pdf")
        e = aus_text(text) if text.strip() else Erkennung(quelle="ocr", konfidenz=0.0, lieferant="Unbekannt",
                                                           hinweise=["Kein Text im PDF; Texterkennung nicht verfügbar"])
        return e
    if mime.startswith("image/") or endung in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"):
        text = ocr_text(inhalt, endung or ".jpg")
        if not text.strip():
            return Erkennung(quelle="ocr", konfidenz=0.0, lieferant="Unbekannt",
                             hinweise=["Texterkennung nicht verfügbar (tesseract fehlt) – Felder bitte von Hand ergänzen"])
        return aus_text(text)
    return Erkennung(quelle="ocr", konfidenz=0.0, lieferant="Unbekannt", hinweise=[f"Dateityp {mime or endung} nicht unterstützt"])


def felder_fuer_beleg(e: Erkennung, heute: date) -> dict[str, Any]:
    """Erkennung in die Spalten von modelle.Beleg übersetzen; fehlende Werte werden mit Platzhaltern gefüllt,
    die das Büro vor der Übergabe korrigiert (Status erfasst)."""
    brutto = e.get("brutto") or Decimal("0.00")
    netto = e.get("netto")
    satz = str(e.get("ust_satz") or "19").split(".")[0]
    if netto is None:
        netto = (brutto / (1 + Decimal(satz) / 100)).quantize(Decimal("0.01"))
    ust = e.get("umsatzsteuer") if e.get("umsatzsteuer") is not None else (brutto - netto)
    return {"lieferant": e.get("lieferant") or "Unbekannt", "nummer": e.get("nummer"), "datum": e.get("datum") or heute,
            "faellig": e.get("faellig"), "netto": f"{netto:.2f}", "umsatzsteuer": f"{ust:.2f}", "brutto": f"{brutto:.2f}",
            "ust_satz": satz, "skonto_prozent": e.get("skonto_prozent"), "positionen": e.get("positionen") or [],
            "quelle": e.get("quelle", "manuell"),
            "erkennung": {"konfidenz": e.get("konfidenz", 0), "hinweise": e.get("hinweise", []), "kommission": e.get("kommission")}}
