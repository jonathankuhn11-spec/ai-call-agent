"""REST-API des Core-Service: CRM, Vorgänge, Disposition, Freigabe-Inbox, Digital Twin,
Ontologie-Export und die Tool-API für den Voice-Agenten.

Authentifizierung: `X-API-Key` (Umgebungsvariable CORE_API_KEY; ohne Variable nur für lokale
Entwicklung offen). Mandantenkontext: `X-Mandant`. Für Büro-Nutzer ist OIDC über Keycloak
vorgesehen (Profil „auth“ im docker-compose); der API-Key bleibt für Maschinen (Voice-Agent, n8n).
"""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core import domaene, management, twin
from core.db import Datenbank
from core.disposition import freie_zeitfenster, kollidiert
from core.modelle import (Anlage, Ereignis, Freigabe, Kommunikation, Kunde, Mandant, Objekt, OutboxEintrag,
                          Ressource, Servicefall, Termin)
from core.ontologie import ANLAGENTYPEN, TBOX, anlagentyp, exportieren
from core.regeln import dringlichkeit_bewerten, qualifikation_fuer, telefon_normalisieren

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


class RessourceAus(_Schema):
    id: str
    name: str
    qualifikationen: list[str]
    arbeitstage: list[int]
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
    wer: str


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


class VoiceGespraechBeendet(_Schema):
    telefon: str | None = None
    servicefall_id: str | None = None
    transkript: str
    zusammenfassung: str | None = None
    extraktion: dict[str, Any] = {}
    aufzeichnung_eingewilligt: bool = False


# --- App --------------------------------------------------------------------------------------
def app_erzeugen(db: Datenbank | None = None, uhr=lambda: datetime.now(timezone.utc)) -> FastAPI:
    db = db or Datenbank()
    db.schema_anlegen()
    app = FastAPI(title="SHK-Backoffice Core-Service", version="0.1.0")
    app.state.db, app.state.uhr = db, uhr
    api_key = os.environ.get("CORE_API_KEY")

    def sitzung():
        yield from db.sitzung()

    def pruefen(x_api_key: str | None = Header(None)) -> None:
        if api_key and not (x_api_key and secrets.compare_digest(x_api_key, api_key)):
            raise HTTPException(401, "X-API-Key fehlt oder ist falsch")

    def mandant_kontext(x_mandant: str = Header(...), s: Session = Depends(sitzung), _=Depends(pruefen)) -> Mandant:
        m = s.get(Mandant, x_mandant)
        if m is None:
            raise HTTPException(404, "Mandant unbekannt")
        return m

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

    @app.get("/mandanten", response_model=list[MandantAus], dependencies=[Depends(pruefen)])
    def mandanten(s: Session = Depends(sitzung)):
        return s.scalars(select(Mandant).order_by(Mandant.name)).all()

    # --- Mandanten ---------------------------------------------------------------------------
    @app.post("/mandanten", response_model=MandantAus, status_code=201, dependencies=[Depends(pruefen)])
    def mandant_anlegen(daten: MandantNeu, s: Session = Depends(sitzung)):
        m = Mandant(**daten.model_dump())
        s.add(m)
        s.commit()
        return m

    # --- CRM ---------------------------------------------------------------------------------
    @app.post("/kunden", response_model=KundeAus, status_code=201)
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

    @app.patch("/kunden/{kunde_id}", response_model=KundeAus)
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

    @app.post("/kunden/{kunde_id}/objekte", response_model=ObjektAus, status_code=201)
    def objekt_anlegen(kunde_id: str, daten: ObjektNeu, m: Mandant = Depends(mandant_kontext),
                       s: Session = Depends(sitzung)):
        k = holen(s, Kunde, kunde_id, m)
        o = Objekt(mandant_id=m.id, kunde_id=k.id, **daten.model_dump())
        s.add(o)
        s.commit()
        return o

    @app.post("/objekte/{objekt_id}/anlagen", response_model=AnlageAus, status_code=201)
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
    @app.post("/ressourcen", response_model=RessourceAus, status_code=201)
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

    @app.get("/disposition/zeitfenster")
    def zeitfenster(dauer_minuten: int = 90, qualifikation: str | None = None, ab: datetime | None = None,
                    tage: int = Query(5, le=30), anzahl: int = Query(5, le=20),
                    m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        fenster = freie_zeitfenster(s, m, ab or app.state.uhr(), tage, dauer_minuten, qualifikation, anzahl)
        return [vars(z) for z in fenster]

    @app.get("/termine", response_model=list[TerminAus])
    def termine(von: datetime | None = None, bis: datetime | None = None, ressource_id: str | None = None,
                m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        abfrage = select(Termin).where(Termin.mandant_id == m.id)
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

    @app.post("/servicefaelle", response_model=ServicefallAus, status_code=201)
    def servicefall_anlegen(daten: ServicefallNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        f = servicefall_erzeugen(s, m, daten, "mensch")
        s.commit()
        return f

    @app.get("/servicefaelle", response_model=list[ServicefallAus])
    def servicefaelle(status: str | None = None, dringlichkeit: str | None = None, limit: int = Query(100, le=1000),
                      m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        abfrage = select(Servicefall).where(Servicefall.mandant_id == m.id)
        if status:
            abfrage = abfrage.where(Servicefall.status == status)
        if dringlichkeit:
            abfrage = abfrage.where(Servicefall.dringlichkeit == dringlichkeit)
        return s.scalars(abfrage.order_by(Servicefall.erstellt.desc()).limit(limit)).all()

    @app.get("/servicefaelle/{fall_id}")
    def servicefall_lesen(fall_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        f = holen(s, Servicefall, fall_id, m)
        ereignisse = s.scalars(select(Ereignis).where(Ereignis.entitaet_id == f.id).order_by(Ereignis.id)).all()
        return {"servicefall": ServicefallAus.model_validate(f),
                "termine": [TerminAus.model_validate(t) for t in f.termine],
                "ereignisse": [{"typ": e.typ, "akteur": e.akteur, "zeit": e.zeit, "payload": e.payload} for e in ereignisse]}

    @app.post("/servicefaelle/{fall_id}/status", response_model=ServicefallAus)
    def status_wechseln(fall_id: str, daten: StatusWechsel, m: Mandant = Depends(mandant_kontext),
                        s: Session = Depends(sitzung)):
        f = holen(s, Servicefall, fall_id, m)
        fachlich(lambda: domaene.uebergang(s, f, daten.ziel, "mensch", daten.grund, daten.wer))
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

    @app.post("/servicefaelle/{fall_id}/termine", status_code=201)
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

    @app.post("/freigaben", response_model=FreigabeAus, status_code=201)
    def aktion_beantragen(daten: AktionNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        f = fachlich(lambda: domaene.aktion_beantragen(s, m, daten.aktion, daten.payload, "mensch",
                                                       daten.servicefall_id, daten.begruendung))
        s.commit()
        return f

    @app.post("/freigaben/{freigabe_id}/entscheidung", response_model=FreigabeAus)
    def entscheiden(freigabe_id: str, daten: Entscheidung, m: Mandant = Depends(mandant_kontext),
                    s: Session = Depends(sitzung)):
        f = holen(s, Freigabe, freigabe_id, m)
        fachlich(lambda: domaene.entscheiden(s, f, daten.freigeben, daten.wer))
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
    @app.post("/wartung/planen")
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
                  s: Session = Depends(sitzung)) -> dict[str, Any]:
        return management.plantafel(s, m, tag or app.state.uhr().date(), tage)

    # --- Tool-API für den Voice-Agenten ------------------------------------------------------
    @app.post("/voice/kunde-suchen")
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

    @app.post("/voice/zeitfenster")
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

    @app.post("/voice/servicefall", status_code=201)
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

    @app.post("/voice/gespraech-beendet", status_code=201)
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

