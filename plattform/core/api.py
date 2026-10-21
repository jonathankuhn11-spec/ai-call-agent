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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core import domaene, loeschung, management, twin
from core.auth import Abgewiesen, Authentifizierung, Prinzipal
from core.db import Datenbank, mandant_binden
from core.disposition import freie_zeitfenster, kollidiert
from core.adapter import ERLAUBT, ZIELE, konfiguration_pruefen
from core.geheimnisse import GEHEIME_FELDER, konfiguration_maskieren, konfiguration_verschluesseln
from core.modelle import (Anlage, Ereignis, Freigabe, Kommunikation, Kunde, Mandant, Objekt, OutboxEintrag, Rechnung,
                          Ressource, Servicefall, Termin)
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

