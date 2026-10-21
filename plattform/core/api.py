"""REST-API des Core-Service: CRM, Vorgänge, Disposition, Freigabe-Inbox, Digital Twin,
Ontologie-Export und die Tool-API für den Voice-Agenten.

Authentifizierung und Rollen: core/auth.py. Maschinen (Voice-Agent, n8n) mit `X-API-Key` und
`X-Mandant`, Personen mit OIDC-Bearer-Token (Keycloak), aus dem Mandant und Rollen stammen.
Jeder Endpunkt verlangt ein Recht (lesen, status, bearbeiten, freigeben, verwalten, plattform).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import base64

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core import belege, domaene, einsatz, loeschung, management, twin
from core.auth import Abgewiesen, Authentifizierung, Prinzipal
from core.db import Datenbank, mandant_binden
from core.disposition import freie_zeitfenster, kollidiert
from core.adapter import ERLAUBT, ZIELE, konfiguration_pruefen
from core.geheimnisse import GEHEIME_FELDER, konfiguration_maskieren, konfiguration_verschluesseln
from core.modelle import (Anhang, Anlage, Beleg, Bereitschaft, Ereignis, Freigabe, Frist, Kommunikation, Kunde, Mandant,
                          Objekt, OutboxEintrag, Rapport, Rechnung, Ressource, Servicefall, Termin, Zeiteintrag)
from core.ontologie import ANLAGENTYPEN, TBOX, anlagentyp, exportieren
from core.regeln import dringlichkeit_bewerten, qualifikation_fuer, telefon_normalisieren
from core.rechnung import pdf as rechnung_pdf
from core.rechnung import xrechnung

UI_VERZEICHNIS = Path(__file__).resolve().parent / "ui"

Dringlichkeit = Literal["notdienst", "hoch", "normal", "niedrig"]


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Schemas ----------------------------------------------------------------------------------
class MandantNeu(_Schema):
    name: str
    gewerk: str = "SHK"
    policies: dict[str, str] = {}
    puffer_minuten: int = Field(30, ge=0, le=240)
    raster_minuten: int = Field(30, ge=5, le=240)


class MandantAus(MandantNeu):
    id: str


class KundeNeu(_Schema):
    name: str
    telefon: str | None = None
    email: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    vertragsstatus: str = "ohne"
    notiz: str | None = None


class KundeAus(KundeNeu):
    id: str
    einwilligungen: dict[str, Any] = {}


class KundeAenderung(_Schema):
    name: str | None = None
    telefon: str | None = None
    email: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    vertragsstatus: str | None = None
    notiz: str | None = None


class ObjektNeu(_Schema):
    bezeichnung: str = "Hauptobjekt"
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    gebaeudetyp: str | None = None


class ObjektAus(ObjektNeu):
    id: str
    kunde_id: str


class AnlageNeu(_Schema):
    anlagentyp: str
    hersteller: str | None = None
    modell: str | None = None
    baujahr: int | None = Field(None, ge=1950, le=2100)
    wartungsintervall_monate: int | None = None
    letzte_wartung: date | None = None


class AnlageAus(AnlageNeu):
    id: str
    objekt_id: str


class RessourceNeu(_Schema):
    name: str
    qualifikationen: list[str] = []
    arbeitstage: list[int] = [0, 1, 2, 3, 4]
    arbeitsbeginn: str = "07:30"
    arbeitsende: str = "16:30"
    benutzer: str | None = None


class RessourceAenderung(_Schema):
    name: str | None = None
    qualifikationen: list[str] | None = None
    arbeitstage: list[int] | None = None
    arbeitsbeginn: str | None = None
    arbeitsende: str | None = None
    aktiv: bool | None = None
    benutzer: str | None = None


class RessourceAus(_Schema):
    id: str
    name: str
    qualifikationen: list[str]
    arbeitstage: list[int]
    benutzer: str | None = None
    aktiv: bool = True
    aktiv: bool


class ServicefallNeu(_Schema):
    kunde_id: str
    anlage_id: str | None = None
    anliegen: str
    symptom: str | None = None
    dringlichkeit: Dringlichkeit | None = None
    quelle: Literal["telefon", "email", "web", "manuell"] = "manuell"
    benoetigte_qualifikation: str | None = None
    dauer_minuten: int = Field(90, ge=15, le=600)


class ServicefallAus(_Schema):
    id: str
    kunde_id: str
    anlage_id: str | None
    anliegen: str
    symptom: str | None
    dringlichkeit: str
    status: str
    quelle: str
    benoetigte_qualifikation: str | None
    dauer_minuten: int
    erstellt: datetime


class StatusWechsel(_Schema):
    ziel: str
    grund: str | None = None
    wer: str = "buero"


class TerminNeu(_Schema):
    ressource_id: str
    beginn: datetime
    ende: datetime | None = None


class TerminAus(_Schema):
    id: str
    servicefall_id: str
    ressource_id: str
    beginn: datetime
    ende: datetime
    status: str


class FreigabeAus(_Schema):
    id: str
    aktion: str
    risikoklasse: str
    payload: dict[str, Any]
    servicefall_id: str | None
    status: str
    begruendung: str | None
    erstellt: datetime
    entschieden_von: str | None


class Entscheidung(_Schema):
    freigeben: bool
    wer: str | None = None  # nur für Maschinen; bei Personen gilt der Name aus dem Token


class AktionNeu(_Schema):
    aktion: str
    payload: dict[str, Any] = {}
    servicefall_id: str | None = None
    begruendung: str | None = None


# Voice-Tool-API: kurze, sprechbare Antworten; der Agent formuliert, der Core entscheidet.
class VoiceKundeSuchen(_Schema):
    telefon: str


class VoiceZeitfenster(_Schema):
    anliegen: str
    anlagentyp: str | None = None
    dringlichkeit: Dringlichkeit | None = None
    anzahl: int = Field(3, ge=1, le=6)


class VoiceServicefall(_Schema):
    telefon: str
    name: str
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    anliegen: str
    symptom: str | None = None
    anlagentyp: str | None = None
    hersteller: str | None = None
    dringlichkeit: Dringlichkeit | None = None
    ressource_id: str | None = Field(None, description="aus voice/zeitfenster, wenn der Anrufer ein Fenster gewählt hat")
    beginn: datetime | None = None


class SparqlAbfrage(_Schema):
    abfrage: str = Field(..., max_length=5000)


class Stammdaten(_Schema):
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    telefon: str | None = None
    email: str | None = None
    inhaber: str | None = None
    ust_id: str | None = None
    steuernummer: str | None = None
    handwerksrolle: str | None = None
    iban: str | None = None
    bic: str | None = None
    bank: str | None = None
    zahlungsziel_tage: int = Field(14, ge=0, le=90)
    kleinunternehmer: bool = False
    rechnungsnummer_praefix: str = Field("RE", max_length=10)
    rechnungshinweis: str | None = None
    kontenrahmen: Literal["SKR03", "SKR04"] = "SKR03"
    datev_beraternummer: str | None = None
    datev_mandantennummer: str | None = None
    kanzlei_email: str | None = None
    # Nachkalkulation (core/einsatz.py): interne Kosten und Verrechnungssatz je Stunde, Aufschlag auf Materialnetto
    stundensatz_kosten: float = Field(55, ge=0, le=500)
    stundensatz_verrechnung: float = Field(69, ge=0, le=500)
    materialaufschlag_prozent: float = Field(15, ge=0, le=200)


class Integration(_Schema):
    """Anbindung eines Ziels: kanaele in Reihenfolge, je Typ ein Konfigurationsblock."""
    kanaele: list[str] = Field(default_factory=list)
    email: dict[str, Any] = {}
    sms: dict[str, Any] = {}
    webhook: dict[str, Any] = {}
    lexware: dict[str, Any] = {}
    sevdesk: dict[str, Any] = {}
    belegpaket: dict[str, Any] = {}


class RechnungAus(_Schema):
    id: str
    nummer: str
    kunde_id: str
    servicefall_id: str | None
    art: str
    storno_von_id: str | None
    datum: date
    leistungsdatum: date | None
    faellig: date
    positionen: list[dict[str, Any]]
    netto: str
    umsatzsteuer: str
    brutto: str
    status: str
    empfaenger: dict[str, Any]


class StornoAntrag(_Schema):
    grund: str = Field(..., min_length=3)


class BelegpaketAntrag(_Schema):
    zeitraum: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class AnhangAus(_Schema):
    id: str
    bezug_typ: str
    bezug_id: str
    art: str
    name: str
    mime: str
    groesse: int
    notiz: str | None
    hochgeladen_von: str | None
    hochgeladen: datetime


class BelegAus(_Schema):
    id: str
    lieferant: str
    nummer: str | None
    datum: date
    faellig: date | None
    netto: str
    umsatzsteuer: str
    brutto: str
    ust_satz: str
    skonto_prozent: str | None
    skonto_bis: date | None
    positionen: list[dict[str, Any]]
    kategorie: str
    servicefall_id: str | None
    anhang_id: str | None
    quelle: str
    erkennung: dict[str, Any]
    status: str
    notiz: str | None
    geprueft_von: str | None
    erstellt: datetime


class BelegAenderung(_Schema):
    lieferant: str | None = None
    nummer: str | None = None
    datum: date | None = None
    faellig: date | None = None
    netto: str | None = None
    umsatzsteuer: str | None = None
    brutto: str | None = None
    ust_satz: Literal["19", "7", "0"] | None = None
    skonto_prozent: str | None = None
    skonto_bis: date | None = None
    kategorie: Literal["material", "fremdleistung", "fahrzeug", "sonstiges"] | None = None
    servicefall_id: str | None = None
    status: Literal["erfasst", "geprueft", "bezahlt"] | None = None
    notiz: str | None = None


class ZeitStart(_Schema):
    art: Literal["arbeit", "fahrt", "pause", "bereitschaft"] = "arbeit"
    servicefall_id: str | None = None
    ressource_id: str | None = None  # nur Büro; Monteure buchen auf ihre eigene Ressource
    notiz: str | None = None


class ZeitNeu(ZeitStart):
    beginn: datetime
    ende: datetime


class ZeitAus(_Schema):
    id: str
    ressource_id: str
    servicefall_id: str | None
    art: str
    beginn: datetime
    ende: datetime | None
    notiz: str | None
    quelle: str
    minuten: int = 0


class RapportNeu(_Schema):
    taetigkeiten: str = Field(..., min_length=3)
    material: list[dict[str, Any]] = []
    arbeitszeit_minuten: int = Field(0, ge=0, le=1440)
    fahrzeit_minuten: int = Field(0, ge=0, le=1440)
    ergebnis: Literal["erledigt", "nacharbeit", "ersatzteil", "abgebrochen"] = "erledigt"
    hinweise_kunde: str | None = None
    messwerte: dict[str, Any] = {}
    termin_id: str | None = None
    ressource_id: str | None = None  # nur Büro
    unterschrift: str | None = Field(None, description="PNG als data:image/png;base64,…")
    unterzeichnet_von: str | None = None


class RapportAus(_Schema):
    id: str
    servicefall_id: str
    termin_id: str | None
    ressource_id: str
    nummer: str
    taetigkeiten: str
    material: list[dict[str, Any]]
    arbeitszeit_minuten: int
    fahrzeit_minuten: int
    ergebnis: str
    hinweise_kunde: str | None
    messwerte: dict[str, Any]
    unterschrift_anhang_id: str | None
    unterzeichnet_von: str | None
    unterzeichnet_am: datetime | None
    erstellt: datetime


class BereitschaftNeu(_Schema):
    ressource_id: str
    von: datetime
    bis: datetime
    telefon: str
    notiz: str | None = None


class BereitschaftAus(BereitschaftNeu):
    id: str


class FristNeu(_Schema):
    bezug_typ: Literal["betrieb", "ressource", "anlage", "fahrzeug"] = "betrieb"
    bezug_id: str | None = None
    art: Literal["pruefung", "unterweisung", "zertifikat", "garantie", "vertrag", "sonstiges"]
    bezeichnung: str = Field(..., min_length=2)
    faellig: date
    intervall_monate: int | None = Field(None, ge=1, le=120)
    hinweis: str | None = None


class FristAus(FristNeu):
    id: str
    erledigt_am: date | None


class FristErledigt(_Schema):
    am: date | None = None


class VoiceGespraechBeendet(_Schema):
    telefon: str | None = None
    servicefall_id: str | None = None
    transkript: str
    zusammenfassung: str | None = None
    extraktion: dict[str, Any] = {}
    aufzeichnung_eingewilligt: bool = False


# --- App --------------------------------------------------------------------------------------
def app_erzeugen(db: Datenbank | None = None, uhr=lambda: datetime.now(timezone.utc),
                 system_db: Datenbank | None = None, auth: Authentifizierung | None = None) -> FastAPI:
    """db: Verbindung des Core (in PostgreSQL Rolle shk_app, unterliegt RLS).
    system_db: mandantenübergreifende Verwaltung (Rolle shk_system); ohne Angabe SYSTEM_DATABASE_URL bzw. db."""
    db = db or Datenbank()
    db.schema_anlegen()
    if system_db is None:
        system_db = Datenbank(os.environ["SYSTEM_DATABASE_URL"]) if os.environ.get("SYSTEM_DATABASE_URL") else db
    auth = auth or Authentifizierung.aus_umgebung()
    app = FastAPI(title="SHK-Backoffice Core-Service", version="0.2.0")
    app.state.db, app.state.system_db, app.state.uhr, app.state.auth = db, system_db, uhr, auth

    def sitzung():
        yield from db.sitzung()

    def system_sitzung():
        yield from system_db.sitzung()

    def prinzipal(request: Request, authorization: str | None = Header(None),
                  x_api_key: str | None = Header(None)) -> Prinzipal:
        try:
            p = auth.prinzipal(authorization, x_api_key)
        except Abgewiesen as e:
            raise HTTPException(e.status, e.grund) from e
        request.state.prinzipal = p
        return p

    def recht(name: str):
        def pruefen(p: Prinzipal = Depends(prinzipal)) -> Prinzipal:
            if not p.darf(name):
                raise HTTPException(403, f"Recht {name!r} fehlt (Rollen: {', '.join(sorted(p.rollen)) or 'keine'})")
            return p
        return pruefen

    def mandant_kontext(x_mandant: str | None = Header(None), s: Session = Depends(sitzung),
                        p: Prinzipal = Depends(recht("lesen"))) -> Mandant:
        if p.art == "person":
            if x_mandant and x_mandant != p.mandant:
                raise HTTPException(403, "X-Mandant weicht vom Mandanten des Tokens ab")
            x_mandant = p.mandant
        if not x_mandant:
            raise HTTPException(422, "X-Mandant fehlt")
        mandant_binden(s, x_mandant)
        m = s.get(Mandant, x_mandant)
        if m is None:
            raise HTTPException(404, "Mandant unbekannt")
        return m

    def eigene_ressourcen(p: Prinzipal, s: Session, m: Mandant) -> list[str] | None:
        """None = alles sehen. Monteure ohne Büro-/Inhaberrolle sehen nur Einsätze ihrer Ressourcen."""
        if p.art != "person" or p.darf("bearbeiten") or "monteur" not in p.rollen:
            return None
        return list(s.scalars(select(Ressource.id).where(Ressource.mandant_id == m.id, Ressource.benutzer == p.name)))

    def eigener_fall(p: Prinzipal, s: Session, m: Mandant, f: Servicefall) -> None:
        eigene = eigene_ressourcen(p, s, m)
        if eigene is not None and not any(t.ressource_id in eigene for t in f.termine):
            raise HTTPException(404, f"Servicefall {f.id} nicht gefunden")

    def wer(p: Prinzipal, angegeben: str | None) -> str:
        # bei Personen zählt der Name aus dem Token, nicht die Angabe im Request (Revisionssicherheit)
        return p.name if p.art == "person" else (angegeben or p.name)

    def holen(s: Session, modell, id_: str, m: Mandant):
        obj = s.get(modell, id_)
        if obj is None or obj.mandant_id != m.id:
            raise HTTPException(404, f"{modell.__name__} {id_} nicht gefunden")
        return obj

    def fachlich(fn):
        try:
            return fn()
        except domaene.Domaenenfehler as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Büro-Oberfläche (statisch, ohne Build-Schritt); spricht dieselbe API mit X-API-Key und X-Mandant
    app.mount("/app", StaticFiles(directory=UI_VERZEICHNIS, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def startseite() -> RedirectResponse:
        return RedirectResponse("/app/")

    @app.get("/auth/konfiguration")
    def auth_konfiguration() -> dict[str, Any]:
        return auth.konfiguration()

    @app.get("/ich")
    def ich(p: Prinzipal = Depends(prinzipal), s: Session = Depends(sitzung)) -> dict[str, Any]:
        ressourcen = []
        if p.art == "person":
            mandant_binden(s, p.mandant)
            ressourcen = list(s.scalars(select(Ressource.id).where(Ressource.mandant_id == p.mandant, Ressource.benutzer == p.name)))
        return {"art": p.art, "name": p.name, "rollen": sorted(p.rollen), "mandant": p.mandant, "rechte": p.rechte,
                "ressourcen": ressourcen, "eingeschraenkt": p.art == "person" and not p.darf("bearbeiten") and "monteur" in p.rollen}

    # --- Mandanten (mandantenübergreifend, deshalb über die System-Verbindung) ------------------
    @app.get("/mandanten", response_model=list[MandantAus])
    def mandanten(p: Prinzipal = Depends(prinzipal), s: Session = Depends(system_sitzung)):
        abfrage = select(Mandant).order_by(Mandant.name)
        if p.art == "person":
            abfrage = abfrage.where(Mandant.id == p.mandant)
        return s.scalars(abfrage).all()

    @app.post("/mandanten", response_model=MandantAus, status_code=201, dependencies=[Depends(recht("plattform"))])
    def mandant_anlegen(daten: MandantNeu, s: Session = Depends(system_sitzung)):
        m = Mandant(**daten.model_dump())
        s.add(m)
        s.commit()
        return m

    # --- CRM ---------------------------------------------------------------------------------
    @app.post("/kunden", response_model=KundeAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def kunde_anlegen(daten: KundeNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        werte = daten.model_dump()
        werte["telefon"] = telefon_normalisieren(daten.telefon)
        if werte["telefon"] and s.scalar(select(Kunde).where(Kunde.mandant_id == m.id, Kunde.telefon == werte["telefon"])):
            raise HTTPException(409, "Kunde mit dieser Rufnummer existiert bereits (Dublette)")
        k = Kunde(mandant_id=m.id, **werte)
        s.add(k)
        s.flush()
        domaene.protokollieren(s, m.id, "kunde.angelegt", "kunde", k.id, "mensch")
        s.commit()
        return k

    @app.get("/kunden", response_model=list[KundeAus])
    def kunden_suchen(q: str | None = None, telefon: str | None = None, limit: int = Query(50, le=500),
                      m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        abfrage = select(Kunde).where(Kunde.mandant_id == m.id)
        if telefon:
            abfrage = abfrage.where(Kunde.telefon == telefon_normalisieren(telefon))
        if q:
            muster = f"%{q}%"
            abfrage = abfrage.where(or_(Kunde.name.ilike(muster), Kunde.ort.ilike(muster), Kunde.email.ilike(muster)))
        return s.scalars(abfrage.order_by(Kunde.name).limit(limit)).all()

    @app.get("/kunden/{kunde_id}")
    def kunde_lesen(kunde_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        faelle = s.scalars(select(Servicefall).where(Servicefall.kunde_id == k.id).order_by(Servicefall.erstellt.desc())).all()
        return {"kunde": KundeAus.model_validate(k),
                "objekte": [{**ObjektAus.model_validate(o).model_dump(),
                             "anlagen": [AnlageAus.model_validate(a) for a in o.anlagen]} for o in k.objekte],
                "servicefaelle": [ServicefallAus.model_validate(f) for f in faelle]}

    @app.patch("/kunden/{kunde_id}", response_model=KundeAus, dependencies=[Depends(recht("bearbeiten"))])
    def kunde_aendern(kunde_id: str, daten: KundeAenderung, m: Mandant = Depends(mandant_kontext),
                      s: Session = Depends(sitzung)):
        k = holen(s, Kunde, kunde_id, m)
        aenderung = daten.model_dump(exclude_unset=True)
        if "telefon" in aenderung:
            aenderung["telefon"] = telefon_normalisieren(aenderung["telefon"])
        for feld, wert in aenderung.items():
            setattr(k, feld, wert)
        domaene.protokollieren(s, m.id, "kunde.geaendert", "kunde", k.id, "mensch", {"felder": sorted(aenderung)})
        s.commit()
        return k

    @app.get("/kunden/{kunde_id}/auskunft", dependencies=[Depends(recht("bearbeiten"))])
    def kunde_auskunft(kunde_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                       p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        """Datenauskunft nach Art. 15 DSGVO; der Abruf wird protokolliert."""
        k = holen(s, Kunde, kunde_id, m)
        daten = loeschung.auskunft(s, k)
        domaene.protokollieren(s, m.id, "kunde.auskunft", "kunde", k.id, "mensch", {}, p.name)
        s.commit()
        return daten

    @app.post("/kunden/{kunde_id}/loeschen", dependencies=[Depends(recht("verwalten"))])
    def kunde_loeschen(kunde_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                       p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        """Löschantrag nach Art. 17 DSGVO: vollständig oder, bei Aufbewahrungspflichten, Anonymisierung."""
        k = holen(s, Kunde, kunde_id, m)
        ergebnis = fachlich(lambda: loeschung.kunde_loeschen(s, k, app.state.uhr(), "mensch"))
        s.commit()
        return ergebnis

    @app.post("/loeschlauf", dependencies=[Depends(recht("verwalten"))])
    def loeschlauf(trocken: bool = True, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, int]:
        """Löschlauf für diesen Mandanten; trocken=true zeigt nur, was gelöscht würde."""
        return loeschung.loeschlauf(s, m, app.state.uhr(), trocken=trocken)

    @app.post("/kunden/{kunde_id}/objekte", response_model=ObjektAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def objekt_anlegen(kunde_id: str, daten: ObjektNeu, m: Mandant = Depends(mandant_kontext),
                       s: Session = Depends(sitzung)):
        k = holen(s, Kunde, kunde_id, m)
        o = Objekt(mandant_id=m.id, kunde_id=k.id, **daten.model_dump())
        s.add(o)
        s.commit()
        return o

    @app.post("/objekte/{objekt_id}/anlagen", response_model=AnlageAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def anlage_anlegen(objekt_id: str, daten: AnlageNeu, m: Mandant = Depends(mandant_kontext),
                       s: Session = Depends(sitzung)):
        o = holen(s, Objekt, objekt_id, m)
        typ = fachlich(lambda: anlagentyp(daten.anlagentyp))
        werte = daten.model_dump()
        werte["wartungsintervall_monate"] = werte["wartungsintervall_monate"] or typ.wartungsintervall_monate
        a = Anlage(mandant_id=m.id, objekt_id=o.id, **werte)
        s.add(a)
        s.commit()
        return a

    @app.get("/anlagentypen")
    def anlagentypen() -> dict[str, Any]:
        return {k: vars(v) for k, v in ANLAGENTYPEN.items()}

    # --- Ressourcen und Disposition ----------------------------------------------------------
    @app.post("/ressourcen", response_model=RessourceAus, status_code=201, dependencies=[Depends(recht("verwalten"))])
    def ressource_anlegen(daten: RessourceNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        werte = daten.model_dump()
        werte["arbeitsbeginn"] = datetime.strptime(daten.arbeitsbeginn, "%H:%M").time()
        werte["arbeitsende"] = datetime.strptime(daten.arbeitsende, "%H:%M").time()
        r = Ressource(mandant_id=m.id, **werte)
        s.add(r)
        s.commit()
        return r

    @app.get("/ressourcen", response_model=list[RessourceAus])
    def ressourcen(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        return s.scalars(select(Ressource).where(Ressource.mandant_id == m.id).order_by(Ressource.name)).all()

    @app.patch("/ressourcen/{ressource_id}", response_model=RessourceAus, dependencies=[Depends(recht("verwalten"))])
    def ressource_aendern(ressource_id: str, daten: RessourceAenderung, m: Mandant = Depends(mandant_kontext),
                          s: Session = Depends(sitzung)):
        r = holen(s, Ressource, ressource_id, m)
        for feld, wert in daten.model_dump(exclude_unset=True).items():
            if feld in ("arbeitsbeginn", "arbeitsende"):
                wert = datetime.strptime(wert, "%H:%M").time()
            setattr(r, feld, wert)
        s.commit()
        return r

    @app.get("/disposition/zeitfenster")
    def zeitfenster(dauer_minuten: int = 90, qualifikation: str | None = None, ab: datetime | None = None,
                    tage: int = Query(5, le=30), anzahl: int = Query(5, le=20),
                    m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        fenster = freie_zeitfenster(s, m, ab or app.state.uhr(), tage, dauer_minuten, qualifikation, anzahl)
        return [vars(z) for z in fenster]

    @app.get("/termine", response_model=list[TerminAus])
    def termine(von: datetime | None = None, bis: datetime | None = None, ressource_id: str | None = None,
                m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)):
        abfrage = select(Termin).where(Termin.mandant_id == m.id)
        eigene = eigene_ressourcen(p, s, m)
        if eigene is not None:
            abfrage = abfrage.where(Termin.ressource_id.in_(eigene))
        if von:
            abfrage = abfrage.where(Termin.ende >= von)
        if bis:
            abfrage = abfrage.where(Termin.beginn <= bis)
        if ressource_id:
            abfrage = abfrage.where(Termin.ressource_id == ressource_id)
        return s.scalars(abfrage.order_by(Termin.beginn)).all()

    # --- Servicefälle ------------------------------------------------------------------------
    def servicefall_erzeugen(s: Session, m: Mandant, daten: ServicefallNeu, akteur: str) -> Servicefall:
        holen(s, Kunde, daten.kunde_id, m)
        anlage = holen(s, Anlage, daten.anlage_id, m) if daten.anlage_id else None
        bewertung = dringlichkeit_bewerten(f"{daten.anliegen} {daten.symptom or ''}", app.state.uhr().month,
                                           daten.dringlichkeit)
        dringlichkeit = daten.dringlichkeit if daten.dringlichkeit == "notdienst" else bewertung.dringlichkeit
        f = Servicefall(mandant_id=m.id, kunde_id=daten.kunde_id, anlage_id=daten.anlage_id, anliegen=daten.anliegen,
                        symptom=daten.symptom, dringlichkeit=dringlichkeit, quelle=daten.quelle,
                        benoetigte_qualifikation=daten.benoetigte_qualifikation
                        or qualifikation_fuer(anlage.anlagentyp if anlage else None),
                        dauer_minuten=daten.dauer_minuten)
        s.add(f)
        s.flush()
        domaene.protokollieren(s, m.id, "servicefall.eingegangen", "servicefall", f.id, akteur,
                               {"quelle": f.quelle, "dringlichkeit": f.dringlichkeit, "regel": bewertung.grund})
        return f

    @app.post("/servicefaelle", response_model=ServicefallAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def servicefall_anlegen(daten: ServicefallNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        f = servicefall_erzeugen(s, m, daten, "mensch")
        s.commit()
        return f

    @app.get("/servicefaelle", response_model=list[ServicefallAus])
    def servicefaelle(status: str | None = None, dringlichkeit: str | None = None, limit: int = Query(100, le=1000),
                      m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)):
        abfrage = select(Servicefall).where(Servicefall.mandant_id == m.id)
        eigene = eigene_ressourcen(p, s, m)
        if eigene is not None:
            abfrage = abfrage.where(Servicefall.id.in_(select(Termin.servicefall_id).where(Termin.ressource_id.in_(eigene))))
        if status:
            abfrage = abfrage.where(Servicefall.status == status)
        if dringlichkeit:
            abfrage = abfrage.where(Servicefall.dringlichkeit == dringlichkeit)
        return s.scalars(abfrage.order_by(Servicefall.erstellt.desc()).limit(limit)).all()

    @app.get("/servicefaelle/{fall_id}")
    def servicefall_lesen(fall_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                          p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        f = holen(s, Servicefall, fall_id, m)
        eigener_fall(p, s, m, f)
        ereignisse = s.scalars(select(Ereignis).where(Ereignis.entitaet_id == f.id).order_by(Ereignis.id)).all()
        return {"servicefall": ServicefallAus.model_validate(f),
                "termine": [TerminAus.model_validate(t) for t in f.termine],
                "ereignisse": [{"typ": e.typ, "akteur": e.akteur, "zeit": e.zeit, "payload": e.payload} for e in ereignisse]}

    @app.post("/servicefaelle/{fall_id}/status", response_model=ServicefallAus)
    def status_wechseln(fall_id: str, daten: StatusWechsel, m: Mandant = Depends(mandant_kontext),
                        s: Session = Depends(sitzung), p: Prinzipal = Depends(recht("status"))):
        if daten.ziel != "durchgefuehrt" and not p.darf("bearbeiten"):
            raise HTTPException(403, "Monteure melden nur „durchgeführt“; andere Statuswechsel macht das Büro")
        f = holen(s, Servicefall, fall_id, m)
        eigener_fall(p, s, m, f)
        fachlich(lambda: domaene.uebergang(s, f, daten.ziel, "mensch", daten.grund, wer(p, daten.wer)))
        s.commit()
        return f

    def termin_vorschlagen(s: Session, m: Mandant, f: Servicefall, daten: TerminNeu, akteur: str) -> tuple[Termin, Freigabe]:
        r = holen(s, Ressource, daten.ressource_id, m)
        if f.benoetigte_qualifikation and f.benoetigte_qualifikation not in (r.qualifikationen or []):
            raise HTTPException(409, f"{r.name} hat die Qualifikation {f.benoetigte_qualifikation} nicht")
        ende = daten.ende or daten.beginn + timedelta(minutes=f.dauer_minuten)
        if kollidiert(s, m, r.id, daten.beginn, ende):
            raise HTTPException(409, "Zeitfenster ist inzwischen belegt")
        if f.status == "eingegangen":
            domaene.uebergang(s, f, "qualifiziert", akteur, "Termin vorgeschlagen")
        t = Termin(mandant_id=m.id, servicefall_id=f.id, ressource_id=r.id, beginn=daten.beginn, ende=ende)
        s.add(t)
        s.flush()
        domaene.protokollieren(s, m.id, "termin.vorgeschlagen", "termin", t.id, akteur, {"servicefall_id": f.id})
        freigabe = domaene.aktion_beantragen(s, m, "termin_bestaetigen", {"termin_id": t.id}, akteur, f.id,
                                             f"{r.name}, {daten.beginn.isoformat()}")
        return t, freigabe

    @app.post("/servicefaelle/{fall_id}/termine", status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def termin_anlegen(fall_id: str, daten: TerminNeu, m: Mandant = Depends(mandant_kontext),
                       s: Session = Depends(sitzung)) -> dict[str, Any]:
        f = holen(s, Servicefall, fall_id, m)
        t, freigabe = fachlich(lambda: termin_vorschlagen(s, m, f, daten, "mensch"))
        s.commit()
        return {"termin": TerminAus.model_validate(t), "freigabe": FreigabeAus.model_validate(freigabe)}

    # --- Freigabe-Inbox, Outbox, Event-Log ---------------------------------------------------
    @app.get("/freigaben", response_model=list[FreigabeAus])
    def freigaben(status: str = "offen", m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        return s.scalars(select(Freigabe).where(Freigabe.mandant_id == m.id, Freigabe.status == status)
                         .order_by(Freigabe.erstellt)).all()

    @app.post("/freigaben", response_model=FreigabeAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def aktion_beantragen(daten: AktionNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        f = fachlich(lambda: domaene.aktion_beantragen(s, m, daten.aktion, daten.payload, "mensch",
                                                       daten.servicefall_id, daten.begruendung))
        s.commit()
        return f

    @app.post("/freigaben/{freigabe_id}/entscheidung", response_model=FreigabeAus)
    def entscheiden(freigabe_id: str, daten: Entscheidung, m: Mandant = Depends(mandant_kontext),
                    s: Session = Depends(sitzung), p: Prinzipal = Depends(recht("freigeben"))):
        f = holen(s, Freigabe, freigabe_id, m)
        if f.risikoklasse == "C" and not p.darf("freigeben_c"):
            raise HTTPException(403, "Finanzwirksame Aktionen (Klasse C) entscheidet nur die Inhaberrolle")
        fachlich(lambda: domaene.entscheiden(s, f, daten.freigeben, wer(p, daten.wer)))
        s.commit()
        return f

    @app.get("/outbox")
    def outbox(status: str = "ausstehend", m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        eintraege = s.scalars(select(OutboxEintrag).where(OutboxEintrag.mandant_id == m.id, OutboxEintrag.status == status)
                              .order_by(OutboxEintrag.id)).all()
        return [{"id": e.id, "ziel": e.ziel, "aktion": e.aktion, "payload": e.payload, "versuche": e.versuche,
                 "letzter_fehler": e.letzter_fehler} for e in eintraege]

    @app.get("/ereignisse")
    def ereignisse(entitaet_id: str | None = None, limit: int = Query(100, le=1000),
                   m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        abfrage = select(Ereignis).where(Ereignis.mandant_id == m.id)
        if entitaet_id:
            abfrage = abfrage.where(Ereignis.entitaet_id == entitaet_id)
        return [{"id": e.id, "typ": e.typ, "entitaet": e.entitaet, "entitaet_id": e.entitaet_id, "akteur": e.akteur,
                 "akteur_name": e.akteur_name, "payload": e.payload, "zeit": e.zeit}
                for e in s.scalars(abfrage.order_by(Ereignis.id.desc()).limit(limit))]

    # --- Digital Twin und Ontologie ----------------------------------------------------------
    @app.get("/twin/betrieb")
    def betriebs_zwilling(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        return twin.betriebs_zwilling(s, m, app.state.uhr())

    @app.get("/twin/anlagen/{anlage_id}")
    def anlagen_zwilling(anlage_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        z = twin.anlagen_zwilling(s, m.id, anlage_id, app.state.uhr().date())
        if z is None:
            raise HTTPException(404, "Anlage nicht gefunden")
        return z

    @app.get("/ontologie/tbox")
    def ontologie_tbox() -> Response:
        return Response(TBOX.read_text(encoding="utf-8"), media_type="text/turtle")

    @app.get("/ontologie/export")
    def ontologie_export(mit_tbox: bool = False, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        return Response(exportieren(s, m, mit_tbox).serialize(format="turtle"), media_type="text/turtle")

    @app.post("/ontologie/sparql")
    def ontologie_sparql(daten: SparqlAbfrage, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        try:
            return management.sparql(s, m, daten.abfrage)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        except Exception as e:  # noqa: BLE001  Syntaxfehler der Abfrage gehören zum Nutzer, nicht zum Server
            raise HTTPException(422, f"SPARQL-Fehler: {e}") from e

    # --- Management ---------------------------------------------------------------------------
    @app.post("/wartung/planen", dependencies=[Depends(recht("bearbeiten"))])
    def wartung_planen(vorlauf_tage: int = Query(60, ge=0, le=365), kunden_anschreiben: bool = True,
                       m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        neu = management.wartung_planen(s, m, app.state.uhr().date(), vorlauf_tage, kunden_anschreiben)
        s.commit()
        return {"angelegt": len(neu), "servicefaelle": neu}

    @app.get("/kennzahlen")
    def kennzahlen(tage: int = Query(30, ge=1, le=365), m: Mandant = Depends(mandant_kontext),
                   s: Session = Depends(sitzung)) -> dict[str, Any]:
        return management.kennzahlen(s, m, app.state.uhr(), tage)

    @app.get("/plantafel")
    def plantafel(tag: date | None = None, tage: int = Query(5, ge=1, le=14), m: Mandant = Depends(mandant_kontext),
                  s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        return management.plantafel(s, m, tag or app.state.uhr().date(), tage, eigene_ressourcen(p, s, m))

    # --- Betrieb: Stammdaten, Anbindungen, Rechnungen, Kanzlei ---------------------------------
    @app.get("/betrieb")
    def betrieb_lesen(m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        return {"id": m.id, "name": m.name, "gewerk": m.gewerk, "policies": m.policies, "stammdaten": m.stammdaten,
                "rechnungsnummer_zaehler": m.rechnungsnummer_zaehler}

    @app.put("/betrieb/stammdaten", dependencies=[Depends(recht("verwalten"))])
    def stammdaten_setzen(daten: Stammdaten, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        m.stammdaten = {**m.stammdaten, **daten.model_dump(exclude_none=True)}
        s.commit()
        return m.stammdaten

    @app.get("/integrationen", dependencies=[Depends(recht("verwalten"))])
    def integrationen(m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        return {"ziele": {z: {"erlaubt": sorted(ERLAUBT[z]), **{k: (konfiguration_maskieren(v) if isinstance(v, dict) else v)
                                                              for k, v in (m.integrationen.get(z) or {}).items()}} for z in ZIELE}}

    @app.put("/integrationen/{ziel}", dependencies=[Depends(recht("verwalten"))])
    def integration_setzen(ziel: str, daten: Integration, m: Mandant = Depends(mandant_kontext),
                           s: Session = Depends(sitzung)) -> dict[str, Any]:
        if ziel not in ZIELE:
            raise HTTPException(404, "Unbekanntes Ziel")
        bisher = m.integrationen.get(ziel) or {}
        neu: dict[str, Any] = {"kanaele": daten.kanaele}
        for typ in ("email", "sms", "webhook", "lexware", "sevdesk", "belegpaket"):
            konfig = dict(getattr(daten, typ))
            for feld in GEHEIME_FELDER:  # maskierte oder leere Geheimnisse behalten den gespeicherten Wert
                if konfig.get(feld) in ("", "••••", None) and (bisher.get(typ) or {}).get(feld):
                    konfig[feld] = bisher[typ][feld]
            if konfig:
                neu[typ] = konfiguration_verschluesseln(konfig)
        if ziel == "kanzlei" and "email" in daten.kanaele and not neu.get("email", {}).get("kanzlei_email"):
            neu.setdefault("email", {})["kanzlei_email"] = m.stammdaten.get("kanzlei_email", "")
        fachlich(lambda: konfiguration_pruefen(ziel, neu))
        m.integrationen = {**m.integrationen, ziel: neu}
        s.commit()
        return {"ziel": ziel, "kanaele": neu["kanaele"]}

    @app.get("/rechnungen", response_model=list[RechnungAus], dependencies=[Depends(recht("finanzen"))])
    def rechnungen(status: str | None = None, limit: int = Query(200, le=2000), m: Mandant = Depends(mandant_kontext),
                   s: Session = Depends(sitzung)):
        abfrage = select(Rechnung).where(Rechnung.mandant_id == m.id)
        if status:
            abfrage = abfrage.where(Rechnung.status == status)
        return s.scalars(abfrage.order_by(Rechnung.datum.desc(), Rechnung.nummer.desc()).limit(limit)).all()

    @app.get("/rechnungen/{rechnung_id}", response_model=RechnungAus, dependencies=[Depends(recht("finanzen"))])
    def rechnung_lesen(rechnung_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        return holen(s, Rechnung, rechnung_id, m)

    @app.get("/rechnungen/{rechnung_id}/pdf", dependencies=[Depends(recht("finanzen"))])
    def rechnung_pdf_laden(rechnung_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        r = holen(s, Rechnung, rechnung_id, m)
        return Response(rechnung_pdf(r, m), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{r.nummer}.pdf"'})

    @app.get("/rechnungen/{rechnung_id}/xrechnung", dependencies=[Depends(recht("finanzen"))])
    def rechnung_xml(rechnung_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        r = holen(s, Rechnung, rechnung_id, m)
        return Response(xrechnung(r, m), media_type="application/xml",
                        headers={"Content-Disposition": f'attachment; filename="{r.nummer}.xml"'})

    @app.post("/rechnungen/{rechnung_id}/storno", response_model=FreigabeAus, status_code=201)
    def storno_beantragen(rechnung_id: str, daten: StornoAntrag, m: Mandant = Depends(mandant_kontext),
                          s: Session = Depends(sitzung), p: Prinzipal = Depends(recht("bearbeiten"))):
        r = holen(s, Rechnung, rechnung_id, m)
        if r.art != "rechnung" or r.status == "storniert":
            raise HTTPException(409, "Nur gestellte Rechnungen können storniert werden")
        f = fachlich(lambda: domaene.aktion_beantragen(s, m, "storno", {"rechnung_id": r.id, "grund": daten.grund}, "mensch",
                                                       r.servicefall_id, f"Storno {r.nummer}: {daten.grund}"))
        s.commit()
        return f

    @app.post("/kanzlei/belegpaket", response_model=FreigabeAus, status_code=201)
    def belegpaket_beantragen(daten: BelegpaketAntrag, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                              p: Prinzipal = Depends(recht("bearbeiten"))):
        f = fachlich(lambda: domaene.aktion_beantragen(s, m, "belegversand_kanzlei", {"zeitraum": daten.zeitraum}, "mensch", None,
                                                       f"Belegpaket {daten.zeitraum} an die Kanzlei"))
        s.commit()
        return f

    # --- Einsatz: Anhänge, Zeiten, Rapport, Kalkulation ---------------------------------------
    def eigene_ressource(p: Prinzipal, s: Session, m: Mandant, angegeben: str | None) -> Ressource:
        """Monteure arbeiten auf ihrer eigenen Ressource; Büro und Maschinen geben die Ressource an."""
        eigene = eigene_ressourcen(p, s, m)
        if eigene is not None:
            if not eigene:
                raise HTTPException(409, "Ihrem Konto ist keine Monteur-Ressource zugeordnet")
            if angegeben and angegeben not in eigene:
                raise HTTPException(403, "Monteure buchen nur auf die eigene Ressource")
            return holen(s, Ressource, angegeben or eigene[0], m)
        if not angegeben:
            raise HTTPException(422, "ressource_id fehlt")
        return holen(s, Ressource, angegeben, m)

    BEZUEGE = {"servicefall": Servicefall, "kunde": Kunde, "objekt": Objekt, "anlage": Anlage, "beleg": Beleg, "rapport": Rapport}

    def bezug_pruefen(s: Session, m: Mandant, p: Prinzipal, bezug_typ: str, bezug_id: str):
        if bezug_typ == "betrieb":
            if bezug_id != m.id:
                raise HTTPException(404, "Betrieb nicht gefunden")
            return
        if bezug_typ not in BEZUEGE:
            raise HTTPException(422, f"bezug_typ {bezug_typ!r} unbekannt")
        obj = holen(s, BEZUEGE[bezug_typ], bezug_id, m)
        if bezug_typ == "servicefall":
            eigener_fall(p, s, m, obj)

    @app.post("/anhaenge", response_model=AnhangAus, status_code=201)
    async def anhang_hochladen(datei: UploadFile = File(...), bezug_typ: str = Form(...), bezug_id: str = Form(...),
                               art: Literal["foto", "dokument", "beleg", "unterschrift"] = Form("dokument"),
                               notiz: str | None = Form(None), m: Mandant = Depends(mandant_kontext),
                               s: Session = Depends(sitzung), p: Prinzipal = Depends(recht("einsatz"))):
        bezug_pruefen(s, m, p, bezug_typ, bezug_id)
        inhalt = await datei.read()
        a = fachlich(lambda: einsatz.anhang_anlegen(s, m, bezug_typ, bezug_id, art, datei.filename or "datei",
                                                    datei.content_type or "application/octet-stream", inhalt, p.name, notiz))
        s.commit()
        return a

    @app.get("/anhaenge", response_model=list[AnhangAus])
    def anhaenge_listen(bezug_typ: str, bezug_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                        p: Prinzipal = Depends(prinzipal)):
        bezug_pruefen(s, m, p, bezug_typ, bezug_id)
        return einsatz.anhaenge(s, m, bezug_typ, bezug_id)

    @app.get("/anhaenge/{anhang_id}/inhalt")
    def anhang_inhalt(anhang_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                      p: Prinzipal = Depends(prinzipal)) -> Response:
        a = holen(s, Anhang, anhang_id, m)
        bezug_pruefen(s, m, p, a.bezug_typ, a.bezug_id)
        return Response(a.inhalt, media_type=a.mime, headers={"Content-Disposition": f'inline; filename="{a.name}"',
                                                             "Cache-Control": "private, max-age=3600"})

    @app.delete("/anhaenge/{anhang_id}", status_code=204, dependencies=[Depends(recht("bearbeiten"))])
    def anhang_loeschen(anhang_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        a = holen(s, Anhang, anhang_id, m)
        if a.art == "unterschrift":
            raise HTTPException(409, "Unterschriften sind Teil des Rapports und werden nicht einzeln gelöscht")
        s.delete(a)
        s.commit()
        return Response(status_code=204)

    def zeit_aus(z: Zeiteintrag) -> dict[str, Any]:
        return {**ZeitAus.model_validate(z).model_dump(), "minuten": einsatz.minuten(z, app.state.uhr())}

    @app.post("/zeiten/start", status_code=201)
    def zeit_starten(daten: ZeitStart, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                     p: Prinzipal = Depends(recht("einsatz"))) -> dict[str, Any]:
        r = eigene_ressource(p, s, m, daten.ressource_id)
        if daten.servicefall_id:
            eigener_fall(p, s, m, holen(s, Servicefall, daten.servicefall_id, m))
        z = fachlich(lambda: einsatz.zeit_starten(s, m, r, daten.art, app.state.uhr(), daten.servicefall_id, daten.notiz, p.name))
        s.commit()
        return zeit_aus(z)

    @app.post("/zeiten/{zeit_id}/stop")
    def zeit_beenden(zeit_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                     p: Prinzipal = Depends(recht("einsatz"))) -> dict[str, Any]:
        z = holen(s, Zeiteintrag, zeit_id, m)
        eigene_ressource(p, s, m, z.ressource_id)
        fachlich(lambda: einsatz.zeit_beenden(s, z, app.state.uhr()))
        s.commit()
        return zeit_aus(z)

    @app.post("/zeiten", status_code=201)
    def zeit_nachtragen(daten: ZeitNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                        p: Prinzipal = Depends(recht("einsatz"))) -> dict[str, Any]:
        r = eigene_ressource(p, s, m, daten.ressource_id)
        if daten.ende <= daten.beginn:
            raise HTTPException(422, "Ende liegt vor dem Beginn")
        if daten.servicefall_id:
            holen(s, Servicefall, daten.servicefall_id, m)
        z = Zeiteintrag(mandant_id=m.id, ressource_id=r.id, servicefall_id=daten.servicefall_id, art=daten.art, beginn=daten.beginn,
                        ende=daten.ende, notiz=daten.notiz, quelle="manuell")
        s.add(z)
        s.flush()
        domaene.protokollieren(s, m.id, "zeit.nachgetragen", "zeiteintrag", z.id, "mensch", {"art": z.art, "ressource_id": r.id}, p.name)
        s.commit()
        return zeit_aus(z)

    @app.get("/zeiten")
    def zeiten(von: datetime | None = None, bis: datetime | None = None, ressource_id: str | None = None,
               servicefall_id: str | None = None, laufend: bool = False, m: Mandant = Depends(mandant_kontext),
               s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> list[dict[str, Any]]:
        abfrage = select(Zeiteintrag).where(Zeiteintrag.mandant_id == m.id)
        eigene = eigene_ressourcen(p, s, m)
        if eigene is not None:
            abfrage = abfrage.where(Zeiteintrag.ressource_id.in_(eigene))
        if ressource_id:
            abfrage = abfrage.where(Zeiteintrag.ressource_id == ressource_id)
        if servicefall_id:
            abfrage = abfrage.where(Zeiteintrag.servicefall_id == servicefall_id)
        if laufend:
            abfrage = abfrage.where(Zeiteintrag.ende.is_(None))
        if von:
            abfrage = abfrage.where(Zeiteintrag.beginn >= von)
        if bis:
            abfrage = abfrage.where(Zeiteintrag.beginn < bis)
        return [zeit_aus(z) for z in s.scalars(abfrage.order_by(Zeiteintrag.beginn.desc()).limit(500))]

    @app.get("/zeiten/lohnexport", dependencies=[Depends(recht("verwalten"))])
    def lohnexport(monat: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), m: Mandant = Depends(mandant_kontext),
                   s: Session = Depends(sitzung)) -> Response:
        jahr, mon = (int(x) for x in monat.split("-"))
        return Response(einsatz.lohnexport(s, m, jahr, mon), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="lohnvorbereitung_{monat}.csv"'})

    @app.get("/einsatz/heute")
    def einsatz_heute(ressource_id: str | None = None, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                      p: Prinzipal = Depends(recht("einsatz"))) -> dict[str, Any]:
        """Tagesansicht des Monteurs: Termine, laufende Zeit, Bereitschaft."""
        eigene = eigene_ressourcen(p, s, m)
        if eigene is not None and not eigene:
            return {"ressource": None, "termine": [], "laufend": None, "bereitschaft": None}
        r = eigene_ressource(p, s, m, ressource_id) if (ressource_id or eigene is not None) else None
        laufend = einsatz.laufender_eintrag(s, r.id) if r else None
        b = einsatz.bereitschaft_aktuell(s, m, app.state.uhr())
        return {"ressource": RessourceAus.model_validate(r).model_dump() if r else None,
                "termine": einsatz.einsatz_heute(s, m, r, app.state.uhr()) if r else [],
                "laufend": zeit_aus(laufend) if laufend else None,
                "bereitschaft": ({"name": b.ressource.name.split(" (")[0], "telefon": b.telefon, "bis": b.bis} if b else None)}

    @app.post("/servicefaelle/{fall_id}/rapport", response_model=RapportAus, status_code=201)
    def rapport_anlegen(fall_id: str, daten: RapportNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                        p: Prinzipal = Depends(recht("einsatz"))):
        f = holen(s, Servicefall, fall_id, m)
        eigener_fall(p, s, m, f)
        r = eigene_ressource(p, s, m, daten.ressource_id or (f.termine[-1].ressource_id if f.termine and eigene_ressourcen(p, s, m) is None else None))
        if daten.termin_id:
            holen(s, Termin, daten.termin_id, m)
        unterschrift = None
        if daten.unterschrift:
            kopf, _, b64 = daten.unterschrift.partition(",")
            if not kopf.startswith("data:image/png"):
                raise HTTPException(422, "Unterschrift muss ein PNG (data:image/png;base64,…) sein")
            try:
                unterschrift = base64.b64decode(b64, validate=True)
            except ValueError as e:
                raise HTTPException(422, "Unterschrift ist kein gültiges Base64") from e
            if len(unterschrift) > 500_000:
                raise HTTPException(422, "Unterschrift zu groß")
        rapport = fachlich(lambda: einsatz.rapport_erzeugen(s, m, f, r, daten.model_dump(exclude={"unterschrift", "ressource_id"}),
                                                            app.state.uhr(), unterschrift, p.name))
        s.commit()
        return rapport

    @app.get("/servicefaelle/{fall_id}/rapporte", response_model=list[RapportAus])
    def rapporte(fall_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)):
        f = holen(s, Servicefall, fall_id, m)
        eigener_fall(p, s, m, f)
        return s.scalars(select(Rapport).where(Rapport.servicefall_id == f.id).order_by(Rapport.erstellt)).all()

    @app.get("/rapporte/{rapport_id}/pdf")
    def rapport_pdf(rapport_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> Response:
        r = holen(s, Rapport, rapport_id, m)
        eigener_fall(p, s, m, r.servicefall)
        return Response(einsatz.rapport_pdf(s, r, m), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{r.nummer}.pdf"'})

    @app.get("/servicefaelle/{fall_id}/kalkulation", dependencies=[Depends(recht("finanzen"))])
    def fall_kalkulation(fall_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return einsatz.kalkulation(s, m, holen(s, Servicefall, fall_id, m))

    # --- Belegeingang -------------------------------------------------------------------------
    @app.post("/belege", response_model=BelegAus, status_code=201)
    async def beleg_erfassen(datei: UploadFile | None = File(None), lieferant: str | None = Form(None), nummer: str | None = Form(None),
                             datum: date | None = Form(None), brutto: str | None = Form(None), netto: str | None = Form(None),
                             ust_satz: str | None = Form(None), kategorie: str = Form("material"), servicefall_id: str | None = Form(None),
                             notiz: str | None = Form(None), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                             p: Prinzipal = Depends(recht("einsatz"))):
        """Beleg aus Datei (XRechnung, ZUGFeRD-PDF, PDF, Foto) oder von Hand; Felder aus dem Formular überschreiben die Erkennung."""
        heute = app.state.uhr().date()
        felder: dict[str, Any]
        anhang = None
        if datei is not None:
            inhalt = await datei.read()
            erkennung = fachlich(lambda: belege.erkennen(inhalt, datei.filename or "beleg", datei.content_type or ""))
            felder = belege.felder_fuer_beleg(erkennung, heute)
        else:
            if not lieferant or not (brutto or netto):
                raise HTTPException(422, "Ohne Datei sind lieferant und brutto (oder netto) Pflicht")
            felder = belege.felder_fuer_beleg(belege.Erkennung(quelle="manuell", konfidenz=1.0, hinweise=[]), heute)
        from decimal import Decimal, InvalidOperation
        try:
            if lieferant:
                felder["lieferant"] = lieferant[:200]
            if nummer:
                felder["nummer"] = nummer[:80]
            if datum:
                felder["datum"] = datum
            if ust_satz:
                felder["ust_satz"] = ust_satz
            if brutto:
                b = Decimal(brutto.replace(",", ".")).quantize(Decimal("0.01"))
                n = Decimal(netto.replace(",", ".")).quantize(Decimal("0.01")) if netto else (b / (1 + Decimal(felder["ust_satz"]) / 100)).quantize(Decimal("0.01"))
                felder.update(brutto=f"{b:.2f}", netto=f"{n:.2f}", umsatzsteuer=f"{b - n:.2f}")
            elif netto:
                n = Decimal(netto.replace(",", ".")).quantize(Decimal("0.01"))
                b = (n * (1 + Decimal(felder["ust_satz"]) / 100)).quantize(Decimal("0.01"))
                felder.update(brutto=f"{b:.2f}", netto=f"{n:.2f}", umsatzsteuer=f"{b - n:.2f}")
        except InvalidOperation as e:
            raise HTTPException(422, "Betrag ist keine Zahl") from e
        if kategorie not in ("material", "fremdleistung", "fahrzeug", "sonstiges"):
            raise HTTPException(422, "Kategorie unbekannt")
        if servicefall_id:
            eigener_fall(p, s, m, holen(s, Servicefall, servicefall_id, m))
        b_ = Beleg(mandant_id=m.id, kategorie=kategorie, servicefall_id=servicefall_id, notiz=notiz, erstellt=app.state.uhr(), **felder)
        s.add(b_)
        s.flush()
        if datei is not None:
            anhang = fachlich(lambda: einsatz.anhang_anlegen(s, m, "beleg", b_.id, "beleg", datei.filename or "beleg",
                                                             datei.content_type or "application/octet-stream", inhalt, p.name))
            b_.anhang_id = anhang.id
        domaene.protokollieren(s, m.id, "beleg.erfasst", "beleg", b_.id, "mensch",
                               {"quelle": b_.quelle, "lieferant": b_.lieferant, "brutto": b_.brutto, "konfidenz": b_.erkennung.get("konfidenz")}, p.name)
        s.commit()
        return b_

    @app.get("/belege", response_model=list[BelegAus], dependencies=[Depends(recht("finanzen"))])
    def belege_listen(status: str | None = None, servicefall_id: str | None = None, monat: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
                      limit: int = Query(200, le=2000), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        abfrage = select(Beleg).where(Beleg.mandant_id == m.id)
        if status:
            abfrage = abfrage.where(Beleg.status == status)
        if servicefall_id:
            abfrage = abfrage.where(Beleg.servicefall_id == servicefall_id)
        if monat:
            jahr, mon = (int(x) for x in monat.split("-"))
            abfrage = abfrage.where(Beleg.datum >= date(jahr, mon, 1), Beleg.datum < date(jahr + (mon == 12), mon % 12 + 1, 1))
        return s.scalars(abfrage.order_by(Beleg.datum.desc(), Beleg.erstellt.desc()).limit(limit)).all()

    @app.get("/belege/{beleg_id}", response_model=BelegAus, dependencies=[Depends(recht("finanzen"))])
    def beleg_lesen(beleg_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        return holen(s, Beleg, beleg_id, m)

    @app.patch("/belege/{beleg_id}", response_model=BelegAus)
    def beleg_aendern(beleg_id: str, daten: BelegAenderung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                      p: Prinzipal = Depends(recht("bearbeiten"))):
        """Prüfen und korrigieren; Status geprueft bedeutet: geht ins nächste Belegpaket. Übergebene Belege sind unveränderlich."""
        b = holen(s, Beleg, beleg_id, m)
        if b.status == "uebergeben" and daten.status != "bezahlt":
            raise HTTPException(409, "Beleg wurde bereits an die Kanzlei übergeben")
        aenderung = daten.model_dump(exclude_unset=True)
        if "servicefall_id" in aenderung and aenderung["servicefall_id"]:
            holen(s, Servicefall, aenderung["servicefall_id"], m)
        from decimal import Decimal, InvalidOperation
        try:
            for feld in ("netto", "umsatzsteuer", "brutto", "skonto_prozent"):
                if aenderung.get(feld) is not None:
                    aenderung[feld] = f"{Decimal(str(aenderung[feld]).replace(',', '.')):.2f}"
        except InvalidOperation as e:
            raise HTTPException(422, "Betrag ist keine Zahl") from e
        for feld, wert in aenderung.items():
            setattr(b, feld, wert)
        if aenderung.get("status") == "geprueft":
            b.geprueft_von = p.name
        domaene.protokollieren(s, m.id, "beleg.geaendert", "beleg", b.id, "mensch", {"felder": sorted(aenderung)}, p.name)
        s.commit()
        return b

    @app.delete("/belege/{beleg_id}", status_code=204, dependencies=[Depends(recht("bearbeiten"))])
    def beleg_loeschen(beleg_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        b = holen(s, Beleg, beleg_id, m)
        if b.status in ("uebergeben", "bezahlt"):
            raise HTTPException(409, "Übergebene Belege unterliegen der Aufbewahrungspflicht")
        if b.anhang_id:
            s.execute(select(Anhang).where(Anhang.id == b.anhang_id))
            a = s.get(Anhang, b.anhang_id)
            b.anhang_id = None
            s.flush()
            if a:
                s.delete(a)
        s.delete(b)
        s.commit()
        return Response(status_code=204)

    # --- Bereitschaft und Fristen -------------------------------------------------------------
    @app.get("/bereitschaft", response_model=list[BereitschaftAus])
    def bereitschaften(ab: datetime | None = None, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        ab = ab or app.state.uhr() - timedelta(days=7)
        return s.scalars(select(Bereitschaft).where(Bereitschaft.mandant_id == m.id, Bereitschaft.bis >= ab).order_by(Bereitschaft.von)).all()

    @app.get("/bereitschaft/aktuell")
    def bereitschaft_aktuell(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any] | None:
        b = einsatz.bereitschaft_aktuell(s, m, app.state.uhr())
        return {"id": b.id, "ressource_id": b.ressource_id, "name": b.ressource.name.split(" (")[0], "telefon": b.telefon, "bis": b.bis} if b else None

    @app.post("/bereitschaft", response_model=BereitschaftAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def bereitschaft_anlegen(daten: BereitschaftNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        r = holen(s, Ressource, daten.ressource_id, m)
        b = fachlich(lambda: einsatz.bereitschaft_anlegen(s, m, r, daten.von, daten.bis, telefon_normalisieren(daten.telefon) or daten.telefon, daten.notiz))
        s.commit()
        return b

    @app.delete("/bereitschaft/{bereitschaft_id}", status_code=204, dependencies=[Depends(recht("bearbeiten"))])
    def bereitschaft_loeschen(bereitschaft_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        s.delete(holen(s, Bereitschaft, bereitschaft_id, m))
        s.commit()
        return Response(status_code=204)

    @app.get("/fristen", response_model=list[FristAus])
    def fristen(offen: bool = True, vorlauf_tage: int = Query(365, ge=0, le=3650), m: Mandant = Depends(mandant_kontext),
                s: Session = Depends(sitzung)):
        if offen:
            return einsatz.fristen_faellig(s, m, app.state.uhr().date(), vorlauf_tage)
        return s.scalars(select(Frist).where(Frist.mandant_id == m.id).order_by(Frist.faellig.desc()).limit(500)).all()

    @app.post("/fristen", response_model=FristAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def frist_anlegen(daten: FristNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        if daten.bezug_typ in ("ressource", "anlage") and daten.bezug_id:
            holen(s, Ressource if daten.bezug_typ == "ressource" else Anlage, daten.bezug_id, m)
        f = Frist(mandant_id=m.id, **daten.model_dump())
        s.add(f)
        s.commit()
        return f

    @app.post("/fristen/{frist_id}/erledigt", dependencies=[Depends(recht("einsatz"))])
    def frist_erledigen(frist_id: str, daten: FristErledigt, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                        p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        f = holen(s, Frist, frist_id, m)
        folge = fachlich(lambda: einsatz.frist_erledigen(s, f, daten.am or app.state.uhr().date()))
        domaene.protokollieren(s, m.id, "frist.erledigt", "frist", f.id, "mensch", {"bezeichnung": f.bezeichnung, "am": f.erledigt_am.isoformat()}, p.name)
        s.commit()
        return {"frist": FristAus.model_validate(f).model_dump(), "folgefrist": FristAus.model_validate(folge).model_dump() if folge else None}

    @app.delete("/fristen/{frist_id}", status_code=204, dependencies=[Depends(recht("bearbeiten"))])
    def frist_loeschen(frist_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        s.delete(holen(s, Frist, frist_id, m))
        s.commit()
        return Response(status_code=204)

    # --- Tool-API für den Voice-Agenten ------------------------------------------------------
    @app.post("/voice/kunde-suchen", dependencies=[Depends(recht("bearbeiten"))])
    def voice_kunde_suchen(daten: VoiceKundeSuchen, m: Mandant = Depends(mandant_kontext),
                           s: Session = Depends(sitzung)) -> dict[str, Any]:
        k = s.scalar(select(Kunde).where(Kunde.mandant_id == m.id, Kunde.telefon == telefon_normalisieren(daten.telefon)))
        if k is None:
            return {"gefunden": False}
        anlagen = [{"anlage_id": a.id, "typ": ANLAGENTYPEN[a.anlagentyp].bezeichnung, "hersteller": a.hersteller,
                    "ort": o.ort} for o in k.objekte for a in o.anlagen]
        offen = s.scalars(select(Servicefall).where(Servicefall.kunde_id == k.id,
                                                    Servicefall.status.in_(twin.OFFENE_STATUS))).all()
        return {"gefunden": True, "kunde_id": k.id, "name": k.name, "ort": k.ort, "wartungsvertrag": k.vertragsstatus == "wartungsvertrag",
                "anlagen": anlagen, "offene_vorgaenge": [{"id": f.id, "anliegen": f.anliegen, "status": f.status} for f in offen]}

    @app.post("/voice/zeitfenster", dependencies=[Depends(recht("bearbeiten"))])
    def voice_zeitfenster(daten: VoiceZeitfenster, m: Mandant = Depends(mandant_kontext),
                          s: Session = Depends(sitzung)) -> dict[str, Any]:
        bewertung = dringlichkeit_bewerten(daten.anliegen, app.state.uhr().month, daten.dringlichkeit)
        if bewertung.sicherheitshinweis:
            return {"sicherheitsfall": True, "hinweis": bewertung.sicherheitshinweis, "zeitfenster": []}
        tage = 1 if bewertung.dringlichkeit == "notdienst" else 2 if bewertung.dringlichkeit == "hoch" else 7
        # Dem Anrufer verschiedene Uhrzeiten anbieten, nicht dieselbe Zeit mit zwei Monteuren
        fenster, gesehen = [], set()
        for z in freie_zeitfenster(s, m, app.state.uhr(), tage, 90, qualifikation_fuer(daten.anlagentyp), daten.anzahl * 4):
            if z.beginn not in gesehen and len(fenster) < daten.anzahl:
                gesehen.add(z.beginn)
                fenster.append(z)
        return {"sicherheitsfall": False, "dringlichkeit": bewertung.dringlichkeit,
                "zeitfenster": [{"ressource_id": z.ressource_id, "monteur": z.ressource_name,
                                 "beginn": z.beginn.isoformat(), "ende": z.ende.isoformat()} for z in fenster]}

    @app.post("/voice/servicefall", status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def voice_servicefall(daten: VoiceServicefall, m: Mandant = Depends(mandant_kontext),
                          s: Session = Depends(sitzung)) -> dict[str, Any]:
        telefon = telefon_normalisieren(daten.telefon)
        k = s.scalar(select(Kunde).where(Kunde.mandant_id == m.id, Kunde.telefon == telefon)) if telefon else None
        if k is None:
            k = Kunde(mandant_id=m.id, name=daten.name, telefon=telefon, strasse=daten.strasse, plz=daten.plz, ort=daten.ort)
            s.add(k)
            s.flush()
            domaene.protokollieren(s, m.id, "kunde.angelegt", "kunde", k.id, "ki", {"quelle": "telefon"})
        anlage_id = None
        if daten.anlagentyp in ANLAGENTYPEN:
            anlage = next((a for o in k.objekte for a in o.anlagen if a.anlagentyp == daten.anlagentyp), None)
            if anlage is None:
                o = k.objekte[0] if k.objekte else Objekt(mandant_id=m.id, kunde_id=k.id, strasse=k.strasse, plz=k.plz, ort=k.ort)
                s.add(o)
                s.flush()
                anlage = Anlage(mandant_id=m.id, objekt_id=o.id, anlagentyp=daten.anlagentyp, hersteller=daten.hersteller,
                                wartungsintervall_monate=ANLAGENTYPEN[daten.anlagentyp].wartungsintervall_monate)
                s.add(anlage)
                s.flush()
            anlage_id = anlage.id
        f = servicefall_erzeugen(s, m, ServicefallNeu(kunde_id=k.id, anlage_id=anlage_id, anliegen=daten.anliegen,
                                                      symptom=daten.symptom, dringlichkeit=daten.dringlichkeit,
                                                      quelle="telefon"), "ki")
        antwort: dict[str, Any] = {"servicefall_id": f.id, "dringlichkeit": f.dringlichkeit, "termin": None}
        if daten.ressource_id and daten.beginn:
            t, freigabe = fachlich(lambda: termin_vorschlagen(s, m, f, TerminNeu(ressource_id=daten.ressource_id,
                                                                                 beginn=daten.beginn), "ki"))
            antwort["termin"] = {"termin_id": t.id, "beginn": t.beginn.isoformat(),
                                 "verbindlich": freigabe.status == "freigegeben",
                                 "hinweis": None if freigabe.status == "freigegeben"
                                 else "Termin ist vorgemerkt; das Büro bestätigt ihn per SMS oder Rückruf."}
        s.commit()
        return antwort

    @app.post("/voice/notdienst", dependencies=[Depends(recht("bearbeiten"))])
    def voice_notdienst(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Bereitschaftsnummer für Notfälle außerhalb der Bürozeit; ohne eingetragene Bereitschaft nur die Büronummer."""
        b = einsatz.bereitschaft_aktuell(s, m, app.state.uhr())
        if b is None:
            return {"bereitschaft": False, "telefon": m.stammdaten.get("telefon"),
                    "hinweis": "Derzeit ist kein Bereitschaftsdienst eingetragen. Rückruf vormerken und auf das Büro verweisen."}
        return {"bereitschaft": True, "name": b.ressource.name.split(" (")[0], "telefon": b.telefon, "bis": b.bis.isoformat(),
                "hinweis": f"Bereitschaft hat {b.ressource.name.split(' (')[0]}, erreichbar unter {b.telefon}."}

    @app.post("/voice/gespraech-beendet", status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def voice_gespraech_beendet(daten: VoiceGespraechBeendet, m: Mandant = Depends(mandant_kontext),
                                s: Session = Depends(sitzung)) -> dict[str, Any]:
        k = None
        if daten.telefon:
            k = s.scalar(select(Kunde).where(Kunde.mandant_id == m.id, Kunde.telefon == telefon_normalisieren(daten.telefon)))
        if daten.servicefall_id:
            holen(s, Servicefall, daten.servicefall_id, m)
        c = Kommunikation(mandant_id=m.id, kunde_id=k.id if k else None, servicefall_id=daten.servicefall_id,
                          kanal="telefon", transkript=daten.transkript, zusammenfassung=daten.zusammenfassung,
                          extraktion=daten.extraktion)
        s.add(c)
        s.flush()
        if k and daten.aufzeichnung_eingewilligt:
            k.einwilligungen = {**(k.einwilligungen or {}), "aufzeichnung": app.state.uhr().isoformat()}
        domaene.protokollieren(s, m.id, "kommunikation.erfasst", "kommunikation", c.id, "ki",
                               {"servicefall_id": daten.servicefall_id})
        s.commit()
        return {"kommunikation_id": c.id}

    return app

