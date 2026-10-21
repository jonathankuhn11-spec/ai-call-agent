"""Belegpaket für die Steuerkanzlei: Rechnungen eines Zeitraums als ZIP mit PDF, XRechnung-XML,
Übersicht (CSV) und Buchungsstapel im DATEV-Format (EXTF), zum Import in DATEV Kanzlei-Rechnungswesen
oder zum Hochladen in DATEV Unternehmen online.

Kein Kontieren im Sinne des § 6 Nr. 3 StBerG: der Stapel trägt nur Debitoren-Sammelkonto, Erlöskonto
nach Steuersatz und Festschreibung 0; die Kanzlei prüft und schreibt fest. Spaltenkopf und Kontenrahmen
vor dem ersten Import mit der Kanzlei abgleichen (DATEV-Formatbeschreibung „Buchungsstapel“, Version 700).
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Anhang, Beleg, Mandant, OutboxEintrag, Rechnung
from core.rechnung import pdf, xrechnung

# SKR03 (Standard im Handwerk) und SKR04; Konten per stammdaten["konten"] überschreibbar
KONTEN = {
    "SKR03": {"debitoren": "1400", "erloes_19": "8400", "erloes_7": "8300", "erloes_0": "8195",
              "kreditoren": "1600", "aufwand_19": "3400", "aufwand_7": "3300", "aufwand_0": "3200"},
    "SKR04": {"debitoren": "1200", "erloes_19": "4400", "erloes_7": "4300", "erloes_0": "4185",
              "kreditoren": "3300", "aufwand_19": "5400", "aufwand_7": "5300", "aufwand_0": "5200"},
}

EXTF_SPALTEN = [
    "Umsatz (ohne Soll/Haben-Kz)", "Soll/Haben-Kennzeichen", "WKZ Umsatz", "Kurs", "Basis-Umsatz", "WKZ Basis-Umsatz",
    "Konto", "Gegenkonto (ohne BU-Schlüssel)", "BU-Schlüssel", "Belegdatum", "Belegfeld 1", "Belegfeld 2", "Skonto",
    "Buchungstext", "Postensperre", "Diverse Adressnummer", "Geschäftspartnerbank", "Sachverhalt", "Zinssperre", "Beleglink",
    *(f"Beleginfo - {a} {i}" for i in range(1, 9) for a in ("Art", "Inhalt")),
    "KOST1 - Kostenstelle", "KOST2 - Kostenstelle", "Kost-Menge", "EU-Land u. UStID", "EU-Steuersatz", "Abw. Versteuerungsart",
    "Sachverhalt L+L", "Funktionsergänzung L+L", "BU 49 Hauptfunktionstyp", "BU 49 Hauptfunktionsnummer", "BU 49 Funktionsergänzung",
    *(f"Zusatzinformation - {a} {i}" for i in range(1, 21) for a in ("Art", "Inhalt")),
    "Stück", "Gewicht", "Zahlweise", "Forderungsart", "Veranlagungsjahr", "Zugeordnete Fälligkeit", "Skontotyp", "Auftragsnummer",
    "Buchungstyp", "Ust-Schlüssel (Anzahlungen)", "EU-Land (Anzahlungen)", "Sachverhalt L+L (Anzahlungen)", "EU-Steuersatz (Anzahlungen)",
    "Erlöskonto (Anzahlungen)", "Herkunft-Kz", "Buchungs GUID", "KOST-Datum", "SEPA-Mandatsreferenz", "Skontosperre", "Gesellschaftername",
    "Beteiligtennummer", "Identifikationsnummer", "Zeichnernummer", "Postensperre bis", "Bezeichnung SoBil-Sachverhalt",
    "Kennzeichen SoBil-Buchung", "Festschreibung", "Leistungsdatum", "Datum Zuord. Steuerperiode", "Fälligkeit", "Generalumkehr (GU)",
    "Steuersatz", "Land", "Abrechnungsreferenz", "BVV-Position", "EU-Land u. UStID (Ursprung)", "EU-Steuersatz (Ursprung)",
]


def zeitraum(p: dict[str, Any], heute: date) -> tuple[date, date, str]:
    """payload["zeitraum"] als JJJJ-MM; ohne Angabe der Vormonat."""
    if p.get("zeitraum"):
        jahr, monat = (int(x) for x in p["zeitraum"].split("-"))
    else:
        jahr, monat = (heute.year, heute.month - 1) if heute.month > 1 else (heute.year - 1, 12)
    von = date(jahr, monat, 1)
    bis = date(jahr + (monat == 12), monat % 12 + 1, 1)
    return von, bis, f"{jahr}-{monat:02d}"


def _dateiname(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text)[:30].strip("_") or "beleg"


def _betrag(d: Decimal) -> str:
    return f"{abs(d):.2f}".replace(".", ",")


def extf_buchungsstapel(m: Mandant, rechnungen: list[Rechnung], von: date, bis: date, erzeugt: datetime,
                       belege: list[Beleg] | None = None) -> bytes:
    """Ausgangsrechnungen (Debitor im Soll gegen Erlös) und Eingangsbelege (Aufwand im Soll gegen Kreditor) in
    einem Stapel; Konten sind Vorschläge je Steuersatz, Festschreibung 0."""
    st = m.stammdaten
    konten = {**KONTEN.get(st.get("kontenrahmen", "SKR03"), KONTEN["SKR03"]), **st.get("konten", {})}
    wj_beginn = date(von.year, int(st.get("wirtschaftsjahr_beginn_monat", 1)), 1)
    kopf = ["EXTF", 700, 21, "Buchungsstapel", 13, erzeugt.strftime("%Y%m%d%H%M%S") + "000", "", "RE", "SHK-Backoffice", "",
            st.get("datev_beraternummer", ""), st.get("datev_mandantennummer", ""), wj_beginn.strftime("%Y%m%d"),
            int(st.get("sachkontenlaenge", 4)), von.strftime("%Y%m%d"), (bis - timedelta(days=1)).strftime("%Y%m%d"),
            f"Ausgangsrechnungen {von.strftime('%m/%Y')}", "", 1, 0, 0, "EUR", "", "", "", "", st.get("kontenrahmen", "SKR03")[3:], "", "", "", ""]
    puffer = io.StringIO()
    w = csv.writer(puffer, delimiter=";", quoting=csv.QUOTE_NONNUMERIC, lineterminator="\r\n")
    w.writerow(kopf)
    w.writerow(EXTF_SPALTEN)
    for r in rechnungen:
        je_satz: dict[str, Decimal] = {}
        for p in r.positionen:
            satz = str(int(Decimal(p["ust_satz"])))
            brutto = Decimal(p["netto"]) * (1 + Decimal(p["ust_satz"]) / 100)
            je_satz[satz] = je_satz.get(satz, Decimal("0")) + brutto
        for satz, brutto in je_satz.items():
            brutto = brutto.quantize(Decimal("0.01"))
            zeile = [""] * len(EXTF_SPALTEN)
            zeile[0] = _betrag(brutto)
            zeile[1] = "H" if r.art == "storno" else "S"  # Ausgangsrechnung: Debitor im Soll, Storno im Haben
            zeile[2] = "EUR"
            zeile[6] = konten["debitoren"]
            zeile[7] = konten.get(f"erloes_{satz}", konten["erloes_0"])
            zeile[9] = r.datum.strftime("%d%m")
            zeile[10] = r.nummer[:36]
            zeile[13] = f"{'Storno ' if r.art == 'storno' else ''}{r.empfaenger.get('name', '')}"[:60]
            zeile[EXTF_SPALTEN.index("Festschreibung")] = "0"
            zeile[EXTF_SPALTEN.index("Leistungsdatum")] = (r.leistungsdatum or r.datum).strftime("%d%m%Y")
            zeile[EXTF_SPALTEN.index("Fälligkeit")] = r.faellig.strftime("%d%m%Y")
            w.writerow(zeile)
    for b in belege or []:
        zeile = [""] * len(EXTF_SPALTEN)
        zeile[0] = _betrag(Decimal(b.brutto))
        zeile[1] = "S"  # Aufwand im Soll, Gegenkonto Kreditoren-Sammelkonto
        zeile[2] = "EUR"
        zeile[6] = konten.get(f"aufwand_{b.ust_satz}", konten["aufwand_0"])
        zeile[7] = konten["kreditoren"]
        zeile[9] = b.datum.strftime("%d%m")
        zeile[10] = (b.nummer or b.id[:12])[:36]
        zeile[13] = f"ER {b.lieferant}"[:60]
        zeile[EXTF_SPALTEN.index("Festschreibung")] = "0"
        if b.faellig:
            zeile[EXTF_SPALTEN.index("Fälligkeit")] = b.faellig.strftime("%d%m%Y")
        w.writerow(zeile)
    return puffer.getvalue().encode("cp1252", errors="replace")


def paket_erzeugen(s: Session, m: Mandant, p: dict[str, Any], heute: date | None = None) -> tuple[str, bytes, str]:
    heute = heute or datetime.now(timezone.utc).date()
    von, bis, kennung = zeitraum(p, heute)
    rechnungen = s.scalars(select(Rechnung).where(Rechnung.mandant_id == m.id, Rechnung.datum >= von, Rechnung.datum < bis)
                           .order_by(Rechnung.nummer)).all()
    belege = s.scalars(select(Beleg).where(Beleg.mandant_id == m.id, Beleg.datum >= von, Beleg.datum < bis,
                                           Beleg.status != "erfasst").order_by(Beleg.datum, Beleg.lieferant)).all()
    erzeugt = datetime.now(timezone.utc)
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        uebersicht = io.StringIO()
        w = csv.writer(uebersicht, delimiter=";", lineterminator="\r\n")
        w.writerow(["Nummer", "Art", "Datum", "Leistungsdatum", "Fällig", "Kunde", "Netto", "USt", "Brutto", "Status", "Storno von"])
        for r in rechnungen:
            w.writerow([r.nummer, r.art, r.datum.isoformat(), (r.leistungsdatum or r.datum).isoformat(), r.faellig.isoformat(),
                        r.empfaenger.get("name", ""), r.netto.replace(".", ","), r.umsatzsteuer.replace(".", ","),
                        r.brutto.replace(".", ","), r.status, r.storno_von_id or ""])
            z.writestr(f"rechnungen/{r.nummer}.pdf", pdf(r, m))
            z.writestr(f"rechnungen/{r.nummer}.xml", xrechnung(r, m))
        z.writestr("uebersicht.csv", uebersicht.getvalue().encode("utf-8-sig"))
        eingang = io.StringIO()
        w = csv.writer(eingang, delimiter=";", lineterminator="\r\n")
        w.writerow(["Lieferant", "Nummer", "Datum", "Fällig", "Netto", "USt", "Brutto", "USt-Satz", "Kategorie", "Auftrag", "Datei", "Status"])
        for b in belege:
            datei = ""
            if b.anhang_id and (a := s.get(Anhang, b.anhang_id)):
                endung = a.name.rsplit(".", 1)[-1].lower() if "." in a.name else "bin"
                datei = f"eingangsbelege/{b.datum.isoformat()}_{_dateiname(b.lieferant)}_{(b.nummer or b.id[:8])}.{endung}"
                z.writestr(datei, a.inhalt)
            w.writerow([b.lieferant, b.nummer or "", b.datum.isoformat(), b.faellig.isoformat() if b.faellig else "",
                        b.netto.replace(".", ","), b.umsatzsteuer.replace(".", ","), b.brutto.replace(".", ","), b.ust_satz,
                        b.kategorie, b.servicefall_id or "", datei, b.status])
            b.status = "uebergeben" if b.status in ("geprueft",) else b.status
        z.writestr("eingangsbelege.csv", eingang.getvalue().encode("utf-8-sig"))
        z.writestr(f"EXTF_Buchungsstapel_{kennung}.csv", extf_buchungsstapel(m, rechnungen, von, bis, erzeugt, belege))
        z.writestr("LIESMICH.txt", (f"Belegpaket {m.name}, Zeitraum {kennung}, erzeugt {erzeugt.strftime('%d.%m.%Y %H:%M')} UTC.\n"
                                    f"{len(rechnungen)} Ausgangsrechnungen, {len(belege)} Eingangsbelege (nur geprüfte). "
                                    "EXTF-Datei: Buchungsstapel im DATEV-Format, Festschreibung 0, Konten als Vorschlag "
                                    "(keine Kontierung im Sinne des StBerG), bitte vor dem Import prüfen.\n"))
    summe = sum((Decimal(r.brutto) for r in rechnungen), Decimal("0"))
    summe_eingang = sum((Decimal(b.brutto) for b in belege), Decimal("0"))
    text = (f"Guten Tag,\n\nanbei das Belegpaket {kennung} mit {len(rechnungen)} Ausgangsbelegen "
            f"(Summe brutto {summe:.2f} €) und {len(belege)} Eingangsbelegen (Summe brutto {summe_eingang:.2f} €). "
            "Enthalten: PDF und XRechnung je Ausgangsrechnung, Eingangsbelege als Datei, Übersichten als CSV und ein "
            "Buchungsstapel im DATEV-Format mit Kontenvorschlag ohne Festschreibung.")
    return f"belege_{kennung}.zip", puffer.getvalue(), text


class BelegpaketAdapter:
    """Ablage des Belegpakets im Dateisystem (z. B. freigegebener Ordner, Nextcloud-Sync); Versand per E-Mail
    übernimmt der EmailAdapter mit kanzlei_email."""
    name = "belegpaket"

    def __init__(self, konfig: dict[str, Any]):
        self.verzeichnis = Path(konfig.get("verzeichnis", "export/kanzlei"))

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        if e.aktion != "belegpaket":
            return None
        m = s.get(Mandant, e.mandant_id)
        dateiname, inhalt, _ = paket_erzeugen(s, m, e.payload)
        ziel = self.verzeichnis / e.mandant_id
        ziel.mkdir(parents=True, exist_ok=True)
        (ziel / dateiname).write_bytes(inhalt)
        return str(ziel / dateiname)
