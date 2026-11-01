"""Betreiberstimme: geklonte Stimme des Inhabers (oder eines Mitarbeiters) für den Sprachagenten.

Fachlogik ohne HTTP: Stimmprofil mit Referenzaufnahme und dokumentierter Einwilligung je Mandant,
Aktivierung genau eines Profils, Widerruf mit Löschung, Übergabe an den Stimmdienst (stimme/) und die
Konfiguration, die der LiveKit-Agent zu Gesprächsbeginn abruft (`GET /voice/konfiguration`).

Rechtlicher Rahmen (Hinweise, keine Rechtsberatung): Die Stimme ist ein personenbezogenes Datum; die
Erstellung eines Stimmmodells stützt sich auf die Einwilligung der sprechenden Person (Art. 6 Abs. 1 lit. a,
Art. 7 DSGVO; bei biometrischer Auswertung Art. 9 Abs. 2 lit. a). Der Agent legt zu Beginn offen, dass
eine KI mit der nachgebildeten Stimme spricht (Art. 50 Abs. 1 KI-VO); die Ausgabe des Stimmdienstes trägt
ein Wasserzeichen (Art. 50 Abs. 2 KI-VO). Widerruf löscht Aufnahme und Modellreferenz.
"""
from __future__ import annotations

import io
import os
import wave
from datetime import date, datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.domaene import protokollieren
from core.modelle import Kommunikation, Mandant, Stimmprofil, jetzt

MAX_PROBE = 15 * 1024 * 1024
MIN_PROBE = 20 * 1024
MIN_DAUER, MAX_DAUER = 5.0, 180.0
MIME = {b"RIFF": "audio/wav", b"fLaC": "audio/flac", b"OggS": "audio/ogg", b"ID3": "audio/mpeg"}
ROLLEN = ("inhaber", "mitarbeiter")

EINWILLIGUNG = ("Ich, {name}, willige ein, dass aus meiner Sprachaufnahme ein synthetisches Stimmmodell erstellt wird, mit dem der "
                "telefonische Assistent von {betrieb} Anrufe führt. Die Aufnahme und das Modell werden ausschließlich dafür auf den "
                "Systemen des Betriebs bzw. seines Auftragsverarbeiters verarbeitet und nicht an Dritte weitergegeben. Der Assistent "
                "nennt zu Gesprächsbeginn, dass eine KI mit meiner nachgebildeten Stimme spricht. Ich kann diese Einwilligung jederzeit "
                "mit Wirkung für die Zukunft widerrufen; Aufnahme und Stimmmodell werden dann gelöscht "
                "(Art. 6 Abs. 1 lit. a, Art. 7, Art. 9 Abs. 2 lit. a DSGVO).")
BEGRUESSUNG = ("Guten Tag, hier ist der digitale Assistent von {betrieb}, ein KI-System. Ich nehme Ihr Anliegen auf und schlage "
               "Ihnen einen Termin vor. Ihre Angaben werden dafür in Text umgewandelt und gespeichert, das Gespräch wird nicht "
               "aufgezeichnet. Wie kann ich helfen?")
BEGRUESSUNG_STIMME = ("Guten Tag, hier ist der digitale Assistent von {betrieb}, ein KI-System, das mit der nachgebildeten Stimme von "
                      "{sprecher} spricht. Ich nehme Ihr Anliegen auf und schlage Ihnen einen Termin vor. Ihre Angaben werden dafür in "
                      "Text umgewandelt und gespeichert, das Gespräch wird nicht aufgezeichnet. Wie kann ich helfen?")


class Stimmfehler(ValueError):
    pass


# --- Referenzaufnahme ------------------------------------------------------------------------------
def wav_dauer(inhalt: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(inhalt)) as w:
            rate, frames = w.getframerate(), w.getnframes()
            return frames / rate if rate else None
    except (wave.Error, EOFError):
        return None


def probe_pruefen(inhalt: bytes, mime: str | None) -> tuple[str, float | None]:
    """Erkennt das Format an den Magic Bytes (der gemeldete MIME-Typ ist Browser-Hörensagen) und prüft Größe und Dauer."""
    if len(inhalt) > MAX_PROBE:
        raise Stimmfehler(f"Aufnahme größer als {MAX_PROBE // 1024 // 1024} MB; 20–60 Sekunden WAV oder FLAC genügen")
    if len(inhalt) < MIN_PROBE:
        raise Stimmfehler("Aufnahme ist zu kurz oder leer")
    kopf = inhalt[:4]
    erkannt = next((typ for magie, typ in MIME.items() if kopf.startswith(magie)), None)
    if erkannt is None and kopf[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        erkannt = "audio/mpeg"
    if erkannt is None:
        raise Stimmfehler("Format nicht erkannt; erlaubt sind WAV, FLAC, MP3 und OGG")
    dauer = wav_dauer(inhalt) if erkannt == "audio/wav" else None
    if dauer is not None and dauer < MIN_DAUER:
        raise Stimmfehler(f"Aufnahme ist nur {dauer:.1f} s lang; mindestens {MIN_DAUER:.0f} s, besser 20–60 s")
    if dauer is not None and dauer > MAX_DAUER:
        raise Stimmfehler(f"Aufnahme ist {dauer:.0f} s lang; höchstens {MAX_DAUER:.0f} s (der Klon braucht nur 20–60 s)")
    return erkannt, dauer


# --- Stimmdienst (stimme/server.py) ------------------------------------------------------------------
class Dienst:
    """Client zum Stimmdienst; ohne STIMME_URL bleibt alles lokal (Profil wird nur gespeichert)."""

    def __init__(self, url: str | None = None, api_key: str | None = None, transport: httpx.BaseTransport | None = None, timeout: float = 20.0):
        self.url = (url if url is not None else os.environ.get("STIMME_URL", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("STIMME_API_KEY", "")
        self.transport, self.timeout = transport, timeout

    @property
    def konfiguriert(self) -> bool:
        return bool(self.url)

    def _kopf(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=self.timeout, headers=self._kopf())

    def gesund(self) -> dict[str, Any] | None:
        if not self.konfiguriert:
            return None
        try:
            with self._client() as c:
                r = c.get(f"{self.url}/health")
            return r.json() if r.status_code == 200 else {"status": f"HTTP {r.status_code}"}
        except (httpx.HTTPError, ValueError) as e:
            return {"status": "nicht erreichbar", "fehler": str(e)[:200]}

    def registrieren(self, p: Stimmprofil) -> None:
        with self._client() as c:
            r = c.post(f"{self.url}/v1/voices", data={"id": p.id, "name": p.name},
                       files={"datei": (p.probe_name, p.probe, p.probe_mime)})
        if r.status_code >= 300:
            raise Stimmfehler(f"Stimmdienst antwortet {r.status_code}: {r.text[:200]}")

    def loeschen(self, stimme_id: str) -> None:
        with self._client() as c:
            r = c.delete(f"{self.url}/v1/voices/{stimme_id}")
        if r.status_code >= 300 and r.status_code != 404:
            raise Stimmfehler(f"Stimmdienst antwortet {r.status_code}")

    def sprechen(self, text: str, stimme_id: str, format: str = "wav", sprache: str = "de") -> tuple[bytes, str]:
        with self._client() as c:
            r = c.post(f"{self.url}/v1/audio/speech", json={"input": text, "voice": stimme_id, "response_format": format, "language": sprache,
                                                            "model": "stimme"})
        if r.status_code >= 300:
            raise Stimmfehler(f"Stimmdienst antwortet {r.status_code}: {r.text[:200]}")
        return r.content, r.headers.get("content-type", "audio/wav")


# --- Profile ---------------------------------------------------------------------------------------
def alle(s: Session, m: Mandant) -> list[Stimmprofil]:
    return list(s.scalars(select(Stimmprofil).where(Stimmprofil.mandant_id == m.id).order_by(Stimmprofil.erstellt.desc())))


def aktives(s: Session, m: Mandant) -> Stimmprofil | None:
    return s.scalar(select(Stimmprofil).where(Stimmprofil.mandant_id == m.id, Stimmprofil.aktiv.is_(True), Stimmprofil.status == "bereit"))


def anlegen(s: Session, m: Mandant, *, name: str, rolle: str, inhalt: bytes, dateiname: str, mime: str | None,
            einwilligung_bestaetigt: bool, einwilligung_durch: str, heute: date, akteur_name: str | None = None) -> Stimmprofil:
    name, einwilligung_durch = name.strip(), einwilligung_durch.strip()
    if len(name) < 2:
        raise Stimmfehler("Name der sprechenden Person fehlt")
    if rolle not in ROLLEN:
        raise Stimmfehler(f"Rolle muss {' oder '.join(ROLLEN)} sein")
    if not einwilligung_bestaetigt:
        raise Stimmfehler("Ohne bestätigte Einwilligung der sprechenden Person wird kein Stimmprofil angelegt")
    if len(einwilligung_durch) < 2:
        raise Stimmfehler("Wer hat die Einwilligung entgegengenommen? (Name)")
    erkannt, dauer = probe_pruefen(inhalt, mime)
    p = Stimmprofil(mandant_id=m.id, name=name, rolle=rolle, status="bereit", aktiv=False, anbieter="eigen",
                    einwilligung_am=heute, einwilligung_text=EINWILLIGUNG.format(name=name, betrieb=m.name), einwilligung_durch=einwilligung_durch,
                    probe_name=(dateiname or "aufnahme")[:200], probe_mime=erkannt, probe_groesse=len(inhalt), probe_dauer_sek=dauer, probe=inhalt)
    s.add(p)
    s.flush()
    protokollieren(s, m.id, "stimme.angelegt", "stimmprofil", p.id, "mensch",
                   {"name": name, "rolle": rolle, "einwilligung_am": heute.isoformat(), "dauer_sek": dauer, "mime": erkannt}, akteur_name)
    return p


def aktivieren(s: Session, m: Mandant, p: Stimmprofil, aktiv: bool, dienst: Dienst | None, akteur_name: str | None = None) -> Stimmprofil:
    if p.status != "bereit":
        raise Stimmfehler("Profil ist widerrufen; bitte neu aufnehmen")
    if aktiv:
        for anderes in alle(s, m):
            if anderes.id != p.id:
                anderes.aktiv = False
        if dienst and dienst.konfiguriert:
            try:
                dienst.registrieren(p)
                p.synchronisiert, p.letzter_fehler = jetzt(), None
            except (Stimmfehler, httpx.HTTPError) as e:
                p.letzter_fehler = str(e)[:300]   # der Dienst holt die Probe beim ersten Anruf selbst nach
    p.aktiv = aktiv
    protokollieren(s, m.id, "stimme.aktiviert" if aktiv else "stimme.deaktiviert", "stimmprofil", p.id, "mensch",
                   {"name": p.name, "synchronisiert": p.synchronisiert.isoformat() if p.synchronisiert else None}, akteur_name)
    return p


def widerrufen(s: Session, m: Mandant, p: Stimmprofil, dienst: Dienst | None, akteur_name: str | None = None) -> Stimmprofil:
    """Widerruf der Einwilligung: Aufnahme sofort löschen, Referenz im Stimmdienst entfernen, Profil als Nachweis behalten."""
    p.status, p.aktiv, p.probe, p.probe_groesse, p.widerrufen_am = "widerrufen", False, b"", 0, jetzt()
    if dienst and dienst.konfiguriert:
        try:
            dienst.loeschen(p.id)
        except (Stimmfehler, httpx.HTTPError) as e:
            p.letzter_fehler = f"Löschung im Stimmdienst ausstehend: {str(e)[:200]}"
    protokollieren(s, m.id, "stimme.widerrufen", "stimmprofil", p.id, "mensch", {"name": p.name}, akteur_name)
    return p


def aus(p: Stimmprofil) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "rolle": p.rolle, "status": p.status, "aktiv": p.aktiv, "anbieter": p.anbieter,
            "einwilligung_am": p.einwilligung_am.isoformat() if p.einwilligung_am else None, "einwilligung_durch": p.einwilligung_durch,
            "einwilligung_text": p.einwilligung_text, "probe_name": p.probe_name, "probe_mime": p.probe_mime, "probe_groesse": p.probe_groesse,
            "probe_dauer_sek": p.probe_dauer_sek, "synchronisiert": p.synchronisiert.isoformat() if p.synchronisiert else None,
            "widerrufen_am": p.widerrufen_am.isoformat() if p.widerrufen_am else None, "letzter_fehler": p.letzter_fehler,
            "erstellt": p.erstellt.isoformat() if p.erstellt else None}


# --- Agentenkonfiguration --------------------------------------------------------------------------
def begruessung(m: Mandant, p: Stimmprofil | None) -> str:
    vorlage = (m.stammdaten.get("begruessung_stimme") or BEGRUESSUNG_STIMME) if p else (m.stammdaten.get("begruessung") or BEGRUESSUNG)
    return vorlage.format(betrieb=m.name, sprecher=p.name if p else "")


def sprachminuten(s: Session, m: Mandant, heute: date) -> float:
    """Telefonminuten des laufenden Monats aus den Gesprächsdaten (extraktion.dauer_sekunden, vom Agenten gemeldet)."""
    anfang = datetime(heute.year, heute.month, 1)
    summe = 0.0
    for extraktion in s.scalars(select(Kommunikation.extraktion).where(Kommunikation.mandant_id == m.id, Kommunikation.kanal == "telefon",
                                                                       Kommunikation.zeit >= anfang)):
        try:
            summe += float((extraktion or {}).get("dauer_sekunden") or 0)
        except (TypeError, ValueError):
            pass
    return round(summe / 60, 1)


def konfiguration(s: Session, m: Mandant, heute: date, dienst: Dienst | None, status: dict[str, Any]) -> dict[str, Any]:
    """Was der Agent zu Gesprächsbeginn braucht: Begrüßung, Stimme, Weiterleitung, Minutenkontingent."""
    p = aktives(s, m) if "eigene_stimme" in status["features"] else None
    stimme = None
    if p is not None:
        stimme = {"anbieter": p.anbieter, "id": p.id, "sprecher": p.name, "rolle": p.rolle, "dienst_url": dienst.url if dienst else "",
                  "synchronisiert": p.synchronisiert is not None}
    minuten = sprachminuten(s, m, heute)
    limit = status["limits"].get("sprachminuten_monat")
    return {"betrieb": m.name, "sprache": "de", "begruessung": begruessung(m, p), "stimme": stimme,
            "weiterleitung": {"erlaubt": bool(m.policies.get("notdienst_weiterleiten", False))},
            "sprachminuten": {"verbraucht": minuten, "limit": limit, "erschoepft": bool(limit and minuten >= limit)},
            "edition": status["edition"], "anzahl_profile": s.scalar(select(func.count()).select_from(Stimmprofil).where(Stimmprofil.mandant_id == m.id))}
