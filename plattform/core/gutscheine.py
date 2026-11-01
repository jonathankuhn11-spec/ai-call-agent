"""Wertgutscheine: Ausgabe mit Code und PDF, Teileinlösung mit Restwert, Ablauf (3 Jahre, § 195 BGB, Beginn Jahresende).

Umsatzsteuer: Ein Wertgutschein, der nur für Leistungen eines Betriebs mit einheitlichem Steuersatz gilt, ist ein
Einzweck-Gutschein (§ 3 Abs. 14 UStG) – die Umsatzsteuer entsteht bei Ausgabe. Gelten mehrere Steuersätze (z. B.
Friseur 19 % und Bücher 7 %), ist er ein Mehrzweck-Gutschein (§ 3 Abs. 15 UStG) mit Besteuerung bei Einlösung. Die
Plattform führt den Gutschein als Zahlungsmittel bei der Einlösung; die steuerliche Behandlung der Ausgabe klärt die
Kanzlei (Hinweis in der Gutscheinliste des Belegpakets).
"""
from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene
from core.modelle import Gutschein, Mandant

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O, 1/I


def code_erzeugen() -> str:
    roh = "".join(secrets.choice(ALPHABET) for _ in range(12))
    return f"{roh[:4]}-{roh[4:8]}-{roh[8:]}"


def _betrag(wert: Any) -> Decimal:
    try:
        d = Decimal(str(wert).replace(",", "."))
    except InvalidOperation as e:
        raise ValueError("Betrag ist keine Zahl") from e
    if d <= 0:
        raise ValueError("Betrag muss größer 0 sein")
    return d.quantize(Decimal("0.01"))


def ausstellen(s: Session, m: Mandant, wert: Any, heute: date, kunde_id: str | None = None, empfaenger: str | None = None,
               gueltig_jahre: int = 3, akteur_name: str | None = None) -> Gutschein:
    betrag = _betrag(wert)
    gueltig_bis = date(heute.year + gueltig_jahre, 12, 31)   # Regelverjährung: 3 Jahre ab Ende des Ausgabejahres
    code = code_erzeugen()
    while s.scalar(select(Gutschein).where(Gutschein.mandant_id == m.id, Gutschein.code == code)):
        code = code_erzeugen()
    g = Gutschein(mandant_id=m.id, code=code, wert=f"{betrag:.2f}", restwert=f"{betrag:.2f}", kunde_id=kunde_id, empfaenger=empfaenger, gueltig_bis=gueltig_bis)
    s.add(g)
    s.flush()
    domaene.protokollieren(s, m.id, "gutschein.ausgestellt", "gutschein", g.id, "mensch", {"code": code, "wert": g.wert, "gueltig_bis": gueltig_bis.isoformat()}, akteur_name)
    return g


def finden(s: Session, m: Mandant, code: str) -> Gutschein | None:
    code = code.strip().upper().replace(" ", "")
    if "-" not in code and len(code) == 12:
        code = f"{code[:4]}-{code[4:8]}-{code[8:]}"
    return s.scalar(select(Gutschein).where(Gutschein.mandant_id == m.id, Gutschein.code == code))


def einloesen(s: Session, m: Mandant, code: str, betrag: Any, heute: date, rechnung_id: str | None = None, akteur_name: str | None = None) -> Gutschein:
    g = finden(s, m, code)
    if g is None:
        raise domaene.Domaenenfehler("Gutschein unbekannt")
    if g.status == "storniert":
        raise domaene.Domaenenfehler("Gutschein ist storniert")
    if g.gueltig_bis < heute:
        g.status = "abgelaufen"
        raise domaene.Domaenenfehler(f"Gutschein ist seit {g.gueltig_bis.strftime('%d.%m.%Y')} abgelaufen")
    rest = Decimal(g.restwert)
    if rest <= 0:
        raise domaene.Domaenenfehler("Gutschein ist bereits vollständig eingelöst")
    wunsch = _betrag(betrag)
    eingeloest = min(rest, wunsch)
    g.restwert = f"{rest - eingeloest:.2f}"
    g.einloesungen = [*g.einloesungen, {"am": heute.isoformat(), "betrag": f"{eingeloest:.2f}", "rechnung_id": rechnung_id, "wer": akteur_name}]
    if Decimal(g.restwert) == 0:
        g.status = "eingeloest"
    domaene.protokollieren(s, m.id, "gutschein.eingeloest", "gutschein", g.id, "mensch", {"betrag": f"{eingeloest:.2f}", "restwert": g.restwert, "rechnung_id": rechnung_id}, akteur_name)
    return g


def stornieren(s: Session, m: Mandant, g: Gutschein, grund: str | None, akteur_name: str | None = None) -> Gutschein:
    if g.einloesungen:
        raise domaene.Domaenenfehler("Teilweise eingelöster Gutschein kann nicht storniert werden")
    g.status = "storniert"
    domaene.protokollieren(s, m.id, "gutschein.storniert", "gutschein", g.id, "mensch", {"grund": grund}, akteur_name)
    return g


def uebersicht(s: Session, m: Mandant, heute: date) -> dict[str, Any]:
    alle = s.scalars(select(Gutschein).where(Gutschein.mandant_id == m.id).order_by(Gutschein.ausgestellt.desc())).all()
    def status(g: Gutschein) -> str:   # Ablauf wird beim Lesen abgeleitet, nicht erst beim Einlösen gespeichert
        return "abgelaufen" if g.status == "aktiv" and g.gueltig_bis < heute else g.status
    offen = sum((Decimal(g.restwert) for g in alle if status(g) == "aktiv"), Decimal(0))
    return {"anzahl": len(alle), "offen_summe": f"{offen:.2f}", "aktiv": sum(1 for g in alle if status(g) == "aktiv"),
            "gutscheine": [{"id": g.id, "code": g.code, "wert": g.wert, "restwert": g.restwert, "status": status(g), "gueltig_bis": g.gueltig_bis.isoformat(),
                            "empfaenger": g.empfaenger, "kunde_id": g.kunde_id, "ausgestellt": g.ausgestellt, "einloesungen": g.einloesungen} for g in alle]}


def pdf(g: Gutschein, m: Mandant) -> bytes:
    import io

    from reportlab.lib.pagesizes import A5, landscape
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    import segno

    st = m.stammdaten or {}
    puffer = io.BytesIO()
    c = canvas.Canvas(puffer, pagesize=landscape(A5))
    breite, hoehe = landscape(A5)
    c.setTitle(f"Gutschein {g.code}")
    c.setStrokeColorRGB(0.2, 0.3, 0.4)
    c.setLineWidth(1.5)
    c.rect(8 * mm, 8 * mm, breite - 16 * mm, hoehe - 16 * mm)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(18 * mm, hoehe - 28 * mm, "Gutschein")
    c.setFont("Helvetica", 12)
    c.drawString(18 * mm, hoehe - 38 * mm, m.name)
    c.setFont("Helvetica-Bold", 34)
    c.drawString(18 * mm, hoehe - 62 * mm, f"{Decimal(g.wert):.2f} €".replace(".", ","))
    c.setFont("Helvetica", 11)
    if g.empfaenger:
        c.drawString(18 * mm, hoehe - 74 * mm, f"Für: {g.empfaenger}")
    c.drawString(18 * mm, hoehe - 84 * mm, f"Code: {g.code}")
    c.drawString(18 * mm, hoehe - 92 * mm, f"Gültig bis {g.gueltig_bis.strftime('%d.%m.%Y')} · einlösbar für alle Leistungen, Teileinlösung möglich, keine Barauszahlung")
    c.setFont("Helvetica", 9)
    c.drawString(18 * mm, 14 * mm, " · ".join(t for t in (st.get("strasse"), f"{st.get('plz', '')} {st.get('ort', '')}".strip(), st.get("telefon"), st.get("email")) if t))
    qr = segno.make(g.code, error="m")
    qr_puffer = io.BytesIO()
    qr.save(qr_puffer, kind="png", scale=4, border=1)
    qr_puffer.seek(0)
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(qr_puffer), breite - 50 * mm, hoehe - 62 * mm, 32 * mm, 32 * mm)
    c.showPage()
    c.save()
    return puffer.getvalue()
