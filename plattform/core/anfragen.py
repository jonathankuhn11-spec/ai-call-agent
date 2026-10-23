"""Anfrageeingang ohne Telefon: Webformular der Betriebswebsite und E-Mail-Postfach (IMAP).

Jede Anfrage wird gespeichert (Rohdaten, Absender) und sofort zum Servicefall mit Dringlichkeit aus der Regelschicht;
bekannte Kunden werden über Rufnummer oder E-Mail zugeordnet, neue angelegt. Das Postfach wird per Tageslauf oder
`python -m core posteingang` abgerufen; gelesene Nachrichten werden an ihrer Message-ID erkannt (idempotent).
Das Webformular braucht keinen API-Key: Der Betrieb bindet seinen Formular-Token ein (Einstellungen → Webformular).
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import re
import secrets
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene
from core.modelle import Anfrage, Kommunikation, Kunde, Mandant, Servicefall
from core.regeln import dringlichkeit_bewerten, telefon_normalisieren

MUSTER_TELEFON = re.compile(r"(?:\+49|0)[\d\s/()-]{6,20}\d")


def _kunde_finden(s: Session, m: Mandant, telefon: str | None, mail: str | None) -> Kunde | None:
    if telefon:
        k = s.scalar(select(Kunde).where(Kunde.mandant_id == m.id, Kunde.telefon == telefon))
        if k:
            return k
    if mail:
        return s.scalar(select(Kunde).where(Kunde.mandant_id == m.id, Kunde.email == mail.lower()))
    return None


def anfrage_verarbeiten(s: Session, m: Mandant, kanal: str, externe_id: str, name: str | None, telefon: str | None, mail: str | None,
                        betreff: str | None, nachricht: str | None, jetzt: datetime, strasse: str | None = None, plz: str | None = None,
                        ort: str | None = None) -> Anfrage | None:
    """Idempotent je externe_id. Legt Anfrage, Kunde (falls neu), Servicefall und Kommunikation an."""
    if s.scalar(select(Anfrage).where(Anfrage.mandant_id == m.id, Anfrage.externe_id == externe_id)):
        return None
    tel = telefon_normalisieren(telefon) if telefon else None
    mail = (mail or "").strip().lower() or None
    kunde = _kunde_finden(s, m, tel, mail)
    text = f"{betreff or ''}\n{nachricht or ''}".strip()
    a = Anfrage(mandant_id=m.id, kanal=kanal, externe_id=externe_id[:200], name=name, telefon=tel or telefon, email=mail, betreff=(betreff or "")[:300] or None,
                nachricht=nachricht, eingegangen=jetzt)
    s.add(a)
    if not text or len(text) < 5:
        a.status = "verworfen"
        s.flush()
        return a
    if kunde is None:
        if not (tel or mail):
            a.status = "neu"  # ohne Rückkanal bleibt es eine Büro-Aufgabe
            s.flush()
            return a
        kunde = Kunde(mandant_id=m.id, name=name or mail or tel or "Unbekannt", telefon=tel, email=mail, strasse=strasse, plz=plz, ort=ort)
        s.add(kunde)
        s.flush()
        domaene.protokollieren(s, m.id, "kunde.angelegt", "kunde", kunde.id, "system", {"quelle": kanal})
    bewertung = dringlichkeit_bewerten(text, jetzt.month)
    fall = Servicefall(mandant_id=m.id, kunde_id=kunde.id, anliegen=(betreff or nachricht or "Anfrage")[:200], symptom=nachricht,
                       dringlichkeit=bewertung.dringlichkeit, quelle=kanal, erstellt=jetzt)
    s.add(fall)
    s.flush()
    domaene.protokollieren(s, m.id, "servicefall.eingegangen", "servicefall", fall.id, "system",
                           {"quelle": kanal, "dringlichkeit": fall.dringlichkeit, "regel": bewertung.grund, "anfrage_id": a.id})
    s.add(Kommunikation(mandant_id=m.id, kunde_id=kunde.id, servicefall_id=fall.id, kanal=kanal, richtung="eingehend",
                        transkript=text, zusammenfassung=(betreff or nachricht or "")[:200], zeit=jetzt))
    a.kunde_id, a.servicefall_id, a.status = kunde.id, fall.id, "uebernommen"
    if bewertung.sicherheitshinweis:
        domaene.aktion_beantragen(s, m, "nachricht_an_endkunde", {"kunde_id": kunde.id, "betreff": "Wichtiger Sicherheitshinweis",
                                  "text": bewertung.sicherheitshinweis},
                                  "system", fall.id, "Sicherheitsfall aus schriftlicher Anfrage")
    s.flush()
    return a


def formular_token_erzeugen() -> str:
    return secrets.token_urlsafe(24)


def webformular_snippet(basis_url: str, token: str) -> str:
    return (f'<form method="post" action="{basis_url}/anfragen/web/{token}">\n'
            '  <input name="name" placeholder="Name" required> <input name="telefon" placeholder="Telefon" required>\n'
            '  <input name="email" type="email" placeholder="E-Mail"> <input name="plz" placeholder="PLZ"> <input name="ort" placeholder="Ort">\n'
            '  <textarea name="nachricht" placeholder="Was können wir für Sie tun?" required></textarea>\n'
            '  <input name="website" style="display:none" tabindex="-1" autocomplete="off">\n'
            '  <button>Anfrage senden</button>\n</form>')


# --- IMAP ---------------------------------------------------------------------------------------
class Postfach(Protocol):
    """Minimale IMAP-Schnittstelle, in Tests durch eine Attrappe ersetzt."""
    def ungelesene(self) -> list[bytes]: ...
    def als_gelesen(self, kennung: Any) -> None: ...


class ImapPostfach:
    def __init__(self, host: str, benutzer: str, passwort: str, ordner: str = "INBOX", port: int = 993):
        self.verbindung = imaplib.IMAP4_SSL(host, port)
        self.verbindung.login(benutzer, passwort)
        self.verbindung.select(ordner)
        self._kennungen: list[bytes] = []

    def ungelesene(self) -> list[bytes]:
        _, daten = self.verbindung.search(None, "UNSEEN")
        self._kennungen = daten[0].split() if daten and daten[0] else []
        nachrichten = []
        for k in self._kennungen[:50]:
            _, teile = self.verbindung.fetch(k, "(BODY.PEEK[])")
            nachrichten.append(teile[0][1])
        return nachrichten

    def als_gelesen(self, index: int) -> None:
        if index < len(self._kennungen):
            self.verbindung.store(self._kennungen[index], "+FLAGS", "\\Seen")

    def schliessen(self) -> None:
        try:
            self.verbindung.logout()
        except Exception:  # noqa: BLE001  Abmeldung ist Höflichkeit, kein Fehlerfall
            pass


def nachricht_lesen(roh: bytes) -> dict[str, Any]:
    n = email.message_from_bytes(roh, policy=email.policy.default)
    koerper = n.get_body(preferencelist=("plain", "html"))
    text = koerper.get_content() if koerper else ""
    if koerper is not None and koerper.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    absender = email.utils.parseaddr(n.get("From", ""))
    tel = MUSTER_TELEFON.search(text or "")
    return {"externe_id": n.get("Message-ID") or f"ohne-id:{hash(roh)}", "name": absender[0] or None, "email": absender[1] or None,
            "betreff": n.get("Subject"), "nachricht": (text or "").strip()[:5000], "telefon": tel.group(0) if tel else None}


def postfach_abrufen(s: Session, m: Mandant, postfach: Postfach, jetzt: datetime) -> dict[str, int]:
    """Ungelesene Nachrichten zu Anfragen; Autoreplies und Newsletter landen als verworfen, der Rest wird Servicefall."""
    ergebnis = {"gelesen": 0, "uebernommen": 0, "verworfen": 0, "bekannt": 0}
    for i, roh in enumerate(postfach.ungelesene()):
        ergebnis["gelesen"] += 1
        d = nachricht_lesen(roh)
        auto = any(k in (d["betreff"] or "").lower() for k in ("auto-reply", "automatische antwort", "out of office", "newsletter", "unsubscribe"))
        a = anfrage_verarbeiten(s, m, "email", d["externe_id"], d["name"], d["telefon"], d["email"], d["betreff"],
                                None if auto else d["nachricht"], jetzt)
        if a is None:
            ergebnis["bekannt"] += 1
        elif a.status == "uebernommen":
            ergebnis["uebernommen"] += 1
        else:
            ergebnis["verworfen"] += 1
        postfach.als_gelesen(i)
    return ergebnis
