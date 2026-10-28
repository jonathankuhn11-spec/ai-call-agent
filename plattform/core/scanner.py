"""Scan-Eingang und Belegübergabe an Steuertools.

Eingangswege (alle landen als Beleg im Status „erfasst“, Erkennung wie in core/belege.py):
  scan-to-mail   Netzwerkscanner oder Multifunktionsgerät mailt PDF/JPEG an ein eigenes Postfach; der Abruf
                 (IMAP) nimmt nur Anhänge erlaubter Absender (erlaubte_absender) und legt je Anhang einen Beleg an
  eingangsordner Scanner legt Dateien in einem Netzlaufwerk/SMB-Ordner ab (per Mount im Container sichtbar);
                 verarbeitete Dateien wandern nach verarbeitet/, nicht lesbare nach fehler/
  kamera         Mehrere Fotos vom Mobiltelefon werden zu einem mehrseitigen PDF zusammengefasst
                 (Graustufen, Kontrast, 2000 px Kante), damit Kanzlei und Steuertool ein Dokument erhalten
Dublettenschutz: Beleg.import_kennung (Message-ID plus Anhangsindex, SHA-256 der Datei) ist je Mandant eindeutig.

Übergabe (Status „geprüft“ löst sie aus, zusätzlich manuell): Outbox-Nachricht beleg_uebermitteln an
  finanz   Lexware Office (Files-API, Belegtyp voucher) oder sevDesk (Voucher-Factory, Entwurf)
  kanzlei  DATEV-Belegtransfer-Ordnerstruktur (belegtransfer) oder Upload-Mail von DATEV Unternehmen online (email mit upload_email)
Keine Kontierung (§ 6 Nr. 3 StBerG): übergeben werden Datei und Kopfdaten; Konten vergibt die Kanzlei im Zielsystem.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import io
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import belege, domaene, einsatz
from core.anfragen import Postfach
from core.modelle import Beleg, Mandant, OutboxEintrag

SCAN_ENDUNGEN = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp", ".xml": "application/xml"}
UEBERGABEFAEHIG = {"finanz": {"lexware", "sevdesk"}, "kanzlei": {"belegtransfer", "email"}}
SEITEN_KANTE = 2000
MAX_SEITEN = 30


# --- Kamera-Scan -------------------------------------------------------------------------------
def bilder_zu_pdf(bilder: list[bytes], scanmodus: bool = True) -> bytes:
    """Fotos zu einem mehrseitigen PDF; scanmodus = Graustufen plus Kontrastspreizung (lesbarer, kleiner)."""
    from PIL import Image, ImageOps

    if not bilder:
        raise ValueError("Keine Bilder übergeben")
    if len(bilder) > MAX_SEITEN:
        raise ValueError(f"Höchstens {MAX_SEITEN} Seiten je Scan")
    seiten = []
    for roh in bilder:
        try:
            bild = Image.open(io.BytesIO(roh))
            bild.load()
        except Exception as e:  # noqa: BLE001  defekte oder unbekannte Bilddatei
            raise ValueError(f"Bild nicht lesbar: {type(e).__name__}") from e
        bild = ImageOps.exif_transpose(bild) or bild
        bild = ImageOps.autocontrast(bild.convert("L"), cutoff=1) if scanmodus else bild.convert("RGB")
        bild.thumbnail((SEITEN_KANTE, SEITEN_KANTE))
        seiten.append(bild)
    puffer = io.BytesIO()
    seiten[0].save(puffer, format="PDF", save_all=True, append_images=seiten[1:], resolution=150.0)
    return puffer.getvalue()


# --- Beleg aus Scan ----------------------------------------------------------------------------
def kennung_fuer(inhalt: bytes) -> str:
    return "sha256:" + hashlib.sha256(inhalt).hexdigest()


def scan_zu_beleg(s: Session, m: Mandant, inhalt: bytes, name: str, mime: str, kennung: str, jetzt: datetime,
                  eingang: str, kategorie: str = "material", servicefall_id: str | None = None,
                  von: str | None = None, notiz: str | None = None) -> Beleg | None:
    """Datei zu Beleg plus Anhang; None, wenn die Kennung schon importiert wurde (Dublette)."""
    kennung = kennung[:200]
    if s.scalar(select(Beleg).where(Beleg.mandant_id == m.id, Beleg.import_kennung == kennung)):
        return None
    erkennung = belege.erkennen(inhalt, name, mime)
    felder = belege.felder_fuer_beleg(erkennung, jetzt.date())
    felder["erkennung"]["eingang"] = eingang
    b = Beleg(mandant_id=m.id, kategorie=kategorie, servicefall_id=servicefall_id, notiz=notiz, erstellt=jetzt,
              import_kennung=kennung, **felder)
    s.add(b)
    s.flush()
    anhang = einsatz.anhang_anlegen(s, m, "beleg", b.id, "beleg", name, mime, inhalt, von)
    b.anhang_id = anhang.id
    domaene.protokollieren(s, m.id, "beleg.erfasst", "beleg", b.id, "mensch" if von else "system",
                           {"quelle": b.quelle, "eingang": eingang, "lieferant": b.lieferant, "brutto": b.brutto,
                            "konfidenz": b.erkennung.get("konfidenz")}, von)
    return b


# --- Scan-to-Mail --------------------------------------------------------------------------------
def _absender_erlaubt(adresse: str, erlaubt: str | list[str] | None) -> bool:
    liste = [x.strip().lower() for x in (erlaubt.split(",") if isinstance(erlaubt, str) else (erlaubt or [])) if x.strip()]
    if not liste:
        return True  # ohne Liste nimmt das Postfach alles an (eigenes Scanner-Postfach vorausgesetzt)
    adresse = adresse.lower()
    domain = adresse.rsplit("@", 1)[-1]
    return any(adresse == e or domain == e.lstrip("@") for e in liste)


def anhaenge_aus_mail(roh: bytes) -> tuple[dict[str, Any], list[tuple[str, str, bytes]]]:
    """(Kopfdaten, [(Dateiname, MIME, Inhalt)]) der belegfähigen Anhänge (PDF, Bild, XML)."""
    n = email.message_from_bytes(roh, policy=email.policy.default)
    absender = email.utils.parseaddr(n.get("From", ""))[1]
    kopf = {"message_id": n.get("Message-ID") or f"ohne-id:{hashlib.sha256(roh).hexdigest()[:24]}", "absender": absender,
            "betreff": n.get("Subject") or ""}
    dateien: list[tuple[str, str, bytes]] = []
    for teil in n.walk():
        if teil.is_multipart():
            continue
        name = teil.get_filename() or ""
        typ = teil.get_content_type()
        endung = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if typ in SCAN_ENDUNGEN.values() or typ.startswith("image/") or endung in SCAN_ENDUNGEN:
            inhalt = teil.get_payload(decode=True) or b""
            if inhalt:
                dateien.append((name or f"scan{endung or '.bin'}", typ if typ != "application/octet-stream" else SCAN_ENDUNGEN.get(endung, typ), inhalt))
    return kopf, dateien


def postfach_scans(s: Session, m: Mandant, postfach: Postfach, jetzt: datetime, erlaubte_absender: str | list[str] | None = None,
                   kategorie: str = "material") -> dict[str, int]:
    ergebnis = {"gelesen": 0, "uebernommen": 0, "bekannt": 0, "verworfen": 0}
    for i, roh in enumerate(postfach.ungelesene()):
        ergebnis["gelesen"] += 1
        kopf, dateien = anhaenge_aus_mail(roh)
        if not dateien or not _absender_erlaubt(kopf["absender"], erlaubte_absender):
            ergebnis["verworfen"] += 1
            postfach.als_gelesen(i)
            continue
        for index, (name, mime, inhalt) in enumerate(dateien):
            b = scan_zu_beleg(s, m, inhalt, name, mime, f"mail:{kopf['message_id']}:{index}", jetzt, "scan-to-mail", kategorie,
                              notiz=f"Scan per E-Mail von {kopf['absender']}" + (f": {kopf['betreff']}" if kopf["betreff"] else ""))
            ergebnis["uebernommen" if b else "bekannt"] += 1
        postfach.als_gelesen(i)
    return ergebnis


# --- Eingangsordner -------------------------------------------------------------------------------
def ordner_scans(s: Session, m: Mandant, verzeichnis: Path, jetzt: datetime, kategorie: str = "material") -> dict[str, int]:
    """Dateien im Ordner (nicht rekursiv) zu Belegen; danach nach verarbeitet/ bzw. fehler/ verschoben."""
    ergebnis = {"gefunden": 0, "uebernommen": 0, "bekannt": 0, "verworfen": 0}
    verzeichnis = Path(verzeichnis)
    if not verzeichnis.is_dir():
        raise FileNotFoundError(f"Eingangsordner {verzeichnis} fehlt")
    verarbeitet, fehler = verzeichnis / "verarbeitet", verzeichnis / "fehler"
    for datei in sorted(p for p in verzeichnis.iterdir() if p.is_file() and not p.name.startswith(".")):
        ergebnis["gefunden"] += 1
        endung = datei.suffix.lower()
        if endung not in SCAN_ENDUNGEN or datei.stat().st_size > einsatz.MAX_ANHANG:
            fehler.mkdir(exist_ok=True)
            shutil.move(str(datei), str(fehler / datei.name))
            ergebnis["verworfen"] += 1
            continue
        inhalt = datei.read_bytes()
        b = scan_zu_beleg(s, m, inhalt, datei.name, SCAN_ENDUNGEN[endung], "datei:" + kennung_fuer(inhalt), jetzt, "eingangsordner",
                          kategorie, notiz=f"Scan aus Eingangsordner: {datei.name}")
        ergebnis["uebernommen" if b else "bekannt"] += 1
        verarbeitet.mkdir(exist_ok=True)
        ziel = verarbeitet / f"{jetzt.strftime('%Y%m%d-%H%M%S')}_{datei.name}"
        shutil.move(str(datei), str(ziel))
    return ergebnis


# --- Übergabe an Steuertools -----------------------------------------------------------------------
def uebergabekanaele(m: Mandant) -> dict[str, list[str]]:
    """Je Ziel die konfigurierten Kanäle, die beleg_uebermitteln beherrschen (email nur mit upload_email)."""
    ergebnis: dict[str, list[str]] = {}
    for ziel, faehig in UEBERGABEFAEHIG.items():
        e = m.integrationen.get(ziel) or {}
        kanaele = e.get("kanaele") or ([e["typ"]] if e.get("typ") else [])
        treffer = []
        for typ in kanaele:
            if typ not in faehig:
                continue
            if typ == "email" and not (e.get("email") or (e if e.get("typ") == "email" else {})).get("upload_email"):
                continue
            treffer.append(typ)
        if treffer:
            ergebnis[ziel] = treffer
    return ergebnis


def uebergabe_beantragen(s: Session, m: Mandant, b: Beleg, wer: str | None = None) -> list[OutboxEintrag]:
    """Outbox-Nachrichten je übergabefähigem Ziel; idempotent je Beleg und Ziel."""
    if b.status not in ("geprueft", "uebergeben", "bezahlt"):
        raise domaene.Domaenenfehler("Nur geprüfte Belege werden übergeben")
    eintraege = []
    for ziel in uebergabekanaele(m):
        eintraege.append(domaene.outbox_schreiben(s, m.id, ziel, "beleg_uebermitteln", {"beleg_id": b.id},
                                                  f"beleg_uebermitteln:{ziel}:{b.id}"))
    if eintraege:
        domaene.protokollieren(s, m.id, "beleg.uebergabe_beantragt", "beleg", b.id, "mensch" if wer else "system",
                               {"ziele": [e.ziel for e in eintraege]}, wer)
    return eintraege
