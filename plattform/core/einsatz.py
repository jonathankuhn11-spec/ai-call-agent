"""Einsatzmodule des Betriebs: Anhänge, Zeiterfassung, Rapport, Nachkalkulation, Bereitschaft, Fristen, Lohnexport.

Leitlinie wie im Core: deterministische Regeln, jede Änderung im Ereignisprotokoll, keine Buchhaltung
(Konten, Lohnabrechnung) in der Plattform; sie liefert geprüfte Vorbereitungsdaten an Kanzlei und Lohnbüro.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import domaene
from core.disposition import ZEITZONE
from core.domaene import als_utc
from core.modelle import (Anhang, Beleg, Bereitschaft, Frist, Kunde, Mandant, Rapport, Rechnung, Ressource, Servicefall,
                          Termin, Zeiteintrag)

MAX_ANHANG = 10 * 1024 * 1024
BILD_KANTE = 1600
ZEITARTEN = ("arbeit", "fahrt", "pause", "bereitschaft")
STANDARD_SAETZE = {"stundensatz_kosten": "55.00", "stundensatz_verrechnung": "69.00", "materialaufschlag_prozent": "15"}


# --- Anhänge -----------------------------------------------------------------------------------
def bild_verkleinern(inhalt: bytes, mime: str) -> tuple[bytes, str]:
    """Fotos vom Mobiltelefon auf 1600 px Kante und JPEG verkleinern (3–5 MB → ~300 KB); PNG bleibt PNG
    (Unterschriften, Screenshots). Ohne Pillow oder bei Fehlern bleibt die Datei unverändert."""
    if not mime.startswith("image/") or mime == "image/svg+xml":
        return inhalt, mime
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return inhalt, mime
    try:
        bild = Image.open(io.BytesIO(inhalt))
        bild = ImageOps.exif_transpose(bild)
        if max(bild.size) <= BILD_KANTE and len(inhalt) < 400_000:
            return inhalt, mime
        bild.thumbnail((BILD_KANTE, BILD_KANTE))
        puffer = io.BytesIO()
        if mime == "image/png":
            bild.save(puffer, "PNG", optimize=True)
            return puffer.getvalue(), "image/png"
        bild.convert("RGB").save(puffer, "JPEG", quality=82, optimize=True)
        return puffer.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001  kein gültiges Bild: unverändert speichern
        return inhalt, mime


def anhang_anlegen(s: Session, m: Mandant, bezug_typ: str, bezug_id: str, art: str, name: str, mime: str,
                   inhalt: bytes, von: str | None = None, notiz: str | None = None) -> Anhang:
    if len(inhalt) > MAX_ANHANG:
        raise ValueError(f"Datei größer als {MAX_ANHANG // 1024 // 1024} MB")
    if not inhalt:
        raise ValueError("Leere Datei")
    inhalt, mime = bild_verkleinern(inhalt, mime or "application/octet-stream")
    a = Anhang(mandant_id=m.id, bezug_typ=bezug_typ, bezug_id=bezug_id, art=art, name=name[:200], mime=mime,
               groesse=len(inhalt), inhalt=inhalt, hochgeladen_von=von, notiz=notiz)
    s.add(a)
    s.flush()
    return a


def anhaenge(s: Session, m: Mandant, bezug_typ: str, bezug_id: str) -> list[Anhang]:
    return s.scalars(select(Anhang).where(Anhang.mandant_id == m.id, Anhang.bezug_typ == bezug_typ, Anhang.bezug_id == bezug_id)
                     .order_by(Anhang.hochgeladen)).all()


# --- Zeiterfassung -----------------------------------------------------------------------------
def laufender_eintrag(s: Session, ressource_id: str) -> Zeiteintrag | None:
    return s.scalar(select(Zeiteintrag).where(Zeiteintrag.ressource_id == ressource_id, Zeiteintrag.ende.is_(None))
                    .order_by(Zeiteintrag.beginn.desc()).limit(1))


def zeit_starten(s: Session, m: Mandant, r: Ressource, art: str, jetzt: datetime, servicefall_id: str | None = None,
                 notiz: str | None = None, akteur_name: str | None = None) -> Zeiteintrag:
    if art not in ZEITARTEN:
        raise ValueError(f"Zeitart {art!r} unbekannt ({', '.join(ZEITARTEN)})")
    if (laufend := laufender_eintrag(s, r.id)) is not None:
        zeit_beenden(s, laufend, jetzt)
    z = Zeiteintrag(mandant_id=m.id, ressource_id=r.id, servicefall_id=servicefall_id, art=art, beginn=jetzt, notiz=notiz)
    s.add(z)
    s.flush()
    domaene.protokollieren(s, m.id, "zeit.gestartet", "zeiteintrag", z.id, "mensch",
                           {"art": art, "ressource_id": r.id, "servicefall_id": servicefall_id}, akteur_name)
    return z


def zeit_beenden(s: Session, z: Zeiteintrag, jetzt: datetime) -> Zeiteintrag:
    if z.ende is not None:
        raise ValueError("Zeiteintrag ist bereits beendet")
    if jetzt < als_utc(z.beginn):
        raise ValueError("Ende liegt vor dem Beginn")
    z.ende = jetzt
    return z


def minuten(z: Zeiteintrag, jetzt: datetime | None = None) -> int:
    ende = als_utc(z.ende) if z.ende else (jetzt or datetime.now(timezone.utc))
    return max(0, int((ende - als_utc(z.beginn)).total_seconds() // 60))


def zeiten_je_fall(s: Session, fall_id: str) -> dict[str, int]:
    summe = {"arbeit": 0, "fahrt": 0, "pause": 0, "bereitschaft": 0}
    for z in s.scalars(select(Zeiteintrag).where(Zeiteintrag.servicefall_id == fall_id, Zeiteintrag.ende.isnot(None))):
        summe[z.art] = summe.get(z.art, 0) + minuten(z)
    return summe


def lohnexport(s: Session, m: Mandant, jahr: int, monat: int) -> bytes:
    """Monatsübersicht je Monteur und Tag für das Lohnbüro (CSV, Semikolon, UTF-8 mit BOM für Excel):
    Arbeit, Fahrt, Pause, Bereitschaft in Minuten sowie zuschlagsrelevante Nachtminuten (22–6 Uhr) und
    Sonntagsminuten. Keine Lohnberechnung; Zuschläge und Tarif wendet das Lohnbüro an."""
    von = datetime(jahr, monat, 1, tzinfo=ZEITZONE)
    bis = datetime(jahr + (monat == 12), monat % 12 + 1, 1, tzinfo=ZEITZONE)
    eintraege = s.scalars(select(Zeiteintrag).where(Zeiteintrag.mandant_id == m.id, Zeiteintrag.ende.isnot(None),
                                                     Zeiteintrag.beginn >= von, Zeiteintrag.beginn < bis)
                          .order_by(Zeiteintrag.ressource_id, Zeiteintrag.beginn)).all()
    tage: dict[tuple[str, date], dict[str, int]] = {}
    namen = {r.id: r.name for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m.id))}
    for z in eintraege:
        beginn, ende = als_utc(z.beginn).astimezone(ZEITZONE), als_utc(z.ende).astimezone(ZEITZONE)
        tag = tage.setdefault((z.ressource_id, beginn.date()), {a: 0 for a in ZEITARTEN} | {"nacht": 0, "sonntag": 0})
        dauer = int((ende - beginn).total_seconds() // 60)
        tag[z.art] += dauer
        if z.art in ("arbeit", "fahrt"):
            t = beginn
            while t < ende:  # minutengenau, damit Nacht- und Sonntagsanteile stimmen
                if t.hour >= 22 or t.hour < 6:
                    tag["nacht"] += 1
                if t.weekday() == 6:
                    tag["sonntag"] += 1
                t += timedelta(minutes=1)
    puffer = io.StringIO()
    w = csv.writer(puffer, delimiter=";", lineterminator="\r\n")
    w.writerow(["Monteur", "Anmeldename", "Datum", "Arbeit (min)", "Fahrt (min)", "Pause (min)", "Bereitschaft (min)",
                "davon Nacht 22-6 (min)", "davon Sonntag (min)"])
    benutzer = {r.id: r.benutzer or "" for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m.id))}
    for (rid, tag_), t in sorted(tage.items(), key=lambda x: (namen.get(x[0][0], ""), x[0][1])):
        w.writerow([namen.get(rid, rid), benutzer.get(rid, ""), tag_.isoformat(), t["arbeit"], t["fahrt"], t["pause"],
                    t["bereitschaft"], t["nacht"], t["sonntag"]])
    return puffer.getvalue().encode("utf-8-sig")


# --- Rapport -----------------------------------------------------------------------------------
def naechste_rapportnummer(s: Session, m: Mandant, jahr: int) -> str:
    anzahl = s.scalar(select(func.count()).select_from(Rapport).where(Rapport.mandant_id == m.id, Rapport.nummer.like(f"RA-{jahr}-%"))) or 0
    return f"RA-{jahr}-{anzahl + 1:05d}"


def rapport_erzeugen(s: Session, m: Mandant, fall: Servicefall, r: Ressource, daten: dict[str, Any], jetzt: datetime,
                     unterschrift_png: bytes | None = None, akteur_name: str | None = None) -> Rapport:
    """Rapport anlegen; mit Unterschrift wird er unveränderlich und der Vorgang auf „durchgeführt“ gesetzt
    (sofern eingeplant und Ergebnis erledigt). Ohne Zeiteinträge entstehen welche aus den Rapportminuten."""
    rapport = Rapport(mandant_id=m.id, servicefall_id=fall.id, termin_id=daten.get("termin_id"), ressource_id=r.id,
                      nummer=naechste_rapportnummer(s, m, jetzt.year), taetigkeiten=daten["taetigkeiten"],
                      material=daten.get("material") or [], arbeitszeit_minuten=int(daten.get("arbeitszeit_minuten") or 0),
                      fahrzeit_minuten=int(daten.get("fahrzeit_minuten") or 0), ergebnis=daten.get("ergebnis") or "erledigt",
                      hinweise_kunde=daten.get("hinweise_kunde"), messwerte=daten.get("messwerte") or {}, erstellt=jetzt)
    s.add(rapport)
    s.flush()
    if unterschrift_png:
        a = anhang_anlegen(s, m, "rapport", rapport.id, "unterschrift", f"{rapport.nummer}-unterschrift.png", "image/png",
                           unterschrift_png, akteur_name)
        rapport.unterschrift_anhang_id = a.id
        rapport.unterzeichnet_von = (daten.get("unterzeichnet_von") or fall.kunde.name)[:200]
        rapport.unterzeichnet_am = jetzt
    vorhanden = s.scalar(select(func.count()).select_from(Zeiteintrag).where(Zeiteintrag.servicefall_id == fall.id,
                                                                            Zeiteintrag.ressource_id == r.id)) or 0
    if not vorhanden:
        ende = jetzt
        for art, dauer in (("arbeit", rapport.arbeitszeit_minuten), ("fahrt", rapport.fahrzeit_minuten)):
            if dauer > 0:
                s.add(Zeiteintrag(mandant_id=m.id, ressource_id=r.id, servicefall_id=fall.id, art=art,
                                  beginn=ende - timedelta(minutes=dauer), ende=ende, quelle="rapport"))
                ende = ende - timedelta(minutes=dauer)
    domaene.protokollieren(s, m.id, "rapport.erstellt", "servicefall", fall.id, "mensch",
                           {"rapport_id": rapport.id, "nummer": rapport.nummer, "ergebnis": rapport.ergebnis,
                            "unterschrieben": bool(unterschrift_png)}, akteur_name)
    if rapport.ergebnis == "erledigt" and fall.status == "eingeplant":
        domaene.uebergang(s, fall, "durchgefuehrt", "mensch", f"Rapport {rapport.nummer}", akteur_name)
    link = m.stammdaten.get("bewertungslink")
    if link and daten.get("ergebnis") == "erledigt" and (fall.kunde.email or fall.kunde.telefon):
        # Bewertungsanfrage (Klasse B, per Policy automatisierbar); Einwilligung zur Werbung ist nicht nötig,
        # weil die Bitte um Bewertung im Nachgang einer Leistung als Kundenservice gilt (vgl. § 7 Abs. 3 UWG für E-Mail)
        domaene.aktion_beantragen(s, m, "bewertung_anfragen", {"kunde_id": fall.kunde_id, "servicefall_id": fall.id,
                                  "betreff": f"Wie zufrieden waren Sie mit {m.name}?",
                                  "text": f"Guten Tag {fall.kunde.name},\n\nvielen Dank für Ihren Auftrag. Wenn Sie zufrieden waren, freuen wir uns "
                                          f"über eine kurze Bewertung: {link}\nWenn etwas nicht in Ordnung war, rufen Sie uns bitte direkt an."},
                                  "system", fall.id, f"Bewertungsanfrage nach Rapport {rapport.nummer}")
    return rapport


def rapport_pdf(s: Session, rapport: Rapport, m: Mandant) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    fall: Servicefall = rapport.servicefall
    kunde: Kunde = fall.kunde
    st = m.stammdaten
    puffer = io.BytesIO()
    c = canvas.Canvas(puffer, pagesize=A4)
    c.setTitle(f"Arbeitsbericht {rapport.nummer}")
    breite, hoehe = A4
    y = hoehe - 20 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, y, m.name)
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, y - 5 * mm, f"{st.get('strasse', '')} · {st.get('plz', '')} {st.get('ort', '')} · {st.get('telefon', '')}")
    y -= 20 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, y, f"Arbeitsbericht {rapport.nummer}")
    c.setFont("Helvetica", 10)
    c.drawRightString(breite - 20 * mm, y, f"Datum: {als_utc(rapport.erstellt).astimezone(ZEITZONE):%d.%m.%Y}")
    y -= 8 * mm
    for zeile in (f"Kunde: {kunde.name}, {kunde.strasse or ''}, {kunde.plz or ''} {kunde.ort or ''}",
                  f"Auftrag: {fall.anliegen}", f"Monteur: {rapport.ressource.name.split(' (')[0]}",
                  f"Ergebnis: {rapport.ergebnis} · Arbeitszeit {rapport.arbeitszeit_minuten} min · Fahrzeit {rapport.fahrzeit_minuten} min"):
        c.drawString(20 * mm, y, zeile[:110])
        y -= 5.5 * mm
    y -= 4 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, y, "Ausgeführte Arbeiten")
    c.setFont("Helvetica", 9)
    for zeile in _umbrechen(rapport.taetigkeiten, 105):
        y -= 5 * mm
        c.drawString(20 * mm, y, zeile)
    if rapport.material:
        y -= 8 * mm
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20 * mm, y, "Material")
        c.setFont("Helvetica", 9)
        for p in rapport.material:
            y -= 5 * mm
            c.drawString(20 * mm, y, f"{p.get('menge', 1)} {p.get('einheit', 'Stk')}  {p.get('bezeichnung', '')}"[:110])
    if rapport.messwerte:
        y -= 8 * mm
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20 * mm, y, "Messwerte")
        c.setFont("Helvetica", 9)
        for k, v in rapport.messwerte.items():
            y -= 5 * mm
            c.drawString(20 * mm, y, f"{k}: {v}"[:110])
    if rapport.hinweise_kunde:
        y -= 8 * mm
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20 * mm, y, "Hinweise an den Kunden")
        c.setFont("Helvetica", 9)
        for zeile in _umbrechen(rapport.hinweise_kunde, 105):
            y -= 5 * mm
            c.drawString(20 * mm, y, zeile)
    y -= 14 * mm
    if rapport.unterschrift_anhang_id and (u := s.get(Anhang, rapport.unterschrift_anhang_id)):
        try:
            c.drawImage(ImageReader(io.BytesIO(u.inhalt)), 20 * mm, y - 20 * mm, width=60 * mm, height=20 * mm,
                        preserveAspectRatio=True, mask="auto")
        except Exception:  # noqa: BLE001  defektes Bild: Text statt Grafik
            c.drawString(20 * mm, y - 10 * mm, "[Unterschrift elektronisch erfasst]")
        c.line(20 * mm, y - 21 * mm, 85 * mm, y - 21 * mm)
        c.setFont("Helvetica", 8)
        c.drawString(20 * mm, y - 25 * mm, f"Unterschrift {rapport.unterzeichnet_von}, "
                     f"{als_utc(rapport.unterzeichnet_am).astimezone(ZEITZONE):%d.%m.%Y %H:%M} Uhr (elektronisch erfasst)")
    else:
        c.setFont("Helvetica", 8)
        c.drawString(20 * mm, y - 10 * mm, "Nicht unterschrieben.")
    fotos = anhaenge(s, m, "servicefall", fall.id)
    fotos = [a for a in fotos if a.art == "foto"]
    if fotos:
        c.showPage()
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20 * mm, hoehe - 20 * mm, f"Fotodokumentation zu {rapport.nummer}")
        x, y = 20 * mm, hoehe - 30 * mm
        for a in fotos[:8]:
            try:
                c.drawImage(ImageReader(io.BytesIO(a.inhalt)), x, y - 60 * mm, width=80 * mm, height=60 * mm, preserveAspectRatio=True)
                c.setFont("Helvetica", 7)
                c.drawString(x, y - 63 * mm, (a.notiz or a.name)[:60])
            except Exception:  # noqa: BLE001
                continue
            x = 110 * mm if x == 20 * mm else 20 * mm
            if x == 20 * mm:
                y -= 70 * mm
                if y < 70 * mm:
                    c.showPage()
                    y = hoehe - 30 * mm
    c.showPage()
    c.save()
    return puffer.getvalue()


def _umbrechen(text: str, breite: int) -> list[str]:
    zeilen = []
    for absatz in (text or "").splitlines() or [""]:
        worte, aktuell = absatz.split(), ""
        for w in worte:
            if len(aktuell) + len(w) + 1 > breite:
                zeilen.append(aktuell)
                aktuell = w
            else:
                aktuell = f"{aktuell} {w}".strip()
        zeilen.append(aktuell)
    return zeilen


# --- Nachkalkulation ---------------------------------------------------------------------------
def kalkulation(s: Session, m: Mandant, fall: Servicefall) -> dict[str, Any]:
    """Deckungsbeitrag je Auftrag: Umsatz (gestellte Rechnungen netto) minus Material (Eingangsbelege netto)
    minus Stunden zu internen Kosten. Sätze aus den Stammdaten (stundensatz_kosten, stundensatz_verrechnung)."""
    st = {**STANDARD_SAETZE, **{k: str(v) for k, v in m.stammdaten.items() if k in STANDARD_SAETZE}}
    satz_kosten, satz_verrechnung = Decimal(st["stundensatz_kosten"]), Decimal(st["stundensatz_verrechnung"])
    zeiten = zeiten_je_fall(s, fall.id)
    belege = s.scalars(select(Beleg).where(Beleg.servicefall_id == fall.id)).all()
    material = sum((Decimal(b.netto) for b in belege if b.kategorie == "material"), Decimal("0"))
    fremd = sum((Decimal(b.netto) for b in belege if b.kategorie != "material"), Decimal("0"))
    rechnungen = s.scalars(select(Rechnung).where(Rechnung.servicefall_id == fall.id, Rechnung.status != "storniert")).all()
    umsatz = sum((Decimal(r.netto) * (-1 if r.art == "storno" else 1) for r in rechnungen), Decimal("0"))
    stunden = Decimal(zeiten["arbeit"] + zeiten["fahrt"]) / 60
    lohnkosten = (stunden * satz_kosten).quantize(Decimal("0.01"))
    soll_umsatz = (stunden * satz_verrechnung + (material + fremd) * (1 + Decimal(st["materialaufschlag_prozent"]) / 100)).quantize(Decimal("0.01"))
    deckungsbeitrag = (umsatz - material - fremd - lohnkosten).quantize(Decimal("0.01"))
    return {"servicefall_id": fall.id, "arbeit_minuten": zeiten["arbeit"], "fahrt_minuten": zeiten["fahrt"],
            "stunden": f"{stunden:.2f}", "lohnkosten": f"{lohnkosten:.2f}", "material": f"{material:.2f}", "fremdleistung": f"{fremd:.2f}",
            "umsatz_netto": f"{umsatz:.2f}", "soll_umsatz": f"{soll_umsatz:.2f}", "deckungsbeitrag": f"{deckungsbeitrag:.2f}",
            "deckungsbeitrag_quote": (f"{(deckungsbeitrag / umsatz * 100):.1f}" if umsatz else None),
            "belege": len(belege), "rechnungen": len(rechnungen), "saetze": st,
            "vorschlag_positionen": [  # Rechnungsentwurf aus Kalkulation (Freigabe Klasse C entscheidet)
                *([{"bezeichnung": "Arbeitszeit Monteur", "menge": f"{stunden:.2f}", "einheit": "h", "einzelpreis_netto": f"{satz_verrechnung:.2f}", "ust_satz": "19"}] if stunden else []),
                *[{"bezeichnung": f"Material {b.lieferant} {b.nummer or ''}".strip(), "menge": "1", "einheit": "psch",
                   "einzelpreis_netto": f"{(Decimal(b.netto) * (1 + Decimal(st['materialaufschlag_prozent']) / 100)):.2f}", "ust_satz": "19"} for b in belege]]}


# --- Bereitschaft ------------------------------------------------------------------------------
def bereitschaft_aktuell(s: Session, m: Mandant, jetzt: datetime) -> Bereitschaft | None:
    return s.scalar(select(Bereitschaft).where(Bereitschaft.mandant_id == m.id, Bereitschaft.von <= jetzt, Bereitschaft.bis > jetzt)
                    .order_by(Bereitschaft.von.desc()).limit(1))


def bereitschaft_anlegen(s: Session, m: Mandant, r: Ressource, von: datetime, bis: datetime, telefon: str,
                         notiz: str | None = None) -> Bereitschaft:
    if bis <= von:
        raise ValueError("Ende liegt vor dem Beginn")
    ueberschneidung = s.scalar(select(Bereitschaft).where(Bereitschaft.mandant_id == m.id, Bereitschaft.von < bis, Bereitschaft.bis > von))
    if ueberschneidung is not None:
        raise ValueError(f"Überschneidet sich mit der Bereitschaft von {ueberschneidung.ressource.name.split(' (')[0]}")
    b = Bereitschaft(mandant_id=m.id, ressource_id=r.id, von=von, bis=bis, telefon=telefon, notiz=notiz)
    s.add(b)
    s.flush()
    return b


# --- Fristen -----------------------------------------------------------------------------------
def fristen_faellig(s: Session, m: Mandant, heute: date, vorlauf_tage: int = 30) -> list[Frist]:
    grenze = heute + timedelta(days=vorlauf_tage)
    return s.scalars(select(Frist).where(Frist.mandant_id == m.id, Frist.erledigt_am.is_(None), Frist.faellig <= grenze)
                     .order_by(Frist.faellig)).all()


def frist_erledigen(s: Session, f: Frist, am: date) -> Frist | None:
    """Erledigt melden; bei Intervall entsteht die Folgefrist (gerechnet ab Erledigung, wie bei Prüfplaketten)."""
    if f.erledigt_am is not None:
        raise ValueError("Frist ist bereits erledigt")
    f.erledigt_am = am
    if not f.intervall_monate:
        return None
    monat = am.month - 1 + f.intervall_monate
    naechste = date(am.year + monat // 12, monat % 12 + 1, min(am.day, 28))
    folge = Frist(mandant_id=f.mandant_id, bezug_typ=f.bezug_typ, bezug_id=f.bezug_id, art=f.art, bezeichnung=f.bezeichnung,
                  faellig=naechste, intervall_monate=f.intervall_monate, hinweis=f.hinweis)
    s.add(folge)
    s.flush()
    return folge


def einsatz_heute(s: Session, m: Mandant, r: Ressource, jetzt: datetime) -> list[dict[str, Any]]:
    """Termine des Monteurs für den heutigen Betriebstag, inklusive Kundenadresse und Rapportstatus."""
    tag = als_utc(jetzt).astimezone(ZEITZONE).date()
    von = datetime.combine(tag, datetime.min.time(), ZEITZONE)
    termine = s.scalars(select(Termin).where(Termin.mandant_id == m.id, Termin.ressource_id == r.id, Termin.beginn >= von,
                                             Termin.beginn < von + timedelta(days=1), Termin.status != "abgesagt")
                        .order_by(Termin.beginn)).all()
    ergebnis = []
    for t in termine:
        f = t.servicefall
        rapport = s.scalar(select(Rapport).where(Rapport.servicefall_id == f.id).order_by(Rapport.erstellt.desc()).limit(1))
        ergebnis.append({"termin_id": t.id, "beginn": t.beginn, "ende": t.ende, "status": t.status, "servicefall_id": f.id,
                         "anliegen": f.anliegen, "symptom": f.symptom, "dringlichkeit": f.dringlichkeit, "fall_status": f.status,
                         "kunde": {"name": f.kunde.name, "telefon": f.kunde.telefon, "strasse": f.kunde.strasse, "plz": f.kunde.plz, "ort": f.kunde.ort},
                         "anlage": ({"typ": f.anlage.anlagentyp, "hersteller": f.anlage.hersteller, "baujahr": f.anlage.baujahr} if f.anlage else None),
                         "rapport": ({"id": rapport.id, "nummer": rapport.nummer, "unterschrieben": rapport.unterzeichnet_am is not None} if rapport else None)})
    return ergebnis
