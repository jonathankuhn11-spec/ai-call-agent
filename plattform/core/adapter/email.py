"""E-Mail an Endkunden (Terminbestätigung, Nachricht, Rechnung) und an die Kanzlei (Belegpaket, Einzelbeleg).

Einzelbelege gehen mit upload_email an die Upload-Adresse von DATEV Unternehmen online (eine Datei je Mail,
Betreff mit Lieferant und Belegnummer); ohne upload_email ist der E-Mail-Kanal dafür nicht zuständig.

SMTP mit STARTTLS oder SMTPS; ein deutscher Postfachanbieter oder der eigene Mailserver genügt.
Rechnungen gehen als PDF, bei Unternehmen zusätzlich als XRechnung-XML (EN 16931).
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any, Callable

from sqlalchemy.orm import Session

from core import vorlagen
from core.adapter.basis import (
    AdapterFehler,
    Konfigurationsfehler,
    beleg_dateiname,
    beleg_laden,
    beleg_referenz,
    beleg_uebergeben,
    kunde_laden,
    rechnung_laden,
    termin_laden,
)
from core.disposition import ZEITZONE
from core.domaene import als_utc
from core.modelle import Mandant, OutboxEintrag
from core.rechnung import pdf, xrechnung

Versand = Callable[[EmailMessage], None]


def smtp_versand(k: dict[str, Any]) -> Versand:
    def senden(nachricht: EmailMessage) -> None:
        try:
            if k.get("smtps"):
                server = smtplib.SMTP_SSL(k["host"], int(k.get("port", 465)), timeout=20)
            else:
                server = smtplib.SMTP(k["host"], int(k.get("port", 587)), timeout=20)
                server.starttls()
            with server:
                if k.get("benutzer"):
                    server.login(k["benutzer"], k.get("passwort", ""))
                server.send_message(nachricht)
        except smtplib.SMTPAuthenticationError as e:
            raise Konfigurationsfehler(f"SMTP-Anmeldung abgewiesen: {e}") from e
        except (smtplib.SMTPException, OSError) as e:
            raise AdapterFehler(f"SMTP: {e}") from e
    return senden


class EmailAdapter:
    name = "email"

    def __init__(self, konfig: dict[str, Any], versand: Versand | None = None):
        if not konfig.get("absender") or not (versand or konfig.get("host")):
            raise Konfigurationsfehler("E-Mail: absender und host sind Pflicht")
        self.k = konfig
        self.versand = versand or smtp_versand(konfig)
        self.gesendet: list[EmailMessage] = []

    def _nachricht(self, an: str, betreff: str, text: str) -> EmailMessage:
        n = EmailMessage()
        n["From"] = self.k["absender"]
        n["To"] = an
        n["Subject"] = betreff
        if self.k.get("antwort_an"):
            n["Reply-To"] = self.k["antwort_an"]
        n.set_content(text)
        return n

    def _senden(self, n: EmailMessage) -> str:
        self.versand(n)
        self.gesendet.append(n)
        return n["Message-ID"] or f"email:{n['To']}:{n['Subject']}"

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        m = s.get(Mandant, e.mandant_id)
        gruss = f"\n\nMit freundlichen Grüßen\n{m.name}\n{m.stammdaten.get('telefon', '')}"
        if e.aktion == "terminbestaetigung":
            t, fall, kunde = termin_laden(s, e)
            if not kunde.email:
                return None  # kein Kanal: SMS-Adapter oder Büro-Aufgabe greift (Degradierung, kein Fehler)
            beginn = als_utc(t.beginn).astimezone(ZEITZONE)
            v = vorlagen.rendern(m, "terminbestaetigung_email", vorlagen.termin_kontext(t, fall, kunde, m, beginn))
            return self._senden(self._nachricht(kunde.email, v["betreff"], v["text"]))
        if e.aktion == "nachricht":
            kunde = kunde_laden(s, e)
            an = e.payload.get("email") or (kunde.email if kunde else None)
            if not an:
                return None
            v = vorlagen.rendern(m, "nachricht_email", {"kunde": {"name": kunde.name if kunde else ""}, "text": e.payload.get("text", "")})
            return self._senden(self._nachricht(an, e.payload.get("betreff", f"Nachricht von {m.name}"), v["text"]))
        if e.aktion == "rechnung_zustellen":
            r, m = rechnung_laden(s, e)
            an = r.empfaenger.get("email")
            if not an:
                return None  # Rechnung geht per Post; Büro sieht das in der Outbox-Übersicht
            art = "Stornorechnung" if r.art == "storno" else "Rechnung"
            v = vorlagen.rendern(m, "rechnung_email", {"kunde": {"name": r.empfaenger.get("name", "")}, "art": art, "nummer": r.nummer, "brutto": r.brutto.replace(".", ",")})
            n = self._nachricht(an, v["betreff"], v["text"])
            n.add_attachment(pdf(r, m), maintype="application", subtype="pdf", filename=f"{r.nummer}.pdf")
            if e.payload.get("xrechnung") or r.leitweg_id:
                n.add_attachment(xrechnung(r, m), maintype="application", subtype="xml", filename=f"{r.nummer}.xml")
            return self._senden(n)
        if e.aktion == "angebot_zustellen":
            from core import angebote
            from core.modelle import Angebot
            a = s.get(Angebot, e.payload.get("angebot_id", ""))
            if a is None or a.mandant_id != e.mandant_id:
                raise Konfigurationsfehler("Angebot zur Outbox-Nachricht nicht gefunden")
            an = a.empfaenger.get("email")
            if not an:
                return None  # Angebot geht per Post oder wird im Büro gedruckt
            v = vorlagen.rendern(m, "angebot_email", {"kunde": {"name": a.empfaenger.get("name", "")}, "nummer": a.nummer, "titel": a.titel,
                                                      "brutto": a.brutto.replace(".", ","), "gueltig_bis": a.gueltig_bis.strftime("%d.%m.%Y")})
            n = self._nachricht(an, v["betreff"], v["text"])
            n.add_attachment(angebote.pdf(a, m), maintype="application", subtype="pdf", filename=f"{a.nummer}.pdf")
            return self._senden(n)
        if e.aktion == "mahnung_zustellen":
            from core import forderungen
            from core.modelle import Mahnung
            mh = s.get(Mahnung, e.payload.get("mahnung_id", ""))
            if mh is None or mh.mandant_id != e.mandant_id:
                raise Konfigurationsfehler("Mahnung zur Outbox-Nachricht nicht gefunden")
            r = mh.rechnung
            an = r.empfaenger.get("email")
            if not an:
                return None
            titel = forderungen.MAHNSTUFEN[mh.stufe][0]
            n = self._nachricht(an, f"{titel} zur Rechnung {r.nummer}", f"Guten Tag {r.empfaenger.get('name', '')},\n\nzur Rechnung {r.nummer} "
                                f"sind noch {mh.offen.replace('.', ',')} € offen. Die {titel} mit Zahlungsfrist {mh.frist.strftime('%d.%m.%Y')} "
                                f"finden Sie im Anhang.{gruss}")
            n.add_attachment(forderungen.mahnung_pdf(mh, m), maintype="application", subtype="pdf", filename=f"{titel}_{r.nummer}.pdf")
            return self._senden(n)
        if e.aktion == "belegpaket":
            an = self.k.get("kanzlei_email") or e.payload.get("email")
            if not an:
                raise Konfigurationsfehler("Belegversand: kanzlei_email fehlt in der E-Mail-Anbindung")
            from core.adapter.belegpaket import paket_erzeugen
            dateiname, inhalt, zusammenfassung = paket_erzeugen(s, m, e.payload)
            n = self._nachricht(an, f"Belege {m.name} {e.payload.get('zeitraum', '')}".strip(), zusammenfassung + gruss)
            n.add_attachment(inhalt, maintype="application", subtype="zip", filename=dateiname)
            return self._senden(n)
        if e.aktion == "beleg_uebermitteln":
            an = self.k.get("upload_email")
            if not an:
                return None  # kein Upload-Postfach: Belegtransfer-Ordner oder Buchhaltungssoftware übernimmt
            b, a, m = beleg_laden(s, e)
            if (vorhanden := beleg_referenz(s, b, self.name)):
                return vorhanden
            if a is None:
                raise Konfigurationsfehler("Beleg ohne Datei kann nicht per Upload-Mail übergeben werden")
            n = self._nachricht(an, f"Eingangsbeleg {b.lieferant} {b.nummer or ''} vom {b.datum.strftime('%d.%m.%Y')}".strip(),
                                f"Eingangsbeleg {b.lieferant}, Nummer {b.nummer or '–'}, Belegdatum {b.datum.strftime('%d.%m.%Y')}, "
                                f"brutto {b.brutto.replace('.', ',')} € (USt {b.ust_satz} %). Kategorie {b.kategorie}"
                                + (f", Vorgang {b.servicefall_id}" if b.servicefall_id else "") + f".\n\nÜbermittelt vom SHK-Backoffice {m.name}.")
            haupt, _, unter = (a.mime or "application/octet-stream").partition("/")
            n.add_attachment(a.inhalt, maintype=haupt, subtype=unter or "octet-stream", filename=beleg_dateiname(b, a))
            return beleg_uebergeben(s, b, self.name, self._senden(n))
        raise Konfigurationsfehler(f"E-Mail-Adapter kennt die Aktion {e.aktion!r} nicht")
