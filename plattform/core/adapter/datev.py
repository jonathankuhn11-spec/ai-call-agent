"""DATEV-Datenservices (DATEV Developer Portal): Belegbilder, Buchungsstapel und Rechnungsdaten direkt nach
DATEV Unternehmen online / Kanzlei-Rechnungswesen, ohne Belegtransfer-Programm oder Upload-Mail.

Zugang: DATEV-Login (OpenID Connect, Authorization Code mit PKCE optional) im Namen des Betriebs; der Betrieb bzw.
seine Kanzlei schaltet die Datenservices im DATEV-Rechenzentrum für die Beraternummer/Mandantennummer frei. Die
Plattform speichert nur das Refresh-Token (verschlüsselt per INTEGRATIONS_KEY) und holt je Lauf ein Access-Token.

Dienste und Pfade nach unserem Kenntnisstand (API-Gateway api.datev.de, Sandbox platform-sandbox, Produktion platform;
Login login.datev.de/openidsandbox bzw. /openid). Die Pfade sind konfigurierbar (pfad_*), weil DATEV die Versionen
fortschreibt – vor Inbetriebnahme gegen das Developer Portal prüfen, Sandbox-Freischaltung über die DATEV-Marktplatz-
Partnerschaft beantragen:
  Belegbilderservice      POST {pfad_belege}/{berater}-{mandant}/documents     multipart file + metadata (category)
  Buchungsdatenservice    POST {pfad_buchungsstapel}/{berater}-{mandant}/dxso-jobs  EXTF-Buchungsstapel (CSV)
  Rechnungsdatenservice   POST {pfad_rechnungen}/{berater}-{mandant}/invoices   XRechnung-XML einer Ausgangsrechnung
Aktionen: rechnung_uebermitteln (PDF als Rechnungsausgang + optional XRechnung an den Rechnungsdatenservice),
beleg_uebermitteln (Eingangsbeleg als Rechnungseingang), belegpaket (EXTF-Buchungsstapel des Monats, Festschreibung 0,
Konten nur als Vorschlag – keine Kontierung, § 6 Nr. 3 StBerG).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.adapter.basis import (AdapterFehler, Konfigurationsfehler, antwort_pruefen, beleg_dateiname, beleg_laden,
                                beleg_referenz, beleg_uebergeben, client, rechnung_laden)
from core.adapter.belegpaket import extf_buchungsstapel, zeitraum
from core.modelle import Beleg, ExternalRef, Mandant, OutboxEintrag, Rechnung
from core.rechnung import pdf, xrechnung

UMGEBUNGEN = {
    "sandbox": {"login": "https://login.datev.de/openidsandbox", "api": "https://api.datev.de/platform-sandbox"},
    "produktion": {"login": "https://login.datev.de/openid", "api": "https://api.datev.de/platform"},
}
PFADE = {"pfad_belege": "/accounting/v1/clients", "pfad_buchungsstapel": "/accounting/v2/clients", "pfad_rechnungen": "/accounting/v1/clients"}
SCOPES = "openid profile email offline_access datev:accounting:documents datev:accounting:extf-files datev:accounting:invoices"
KATEGORIEN = {"rechnung": "Rechnungsausgang", "storno": "Rechnungsausgang", "beleg": "Rechnungseingang"}


def umgebung(konfig: dict[str, Any]) -> dict[str, str]:
    u = dict(UMGEBUNGEN.get(konfig.get("umgebung", "sandbox"), UMGEBUNGEN["sandbox"]))
    if konfig.get("login_url"):
        u["login"] = konfig["login_url"].rstrip("/")
    if konfig.get("basis_url"):
        u["api"] = konfig["basis_url"].rstrip("/")
    return u


def _schluessel() -> bytes:
    return (os.environ.get("INTEGRATIONS_KEY") or os.environ.get("CORE_API_KEY") or "entwicklung").encode()


def zustand(mandant_id: str) -> str:
    """OAuth-state: Mandanten-ID plus HMAC, damit der Rückruf dem richtigen Betrieb zugeordnet und nicht gefälscht wird."""
    return f"{mandant_id}.{hmac.new(_schluessel(), f'datev:{mandant_id}'.encode(), hashlib.sha256).hexdigest()[:32]}"


def zustand_pruefen(wert: str | None) -> str | None:
    if not wert or "." not in wert:
        return None
    mandant_id, _, sig = wert.partition(".")
    return mandant_id if hmac.compare_digest(zustand(mandant_id).split(".")[1], sig) else None


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def anmelde_url(konfig: dict[str, Any], mandant_id: str, redirect_uri: str, code_challenge: str | None = None) -> str:
    if not konfig.get("client_id"):
        raise Konfigurationsfehler("DATEV: client_id fehlt (Registrierung im DATEV Developer Portal)")
    params = {"response_type": "code", "client_id": konfig["client_id"], "redirect_uri": redirect_uri, "scope": konfig.get("scopes", SCOPES),
              "state": zustand(mandant_id), "nonce": secrets.token_urlsafe(16)}
    if code_challenge:
        params.update({"code_challenge": code_challenge, "code_challenge_method": "S256"})
    return f"{umgebung(konfig)['login']}/authorize?{urlencode(params)}"


def _basic(konfig: dict[str, Any]) -> dict[str, str]:
    if not konfig.get("client_id") or not konfig.get("client_secret"):
        raise Konfigurationsfehler("DATEV: client_id und client_secret fehlen")
    return {"Authorization": "Basic " + base64.b64encode(f"{konfig['client_id']}:{konfig['client_secret']}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded"}


def code_einloesen(konfig: dict[str, Any], code: str, redirect_uri: str, code_verifier: str | None = None,
                   transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    daten = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    if code_verifier:
        daten["code_verifier"] = code_verifier
    with client(umgebung(konfig)["login"], _basic(konfig), transport) as http:
        antwort = antwort_pruefen(http.post("/token", data=daten), "DATEV Login (Code einlösen)")
    if not antwort.get("refresh_token"):
        raise AdapterFehler("DATEV Login: Antwort ohne refresh_token (Scope offline_access fehlt?)")
    return antwort


class DatevAdapter:
    name = "datev"

    def __init__(self, konfig: dict[str, Any], transport: httpx.BaseTransport | None = None):
        if not konfig.get("client_id") or not konfig.get("client_secret"):
            raise Konfigurationsfehler("DATEV: client_id und client_secret fehlen")
        if not konfig.get("berater_nr") or not konfig.get("mandant_nr"):
            raise Konfigurationsfehler("DATEV: berater_nr und mandant_nr fehlen")
        self.k = konfig
        self.transport = transport
        self.u = umgebung(konfig)
        self.klient = f"{konfig['berater_nr']}-{konfig['mandant_nr']}"
        self.pfade = {k: konfig.get(k, v) for k, v in PFADE.items()}
        self.xrechnung = bool(konfig.get("rechnungsdaten", False))
        self.buchungsstapel = bool(konfig.get("buchungsstapel", True))
        self._token: tuple[str, float] | None = None

    # --- Token -----------------------------------------------------------------------------
    def access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        if not self.k.get("refresh_token"):
            raise Konfigurationsfehler("DATEV: Verbindung noch nicht hergestellt (Anmeldung über Einstellungen → DATEV verbinden)")
        with client(self.u["login"], _basic(self.k), self.transport) as http:
            antwort = antwort_pruefen(http.post("/token", data={"grant_type": "refresh_token", "refresh_token": self.k["refresh_token"]}),
                                      "DATEV Login (Token erneuern)")
        if not antwort.get("access_token"):
            raise AdapterFehler("DATEV Login: Antwort ohne access_token")
        self._token = (antwort["access_token"], time.time() + int(antwort.get("expires_in", 300)))
        return self._token[0]

    def _http(self) -> httpx.Client:
        return client(self.u["api"], {"Authorization": f"Bearer {self.access_token()}", "X-DATEV-Client-Id": self.k["client_id"],
                                      "Accept": "application/json"}, self.transport)

    # --- Dienste ---------------------------------------------------------------------------
    def dokument_hochladen(self, inhalt: bytes, dateiname: str, mime: str, kategorie: str, notiz: str = "") -> str:
        metadaten = {"category": kategorie, "note": notiz[:200]}
        with self._http() as http:
            antwort = antwort_pruefen(http.post(f"{self.pfade['pfad_belege']}/{self.klient}/documents",
                                                files={"file": (dateiname, inhalt, mime),
                                                       "metadata": ("metadata.json", json.dumps(metadaten).encode(), "application/json")}),
                                      "DATEV Belegbilderservice")
        kennung = antwort.get("id") or antwort.get("document_id") or antwort.get("guid")
        if not kennung:
            raise AdapterFehler("DATEV Belegbilderservice: Antwort ohne Dokument-ID")
        return str(kennung)

    def buchungsstapel_hochladen(self, extf: bytes, dateiname: str) -> str:
        with self._http() as http:
            antwort = antwort_pruefen(http.post(f"{self.pfade['pfad_buchungsstapel']}/{self.klient}/dxso-jobs",
                                                files={"file": (dateiname, extf, "text/csv")}), "DATEV Buchungsdatenservice")
        return str(antwort.get("id") or antwort.get("job_id") or dateiname)

    def rechnungsdaten_hochladen(self, xml: bytes, dateiname: str) -> str:
        with self._http() as http:
            antwort = antwort_pruefen(http.post(f"{self.pfade['pfad_rechnungen']}/{self.klient}/invoices",
                                                files={"file": (dateiname, xml, "application/xml")}), "DATEV Rechnungsdatenservice")
        return str(antwort.get("id") or dateiname)

    # --- Outbox ----------------------------------------------------------------------------
    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        if e.aktion == "rechnung_uebermitteln":
            r, m = rechnung_laden(s, e)
            ref = s.scalar(select(ExternalRef).where(ExternalRef.mandant_id == r.mandant_id, ExternalRef.entitaet == "rechnung",
                                                     ExternalRef.entitaet_id == r.id, ExternalRef.zielsystem == self.name))
            if ref:
                return ref.externe_id
            kennung = self.dokument_hochladen(pdf(r, m), f"{r.nummer}.pdf", "application/pdf", KATEGORIEN[r.art],
                                              f"{'Storno' if r.art == 'storno' else 'Rechnung'} {r.nummer} {r.empfaenger.get('name', '')}")
            if self.xrechnung:
                self.rechnungsdaten_hochladen(xrechnung(r, m), f"{r.nummer}.xml")
            s.add(ExternalRef(mandant_id=r.mandant_id, entitaet="rechnung", entitaet_id=r.id, zielsystem=self.name, externe_id=kennung))
            return kennung
        if e.aktion == "beleg_uebermitteln":
            b, a, _ = beleg_laden(s, e)
            if (vorhanden := beleg_referenz(s, b, self.name)):
                return vorhanden
            if a is None:
                raise Konfigurationsfehler("Beleg ohne Datei kann nicht an DATEV übermittelt werden")
            kennung = self.dokument_hochladen(a.inhalt, beleg_dateiname(b, a), a.mime, KATEGORIEN["beleg"], f"{b.lieferant} {b.nummer or ''}")
            return beleg_uebergeben(s, b, self.name, kennung)
        if e.aktion == "belegpaket":
            if not self.buchungsstapel:
                return None
            m = s.get(Mandant, e.mandant_id)
            von, bis, kennung = zeitraum(e.payload, datetime.now(timezone.utc).date())
            rechnungen = s.scalars(select(Rechnung).where(Rechnung.mandant_id == m.id, Rechnung.datum >= von, Rechnung.datum < bis).order_by(Rechnung.nummer)).all()
            belege = s.scalars(select(Beleg).where(Beleg.mandant_id == m.id, Beleg.datum >= von, Beleg.datum < bis, Beleg.status != "erfasst")).all()
            if not rechnungen and not belege:
                return f"leer:{kennung}"
            return self.buchungsstapel_hochladen(extf_buchungsstapel(m, rechnungen, von, bis, datetime.now(timezone.utc), belege), f"EXTF_Buchungsstapel_{kennung}.csv")
        return None
