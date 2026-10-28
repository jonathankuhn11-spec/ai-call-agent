"""Excel-Exporte (XLSX über openpyxl, BSD-3-Clause): Auswertungen, Kundenliste, offene Posten, Termine.

Die Arbeitsmappen entstehen im Speicher und werden als Download ausgeliefert; Zahlen sind echte Zellwerte
(Decimal → float) mit Euro-Zahlenformat, Daten echte Datumswerte, damit Filter und Pivot in Excel/LibreOffice
funktionieren. Keine Formeln, keine Makros."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Kunde, Mandant, Termin

EURO = '#,##0.00 "€"'
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _wert(w: Any) -> Any:
    if isinstance(w, Decimal):
        return float(w)
    if isinstance(w, str):
        try:
            if w and (w.replace(".", "", 1).replace("-", "", 1).isdigit()):
                return float(w)
        except ValueError:
            pass
        return w
    if isinstance(w, datetime):
        return w.replace(tzinfo=None)
    return w


def tabelle(wb: Workbook, titel: str, kopf: list[str], zeilen: Iterable[list[Any]], euro_spalten: tuple[int, ...] = (), datum_spalten: tuple[int, ...] = (),
            erste: bool = False):
    """Blatt mit formatierter Tabelle (Kopfzeile fett, Autofilter, Spaltenbreite nach Inhalt)."""
    ws = wb.active if erste else wb.create_sheet()
    ws.title = titel[:31]
    ws.append(kopf)
    for z in ws[1]:
        z.font, z.fill, z.alignment = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0B5CAD"), Alignment(vertical="center")
    n = 0
    for zeile in zeilen:
        ws.append([_wert(w) for w in zeile])
        n += 1
    for i in euro_spalten:
        for z in ws.iter_rows(min_row=2, min_col=i + 1, max_col=i + 1):
            z[0].number_format = EURO
    for i in datum_spalten:
        for z in ws.iter_rows(min_row=2, min_col=i + 1, max_col=i + 1):
            z[0].number_format = "DD.MM.YYYY"
    for i, k in enumerate(kopf, start=1):
        breite = max([len(str(k))] + [len(str(c.value)) for c in ws[get_column_letter(i)][1:n + 1] if c.value is not None][:200])
        ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, breite + 2))
    if n:
        ref = f"A1:{get_column_letter(len(kopf))}{n + 1}"
        t = Table(displayName=f"T{abs(hash(titel)) % 10_000_000}", ref=ref)
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(t)
    ws.freeze_panes = "A2"
    return ws


def _bytes(wb: Workbook) -> bytes:
    puffer = BytesIO()
    wb.save(puffer)
    return puffer.getvalue()


def auswertungen_xlsx(a: dict[str, Any], betrieb: str) -> bytes:
    wb = Workbook()
    wb.properties.creator = betrieb
    tabelle(wb, "Monate", ["Monat", "Umsatz netto", "Zahlungen", "Material (Belege)"],
            ([m["monat"], m["umsatz_netto"], m["zahlungen"], m["material_netto"]] for m in a["monate"]), euro_spalten=(1, 2, 3), erste=True)
    tabelle(wb, "Monteure", ["Monteur", "Rapporte", "Stunden", "Umsatz netto", "Material", "Lohnkosten", "Deckungsbeitrag"],
            ([m["name"], m["rapporte"], m["stunden"], m["umsatz_netto"], m["material_netto"], m["lohnkosten"], m["deckungsbeitrag"]] for m in a["monteure"]),
            euro_spalten=(3, 4, 5, 6))
    kennzahlen = [["Zeitraum", f"{a['von']} bis {a['bis']}"], ["Umsatz netto gesamt", a["umsatz_netto_gesamt"]],
                  ["Offene Posten (Summe)", a["offene_posten"]["summe"]], ["davon überfällig", a["offene_posten"]["ueberfaellig"]],
                  ["Offene Posten (Anzahl)", a["offene_posten"]["anzahl"]], ["Gemahnt", a["offene_posten"]["gemahnt"]],
                  ["Angebotsquote", a["angebote"].get("quote")], ["Angebote angenommen", a["angebote"].get("angenommen")],
                  ["Angebote entschieden", a["angebote"].get("entschieden")], ["Angebotsvolumen offen (netto)", a["angebote"].get("volumen_offen_netto")],
                  ["Wartungsverträge aktiv", a["vertraege"].get("aktive")], ["Vertragsvolumen je Jahr (netto)", a["vertraege"].get("jahresvolumen_netto")]]
    kennzahlen += [[f"Anfragen über {k}", v] for k, v in (a.get("anfragen_je_kanal") or {}).items()]
    tabelle(wb, "Kennzahlen", ["Kennzahl", "Wert"], kennzahlen)
    return _bytes(wb)


def kunden_xlsx(s: Session, m: Mandant) -> bytes:
    wb = Workbook()
    wb.properties.creator = m.name
    kunden = s.scalars(select(Kunde).where(Kunde.mandant_id == m.id).order_by(Kunde.name)).all()
    tabelle(wb, "Kunden", ["Name", "Telefon", "E-Mail", "Straße", "PLZ", "Ort", "Vertragsstatus", "Einwilligung E-Mail", "Einwilligung SMS", "Einwilligung WhatsApp",
                           "Anlagen", "Nicht erschienen", "Angelegt"],
            ([k.name, k.telefon, k.email, k.strasse, k.plz, k.ort, k.vertragsstatus, bool(k.einwilligungen.get("email")), bool(k.einwilligungen.get("sms")),
              bool(k.einwilligungen.get("whatsapp")), sum(len(o.anlagen) for o in k.objekte), k.nicht_erschienen, k.erstellt] for k in kunden),
            datum_spalten=(12,), erste=True)
    return _bytes(wb)


def offene_posten_xlsx(posten: list[dict[str, Any]], betrieb: str) -> bytes:
    wb = Workbook()
    wb.properties.creator = betrieb
    tabelle(wb, "Offene Posten", ["Rechnung", "Kunde", "Datum", "Fällig", "Brutto", "Offen", "Tage überfällig", "Mahnstufe", "Mahnfrist", "Status"],
            ([p["nummer"], p["kunde"], p["datum"], p["faellig"], p["brutto"], p["offen"], p["tage_ueberfaellig"], p["mahnstufe"], p["mahnfrist"], p["status"]] for p in posten),
            euro_spalten=(4, 5), datum_spalten=(2, 3, 8), erste=True)
    return _bytes(wb)


def termine_xlsx(s: Session, m: Mandant, von: datetime, bis: datetime) -> bytes:
    from core.disposition import ZEITZONE
    from core.domaene import als_utc
    wb = Workbook()
    wb.properties.creator = m.name
    termine = s.scalars(select(Termin).join(Termin.servicefall).where(Termin.beginn >= von, Termin.beginn < bis).order_by(Termin.beginn)).all()
    termine = [t for t in termine if t.servicefall.mandant_id == m.id]
    tabelle(wb, "Termine", ["Datum", "Beginn", "Ende", "Monteur", "Kunde", "Anliegen", "Status", "Dringlichkeit"],
            ([als_utc(t.beginn).astimezone(ZEITZONE).date(), als_utc(t.beginn).astimezone(ZEITZONE).strftime("%H:%M"), als_utc(t.ende).astimezone(ZEITZONE).strftime("%H:%M"),
              t.ressource.name if t.ressource else "", t.servicefall.kunde.name if t.servicefall.kunde else "", t.servicefall.anliegen, t.status, t.servicefall.dringlichkeit]
             for t in termine), datum_spalten=(0,), erste=True)
    return _bytes(wb)


def dateiname(art: str, heute: date) -> str:
    return f"{art}-{heute.isoformat()}.xlsx"
