"""Rechnungen: Erzeugung aus dem Servicefall, Nummernkreis, XRechnung (UBL 2.1, EN 16931) und PDF.

Rechtlicher Rahmen (ohne Gewähr, mit der Kanzlei abstimmen): Pflichtangaben nach § 14 Abs. 4 UStG,
fortlaufende Rechnungsnummer (GoBD), Kleinunternehmerhinweis nach § 19 UStG, für Unternehmen als
Empfänger seit 2025 E-Rechnung nach EN 16931 (XRechnung oder ZUGFeRD); Privatkunden erhalten PDF.
Beträge werden als Decimal gerechnet und als Zeichenketten gespeichert, nie als float.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Kunde, Mandant, Rechnung, Servicefall

CENT = Decimal("0.01")
STANDARD_UST = Decimal("19")
XRECHNUNG_ID = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"


def geld(wert: Any) -> Decimal:
    return Decimal(str(wert)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Position:
    bezeichnung: str
    menge: Decimal
    einheit: str
    einzelpreis_netto: Decimal
    ust_satz: Decimal

    @property
    def netto(self) -> Decimal:
        return geld(self.menge * self.einzelpreis_netto)

    def als_dict(self) -> dict[str, str]:
        return {"bezeichnung": self.bezeichnung, "menge": str(self.menge), "einheit": self.einheit,
                "einzelpreis_netto": str(self.einzelpreis_netto), "ust_satz": str(self.ust_satz), "netto": str(self.netto)}


def positionen_aus_payload(p: dict[str, Any], kleinunternehmer: bool) -> list[Position]:
    satz = Decimal("0") if kleinunternehmer else STANDARD_UST
    if p.get("positionen"):
        return [Position(str(x["bezeichnung"]), Decimal(str(x.get("menge", 1))), str(x.get("einheit", "Stk")),
                         geld(x["einzelpreis_netto"]), Decimal(str(x.get("ust_satz", satz)))) for x in p["positionen"]]
    if "betrag_netto" in p:
        return [Position(p.get("bezeichnung", "Kundendienst laut Rapport"), Decimal("1"), "psch", geld(p["betrag_netto"]), satz)]
    raise ValueError("Rechnung ohne Positionen und ohne betrag_netto")


def summen(positionen: list[Position]) -> tuple[Decimal, Decimal, Decimal, dict[Decimal, tuple[Decimal, Decimal]]]:
    """netto, ust, brutto und Aufschlüsselung je Steuersatz (Steuer je Satz auf die Summe gerundet)."""
    je_satz: dict[Decimal, Decimal] = {}
    for pos in positionen:
        je_satz[pos.ust_satz] = je_satz.get(pos.ust_satz, Decimal("0")) + pos.netto
    netto = sum(je_satz.values(), Decimal("0"))
    aufschluesselung = {satz: (basis, geld(basis * satz / 100)) for satz, basis in je_satz.items()}
    ust = sum((st for _, st in aufschluesselung.values()), Decimal("0"))
    return geld(netto), geld(ust), geld(netto + ust), aufschluesselung


def naechste_nummer(s: Session, m: Mandant, datum: date) -> str:
    """Fortlaufend je Mandant; in PostgreSQL mit Zeilensperre gegen parallele Vergabe."""
    gesperrt = s.execute(select(Mandant).where(Mandant.id == m.id).with_for_update()).scalar_one()
    gesperrt.rechnungsnummer_zaehler += 1
    s.flush()
    return f"{m.stammdaten.get('rechnungsnummer_praefix', 'RE')}-{datum.year}-{gesperrt.rechnungsnummer_zaehler:05d}"


def rechnung_erzeugen(s: Session, m: Mandant, payload: dict[str, Any], datum: date) -> Rechnung:
    kleinunternehmer = bool(m.stammdaten.get("kleinunternehmer"))
    fall = s.get(Servicefall, payload["servicefall_id"]) if payload.get("servicefall_id") else None
    if fall is not None and fall.mandant_id != m.id:
        raise ValueError("Servicefall gehört nicht zum Mandanten")
    kunde_id = payload.get("kunde_id") or (fall.kunde_id if fall else None)
    kunde = s.get(Kunde, kunde_id) if kunde_id else None
    if kunde is None or kunde.mandant_id != m.id:
        raise ValueError("Rechnung ohne Kunde")
    positionen = positionen_aus_payload(payload, kleinunternehmer)
    netto, ust, brutto, _ = summen(positionen)
    r = Rechnung(mandant_id=m.id, nummer=naechste_nummer(s, m, datum), kunde_id=kunde.id,
                 servicefall_id=fall.id if fall else None, datum=datum,
                 leistungsdatum=date.fromisoformat(payload["leistungsdatum"]) if payload.get("leistungsdatum") else datum,
                 faellig=datum + timedelta(days=int(m.stammdaten.get("zahlungsziel_tage", 14))),
                 positionen=[p.als_dict() for p in positionen], netto=str(netto), umsatzsteuer=str(ust), brutto=str(brutto),
                 empfaenger={"name": kunde.name, "strasse": kunde.strasse, "plz": kunde.plz, "ort": kunde.ort, "email": kunde.email},
                 leitweg_id=payload.get("leitweg_id"))
    s.add(r)
    s.flush()
    return r


def storno_erzeugen(s: Session, m: Mandant, original: Rechnung, datum: date) -> Rechnung:
    if original.mandant_id != m.id or original.art != "rechnung":
        raise ValueError("Nur eigene Rechnungen können storniert werden")
    if original.status == "storniert":
        raise ValueError(f"Rechnung {original.nummer} ist bereits storniert")
    positionen = [{**p, "menge": str(-Decimal(p["menge"])), "netto": str(-Decimal(p["netto"]))} for p in original.positionen]
    st = Rechnung(mandant_id=m.id, nummer=naechste_nummer(s, m, datum), kunde_id=original.kunde_id,
                  servicefall_id=original.servicefall_id, art="storno", storno_von_id=original.id, datum=datum,
                  leistungsdatum=original.leistungsdatum, faellig=datum, positionen=positionen,
                  netto=str(-Decimal(original.netto)), umsatzsteuer=str(-Decimal(original.umsatzsteuer)),
                  brutto=str(-Decimal(original.brutto)), empfaenger=original.empfaenger, leitweg_id=original.leitweg_id)
    original.status = "storniert"
    s.add(st)
    s.flush()
    return st


def _positionen(r: Rechnung) -> list[Position]:
    return [Position(p["bezeichnung"], Decimal(p["menge"]), p["einheit"], Decimal(p["einzelpreis_netto"]), Decimal(p["ust_satz"]))
            for p in r.positionen]


# --- XRechnung (UBL 2.1) ----------------------------------------------------------------------
EINHEITEN = {"Stk": "H87", "psch": "LS", "h": "HUR", "m": "MTR", "kg": "KGM", "l": "LTR"}  # UN/ECE Rec. 20


def xrechnung(r: Rechnung, m: Mandant) -> bytes:
    """UBL-Invoice nach EN 16931 im XRechnung-Profil. Pflichtfelder: BT-1 Nummer, BT-2 Datum, BT-3 Typ,
    BT-5 Währung, BT-10 Käuferreferenz (bei B2G Leitweg-ID), Verkäufer mit USt-IdNr./Steuernummer,
    Käufer, Zahlungsinformationen, Steuersummen, Positionen."""
    st = m.stammdaten
    typ = "381" if r.art == "storno" else "380"
    netto, ust, brutto, je_satz = summen(_positionen(r))
    vorzeichen = Decimal("-1") if r.art == "storno" else Decimal("1")
    e = r.empfaenger
    kleinunternehmer = bool(st.get("kleinunternehmer"))

    def steuer_kategorie(satz: Decimal) -> str:
        return "E" if kleinunternehmer else ("Z" if satz == 0 else "S")

    steuerzeilen = "".join(f"""
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">{basis * vorzeichen:.2f}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">{steuer * vorzeichen:.2f}</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>{steuer_kategorie(satz)}</cbc:ID><cbc:Percent>{satz:.2f}</cbc:Percent>
        {"<cbc:TaxExemptionReason>Kein Ausweis von Umsatzsteuer, da Kleinunternehmer gemäß § 19 UStG</cbc:TaxExemptionReason>" if kleinunternehmer else ""}
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal>""" for satz, (basis, steuer) in sorted(je_satz.items()))
    zeilen = "".join(f"""
  <cac:InvoiceLine>
    <cbc:ID>{i}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="{EINHEITEN.get(p.einheit, 'C62')}">{p.menge}</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="EUR">{p.netto:.2f}</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>{escape(p.bezeichnung)}</cbc:Name>
      <cac:ClassifiedTaxCategory><cbc:ID>{steuer_kategorie(p.ust_satz)}</cbc:ID><cbc:Percent>{p.ust_satz:.2f}</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="EUR">{p.einzelpreis_netto:.2f}</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>""" for i, p in enumerate(_positionen(r), 1))
    steuer_id = (f"<cac:PartyTaxScheme><cbc:CompanyID>{escape(st['ust_id'])}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>"
                 if st.get("ust_id") else
                 f"<cac:PartyTaxScheme><cbc:CompanyID>{escape(st.get('steuernummer', ''))}</cbc:CompanyID><cac:TaxScheme><cbc:ID>FC</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ubl:Invoice xmlns:ubl="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
  xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
  xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>{XRECHNUNG_ID}</cbc:CustomizationID>
  <cbc:ProfileID>urn:fdc:peppol.eu:2017:poacc:billing:01:1.0</cbc:ProfileID>
  <cbc:ID>{escape(r.nummer)}</cbc:ID>
  <cbc:IssueDate>{r.datum.isoformat()}</cbc:IssueDate>
  <cbc:DueDate>{r.faellig.isoformat()}</cbc:DueDate>
  <cbc:InvoiceTypeCode>{typ}</cbc:InvoiceTypeCode>
  <cbc:Note>{escape(st.get('rechnungshinweis', 'Vielen Dank für Ihren Auftrag.'))}</cbc:Note>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cbc:BuyerReference>{escape(r.leitweg_id or r.servicefall_id or r.nummer)}</cbc:BuyerReference>
  {"<cac:InvoicePeriod><cbc:StartDate>" + r.leistungsdatum.isoformat() + "</cbc:StartDate><cbc:EndDate>" + r.leistungsdatum.isoformat() + "</cbc:EndDate></cac:InvoicePeriod>" if r.leistungsdatum else ""}
  {"<cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>" + escape(r.storno_von_id or "") + "</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference>" if r.art == "storno" else ""}
  <cac:AccountingSupplierParty><cac:Party>
    <cbc:EndpointID schemeID="EM">{escape(st.get('email', ''))}</cbc:EndpointID>
    <cac:PartyName><cbc:Name>{escape(m.name)}</cbc:Name></cac:PartyName>
    <cac:PostalAddress><cbc:StreetName>{escape(st.get('strasse', ''))}</cbc:StreetName><cbc:CityName>{escape(st.get('ort', ''))}</cbc:CityName>
      <cbc:PostalZone>{escape(st.get('plz', ''))}</cbc:PostalZone><cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    {steuer_id}
    <cac:PartyLegalEntity><cbc:RegistrationName>{escape(m.name)}</cbc:RegistrationName></cac:PartyLegalEntity>
    <cac:Contact><cbc:Name>{escape(st.get('inhaber', m.name))}</cbc:Name><cbc:Telephone>{escape(st.get('telefon', ''))}</cbc:Telephone>
      <cbc:ElectronicMail>{escape(st.get('email', ''))}</cbc:ElectronicMail></cac:Contact>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cbc:EndpointID schemeID="EM">{escape(e.get('email') or 'unbekannt@example.invalid')}</cbc:EndpointID>
    <cac:PartyName><cbc:Name>{escape(e.get('name', ''))}</cbc:Name></cac:PartyName>
    <cac:PostalAddress><cbc:StreetName>{escape(e.get('strasse') or '')}</cbc:StreetName><cbc:CityName>{escape(e.get('ort') or '')}</cbc:CityName>
      <cbc:PostalZone>{escape(e.get('plz') or '')}</cbc:PostalZone><cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    <cac:PartyLegalEntity><cbc:RegistrationName>{escape(e.get('name', ''))}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode>
    <cac:PayeeFinancialAccount><cbc:ID>{escape(st.get('iban', ''))}</cbc:ID><cbc:Name>{escape(m.name)}</cbc:Name>
      <cac:FinancialInstitutionBranch><cbc:ID>{escape(st.get('bic', ''))}</cbc:ID></cac:FinancialInstitutionBranch></cac:PayeeFinancialAccount></cac:PaymentMeans>
  <cac:PaymentTerms><cbc:Note>Zahlbar bis {r.faellig.strftime('%d.%m.%Y')} ohne Abzug.</cbc:Note></cac:PaymentTerms>
  <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">{ust * vorzeichen:.2f}</cbc:TaxAmount>{steuerzeilen}
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">{netto * vorzeichen:.2f}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">{netto * vorzeichen:.2f}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">{brutto * vorzeichen:.2f}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">{brutto * vorzeichen:.2f}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>{zeilen}
</ubl:Invoice>
"""
    return xml.encode("utf-8")


# --- PDF -------------------------------------------------------------------------------------
def dokument_pdf(m: Mandant, titel: str, nummer: str, datum: date, empfaenger: dict[str, Any], positionen: list[Position],
                 netto: Decimal, brutto: Decimal, kopfzeilen: list[str] = (), vortext: list[str] = (), schluss: list[str] = (),
                 vorzeichen: int = 1) -> bytes:
    """Gemeinsames Layout für Rechnung, Storno, Angebot und Mahnung: Absender, Empfänger, Positionen, Summen, Fußzeile."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    st = m.stammdaten
    puffer = io.BytesIO()
    c = canvas.Canvas(puffer, pagesize=A4)
    c.setTitle(f"{titel} {nummer}")
    breite, hoehe = A4
    y = hoehe - 20 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, y, m.name)
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, y - 5 * mm, f"{st.get('strasse', '')} · {st.get('plz', '')} {st.get('ort', '')} · {st.get('telefon', '')} · {st.get('email', '')}")
    e = empfaenger
    y -= 25 * mm
    c.setFont("Helvetica", 10)
    for zeile in (e.get("name", ""), e.get("strasse") or "", f"{e.get('plz') or ''} {e.get('ort') or ''}".strip()):
        c.drawString(20 * mm, y, zeile)
        y -= 5 * mm
    y -= 10 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, y, f"{titel} {nummer}")
    c.setFont("Helvetica", 10)
    c.drawRightString(breite - 20 * mm, y, f"Datum: {datum.strftime('%d.%m.%Y')}")
    for zeile in kopfzeilen:
        y -= 6 * mm
        c.drawRightString(breite - 20 * mm, y, zeile)
    y -= 10 * mm
    for zeile in vortext:
        c.drawString(20 * mm, y, zeile[:110])
        y -= 5 * mm
    if positionen:
        y -= 4 * mm
        c.setFont("Helvetica-Bold", 9)
        for x, text in ((20, "Pos."), (32, "Bezeichnung"), (120, "Menge"), (140, "Einzelpreis"), (165, "Netto")):
            c.drawString(x * mm, y, text)
        y -= 2 * mm
        c.line(20 * mm, y, breite - 20 * mm, y)
        c.setFont("Helvetica", 9)
        for i, p in enumerate(positionen, 1):
            y -= 6 * mm
            if y < 40 * mm:
                c.showPage()
                c.setFont("Helvetica", 9)
                y = hoehe - 25 * mm
            c.drawString(20 * mm, y, str(i))
            c.drawString(32 * mm, y, p.bezeichnung[:60])
            c.drawString(120 * mm, y, f"{p.menge} {p.einheit}")
            c.drawRightString(160 * mm, y, f"{p.einzelpreis_netto:.2f} €".replace(".", ","))
            c.drawRightString(190 * mm, y, f"{p.netto:.2f} €".replace(".", ","))
        _, _, _, je_satz = summen(positionen)
        y -= 10 * mm
        c.drawRightString(160 * mm, y, "Netto")
        c.drawRightString(190 * mm, y, f"{netto:.2f} €".replace(".", ","))
        for satz, (_, steuer) in sorted(je_satz.items()):
            y -= 5 * mm
            c.drawRightString(160 * mm, y, f"Umsatzsteuer {satz:.0f} %")
            c.drawRightString(190 * mm, y, f"{(steuer * vorzeichen):.2f} €".replace(".", ","))
        y -= 6 * mm
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(160 * mm, y, "Brutto")
        c.drawRightString(190 * mm, y, f"{brutto:.2f} €".replace(".", ","))
    c.setFont("Helvetica", 9)
    y -= 14 * mm
    if st.get("kleinunternehmer") and positionen:
        c.drawString(20 * mm, y, "Kein Ausweis von Umsatzsteuer, da Kleinunternehmer gemäß § 19 UStG.")
        y -= 5 * mm
    for zeile in schluss:
        c.drawString(20 * mm, y, zeile[:110])
        y -= 5 * mm
    c.setFont("Helvetica", 7)
    fuss = " · ".join(x for x in (m.name, st.get("inhaber"), f"USt-IdNr. {st['ust_id']}" if st.get("ust_id") else
                                   (f"Steuernummer {st['steuernummer']}" if st.get("steuernummer") else None),
                                   st.get("handwerksrolle")) if x)
    c.drawString(20 * mm, 15 * mm, fuss)
    c.showPage()
    c.save()
    return puffer.getvalue()


def pdf(r: Rechnung, m: Mandant) -> bytes:
    st = m.stammdaten
    storno = r.art == "storno"
    kopf = [f"Leistungsdatum: {r.leistungsdatum.strftime('%d.%m.%Y')}"] if r.leistungsdatum else []
    vortext = [f"Storno zur Rechnung mit der internen Kennung {r.storno_von_id}"] if storno and r.storno_von_id else []
    schluss = [] if storno else [f"Zahlbar bis {r.faellig.strftime('%d.%m.%Y')} ohne Abzug auf IBAN {st.get('iban', '')} ({st.get('bank', '')})."]
    if st.get("rechnungshinweis"):
        schluss.append(str(st["rechnungshinweis"]))
    return dokument_pdf(m, "Stornorechnung" if storno else "Rechnung", r.nummer, r.datum, r.empfaenger, _positionen(r),
                        Decimal(r.netto), Decimal(r.brutto), kopf, vortext, schluss, vorzeichen=-1 if storno else 1)
