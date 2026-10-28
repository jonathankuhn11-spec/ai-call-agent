"""REST-API des Core-Service: CRM, Vorgänge, Disposition, Freigabe-Inbox, Digital Twin,
Ontologie-Export und die Tool-API für den Voice-Agenten.

Authentifizierung und Rollen: core/auth.py. Maschinen (Voice-Agent, n8n) mit `X-API-Key` und
`X-Mandant`, Personen mit OIDC-Bearer-Token (Keycloak), aus dem Mandant und Rollen stammen.
Jeder Endpunkt verlangt ein Recht (lesen, status, bearbeiten, freigeben, verwalten, plattform).
"""
from __future__ import annotations

import base64
import html
import os
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as time_
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from schwifty import IBAN
from schwifty.exceptions import SchwiftyException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core import (
    anfragen,
    angebote,
    belege,
    branchen,
    buchung,
    checklisten,
    dienstplan,
    domaene,
    dubletten,
    einrichtung,
    einsatz,
    export,
    feiertage,
    foerderung,
    forderungen,
    garantie,
    grosshandel,
    gutscheine,
    hinweise,
    kalender,
    karte,
    lebenszyklus,
    loeschung,
    management,
    metriken,
    optimierung,
    portal,
    produkt,
    prognose,
    registrierung,
    qualifikationen,
    risiko,
    routing,
    scanner,
    schaetzung,
    suche,
    tagesaufgaben,
    twin,
    upsell,
    vertraege,
    vorlagen,
    warteliste,
    website,
    zaehler,
)
from core import integrationen as hub
from core.adapter import ERLAUBT, ZIELE, konfiguration_pruefen
from core.adapter import datev as datev_api
from core.adapter import stripe as stripe_api
from core.adapter import whatsapp as whatsapp_api
from core.adapter.basis import AdapterFehler
from core.auth import Abgewiesen, Authentifizierung, Prinzipal
from core.db import Datenbank, mandant_binden
from core.disposition import (
    ZEITZONE,
    freie_zeitfenster,
    kollidiert,
    sachmittel_belegungen,
    sachmittel_frei,
)
from core.domaene import als_utc
from core.geheimnisse import (
    GEHEIME_FELDER,
    konfiguration_entschluesseln,
    konfiguration_maskieren,
    konfiguration_verschluesseln,
)
from core.modelle import (
    Abwesenheit,
    Anfrage,
    Angebot,
    Anhang,
    Anlage,
    Artikel,
    Bauteil,
    Beleg,
    Bereitschaft,
    Dienst,
    Ereignis,
    ExternalRef,
    Freigabe,
    Frist,
    Kommunikation,
    Kunde,
    Mahnung,
    Mandant,
    Objekt,
    OutboxEintrag,
    Rapport,
    Rechnung,
    Ressource,
    Servicefall,
    Termin,
    Vertrag,
    Wartelisteneintrag,
    Zahlung,
    Zeiteintrag,
)
from core.ontologie import ANLAGENTYPEN, TBOX, anlagentyp, exportieren
from core.rechnung import girocode as rechnung_girocode
from core.rechnung import pdf as rechnung_pdf
from core.rechnung import positionen_aus_payload, summen, xrechnung
from core.regeln import (
    dringlichkeit_bewerten,
    qualifikation_fuer,
    telefon_normalisieren,
)

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


class LizenzEingabe(_Schema):
    schluessel: str = Field(min_length=20, max_length=2000)


class VorlagenEingabe(_Schema):
    vorlagen: dict[str, dict[str, str | None]]


class VorlagenVorschau(_Schema):
    schluessel: str
    text: str | None = None
    betreff: str | None = None


class EditionZuweisung(_Schema):
    edition: str | None = None       # None nimmt die Zuweisung zurück
    gueltig_bis: date | None = None


class TestphaseVerlaengerung(_Schema):
    tage: int = Field(14, ge=1, le=365)


class LizenzAusstellung(_Schema):
    edition: str
    kunde: str = Field(min_length=2, max_length=200)
    gueltig_bis: date
    mandant: str | None = None
    sitze: int | None = Field(None, ge=1, le=1000)


class KundeNeu(_Schema):
    name: str
    telefon: str | None = None
    email: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    vertragsstatus: str = "ohne"
    notiz: str | None = None
    kartei: dict[str, Any] = {}
    nicht_erschienen: int = 0
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)


class KundeAus(KundeNeu):
    id: str
    einwilligungen: dict[str, Any] = {}
    geo_quelle: str | None = None
    geo_stand: datetime | None = None


class KundeAenderung(_Schema):
    name: str | None = None
    telefon: str | None = None
    email: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    vertragsstatus: str | None = None
    notiz: str | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)


class Einwilligungen(_Schema):
    """Schalter je Einwilligungsart; true setzt den Zeitstempel, false entzieht die Einwilligung."""
    aufzeichnung: bool | None = None
    email: bool | None = None
    sms: bool | None = None
    kontakt: bool | None = None
    bewertung: bool | None = None
    whatsapp: bool | None = None
    gesundheitsdaten: bool | None = None   # Art. 9 Abs. 2 lit. a DSGVO: Kartei mit Allergien, Kontraindikationen, Befunden


class ObjektNeu(_Schema):
    bezeichnung: str = "Hauptobjekt"
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    gebaeudetyp: str | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)


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
    merkmale: dict[str, Any] = {}      # Anlagenkartei (branchen.ANLAGENKARTEI), z. B. Kennzeichen, FIN, HU


class AnlageAenderung(_Schema):
    hersteller: str | None = None
    modell: str | None = None
    baujahr: int | None = Field(None, ge=1950, le=2100)
    wartungsintervall_monate: int | None = None
    letzte_wartung: date | None = None
    merkmale: dict[str, Any] | None = None   # wird zusammengeführt; leerer Wert löscht das Merkmal


class AnlageAus(AnlageNeu):
    id: str
    objekt_id: str
    merkmale: dict[str, Any] = {}


class RessourceNeu(_Schema):
    name: str
    qualifikationen: list[str] = []
    arbeitstage: list[int] = [0, 1, 2, 3, 4]
    arbeitsbeginn: str = "07:30"
    arbeitsende: str = "16:30"
    benutzer: str | None = None
    qualifikationen_ablauf: dict[str, str] = {}
    wochenstunden: float = Field(40.0, ge=0, le=60)
    urlaubsanspruch_tage: int = Field(30, ge=0, le=60)
    art: Literal["person", "sachmittel"] = "person"
    sachmittel_typ: str | None = Field(None, max_length=30)


class RessourceAenderung(_Schema):
    name: str | None = None
    qualifikationen: list[str] | None = None
    arbeitstage: list[int] | None = None
    arbeitsbeginn: str | None = None
    arbeitsende: str | None = None
    aktiv: bool | None = None
    benutzer: str | None = None
    qualifikationen_ablauf: dict[str, str | None] | None = None
    wochenstunden: float | None = Field(None, ge=0, le=60)
    urlaubsanspruch_tage: int | None = Field(None, ge=0, le=60)
    sachmittel_typ: str | None = Field(None, max_length=30)


class RessourceAus(_Schema):
    id: str
    name: str
    qualifikationen: list[str]
    arbeitstage: list[int]
    arbeitsbeginn: time_ | None = None
    arbeitsende: time_ | None = None
    benutzer: str | None = None
    aktiv: bool = True
    qualifikationen_ablauf: dict[str, Any] = {}
    wochenstunden: float = 40.0
    urlaubsanspruch_tage: int = 30
    art: str = "person"
    sachmittel_typ: str | None = None


class BauteilNeu(_Schema):
    art: str = Field(max_length=50)
    bezeichnung: str = Field(max_length=200)
    pflicht: bool = False
    nutzungsdauer_jahre: int | None = Field(None, ge=1, le=80)
    intervall_monate: int | None = Field(None, ge=1, le=600)
    einbau: date | None = None
    letzte_erneuerung: date | None = None
    kosten_schaetzung: str | None = None
    rechtsgrundlage: str | None = Field(None, max_length=200)
    notiz: str | None = Field(None, max_length=300)


class BauteilErneuert(_Schema):
    am: date | None = None
    kosten: str | None = None
    notiz: str | None = Field(None, max_length=300)


class DienstNeu(_Schema):
    ressource_id: str
    datum: date
    art: Literal["arbeit", "bereitschaft", "frei"] = "arbeit"
    schicht: str | None = None
    beginn: str | None = None
    ende: str | None = None
    pause_minuten: int | None = Field(None, ge=0, le=180)
    notiz: str | None = Field(None, max_length=200)


class DienstUebernahme(_Schema):
    dienste: list[dict[str, Any]]


class ZaehlerstandNeu(_Schema):
    art: str
    wert: float = Field(ge=0)
    datum: date | None = None
    anlage_id: str | None = None
    quelle: Literal["buero", "monteur", "kunde", "import"] = "buero"
    notiz: str | None = Field(None, max_length=200)


class ServicefallNeu(_Schema):
    kunde_id: str
    anlage_id: str | None = None
    anliegen: str
    symptom: str | None = None
    dringlichkeit: Dringlichkeit | None = None
    quelle: Literal["telefon", "email", "web", "manuell"] = "manuell"
    benoetigte_qualifikation: str | None = None
    sachmittel: str | None = Field(None, max_length=30)      # nötiges Sachmittel, z. B. hebebuehne (branchen.SACHMITTEL)
    dauer_minuten: int | None = Field(None, ge=15, le=600)   # None: lernende Schätzung aus Rapporten (schaetzung.py)


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
    sachmittel: str | None = None
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
    sachmittel_id: str | None = None   # leer: wird automatisch gewählt, wenn der Vorgang ein Sachmittel braucht


class TerminAus(_Schema):
    id: str
    servicefall_id: str
    ressource_id: str
    beginn: datetime
    ende: datetime
    status: str
    sachmittel_id: str | None = None


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
    inferenz: bool = False   # RDFS-Schlussfolgerung (owlrl) vor der Abfrage: Unterklassen, Domänen, Bereiche


class TourUmsetzen(_Schema):
    ressource_id: str
    tag: date | None = None
    reihenfolge: list[str] = Field(..., min_length=1)


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
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    karte_kacheln: str | None = None   # Kachel-URL mit {z}/{x}/{y}; Standard OpenStreetMap, CSP img-src beachten
    karte_hinweis: str | None = None
    bundesland: str | None = Field(None, max_length=2)   # Feiertage: NW, BY, … (Standard NW)
    routing_url: str | None = None     # eigener OSRM-Server (Profil routing); leer: ROUTING_URL bzw. Demo-Server
    kontoinhaber: str | None = None    # Name auf dem GiroCode, Standard Betriebsname
    basis_url: str | None = None       # öffentliche Adresse der Plattform (Portal-Links, Push-Links, DATEV-Rückruf)
    ids_shop_url: str | None = None    # IDS-Connect-Einstiegsadresse des Großhändlers (Warenkorbübergabe)
    whatsapp_nummer: str | None = None # Rufnummer des WhatsApp-Business-Kontos (Klick-zum-Chat auf Website und Portal)
    absage_frist_stunden: int = Field(24, ge=0, le=168)  # Portal: kürzere Absagen nur telefonisch
    impressum_url: str | None = None
    datenschutz_url: str | None = None
    buchung_vorlauf_tage: int = Field(14, ge=1, le=90)      # Online-Buchung: wie weit voraus
    buchung_vorlauf_stunden: int = Field(2, ge=0, le=72)    # Online-Buchung: Mindestvorlauf
    no_show_hinweis: str | None = None                      # Text in Terminbestätigung (z. B. Ausfallgebühr bei Nichterscheinen)
    # Runde 6 (core/dienstplan.py, core/garantie.py, core/qualifikationen.py, core/lebenszyklus.py)
    schichtbetrieb: bool | None = None                      # Dienstplan-Vorschlag mit Früh-/Spätschicht statt Tagschicht
    vob: bool | None = None                                 # Gewährleistungsfristen nach VOB/B statt BGB
    garantie_jahre: int | None = Field(None, ge=0, le=15)   # Herstellergarantie ab Baujahr, wenn keine Frist hinterlegt
    garantie_hersteller: dict[str, int] | None = None       # je Hersteller abweichende Garantiejahre
    pflichtqualifikationen: list[str] | None = None         # Spalten der Qualifikationsmatrix, die jeder haben muss
    bauteilkosten: dict[str, str] | None = None             # Kostenschätzung je Bauteilart überschreiben
    warteliste_frist_stunden: int | None = Field(None, ge=1, le=72)
    # Nachkalkulation (core/einsatz.py): interne Kosten und Verrechnungssatz je Stunde, Aufschlag auf Materialnetto
    stundensatz_kosten: float = Field(55, ge=0, le=500)
    stundensatz_verrechnung: float = Field(69, ge=0, le=500)
    materialaufschlag_prozent: float = Field(15, ge=0, le=200)
    # Angebote, Mahnwesen, Bewertungen, Website (core/angebote.py, core/forderungen.py, core/einsatz.py)
    angebotsnummer_praefix: str = Field("AN", max_length=10)
    angebot_gueltig_tage: int = Field(30, ge=1, le=365)
    wartungsvertrag_preis_jahr: float | None = Field(None, ge=0)   # Jahrespreis netto für den Upsell-Vorschlag „Wartungsvertrag“
    mahn_karenz_tage: int = Field(7, ge=0, le=90)
    mahn_frist_tage: int = Field(10, ge=1, le=60)
    mahngebuehr_stufe2: float = Field(5.00, ge=0, le=100)
    mahngebuehr_stufe3: float = Field(10.00, ge=0, le=100)
    bewertungslink: str | None = None
    basis_url: str | None = None


class Integration(_Schema):
    """Anbindung eines Ziels: kanaele in Reihenfolge, je Typ ein Konfigurationsblock."""
    kanaele: list[str] = Field(default_factory=list)
    email: dict[str, Any] = {}
    sms: dict[str, Any] = {}
    webhook: dict[str, Any] = {}
    lexware: dict[str, Any] = {}
    sevdesk: dict[str, Any] = {}
    belegpaket: dict[str, Any] = {}
    belegtransfer: dict[str, Any] = {}
    ntfy: dict[str, Any] = {}
    datev: dict[str, Any] = {}
    whatsapp: dict[str, Any] = {}
    stripe: dict[str, Any] = {}


class LagerBuchung(_Schema):
    artikel_id: str
    lager: str = "zentral"
    menge: float
    grund: str = Field("manuell", max_length=200)
    mindestbestand: float | None = Field(None, ge=0)


class WarenkorbPosition(_Schema):
    nummer: str
    menge: float = Field(1, gt=0)
    einheit: str = "Stk"
    bezeichnung: str = ""
    kommission: str = ""


class Warenkorb(_Schema):
    positionen: list[WarenkorbPosition] = Field(..., min_length=1)
    shop_url: str | None = None
    kennung: str | None = None


class FoerderAnfrage(_Schema):
    programm: Literal["heizung", "einzelmassnahme"] = "heizung"
    kosten: float = Field(..., gt=0)
    wohneinheiten: int = Field(1, ge=1, le=500)
    heizungsart: str = "waermepumpe"
    selbstnutzend: bool = True
    alte_heizung_ersetzt: bool = False
    einkommen_unter_40k: bool = False
    natuerliches_kaeltemittel: bool = False
    erd_oder_wasserquelle: bool = False
    biomasse_staubarm: bool = False
    isfp: bool = False
    massnahme: str = "anlagentechnik"


class BrancheSetzen(_Schema):
    branche: str
    leistungen_anlegen: bool = True


class EinrichtungMitarbeiter(_Schema):
    name: str
    qualifikationen: list[str] = []
    arbeitstage: list[int] | None = None
    arbeitsbeginn: str | None = None
    arbeitsende: str | None = None
    wochenstunden: float | None = None
    benutzer: str | None = None


class EinrichtungSachmittel(_Schema):
    name: str
    typ: str


class Einrichtung(_Schema):
    """Einrichtungsassistent: alles in einem Aufruf, idempotent."""
    branche: str | None = None
    name: str | None = None
    stammdaten: Stammdaten | None = None
    mitarbeiter: list[EinrichtungMitarbeiter] = []
    sachmittel: list[EinrichtungSachmittel] = []
    leistungen_anlegen: bool = True
    online_buchung: bool = False


class KarteiAenderung(_Schema):
    felder: dict[str, Any]


class GutscheinNeu(_Schema):
    wert: float = Field(..., gt=0, le=10000)
    kunde_id: str | None = None
    empfaenger: str | None = Field(None, max_length=200)


class GutscheinEinloesen(_Schema):
    betrag: float = Field(..., gt=0)
    rechnung_id: str | None = None


class BuchungNeu(_Schema):
    leistung_id: str
    ressource_id: str
    beginn: datetime
    name: str = Field(..., min_length=2, max_length=200)
    telefon: str | None = None
    email: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    hinweis: str | None = Field(None, max_length=500)
    einwilligung_email: bool = False
    website: str | None = None  # Honeypot


class WartelisteNeu(_Schema):
    wunsch_von: date
    wunsch_bis: date
    tageszeiten: list[Literal["vormittag", "nachmittag"]] = []
    wochentage: list[int] = Field([], max_length=7)
    notiz: str | None = Field(None, max_length=300)


class TerminAbsage(_Schema):
    grund: str | None = Field(None, max_length=300)
    nachbesetzen: bool = True


class PlanUmsetzen(_Schema):
    plan: dict[str, Any]


class Zusammenfuehren(_Schema):
    quelle_id: str


class PortalAktion(_Schema):
    aktion: Literal["bestaetigen", "absagen"]
    grund: str | None = Field(None, max_length=300)


class WebsiteEinstellungen(_Schema):
    aktiv: bool | None = None
    slug: str | None = Field(None, max_length=80)
    domain: str | None = Field(None, max_length=253)
    claim: str | None = Field(None, max_length=160)
    ueber_uns: str | None = Field(None, max_length=3000)
    farbe: str | None = Field(None, max_length=7)
    logo_url: str | None = Field(None, max_length=500)
    bild_url: str | None = Field(None, max_length=500)
    oeffnungszeiten: str | None = Field(None, max_length=200)
    einzugsgebiet: str | None = Field(None, max_length=200)
    notdienst_telefon: str | None = Field(None, max_length=40)
    notdienst_text: str | None = Field(None, max_length=300)
    impressum_zusatz: str | None = Field(None, max_length=2000)
    bereiche: dict[str, bool] | None = None
    bewertungen_min_sterne: int | None = Field(None, ge=1, le=5)
    leistungen_text: dict[str, str] | None = None
    faq: list[dict[str, str]] | None = None


class BewertungVeroeffentlichen(_Schema):
    veroeffentlicht: bool = True


class UpsellAngebot(_Schema):
    servicefall_id: str | None = None
    versenden: bool = False


class UpsellAblehnung(_Schema):
    grund: str | None = Field(None, max_length=300)


class PortalWunschtermin(_Schema):
    ressource_id: str
    beginn: datetime
    sachmittel_id: str | None = None


class PortalBewertung(_Schema):
    sterne: int = Field(ge=1, le=5)
    text: str | None = Field(None, max_length=1000)


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
    import_kennung: str | None = None
    uebergaben: list[dict[str, Any]] = []
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
    bezug_typ: Literal["betrieb", "ressource", "anlage", "fahrzeug", "vertrag"] = "betrieb"
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


class ArtikelNeu(_Schema):
    nummer: str = Field(..., min_length=1, max_length=40)
    bezeichnung: str = Field(..., min_length=2, max_length=200)
    kategorie: Literal["material", "arbeit", "fahrt", "pauschale", "leistung"] = "material"
    einheit: str = Field("Stk", max_length=10)
    einkauf_netto: float | None = None
    verkauf_netto: float = Field(..., ge=0)
    ust_satz: str = "19"
    lieferant: str | None = None
    aktiv: bool = True
    dauer_minuten: int | None = Field(None, ge=5, le=600)
    qualifikation: str | None = Field(None, max_length=50)


class ArtikelAenderung(_Schema):
    bezeichnung: str | None = Field(None, min_length=2, max_length=200)
    kategorie: Literal["material", "arbeit", "fahrt", "pauschale", "leistung"] | None = None
    einheit: str | None = None
    einkauf_netto: float | None = None
    verkauf_netto: float | None = Field(None, ge=0)
    ust_satz: str | None = None
    lieferant: str | None = None
    aktiv: bool | None = None
    dauer_minuten: int | None = Field(None, ge=5, le=600)
    qualifikation: str | None = Field(None, max_length=50)


class ArtikelAus(_Schema):
    id: str
    nummer: str
    bezeichnung: str
    kategorie: str
    einheit: str
    einkauf_netto: str | None
    verkauf_netto: str
    ust_satz: str
    lieferant: str | None
    aktiv: bool
    dauer_minuten: int | None = None
    qualifikation: str | None = None


class AngebotNeu(_Schema):
    kunde_id: str
    servicefall_id: str | None = None
    titel: str | None = Field(None, max_length=200)
    einleitung: str | None = None
    gueltig_bis: date | None = None
    positionen: list[dict[str, Any]] = Field(default_factory=list)  # {artikel_id|bezeichnung, menge, einheit, einzelpreis_netto, ust_satz}


class AngebotAenderung(_Schema):
    titel: str | None = Field(None, max_length=200)
    einleitung: str | None = None
    gueltig_bis: date | None = None
    positionen: list[dict[str, Any]] | None = None


class AngebotAus(_Schema):
    id: str
    nummer: str
    kunde_id: str
    servicefall_id: str | None
    rechnung_id: str | None
    titel: str
    einleitung: str | None
    datum: date
    gueltig_bis: date
    positionen: list[dict[str, Any]]
    netto: str
    umsatzsteuer: str
    brutto: str
    status: str
    empfaenger: dict[str, Any]
    entschieden_am: date | None


class Ablehnung(_Schema):
    grund: str | None = None


class ZahlungNeu(_Schema):
    rechnung_id: str
    betrag: float
    datum: date | None = None
    art: Literal["ueberweisung", "bar", "karte", "skonto", "ausbuchung"] = "ueberweisung"
    referenz: str | None = Field(None, max_length=200)


class ZahlungAus(_Schema):
    id: str
    rechnung_id: str
    rechnung_nummer: str | None = None
    datum: date
    betrag: str
    art: str
    referenz: str | None
    quelle: str


class MahnungAus(_Schema):
    id: str
    rechnung_id: str
    stufe: int
    datum: date
    frist: date
    gebuehr: str
    offen: str


class VertragNeu(_Schema):
    kunde_id: str
    bezeichnung: str = Field("Wartungsvertrag", max_length=200)
    anlagen_ids: list[str] = Field(default_factory=list)
    leistungen: str | None = None
    beginn: date | None = None
    laufzeit_monate: int = Field(12, ge=1, le=120)
    kuendigungsfrist_monate: int = Field(3, ge=0, le=12)
    verlaengerung_monate: int = Field(12, ge=0, le=60)
    preis_jahr_netto: float = Field(..., ge=0)
    ust_satz: str = "19"
    abrechnung_monate: int = Field(12, ge=1, le=12)
    naechste_abrechnung: date | None = None
    notiz: str | None = None


class Kuendigung(_Schema):
    zum: date | None = None


class AbwesenheitNeu(_Schema):
    ressource_id: str
    von: date
    bis: date
    art: Literal["urlaub", "krank", "schulung", "sonstiges"] = "urlaub"
    notiz: str | None = Field(None, max_length=300)


class AbwesenheitAus(AbwesenheitNeu):
    id: str


class AnfrageAus(_Schema):
    id: str
    kanal: str
    externe_id: str
    name: str | None
    telefon: str | None
    email: str | None
    betreff: str | None
    nachricht: str | None
    kunde_id: str | None
    servicefall_id: str | None
    status: str
    eingegangen: datetime


class WebformularEinstellung(_Schema):
    aktiv: bool = True
    erneuern: bool = False
    danke_url: str | None = None


class Posteingang(_Schema):
    host: str
    port: int = Field(993, ge=1, le=65535)
    benutzer: str = ""
    passwort: str = ""
    ordner: str = "INBOX"


class ScannerEinstellung(_Schema):
    """Scan-Eingang: Scan-to-Mail-Postfach (IMAP) und/oder Eingangsordner des Netzwerkscanners."""
    host: str = ""
    port: int = Field(993, ge=1, le=65535)
    benutzer: str = ""
    passwort: str = ""
    ordner: str = "INBOX"
    erlaubte_absender: str = ""  # Komma-getrennt: Adressen oder Domains der Scanner (leer = alle)
    eingangsordner: str = ""
    kategorie: Literal["material", "fremdleistung", "fahrzeug", "sonstiges"] = "material"


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
    app = FastAPI(title=f"{produkt.produkt()['name']} Core-Service", version=produkt.__version__)
    app.state.db, app.state.system_db, app.state.uhr, app.state.auth = db, system_db, uhr, auth
    app.state.geocoder = None   # Tests und Betrieb ohne Netz setzen hier eine eigene Funktion (Adresse → (lat, lon))
    app.state.router = None     # routing.Router; Tests setzen Router(None) (Luftlinie)
    app.state.metriken = metriken.Metriken(system_db.sitzungen, uhr)
    app.state.datev_transport = None  # Tests: httpx.MockTransport für den DATEV-Login
    app.state.adapter_zusatz = {}     # Tests: {"transport": httpx.MockTransport(...)} für Verbindungstests und Webhooks

    domain_cache: dict[str, tuple[float, str | None]] = {}

    def website_slug_fuer_host(host: str) -> str | None:
        """Eigene Domain eines Betriebs → Slug (60 s zwischengespeichert); localhost und die Plattformdomain liefern None."""
        host = host.lower().split(":")[0]
        if not host or host in ("localhost", "127.0.0.1") or host.replace(".", "").isdigit():
            return None
        jetzt = time.monotonic()
        treffer = domain_cache.get(host)
        if treffer and jetzt - treffer[0] < 60:
            return treffer[1]
        slug = None
        with system_db.sitzungen() as s:
            m = website.mandant_nach_domain(s, host)
            slug = website.konfig(m)["slug"] if m is not None else None
        domain_cache[host] = (jetzt, slug)
        return slug

    @app.middleware("http")
    async def eigene_domain(request: Request, call_next):
        pfad = request.scope.get("path", "")
        if pfad in ("/", "/impressum", "/datenschutz", "/sitemap.xml", "/robots.txt", "/anfrage") and request.headers.get("host"):
            slug = website_slug_fuer_host(request.headers["host"])
            if slug:
                neu = f"/web/{slug}" + ("" if pfad == "/" else pfad)
                request.scope["path"], request.scope["raw_path"] = neu, neu.encode()
        return await call_next(request)

    @app.middleware("http")
    async def messen(request: Request, call_next):
        start = time.perf_counter()
        antwort = await call_next(request)
        route = request.scope.get("route")
        app.state.metriken.messen(request.method, getattr(route, "path", request.url.path)[:80], antwort.status_code, start)
        return antwort

    def router_fuer(m: Mandant) -> routing.Router:
        return app.state.router or routing.Router.aus_umgebung(m.stammdaten)

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
        return {"status": "ok", "version": produkt.__version__}

    @app.get("/metrics", include_in_schema=False, dependencies=[Depends(recht("verwalten"))])
    def metrics() -> Response:
        """Prometheus-Format; hinter Caddy nicht öffentlich, Prometheus im Compose-Netz ruft mit X-API-Key ab."""
        return Response(app.state.metriken.ausgabe(), media_type=metriken.CONTENT_TYPE)

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

    # --- Produkt: Editionen, Lizenz, White-Label, Registrierung, Betreiberkonsole (Runde 10) ---------
    def heute() -> date:
        return app.state.uhr().date()

    def feature(name: str):
        """Abhängigkeit: Funktion muss in der wirksamen Edition des Mandanten enthalten sein, sonst 402."""
        def pruefen(m: Mandant = Depends(mandant_kontext)) -> Mandant:
            st = produkt.status(m, heute())
            if name not in st["features"]:
                raise HTTPException(402, f"„{produkt.FEATURES[name]}“ ist in der Edition {st['titel']} nicht enthalten; Lizenz unter Einstellungen → Edition und Lizenz")
            return m
        return pruefen

    def limit(s: Session, m: Mandant, art: str) -> None:
        try:
            produkt.limit_pruefen(s, m, heute(), art)
        except produkt.Lizenzfehler as e:
            raise HTTPException(402, str(e)) from e

    @app.get("/produkt")
    def produkt_info() -> dict[str, Any]:
        """Öffentlich: Produktname, Version, Branding, Editionen (für Oberfläche, Registrierung und Monitoring)."""
        return produkt.produkt()

    @app.get("/betrieb/lizenz")
    def lizenz_lesen(m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        st = produkt.status(m, heute())
        st["oeffentlicher_schluessel_hinterlegt"] = bool(produkt.oeffentlicher_schluessel())
        return st

    @app.put("/betrieb/lizenz", dependencies=[Depends(recht("verwalten"))])
    def lizenz_setzen(daten: LizenzEingabe, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        try:
            st = produkt.lizenz_setzen(m, daten.schluessel, heute())
        except produkt.Lizenzfehler as e:
            raise HTTPException(422, str(e)) from e
        domaene.protokollieren(s, m.id, "lizenz.eingetragen", "mandant", m.id, "mensch", {"edition": st["edition"], "gueltig_bis": st["gueltig_bis"]}, p.name)
        s.commit()
        return st

    @app.get("/betrieb/vorlagen", dependencies=[Depends(recht("bearbeiten"))])
    def vorlagen_lesen(m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        return {"vorlagen": vorlagen.alle(m), "beispiel": vorlagen.BEISPIEL}

    @app.put("/betrieb/vorlagen", dependencies=[Depends(recht("verwalten"))])
    def vorlagen_setzen(daten: VorlagenEingabe, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        try:
            neu = vorlagen.speichern(m, daten.vorlagen)
        except vorlagen.Vorlagenfehler as e:
            raise HTTPException(422, str(e)) from e
        domaene.protokollieren(s, m.id, "vorlagen.geaendert", "mandant", m.id, "mensch", {"angepasst": sorted(neu)}, p.name)
        s.commit()
        return {"vorlagen": vorlagen.alle(m)}

    @app.post("/betrieb/vorlagen/vorschau", dependencies=[Depends(recht("bearbeiten"))])
    def vorlagen_vorschau(daten: VorlagenVorschau, m: Mandant = Depends(mandant_kontext)) -> dict[str, str]:
        try:
            return vorlagen.vorschau(m, daten.schluessel, daten.text, daten.betreff)
        except (vorlagen.Vorlagenfehler, KeyError) as e:
            raise HTTPException(422, str(e)) from e

    def xlsx(inhalt: bytes, art: str) -> Response:
        return Response(inhalt, media_type=export.MIME, headers={"Content-Disposition": f'attachment; filename="{export.dateiname(art, heute())}"'})

    @app.get("/auswertungen/export.xlsx", dependencies=[Depends(recht("finanzen")), Depends(feature("auswertungen"))])
    def auswertungen_xlsx(monate: int = Query(12, ge=1, le=36), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        return xlsx(export.auswertungen_xlsx(management.auswertungen(s, m, app.state.uhr(), monate), m.name), "auswertungen")

    @app.get("/kunden/export.xlsx", dependencies=[Depends(recht("bearbeiten"))])
    def kunden_xlsx(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        return xlsx(export.kunden_xlsx(s, m), "kunden")

    @app.get("/offene-posten/export.xlsx", dependencies=[Depends(recht("finanzen"))])
    def offene_posten_xlsx(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        return xlsx(export.offene_posten_xlsx(forderungen.offene_posten(s, m, heute()), m.name), "offene-posten")

    @app.get("/termine/export.xlsx", dependencies=[Depends(recht("einsatz"))])
    def termine_xlsx(von: date | None = None, bis: date | None = None, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        von_ = datetime.combine(von or heute(), datetime.min.time(), ZEITZONE)
        bis_ = datetime.combine(bis or (heute() + timedelta(days=30)), datetime.max.time(), ZEITZONE)
        return xlsx(export.termine_xlsx(s, m, als_utc(von_), als_utc(bis_)), "termine")

    registrierung_zaehler: dict[str, list[float]] = {}

    @app.get("/registrieren", include_in_schema=False, response_class=HTMLResponse)
    def registrieren_seite() -> HTMLResponse:
        if not produkt.registrierung_offen():
            raise HTTPException(404, "Selbstregistrierung ist nicht freigeschaltet (REGISTRIERUNG_OFFEN)")
        return HTMLResponse(registrierung.seite(produkt.produkt(), auth.oidc is not None), headers={"Cache-Control": "public, max-age=300"})

    @app.post("/registrierung", status_code=201)
    async def registrierung_anlegen(request: Request, s: Session = Depends(system_sitzung)) -> dict[str, Any]:
        """Öffentlich (kein Login): legt Mandant mit Testphase an. Honeypot `website`, 5 Registrierungen je Adresse und Stunde."""
        if not produkt.registrierung_offen():
            raise HTTPException(404, "Selbstregistrierung ist nicht freigeschaltet (REGISTRIERUNG_OFFEN)")
        try:
            daten = await request.json()
        except ValueError:
            raise HTTPException(400, "JSON erwartet") from None
        if not isinstance(daten, dict):
            raise HTTPException(400, "JSON-Objekt erwartet")
        if daten.get("website"):
            return {"mandant_id": None, "name": daten.get("name"), "testphase_bis": None, "hinweis": "Danke"}  # Bot: still ignorieren
        adresse = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
        jetzt = time.monotonic()
        fenster = [x for x in registrierung_zaehler.get(adresse, []) if jetzt - x < 3600]
        if len(fenster) >= 5:
            raise HTTPException(429, "Zu viele Registrierungen von dieser Adresse; bitte später erneut versuchen")
        basis = os.environ.get("BASIS_URL") or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
        try:
            ergebnis = registrierung.anlegen(s, daten, heute(), basis)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        s.commit()
        registrierung_zaehler[adresse] = fenster + [jetzt]  # nur erfolgreiche Registrierungen zählen
        return ergebnis

    @app.get("/betreiber/mandanten", dependencies=[Depends(recht("plattform"))])
    def betreiber_mandanten(s: Session = Depends(system_sitzung)) -> dict[str, Any]:
        """Betreiberkonsole (nur Maschinenschlüssel): alle Mandanten mit Edition, Testphase, Nutzung."""
        liste = produkt.mandanten_uebersicht(s, heute())
        return {"mandanten": liste, "summen": produkt.summen(liste), "editionen": {k: v["titel"] for k, v in produkt.EDITIONEN.items()},
                "privater_schluessel_hinterlegt": bool(os.environ.get("LIZENZ_PRIVATER_SCHLUESSEL")), "oeffentlicher_schluessel_hinterlegt": bool(produkt.oeffentlicher_schluessel())}

    @app.put("/betreiber/mandanten/{mandant_id}/edition", dependencies=[Depends(recht("plattform"))])
    def betreiber_edition(mandant_id: str, daten: EditionZuweisung, s: Session = Depends(system_sitzung)) -> dict[str, Any]:
        m = s.get(Mandant, mandant_id)
        if m is None:
            raise HTTPException(404, "Mandant unbekannt")
        try:
            st = produkt.edition_zuweisen(m, daten.edition, daten.gueltig_bis, heute())
        except produkt.Lizenzfehler as e:
            raise HTTPException(422, str(e)) from e
        domaene.protokollieren(s, m.id, "lizenz.zugewiesen", "mandant", m.id, "system", {"edition": daten.edition, "gueltig_bis": st["gueltig_bis"]}, "betreiber")
        s.commit()
        return st

    @app.post("/betreiber/mandanten/{mandant_id}/testphase", dependencies=[Depends(recht("plattform"))])
    def betreiber_testphase(mandant_id: str, daten: TestphaseVerlaengerung, s: Session = Depends(system_sitzung)) -> dict[str, Any]:
        m = s.get(Mandant, mandant_id)
        if m is None:
            raise HTTPException(404, "Mandant unbekannt")
        neu = produkt.testphase_verlaengern(m, daten.tage, heute())
        domaene.protokollieren(s, m.id, "testphase.verlaengert", "mandant", m.id, "system", {"bis": neu.isoformat(), "tage": daten.tage}, "betreiber")
        s.commit()
        return produkt.status(m, heute())

    @app.post("/betreiber/lizenzschluessel", dependencies=[Depends(recht("plattform"))])
    def betreiber_lizenzschluessel(daten: LizenzAusstellung) -> dict[str, Any]:
        """Signierten Lizenzschlüssel ausstellen (privater Schlüssel aus LIZENZ_PRIVATER_SCHLUESSEL, nur auf der Anbieterinstallation)."""
        privat = os.environ.get("LIZENZ_PRIVATER_SCHLUESSEL")
        if not privat:
            raise HTTPException(409, "Kein privater Lizenzschlüssel hinterlegt (LIZENZ_PRIVATER_SCHLUESSEL); Schlüsselpaar mit `python -m core lizenz schluesselpaar`")
        try:
            schl = produkt.schluessel_erzeugen(privat, edition=daten.edition, kunde=daten.kunde, gueltig_bis=daten.gueltig_bis, mandant=daten.mandant, sitze=daten.sitze)
        except (produkt.Lizenzfehler, ValueError) as e:
            raise HTTPException(422, str(e)) from e
        return {"schluessel": schl, "edition": daten.edition, "gueltig_bis": daten.gueltig_bis.isoformat(), "mandant": daten.mandant or "*"}

    # --- CRM ---------------------------------------------------------------------------------
    @app.post("/kunden", response_model=KundeAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def kunde_anlegen(daten: KundeNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        werte = daten.model_dump()
        werte["telefon"] = telefon_normalisieren(daten.telefon)
        if werte["telefon"] and s.scalar(select(Kunde).where(Kunde.mandant_id == m.id, Kunde.telefon == werte["telefon"])):
            raise HTTPException(409, "Kunde mit dieser Rufnummer existiert bereits (Dublette)")
        if werte.get("lat") is not None and werte.get("lon") is not None:
            werte["geo_quelle"], werte["geo_stand"] = "manuell", app.state.uhr()
        limit(s, m, "kunden")
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

    @app.get("/kunden/karte")
    def kunden_karte(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Kundenkarte für das Cockpit: Kundenpunkte, heutige Einsätze, Betriebsstandort, Verteilung je Ort."""
        return karte.kundenkarte(s, m, app.state.uhr())

    @app.get("/kunden/vollstaendigkeit")
    def kunden_vollstaendigkeit(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Welche Kundendatensätze sind für den automatischen Betrieb unvollständig und was fehlt."""
        return karte.vollstaendigkeit_betrieb(s, m)

    @app.post("/kunden/geokodieren", dependencies=[Depends(recht("bearbeiten"))])
    def kunden_geokodieren(limit: int = Query(50, ge=1, le=500), alle: bool = False, m: Mandant = Depends(mandant_kontext),
                           s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Adressen ohne Koordinaten über den konfigurierten Geokodierungsdienst (Nominatim) auflösen."""
        if os.environ.get("GEOCODER_AUS", "").lower() in ("1", "true", "ja"):
            raise HTTPException(409, "Geokodierung ist abgeschaltet (GEOCODER_AUS)")
        geocoder = app.state.geocoder or karte.nominatim(email=(m.stammdaten or {}).get("email"))
        ergebnis = karte.geokodieren(s, m, geocoder, app.state.uhr(), limit, nur_fehlende=not alle)
        domaene.protokollieren(s, m.id, "kunden.geokodiert", "mandant", m.id, "mensch", ergebnis)
        s.commit()
        return ergebnis

    @app.get("/kunden/{kunde_id}")
    def kunde_lesen(kunde_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        faelle = s.scalars(select(Servicefall).where(Servicefall.kunde_id == k.id).order_by(Servicefall.erstellt.desc())).all()
        return {"kunde": KundeAus.model_validate(k),
                "objekte": [{**ObjektAus.model_validate(o).model_dump(),
                             "anlagen": [AnlageAus.model_validate(a) for a in o.anlagen]} for o in k.objekte],
                "servicefaelle": [ServicefallAus.model_validate(f) for f in faelle],
                "vollstaendigkeit": karte.vollstaendigkeit(k)}

    @app.patch("/kunden/{kunde_id}", response_model=KundeAus, dependencies=[Depends(recht("bearbeiten"))])
    def kunde_aendern(kunde_id: str, daten: KundeAenderung, m: Mandant = Depends(mandant_kontext),
                      s: Session = Depends(sitzung)):
        k = holen(s, Kunde, kunde_id, m)
        aenderung = daten.model_dump(exclude_unset=True)
        if "telefon" in aenderung:
            aenderung["telefon"] = telefon_normalisieren(aenderung["telefon"])
        for feld, wert in aenderung.items():
            setattr(k, feld, wert)
        if "lat" in aenderung or "lon" in aenderung:
            k.geo_quelle, k.geo_stand = ("manuell", app.state.uhr()) if k.lat is not None and k.lon is not None else (None, None)
        elif {"strasse", "plz", "ort"} & set(aenderung) and k.geo_quelle == "nominatim":
            k.lat = k.lon = k.geo_quelle = k.geo_stand = None   # Adresse geändert: Koordinaten neu ermitteln
        domaene.protokollieren(s, m.id, "kunde.geaendert", "kunde", k.id, "mensch", {"felder": sorted(aenderung)})
        s.commit()
        return k

    @app.put("/kunden/{kunde_id}/einwilligungen", response_model=KundeAus, dependencies=[Depends(recht("bearbeiten"))])
    def einwilligungen_setzen(kunde_id: str, daten: Einwilligungen, m: Mandant = Depends(mandant_kontext),
                              s: Session = Depends(sitzung)):
        """Einwilligungen mit Zeitstempel setzen oder entziehen (Art. 7 DSGVO, Nachweis im Ereignisprotokoll)."""
        k = holen(s, Kunde, kunde_id, m)
        stand = dict(k.einwilligungen or {})
        aenderung = daten.model_dump(exclude_unset=True)
        for art, wert in aenderung.items():
            if wert:
                stand[art] = app.state.uhr().isoformat()
            else:
                stand.pop(art, None)
        k.einwilligungen = stand
        domaene.protokollieren(s, m.id, "kunde.einwilligung", "kunde", k.id, "mensch", {"gesetzt": [a for a, w in aenderung.items() if w],
                                                                                          "entzogen": [a for a, w in aenderung.items() if not w]})
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
        werte = daten.model_dump()
        if werte.get("lat") is not None and werte.get("lon") is not None:
            werte["geo_quelle"], werte["geo_stand"] = "manuell", app.state.uhr()
        o = Objekt(mandant_id=m.id, kunde_id=k.id, **werte)
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
        werte["merkmale"] = fachlich(lambda: branchen.merkmale_pruefen(daten.anlagentyp, daten.merkmale))
        a = Anlage(mandant_id=m.id, objekt_id=o.id, **werte)
        s.add(a)
        s.flush()
        lebenszyklus.bauteile_anlegen(s, m, a)
        lebenszyklus.merkmale_uebernehmen(s, m, a, app.state.uhr().date())
        s.commit()
        return a

    @app.patch("/anlagen/{anlage_id}", response_model=AnlageAus, dependencies=[Depends(recht("bearbeiten"))])
    def anlage_aendern(anlage_id: str, daten: AnlageAenderung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        a = holen(s, Anlage, anlage_id, m)
        for feld, wert in daten.model_dump(exclude_unset=True).items():
            if feld == "merkmale":
                zusammen = {**(a.merkmale or {}), **(wert or {})}
                a.merkmale = fachlich(lambda: branchen.merkmale_pruefen(a.anlagentyp, {k: v for k, v in zusammen.items() if v not in (None, "")}))
            else:
                setattr(a, feld, wert)
        lebenszyklus.merkmale_uebernehmen(s, m, a, app.state.uhr().date())
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
        if daten.art == "sachmittel":
            if daten.sachmittel_typ not in branchen.SACHMITTEL:
                raise HTTPException(422, f"Sachmitteltyp fehlt oder unbekannt; erlaubt: {sorted(branchen.SACHMITTEL)}")
            werte["arbeitstage"] = [0, 1, 2, 3, 4, 5, 6]
        else:
            werte["sachmittel_typ"] = None
            limit(s, m, "mitarbeiter")
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
            elif feld == "qualifikationen_ablauf":
                wert = {**(r.qualifikationen_ablauf or {}), **(wert or {})}
                wert = {k: v for k, v in wert.items() if v}
            setattr(r, feld, wert)
        s.commit()
        return r

    @app.get("/disposition/zeitfenster")
    def zeitfenster(dauer_minuten: int = 90, qualifikation: str | None = None, ab: datetime | None = None,
                    tage: int = Query(5, le=30), anzahl: int = Query(5, le=20), sachmittel: str | None = None,
                    m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        fenster = freie_zeitfenster(s, m, ab or app.state.uhr(), tage, dauer_minuten, qualifikation, anzahl, sachmittel=sachmittel)
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
        geschaetzt = None
        if daten.dauer_minuten is None:
            geschaetzt = schaetzung.schaetzen(s, m, f"{daten.anliegen} {daten.symptom or ''}", anlage.anlagentyp if anlage else None, dringlichkeit)
        f = Servicefall(mandant_id=m.id, kunde_id=daten.kunde_id, anlage_id=daten.anlage_id, anliegen=daten.anliegen,
                        symptom=daten.symptom, dringlichkeit=dringlichkeit, quelle=daten.quelle,
                        benoetigte_qualifikation=daten.benoetigte_qualifikation
                        or qualifikation_fuer(anlage.anlagentyp if anlage else None),
                        sachmittel=daten.sachmittel,
                        dauer_minuten=daten.dauer_minuten or (geschaetzt["planung_minuten"] if geschaetzt else 90))
        s.add(f)
        s.flush()
        domaene.protokollieren(s, m.id, "servicefall.eingegangen", "servicefall", f.id, akteur,
                               {"quelle": f.quelle, "dringlichkeit": f.dringlichkeit, "regel": bewertung.grund,
                                **({"dauer_schaetzung": {k: geschaetzt[k] for k in ("planung_minuten", "ebene", "stichprobe", "konfidenz")}} if geschaetzt else {})})
        if f.dringlichkeit == "notdienst":
            hinweise.senden(s, m, "notdienst", "Notdienst eingegangen", f"{f.kunde.name}: {f.anliegen[:80]}", f"servicefall:{f.id}", "vorgaenge")
        g = garantie.pruefen(s, m, f, app.state.uhr().date())
        if g["moeglicher_gewaehrleistungsfall"]:
            domaene.protokollieren(s, m.id, "garantie.hinweis", "servicefall", f.id, "system", {"hinweise": g["hinweise"]})
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
        sachmittel_id = daten.sachmittel_id
        if sachmittel_id:
            sm = holen(s, Ressource, sachmittel_id, m)
            if sm.art != "sachmittel":
                raise HTTPException(422, f"{sm.name} ist kein Sachmittel")
        elif f.sachmittel:
            mittel = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv, Ressource.art == "sachmittel", Ressource.sachmittel_typ == f.sachmittel).order_by(Ressource.name)).all()
            if not mittel:
                raise HTTPException(409, f"Kein Sachmittel vom Typ {f.sachmittel} angelegt (Einstellungen → Team und Sachmittel)")
            belegt = sachmittel_belegungen(s, m.id, [x.id for x in mittel], als_utc(daten.beginn), als_utc(ende), timedelta(minutes=m.puffer_minuten))
            frei = sachmittel_frei(belegt, mittel, daten.beginn, ende)
            if frei is None:
                raise HTTPException(409, f"Alle Sachmittel vom Typ {f.sachmittel} sind im Zeitfenster belegt")
            sachmittel_id = frei.id
        if kollidiert(s, m, r.id, daten.beginn, ende, sachmittel_id):
            raise HTTPException(409, "Zeitfenster ist inzwischen belegt")
        if f.status == "eingegangen":
            domaene.uebergang(s, f, "qualifiziert", akteur, "Termin vorgeschlagen")
        t = Termin(mandant_id=m.id, servicefall_id=f.id, ressource_id=r.id, beginn=daten.beginn, ende=ende, sachmittel_id=sachmittel_id)
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
        if f.risikoklasse == "C" and p.art != "person" and (daten.wer or "").lower() in ("buero", "büro", "monteur", "steuerberater"):
            raise HTTPException(403, "Klasse C entscheidet nur die Inhaberrolle; ohne Anmeldung wer=\"inhaber\" angeben")
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
            return management.sparql(s, m, daten.abfrage, daten.inferenz)
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

    # --- Touren, Kalender, Feiertage ---------------------------------------------------------
    @app.get("/tour")
    def tour_lesen(ressource_id: str | None = None, tag: date | None = None, m: Mandant = Depends(mandant_kontext),
                   s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        """Tagestour je Monteur: Fahrzeiten ab Betriebsstandort, Linie für die Karte, optimierte Reihenfolge als Vorschlag."""
        jetzt_ = app.state.uhr()
        zeitpunkt = datetime.combine(tag, datetime.min.time(), timezone.utc) + timedelta(hours=12) if tag else jetzt_
        eigene = eigene_ressourcen(p, s, m)
        ressourcen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv, Ressource.art == "person").order_by(Ressource.name)).all()
        if ressource_id:
            ressourcen = [holen(s, Ressource, ressource_id, m)]
        if eigene is not None:
            ressourcen = [r for r in ressourcen if r.id in eigene]
        router = router_fuer(m)
        return {"tag": zeitpunkt.astimezone(ZEITZONE).date().isoformat(), "touren": [routing.tour(s, m, r, zeitpunkt, router) for r in ressourcen]}

    @app.post("/tour/umsetzen", dependencies=[Depends(recht("bearbeiten"))])
    def tour_umsetzen(daten: TourUmsetzen, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                      p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        r = holen(s, Ressource, daten.ressource_id, m)
        zeitpunkt = datetime.combine(daten.tag, datetime.min.time(), timezone.utc) + timedelta(hours=12) if daten.tag else app.state.uhr()
        ergebnis = fachlich(lambda: routing.tour_umsetzen(s, m, r, zeitpunkt, daten.reihenfolge, router_fuer(m), wer(p, None)))
        s.commit()
        return ergebnis

    @app.get("/kalender/links", dependencies=[Depends(recht("bearbeiten"))])
    def kalender_links(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        basis = (m.stammdaten or {}).get("basis_url") or os.environ.get("BASIS_URL") or "http://localhost:8100"
        ressourcen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv, Ressource.art == "person").order_by(Ressource.name)).all()
        return kalender.links(m, ressourcen, basis)

    @app.get("/kalender/betrieb/{mandant_id}.ics", include_in_schema=False)
    def kalender_betrieb(mandant_id: str, token: str | None = None, s: Session = Depends(sitzung)) -> Response:
        m = s.get(Mandant, mandant_id)
        if m is None or not kalender.token_gueltig("betrieb:" + mandant_id, token):
            raise HTTPException(404, "Kalender nicht gefunden")
        return Response(kalender.kalender(s, m, None, app.state.uhr()), media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": "inline; filename=betrieb.ics", "Cache-Control": "private, max-age=900"})

    @app.get("/kalender/{ressource_id}.ics", include_in_schema=False)
    def kalender_ressource(ressource_id: str, token: str | None = None, s: Session = Depends(sitzung)) -> Response:
        r = s.get(Ressource, ressource_id)
        if r is None or not kalender.token_gueltig("ressource:" + ressource_id, token):
            raise HTTPException(404, "Kalender nicht gefunden")
        m = s.get(Mandant, r.mandant_id)
        return Response(kalender.kalender(s, m, r, app.state.uhr()), media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": "inline; filename=einsaetze.ics", "Cache-Control": "private, max-age=900"})

    @app.get("/feiertage")
    def feiertage_lesen(jahr: int | None = None, m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        jahr = jahr or app.state.uhr().year
        return {"bundesland": feiertage.bundesland(m.stammdaten), "jahr": jahr, "feiertage": feiertage.feiertage(jahr, m.stammdaten),
                "bundeslaender": feiertage.BUNDESLAENDER}

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
        werte = daten.model_dump(exclude_none=True)
        if werte.get("iban"):
            try:
                iban = IBAN(werte["iban"])
            except SchwiftyException as e:
                raise HTTPException(422, f"IBAN ungültig: {e}") from e
            werte["iban"] = iban.formatted
            if not werte.get("bic") and iban.bic:
                werte["bic"] = str(iban.bic)
            if not werte.get("bank") and iban.bank_name:
                werte["bank"] = iban.bank_name
        if werte.get("bundesland") and werte["bundesland"].upper() not in feiertage.BUNDESLAENDER:
            raise HTTPException(422, f"Unbekanntes Bundesland {werte['bundesland']!r} (Kürzel wie NW, BY, HE)")
        if werte.get("bundesland"):
            werte["bundesland"] = werte["bundesland"].upper()
        m.stammdaten = {**m.stammdaten, **werte}
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
        for typ, f in (("whatsapp", "whatsapp"), ("stripe", "online_zahlung"), ("lexware", "finanzanbindung"), ("sevdesk", "finanzanbindung"), ("datev", "finanzanbindung")):
            if typ in daten.kanaele and not produkt.hat_feature(m, heute(), f):
                raise HTTPException(402, f"„{produkt.FEATURES[f]}“ ist in der Edition {produkt.status(m, heute())['titel']} nicht enthalten")
        bisher = m.integrationen.get(ziel) or {}
        neu: dict[str, Any] = {"kanaele": daten.kanaele}
        for typ in ("email", "sms", "webhook", "lexware", "sevdesk", "belegpaket", "belegtransfer", "ntfy", "datev", "whatsapp", "stripe"):
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


    # --- Integrations-Hub: Zustand, Verbindungstests, eingehende Webhooks (Runde 9) ----------------------
    @app.get("/integrationen/status", dependencies=[Depends(recht("verwalten"))])
    def integrationen_status(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        dienste = {"voice": bool(os.environ.get("LIVEKIT_URL")), "routing": bool(m.stammdaten.get("routing_url") or os.environ.get("ROUTING_URL")) or None,
                   "geocoder": app.state.geocoder is not None or bool(os.environ.get("GEOCODER_URL")) or None, "oidc": app.state.auth.oidc is not None,
                   "scanner": bool(os.environ.get("SCAN_IMAP_HOST") or os.environ.get("SCAN_EINGANGSORDNER")) or None, "grosshandel": bool(m.stammdaten.get("ids_shop_url"))}
        return hub.status(s, m, app.state.uhr(), dienste)

    @app.post("/integrationen/{ziel}/{typ}/pruefen", dependencies=[Depends(recht("verwalten"))])
    def integration_pruefen(ziel: str, typ: str, m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        if ziel not in ZIELE:
            raise HTTPException(404, "Unbekanntes Ziel")
        return fachlich(lambda: hub.verbindung_pruefen(m, ziel, typ, app.state.adapter_zusatz))

    @app.get("/integrationen/whatsapp/webhook", include_in_schema=False)
    def whatsapp_webhook_pruefung(request: Request, s: Session = Depends(system_sitzung)) -> Response:
        """Meta prüft den Webhook einmalig mit hub.verify_token; gültig ist das verify_token irgendeines Betriebs."""
        q = request.query_params
        if q.get("hub.mode") != "subscribe" or not q.get("hub.challenge"):
            raise HTTPException(400, "Ungültige Prüfanfrage")
        if hub.mandant_nach_feld(s, "endkunde", "whatsapp", "verify_token", q.get("hub.verify_token", "")) is None:
            raise HTTPException(403, "verify_token unbekannt")
        return PlainTextResponse(q["hub.challenge"])

    @app.post("/integrationen/whatsapp/webhook", include_in_schema=False)
    async def whatsapp_webhook(request: Request, s: Session = Depends(system_sitzung)) -> dict[str, Any]:
        """Eingehende Kundennachrichten: offener Vorgang → Kommunikation und Hinweis; sonst neue Anfrage (Kanal whatsapp).
        Idempotent je Nachrichten-ID; der Betrieb ergibt sich aus der phone_number_id."""
        try:
            daten = await request.json()
        except ValueError:
            raise HTTPException(400, "Kein JSON")
        verarbeitet = 0
        jetzt = app.state.uhr()
        for n in whatsapp_api.eingang_lesen(daten if isinstance(daten, dict) else {}):
            m = hub.mandant_nach_feld(s, "endkunde", "whatsapp", "phone_number_id", n.get("phone_number_id") or "")
            if m is None or not n.get("id"):
                continue
            mandant_binden(s, m.id)
            externe_id = f"wa:{n['id']}"
            if s.scalar(select(Anfrage).where(Anfrage.mandant_id == m.id, Anfrage.externe_id == externe_id)) or \
               s.scalar(select(Kommunikation).where(Kommunikation.mandant_id == m.id, Kommunikation.extraktion["externe_id"].as_string() == externe_id)):
                continue
            telefon = "+" + n["von"] if n.get("von") else None
            kunde = anfragen._kunde_finden(s, m, anfragen.telefon_normalisieren(telefon), None) if telefon else None
            offen = s.scalar(select(Servicefall).where(Servicefall.kunde_id == kunde.id, Servicefall.status.notin_(("durchgefuehrt", "abgerechnet", "uebergeben", "storniert")))
                             .order_by(Servicefall.erstellt.desc())) if kunde else None
            if offen is not None:
                s.add(Kommunikation(mandant_id=m.id, kunde_id=kunde.id, servicefall_id=offen.id, kanal="whatsapp", richtung="eingehend", zusammenfassung=n["text"][:2000],
                                    extraktion={"externe_id": externe_id, "von": n.get("von")}, zeit=jetzt))
                hinweise.senden(s, m, "info", "WhatsApp-Nachricht", f"{kunde.name}: {n['text'][:160]}", externe_id, f"vorgaenge/{offen.id}")
            else:
                anfragen.anfrage_verarbeiten(s, m, "whatsapp", externe_id, n.get("name"), telefon, None, "WhatsApp", n["text"], jetzt)
            verarbeitet += 1
        s.commit()
        return {"verarbeitet": verarbeitet}

    @app.post("/integrationen/stripe/webhook", include_in_schema=False)
    async def stripe_webhook(request: Request, s: Session = Depends(system_sitzung)) -> dict[str, Any]:
        """checkout.session.completed → Zahlung auf die Rechnung; Signatur mit dem webhook_secret des Betriebs, idempotent je Session."""
        body = await request.body()
        ev = stripe_api.ereignis_lesen(body)
        if ev is None:
            return {"ok": True, "ignoriert": True}
        treffer = hub.mandant_nach_referenz(s, "stripe", ev["session_id"] or "")
        if treffer is None:
            raise HTTPException(404, "Session unbekannt")
        m, ref = treffer
        geheim = hub.konfig_lesen(m, "zahlung", "stripe").get("webhook_secret") or ""
        if not stripe_api.signatur_pruefen(body, request.headers.get("stripe-signature"), geheim, app.state.uhr().timestamp()):
            raise HTTPException(400, "Signatur ungültig")
        mandant_binden(s, m.id)
        r = s.get(Rechnung, ref.entitaet_id)
        if r is None:
            raise HTTPException(404, "Rechnung unbekannt")
        if s.scalar(select(Zahlung).where(Zahlung.rechnung_id == r.id, Zahlung.referenz == ev["session_id"])) is None and r.status != "bezahlt":
            forderungen.zahlung_buchen(s, m, r, ev["betrag"], app.state.uhr().date(), "online", ev["session_id"], "stripe", "Stripe")
            hinweise.senden(s, m, "info", "Online bezahlt", f"Rechnung {r.nummer} über {r.brutto.replace('.', ',')} € wurde im Portal bezahlt.", f"stripe:{ev['session_id']}", "rechnungen")
        s.commit()
        return {"ok": True, "rechnung": r.nummer, "status": r.status}

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

    @app.post("/belege/scan", response_model=BelegAus, status_code=201)
    async def beleg_scannen(bilder: list[UploadFile] = File(...), kategorie: str = Form("material"), scanmodus: bool = Form(True),
                            servicefall_id: str | None = Form(None), notiz: str | None = Form(None), m: Mandant = Depends(mandant_kontext),
                            s: Session = Depends(sitzung), p: Prinzipal = Depends(recht("einsatz"))):
        """Kamera-Scan: mehrere Fotos (Seiten) werden zu einem PDF zusammengefasst und wie ein Scan erfasst."""
        if kategorie not in ("material", "fremdleistung", "fahrzeug", "sonstiges"):
            raise HTTPException(422, "Kategorie unbekannt")
        if servicefall_id:
            eigener_fall(p, s, m, holen(s, Servicefall, servicefall_id, m))
        seiten = [await b.read() for b in bilder]
        pdf = fachlich(lambda: scanner.bilder_zu_pdf(seiten, scanmodus))
        name = f"scan_{app.state.uhr().strftime('%Y%m%d_%H%M%S')}_{len(seiten)}s.pdf"
        kennung = "kamera:" + scanner.kennung_fuer(b"".join(seiten))  # Hash der Fotos, nicht des PDF (das trägt einen Zeitstempel)
        b_ = fachlich(lambda: scanner.scan_zu_beleg(s, m, pdf, name, "application/pdf", kennung, app.state.uhr(),
                                                    "kamera", kategorie, servicefall_id, p.name, notiz))
        if b_ is None:
            raise HTTPException(409, "Dieser Scan wurde bereits erfasst")
        s.commit()
        return b_

    @app.post("/belege/scaneingang/abrufen", dependencies=[Depends(recht("bearbeiten"))])
    def scaneingang_abrufen(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Scanner-Postfach und Eingangsordner jetzt abrufen (sonst Cron: python -m core scaneingang)."""
        konfig = m.integrationen.get("scanner") or {}
        if not konfig.get("host") and not konfig.get("eingangsordner"):
            raise HTTPException(409, "Kein Scan-Eingang hinterlegt (Einstellungen → Scanner)")
        from core.geheimnisse import konfiguration_entschluesseln
        k = konfiguration_entschluesseln(konfig)
        ergebnis: dict[str, Any] = {}
        try:
            if k.get("host"):
                postfach = anfragen.ImapPostfach(k["host"], k.get("benutzer", ""), k.get("passwort", ""), k.get("ordner", "INBOX"), int(k.get("port", 993)))
                try:
                    ergebnis["postfach"] = scanner.postfach_scans(s, m, postfach, app.state.uhr(), k.get("erlaubte_absender"), k.get("kategorie", "material"))
                finally:
                    postfach.schliessen()
            if k.get("eingangsordner"):
                ergebnis["ordner"] = scanner.ordner_scans(s, m, Path(k["eingangsordner"]), app.state.uhr(), k.get("kategorie", "material"))
        except OSError as e:
            s.rollback()
            raise HTTPException(502, f"Scan-Eingang nicht erreichbar: {e}") from e
        s.commit()
        return ergebnis

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
        if aenderung.get("status") == "geprueft":
            scanner.uebergabe_beantragen(s, m, b, p.name)  # Steuertools erhalten geprüfte Belege sofort (Outbox)
        s.commit()
        return b

    @app.post("/belege/{beleg_id}/uebergabe", response_model=BelegAus)
    def beleg_uebergeben(beleg_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                         p: Prinzipal = Depends(recht("finanzen"))):
        """Übergabe an die angebundenen Steuertools anstoßen (idempotent je Ziel); erfasste Belege erst prüfen."""
        b = holen(s, Beleg, beleg_id, m)
        if not scanner.uebergabekanaele(m):
            raise HTTPException(409, "Kein übergabefähiger Kanal: Lexware Office, sevDesk, DATEV Belegtransfer oder Upload-Mail in den Einstellungen anbinden")
        eintraege = fachlich(lambda: scanner.uebergabe_beantragen(s, m, b, p.name))
        for e in eintraege:
            if e.status in ("tot",):
                e.status, e.versuche, e.letzter_fehler = "ausstehend", 0, None  # erneuter Anlauf nach Konfigurationskorrektur
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
        empf = [e for e in upsell.empfehlungen(s, m, k, app.state.uhr().date()) if e["prioritaet"] >= 2][:2]
        return {"gefunden": True, "kunde_id": k.id, "name": k.name, "ort": k.ort, "wartungsvertrag": k.vertragsstatus == "wartungsvertrag",
                "anlagen": anlagen, "offene_vorgaenge": [{"id": f.id, "anliegen": f.anliegen, "status": f.status} for f in offen],
                "empfehlungen": [{"titel": e["titel"], "preis_brutto": e["preis_brutto"], "schluessel": e["schluessel"]} for e in empf]}

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

    # --- Artikelstamm und Angebote ------------------------------------------------------------
    @app.get("/artikel", response_model=list[ArtikelAus])
    def artikel_listen(q: str | None = None, alle: bool = False, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        """Standard: nur aktive Artikel (für Angebotspositionen); alle=true für die Stammdatenpflege."""
        abfrage = select(Artikel).where(Artikel.mandant_id == m.id)
        if not alle:
            abfrage = abfrage.where(Artikel.aktiv.is_(True))
        if q:
            abfrage = abfrage.where(or_(Artikel.bezeichnung.ilike(f"%{q}%"), Artikel.nummer.ilike(f"%{q}%")))
        return s.scalars(abfrage.order_by(Artikel.kategorie, Artikel.nummer)).all()

    def _geld(wert: Any, feld: str) -> str:
        from decimal import Decimal, InvalidOperation
        try:
            return f"{Decimal(str(wert).replace(',', '.')):.2f}"
        except InvalidOperation as e:
            raise HTTPException(422, f"{feld} ist keine Zahl") from e

    @app.post("/artikel", response_model=ArtikelAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def artikel_anlegen(daten: ArtikelNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        if s.scalar(select(Artikel).where(Artikel.mandant_id == m.id, Artikel.nummer == daten.nummer)):
            raise HTTPException(409, f"Artikelnummer {daten.nummer} ist vergeben")
        a = Artikel(mandant_id=m.id, **daten.model_dump(exclude={"einkauf_netto", "verkauf_netto"}),
                    einkauf_netto=_geld(daten.einkauf_netto, "einkauf_netto") if daten.einkauf_netto is not None else None,
                    verkauf_netto=_geld(daten.verkauf_netto, "verkauf_netto"))
        s.add(a)
        s.commit()
        return a

    @app.patch("/artikel/{artikel_id}", response_model=ArtikelAus, dependencies=[Depends(recht("bearbeiten"))])
    def artikel_aendern(artikel_id: str, daten: ArtikelAenderung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        a = holen(s, Artikel, artikel_id, m)
        for feld, wert in daten.model_dump(exclude_unset=True).items():
            setattr(a, feld, _geld(wert, feld) if feld in ("einkauf_netto", "verkauf_netto") and wert is not None else wert)
        s.commit()
        return a

    @app.get("/angebote", response_model=list[AngebotAus])
    def angebote_listen(status: str | None = None, kunde_id: str | None = None, limit: int = Query(200, le=2000),
                        m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        abfrage = select(Angebot).where(Angebot.mandant_id == m.id)
        if status:
            abfrage = abfrage.where(Angebot.status == status)
        if kunde_id:
            abfrage = abfrage.where(Angebot.kunde_id == kunde_id)
        return s.scalars(abfrage.order_by(Angebot.datum.desc(), Angebot.nummer.desc()).limit(limit)).all()

    @app.post("/angebote", response_model=AngebotAus, status_code=201)
    def angebot_anlegen(daten: AngebotNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                        p: Prinzipal = Depends(recht("bearbeiten"))):
        payload = daten.model_dump(exclude_none=True)
        if "gueltig_bis" in payload:
            payload["gueltig_bis"] = payload["gueltig_bis"].isoformat()
        a = fachlich(lambda: angebote.angebot_erzeugen(s, m, payload, app.state.uhr().date(), p.name))
        s.commit()
        return a

    @app.get("/angebote/{angebot_id}", response_model=AngebotAus)
    def angebot_lesen(angebot_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        return holen(s, Angebot, angebot_id, m)

    @app.patch("/angebote/{angebot_id}", response_model=AngebotAus, dependencies=[Depends(recht("bearbeiten"))])
    def angebot_aendern(angebot_id: str, daten: AngebotAenderung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        """Nur Entwürfe sind änderbar; versendete Angebote sind preisgebunden und werden neu erstellt."""
        a = holen(s, Angebot, angebot_id, m)
        if a.status != "entwurf":
            raise HTTPException(409, f"Angebot {a.nummer} ist {a.status} und nicht mehr änderbar")
        aenderung = daten.model_dump(exclude_unset=True)
        if "positionen" in aenderung:
            zeilen = fachlich(lambda: angebote.positionen_aus_artikeln(s, m, aenderung.pop("positionen") or []))
            positionen = fachlich(lambda: positionen_aus_payload({"positionen": zeilen}, bool(m.stammdaten.get("kleinunternehmer")))) if zeilen else []
            netto, ust, brutto, _ = summen(positionen)
            a.positionen = [{**p.als_dict(), **({"artikel_id": z["artikel_id"]} if z.get("artikel_id") else {})} for p, z in zip(positionen, zeilen)]
            a.netto, a.umsatzsteuer, a.brutto = str(netto), str(ust), str(brutto)
        for feld, wert in aenderung.items():
            setattr(a, feld, wert)
        s.commit()
        return a

    @app.get("/angebote/{angebot_id}/pdf")
    def angebot_pdf(angebot_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        a = holen(s, Angebot, angebot_id, m)
        return Response(angebote.pdf(a, m), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{a.nummer}.pdf"'})

    @app.post("/angebote/{angebot_id}/senden", response_model=FreigabeAus, status_code=201)
    def angebot_senden(angebot_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                       p: Prinzipal = Depends(recht("bearbeiten"))):
        a = holen(s, Angebot, angebot_id, m)
        f = fachlich(lambda: angebote.versand_beantragen(s, m, a, p.name))
        s.commit()
        return f

    @app.post("/angebote/{angebot_id}/annehmen", response_model=ServicefallAus)
    def angebot_annehmen(angebot_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                         p: Prinzipal = Depends(recht("bearbeiten"))):
        a = holen(s, Angebot, angebot_id, m)
        fall = fachlich(lambda: angebote.annehmen(s, m, a, app.state.uhr().date(), p.name))
        s.commit()
        return fall

    @app.post("/angebote/{angebot_id}/ablehnen", response_model=AngebotAus)
    def angebot_ablehnen(angebot_id: str, daten: Ablehnung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                         p: Prinzipal = Depends(recht("bearbeiten"))):
        a = holen(s, Angebot, angebot_id, m)
        fachlich(lambda: angebote.ablehnen(s, a, app.state.uhr().date(), daten.grund, p.name))
        s.commit()
        return a

    @app.post("/angebote/{angebot_id}/rechnung", response_model=FreigabeAus, status_code=201)
    def angebot_abrechnen(angebot_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                          p: Prinzipal = Depends(recht("bearbeiten"))):
        a = holen(s, Angebot, angebot_id, m)
        f = fachlich(lambda: angebote.rechnung_beantragen(s, m, a, p.name))
        s.commit()
        return f

    # --- Offene Posten, Zahlungen, Bankimport, Mahnwesen ---------------------------------------
    @app.get("/offene-posten", dependencies=[Depends(recht("finanzen"))])
    def offene_posten(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        return forderungen.offene_posten(s, m, app.state.uhr().date())

    @app.get("/zahlungen", response_model=list[ZahlungAus], dependencies=[Depends(recht("finanzen"))])
    def zahlungen(rechnung_id: str | None = None, limit: int = Query(200, le=2000), m: Mandant = Depends(mandant_kontext),
                  s: Session = Depends(sitzung)):
        abfrage = select(Zahlung).where(Zahlung.mandant_id == m.id)
        if rechnung_id:
            abfrage = abfrage.where(Zahlung.rechnung_id == rechnung_id)
        return [{**ZahlungAus.model_validate(z).model_dump(), "rechnung_nummer": z.rechnung.nummer}
                for z in s.scalars(abfrage.order_by(Zahlung.datum.desc(), Zahlung.erstellt.desc()).limit(limit))]

    @app.post("/zahlungen", response_model=ZahlungAus, status_code=201)
    def zahlung_anlegen(daten: ZahlungNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                        p: Prinzipal = Depends(recht("finanzen"))):
        r = holen(s, Rechnung, daten.rechnung_id, m)
        z = fachlich(lambda: forderungen.zahlung_buchen(s, m, r, daten.betrag, daten.datum or app.state.uhr().date(), daten.art,
                                                        daten.referenz, "manuell", p.name))
        s.commit()
        return z

    @app.post("/zahlungen/bankimport")
    async def zahlungen_bankimport(datei: UploadFile = File(...), buchen: bool = Form(True), m: Mandant = Depends(mandant_kontext),
                                   s: Session = Depends(sitzung), p: Prinzipal = Depends(recht("finanzen"))) -> dict[str, Any]:
        """CSV-Export des Online-Bankings (Buchungstag, Betrag, Verwendungszweck); Rechnungsnummern im Zweck werden zugeordnet."""
        inhalt = await datei.read()
        if len(inhalt) > 5_000_000:
            raise HTTPException(413, "Datei größer als 5 MB")
        ergebnis = fachlich(lambda: forderungen.bankimport(s, m, inhalt, app.state.uhr().date(), buchen, p.name))
        s.commit()
        return ergebnis

    @app.post("/mahnlauf", dependencies=[Depends(recht("finanzen"))])
    def mahnlauf(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        """Erzeugt Freigaben je fälliger Mahnstufe (Erinnerung B, Mahnung C); verschickt selbst nichts."""
        v = forderungen.mahnlauf(s, m, app.state.uhr().date(), "mensch")
        s.commit()
        return v

    @app.get("/mahnungen", response_model=list[MahnungAus], dependencies=[Depends(recht("finanzen"))])
    def mahnungen(rechnung_id: str | None = None, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        abfrage = select(Mahnung).where(Mahnung.mandant_id == m.id)
        if rechnung_id:
            abfrage = abfrage.where(Mahnung.rechnung_id == rechnung_id)
        return s.scalars(abfrage.order_by(Mahnung.datum.desc(), Mahnung.stufe.desc())).all()

    @app.get("/mahnungen/{mahnung_id}/pdf", dependencies=[Depends(recht("finanzen"))])
    def mahnung_pdf(mahnung_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        mh = holen(s, Mahnung, mahnung_id, m)
        return Response(forderungen.mahnung_pdf(mh, m), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="Mahnung-{mh.stufe}-{mh.rechnung.nummer}.pdf"'})

    # --- Wartungsverträge ---------------------------------------------------------------------
    def vertrag_aus(v: Vertrag, heute: date) -> dict[str, Any]:
        return {"id": v.id, "kunde_id": v.kunde_id, "kunde": v.kunde.name, "bezeichnung": v.bezeichnung, "anlagen_ids": v.anlagen_ids,
                "leistungen": v.leistungen, "beginn": v.beginn, "laufzeit_monate": v.laufzeit_monate, "kuendigungsfrist_monate": v.kuendigungsfrist_monate,
                "verlaengerung_monate": v.verlaengerung_monate, "preis_jahr_netto": v.preis_jahr_netto, "ust_satz": v.ust_satz,
                "abrechnung_monate": v.abrechnung_monate, "naechste_abrechnung": v.naechste_abrechnung, "status": v.status,
                "gekuendigt_zum": v.gekuendigt_zum, "notiz": v.notiz, "laufzeitende": vertraege.laufzeitende(v, heute),
                "kuendigungstermin": vertraege.kuendigungstermin(v, heute)}

    @app.get("/vertraege")
    def vertraege_listen(status: str | None = None, kunde_id: str | None = None, m: Mandant = Depends(mandant_kontext),
                         s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        abfrage = select(Vertrag).where(Vertrag.mandant_id == m.id)
        if status:
            abfrage = abfrage.where(Vertrag.status == status)
        if kunde_id:
            abfrage = abfrage.where(Vertrag.kunde_id == kunde_id)
        heute = app.state.uhr().date()
        return [vertrag_aus(v, heute) for v in s.scalars(abfrage.order_by(Vertrag.naechste_abrechnung))]

    @app.post("/vertraege", status_code=201)
    def vertrag_anlegen(daten: VertragNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                        p: Prinzipal = Depends(recht("bearbeiten"))) -> dict[str, Any]:
        heute = app.state.uhr().date()
        v = fachlich(lambda: vertraege.vertrag_anlegen(s, m, daten.model_dump(exclude_none=True), heute, p.name))
        s.commit()
        return vertrag_aus(v, heute)

    @app.get("/vertraege/{vertrag_id}")
    def vertrag_lesen(vertrag_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return vertrag_aus(holen(s, Vertrag, vertrag_id, m), app.state.uhr().date())

    @app.post("/vertraege/{vertrag_id}/kuendigen")
    def vertrag_kuendigen(vertrag_id: str, daten: Kuendigung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                          p: Prinzipal = Depends(recht("bearbeiten"))) -> dict[str, Any]:
        v = holen(s, Vertrag, vertrag_id, m)
        heute = app.state.uhr().date()
        fachlich(lambda: vertraege.kuendigen(s, v, heute, daten.zum, p.name))
        s.commit()
        return vertrag_aus(v, heute)

    # --- Abwesenheiten ------------------------------------------------------------------------
    @app.get("/abwesenheiten", response_model=list[AbwesenheitAus])
    def abwesenheiten(ab: date | None = None, ressource_id: str | None = None, m: Mandant = Depends(mandant_kontext),
                      s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)):
        abfrage = select(Abwesenheit).where(Abwesenheit.mandant_id == m.id, Abwesenheit.bis >= (ab or app.state.uhr().date() - timedelta(days=30)))
        eigene = eigene_ressourcen(p, s, m)
        if eigene is not None:
            abfrage = abfrage.where(Abwesenheit.ressource_id.in_(eigene))
        if ressource_id:
            abfrage = abfrage.where(Abwesenheit.ressource_id == ressource_id)
        return s.scalars(abfrage.order_by(Abwesenheit.von)).all()

    @app.post("/abwesenheiten", response_model=AbwesenheitAus, status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def abwesenheit_anlegen(daten: AbwesenheitNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        holen(s, Ressource, daten.ressource_id, m)
        if daten.bis < daten.von:
            raise HTTPException(422, "bis liegt vor von")
        a = Abwesenheit(mandant_id=m.id, **daten.model_dump())
        s.add(a)
        s.commit()
        return a

    @app.delete("/abwesenheiten/{abwesenheit_id}", status_code=204, dependencies=[Depends(recht("bearbeiten"))])
    def abwesenheit_loeschen(abwesenheit_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)):
        s.delete(holen(s, Abwesenheit, abwesenheit_id, m))
        s.commit()
        return Response(status_code=204)

    # --- Anfrageeingang: Webformular (öffentlich, Token) und Postfach -------------------------
    @app.get("/anfragen", response_model=list[AnfrageAus], dependencies=[Depends(recht("bearbeiten"))])
    def anfragen_listen(status: str | None = None, limit: int = Query(200, le=2000), m: Mandant = Depends(mandant_kontext),
                        s: Session = Depends(sitzung)):
        abfrage = select(Anfrage).where(Anfrage.mandant_id == m.id)
        if status:
            abfrage = abfrage.where(Anfrage.status == status)
        return s.scalars(abfrage.order_by(Anfrage.eingegangen.desc()).limit(limit)).all()

    @app.get("/betrieb/webformular", dependencies=[Depends(recht("verwalten"))])
    def webformular_lesen(request: Request, m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        token = m.stammdaten.get("webformular_token")
        basis = m.stammdaten.get("basis_url") or str(request.base_url).rstrip("/")
        return {"aktiv": bool(token), "url": f"{basis}/anfragen/web/{token}" if token else None,
                "snippet": anfragen.webformular_snippet(basis, token) if token else None}

    @app.put("/betrieb/webformular", dependencies=[Depends(recht("verwalten"))])
    def webformular_setzen(request: Request, daten: WebformularEinstellung, m: Mandant = Depends(mandant_kontext),
                           s: Session = Depends(sitzung)) -> dict[str, Any]:
        """aktiv=true erzeugt (oder erneuert) den Token; aktiv=false schaltet das Formular ab."""
        stamm = dict(m.stammdaten)
        if daten.aktiv and (daten.erneuern or not stamm.get("webformular_token")):
            stamm["webformular_token"] = f"{m.id}.{anfragen.formular_token_erzeugen()}"
        if not daten.aktiv:
            stamm.pop("webformular_token", None)
        if daten.danke_url is not None:
            stamm["webformular_danke_url"] = daten.danke_url
        m.stammdaten = stamm
        s.commit()
        return webformular_lesen(request, m)

    formular_zaehler: dict[str, list[float]] = {}

    @app.post("/anfragen/web/{token}", include_in_schema=True)
    async def anfrage_webformular(token: str, request: Request, s: Session = Depends(sitzung)):
        """Öffentlich (kein Login): Ziel des Kontaktformulars auf der Website des Betriebs. Honeypot-Feld `website`,
        Drosselung je Token, idempotent je Formularkennung. Antwortet mit JSON oder leitet auf die Danke-Seite um."""
        import hmac
        import time
        mandant_id = token.split(".", 1)[0]
        if not mandant_id or len(mandant_id) > 32:
            raise HTTPException(404, "Formular unbekannt")
        mandant_binden(s, mandant_id)
        m = s.get(Mandant, mandant_id)
        erwartet = (m.stammdaten.get("webformular_token") if m else None) or ""
        if not erwartet or not hmac.compare_digest(erwartet, token):
            raise HTTPException(404, "Formular unbekannt")
        jetzt = time.monotonic()
        fenster = [t for t in formular_zaehler.get(token, []) if jetzt - t < 3600]
        if len(fenster) >= 60:
            raise HTTPException(429, "Zu viele Anfragen, bitte rufen Sie uns an")
        formular_zaehler[token] = fenster + [jetzt]
        if "json" in (request.headers.get("content-type") or ""):
            felder = await request.json()
        else:
            felder = dict(await request.form())
        felder = {k: (str(v).strip() if v is not None else None) for k, v in felder.items()}
        if felder.get("website"):  # Honeypot: Bots füllen das unsichtbare Feld
            return {"status": "angenommen"}
        kennung = felder.get("kennung") or f"web:{abs(hash((felder.get('telefon'), felder.get('email'), felder.get('nachricht'), app.state.uhr().strftime('%Y%m%d%H%M'))))}"
        if not (felder.get("telefon") or felder.get("email")):
            raise HTTPException(422, "Telefon oder E-Mail wird benötigt")
        anfragen.anfrage_verarbeiten(s, m, "web", kennung, felder.get("name"), felder.get("telefon"), felder.get("email"), felder.get("betreff"),
                                     felder.get("nachricht"), app.state.uhr(), felder.get("strasse"), felder.get("plz"), felder.get("ort"))
        s.commit()
        if m.stammdaten.get("webformular_danke_url") and "json" not in (request.headers.get("accept") or ""):
            return RedirectResponse(m.stammdaten["webformular_danke_url"], status_code=303)
        return {"status": "angenommen", "hinweis": "Vielen Dank, wir melden uns."}

    @app.get("/betrieb/posteingang", dependencies=[Depends(recht("verwalten"))])
    def posteingang_lesen(m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        return konfiguration_maskieren(m.integrationen.get("posteingang") or {})

    @app.put("/betrieb/posteingang", dependencies=[Depends(recht("verwalten"))])
    def posteingang_setzen(daten: Posteingang, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        bisher = m.integrationen.get("posteingang") or {}
        konfig = daten.model_dump()
        if konfig.get("passwort") in ("", "••••", None) and bisher.get("passwort"):
            konfig["passwort"] = bisher["passwort"]
        else:
            konfig = konfiguration_verschluesseln(konfig)
        m.integrationen = {**m.integrationen, "posteingang": konfig}
        s.commit()
        return konfiguration_maskieren(konfig)

    @app.get("/betrieb/scanner", dependencies=[Depends(recht("verwalten"))])
    def scanner_lesen(m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        return {**konfiguration_maskieren(m.integrationen.get("scanner") or {}), "uebergabekanaele": scanner.uebergabekanaele(m)}

    @app.put("/betrieb/scanner", dependencies=[Depends(recht("verwalten"))])
    def scanner_setzen(daten: ScannerEinstellung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        bisher = m.integrationen.get("scanner") or {}
        konfig = daten.model_dump()
        if konfig.get("passwort") in ("", "••••", None) and bisher.get("passwort"):
            konfig["passwort"] = bisher["passwort"]
        else:
            konfig = konfiguration_verschluesseln(konfig)
        m.integrationen = {**m.integrationen, "scanner": konfig}
        s.commit()
        return {**konfiguration_maskieren(konfig), "uebergabekanaele": scanner.uebergabekanaele(m)}

    @app.post("/posteingang/abrufen", dependencies=[Depends(recht("bearbeiten"))])
    def posteingang_abrufen(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, int]:
        konfig = m.integrationen.get("posteingang")
        if not konfig or not konfig.get("host"):
            raise HTTPException(409, "Kein Postfach hinterlegt (Einstellungen → Posteingang)")
        from core.geheimnisse import konfiguration_entschluesseln
        k = konfiguration_entschluesseln(konfig)
        try:
            postfach = anfragen.ImapPostfach(k["host"], k.get("benutzer", ""), k.get("passwort", ""), k.get("ordner", "INBOX"), int(k.get("port", 993)))
            ergebnis = anfragen.postfach_abrufen(s, m, postfach, app.state.uhr())
        except OSError as e:
            raise HTTPException(502, f"Postfach nicht erreichbar: {e}") from e
        s.commit()
        return ergebnis

    # --- Auswertungen und Tageslauf -----------------------------------------------------------
    @app.get("/auswertungen", dependencies=[Depends(recht("finanzen"))])
    def auswertungen(monate: int = Query(12, ge=1, le=36), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return management.auswertungen(s, m, app.state.uhr(), monate)

    @app.post("/tagesaufgaben", dependencies=[Depends(recht("bearbeiten"))])
    def tagesaufgaben_ausfuehren(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        ergebnis = tagesaufgaben.tagesaufgaben(s, m, app.state.uhr())
        s.commit()
        return ergebnis

    # --- Globale Suche, Checklisten, Förderung ------------------------------------------------
    @app.get("/suche")
    def suchen(q: str = Query(..., min_length=2, max_length=80), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
               p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        rechte = {r for r in ("lesen", "bearbeiten", "finanzen", "einsatz", "verwalten") if p.darf(r)}
        return {"q": q, "treffer": suche.suchen(s, m, q, rechte, eigene_ressourcen(p, s, m))}

    @app.get("/checklisten")
    def checklisten_liste() -> list[dict[str, Any]]:
        return checklisten.alle()

    @app.get("/checklisten/{anlagentyp}")
    def checkliste_lesen(anlagentyp: str) -> dict[str, Any]:
        return fachlich(lambda: checklisten.checkliste(anlagentyp))

    @app.get("/foerderung/heizungsarten")
    def foerderung_heizungsarten() -> dict[str, Any]:
        return {"heizungsarten": foerderung.HEIZUNGSARTEN, "stand": foerderung.STAND}

    @app.post("/foerderung/rechner")
    def foerderung_rechner(daten: FoerderAnfrage) -> dict[str, Any]:
        if daten.programm == "heizung":
            ergebnis = fachlich(lambda: foerderung.heizung(daten.kosten, daten.heizungsart, daten.wohneinheiten, daten.selbstnutzend, daten.alte_heizung_ersetzt,
                                                           daten.einkommen_unter_40k, daten.natuerliches_kaeltemittel, daten.erd_oder_wasserquelle, daten.biomasse_staubarm))
        else:
            ergebnis = fachlich(lambda: foerderung.einzelmassnahme(daten.kosten, daten.wohneinheiten, daten.isfp, daten.massnahme))
        return {**ergebnis, "angebotstext": foerderung.angebotstext(ergebnis)}

    # --- Großhandel: DATANORM, Lager, IDS-Connect ---------------------------------------------
    @app.post("/artikel/datanorm", dependencies=[Depends(recht("bearbeiten"))])
    async def datanorm_import(datei: UploadFile = File(...), lieferant: str = Form(...), aufschlag_prozent: str | None = Form(None),
                              trocken: bool = Form(False), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                              p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        inhalt = await datei.read()
        if len(inhalt) > 50_000_000:
            raise HTTPException(413, "DATANORM-Datei größer als 50 MB; bitte aufteilen")
        from decimal import Decimal, InvalidOperation
        try:
            aufschlag = Decimal(aufschlag_prozent.replace(",", ".")) if aufschlag_prozent not in (None, "") else None
        except InvalidOperation as e:
            raise HTTPException(422, "aufschlag_prozent ist keine Zahl") from e
        try:
            ergebnis = grosshandel.datanorm_importieren(s, m, inhalt, lieferant, aufschlag, trocken, p.name)
        except Exception as e:  # defekte Datei soll 422 liefern, nicht 500
            raise HTTPException(422, f"DATANORM-Datei nicht lesbar: {e}") from e
        if trocken:
            s.rollback()
        else:
            s.commit()
        return ergebnis

    @app.get("/lager")
    def lager_listen(lager: str | None = None, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        return grosshandel.bestand_listen(s, m, lager)

    @app.get("/lager/export.csv")
    def lager_export(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        return Response(grosshandel.bestand_csv(s, m), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="lagerbestand.csv"'})

    @app.get("/lager/bestellvorschlag")
    def lager_bestellvorschlag(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return grosshandel.bestellvorschlag(s, m)

    @app.post("/lager/buchen")
    def lager_buchen(daten: LagerBuchung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                     p: Prinzipal = Depends(recht("einsatz"))) -> dict[str, Any]:
        eigene = eigene_ressourcen(p, s, m)
        if eigene is not None and daten.lager not in eigene:
            raise HTTPException(403, "Monteure buchen nur auf das eigene Fahrzeuglager")
        b = fachlich(lambda: grosshandel.buchen(s, m, daten.artikel_id, daten.lager, daten.menge, daten.grund, daten.mindestbestand, p.name))
        s.commit()
        return {"artikel_id": b.artikel_id, "lager": b.lager, "menge": b.menge, "mindestbestand": b.mindestbestand}

    @app.post("/lager/warenkorb", response_class=HTMLResponse, dependencies=[Depends(recht("bearbeiten"))])
    def lager_warenkorb(daten: Warenkorb, m: Mandant = Depends(mandant_kontext)) -> HTMLResponse:
        """IDS-Connect: HTML-Seite mit Formular, die den Warenkorb an den Shop des Großhändlers übergibt."""
        shop = daten.shop_url or m.stammdaten.get("ids_shop_url")
        if not shop or not shop.startswith("https://"):
            raise HTTPException(422, "IDS-Shop-Adresse fehlt (Stammdatum ids_shop_url, https)")
        kennung = daten.kennung or f"{m.id[:8]}-{app.state.uhr().strftime('%Y%m%d%H%M')}"
        return HTMLResponse(grosshandel.ids_formular(shop, [p.model_dump() for p in daten.positionen], kennung))

    # --- Kundenportal (öffentlich, Token je Vorgang) -------------------------------------------
    def portal_fall(s: Session, fall_id: str, token: str | None) -> tuple[Servicefall, Mandant]:
        if not portal.gueltig(fall_id, token):
            raise HTTPException(404, "Vorgang nicht gefunden")
        f = s.get(Servicefall, fall_id)
        if f is None:
            raise HTTPException(404, "Vorgang nicht gefunden")
        mandant_binden(s, f.mandant_id)
        return f, s.get(Mandant, f.mandant_id)

    @app.get("/portal/", include_in_schema=False)
    def portal_seite() -> FileResponse:
        return FileResponse(UI_VERZEICHNIS / "portal.html", media_type="text/html")

    @app.get("/portal/portal.js", include_in_schema=False)
    def portal_skript() -> FileResponse:
        return FileResponse(UI_VERZEICHNIS / "portal.js", media_type="text/javascript")

    @app.get("/portal/faelle/{fall_id}")
    def portal_daten(fall_id: str, token: str | None = None, s: Session = Depends(sitzung)) -> dict[str, Any]:
        f, m = portal_fall(s, fall_id, token)
        return portal.daten(s, m, f, app.state.uhr())

    @app.post("/portal/faelle/{fall_id}/termine/{termin_id}")
    def portal_termin(fall_id: str, termin_id: str, daten: PortalAktion, token: str | None = None, s: Session = Depends(sitzung)) -> dict[str, Any]:
        f, m = portal_fall(s, fall_id, token)
        t = s.get(Termin, termin_id)
        if t is None or t.servicefall_id != f.id:
            raise HTTPException(404, "Termin nicht gefunden")
        if daten.aktion == "bestaetigen":
            ergebnis = fachlich(lambda: portal.termin_bestaetigen(s, m, f, t, app.state.uhr()))
        else:
            ergebnis = fachlich(lambda: portal.termin_absagen(s, m, f, t, app.state.uhr(), daten.grund))
        s.commit()
        return ergebnis

    @app.get("/portal/faelle/{fall_id}/alternativen")
    def portal_alternativen(fall_id: str, token: str | None = None, s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        f, m = portal_fall(s, fall_id, token)
        if not (f.status in ("eingegangen", "qualifiziert", "eingeplant") and not any(t.status == "bestaetigt" for t in f.termine)):
            return []
        return portal.alternativen(s, m, f, app.state.uhr())

    @app.post("/portal/faelle/{fall_id}/alternativen")
    def portal_alternative_waehlen(fall_id: str, daten: PortalWunschtermin, token: str | None = None, s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Kunde wählt eines der angebotenen Fenster: neuer Terminvorschlag (Freigabe B im Büro), alte Vorschläge werden zurückgezogen."""
        f, m = portal_fall(s, fall_id, token)
        if any(t.status == "bestaetigt" for t in f.termine):
            raise HTTPException(409, "Es gibt bereits einen bestätigten Termin; bitte zuerst absagen oder anrufen.")
        if als_utc(daten.beginn) < als_utc(app.state.uhr()):
            raise HTTPException(422, "Der Termin liegt in der Vergangenheit")
        t, _ = fachlich(lambda: termin_vorschlagen(s, m, f, TerminNeu(ressource_id=daten.ressource_id, beginn=daten.beginn, sachmittel_id=daten.sachmittel_id), "mensch"))
        ergebnis = portal.alternative_gewaehlt(s, m, f, t, app.state.uhr())
        s.commit()
        return ergebnis

    @app.get("/portal/faelle/{fall_id}/termine/{termin_id}.ics")
    def portal_termin_ics(fall_id: str, termin_id: str, token: str | None = None, s: Session = Depends(sitzung)) -> Response:
        f, m = portal_fall(s, fall_id, token)
        t = s.get(Termin, termin_id)
        if t is None or t.servicefall_id != f.id or t.status == "abgesagt":
            raise HTTPException(404, "Termin nicht gefunden")
        return Response(kalender.termin_ics(m, t), media_type="text/calendar; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="termin.ics"'})

    @app.get("/portal/faelle/{fall_id}/rechnungen/{rechnung_id}/girocode.png")
    def portal_girocode(fall_id: str, rechnung_id: str, token: str | None = None, s: Session = Depends(sitzung)) -> Response:
        """EPC-QR-Code zur Rechnung: Banking-App scannen, Überweisung ist vorausgefüllt (nur mit IBAN in den Stammdaten)."""
        f, m = portal_fall(s, fall_id, token)
        r = s.get(Rechnung, rechnung_id)
        if r is None or r.servicefall_id != f.id or r.art != "rechnung":
            raise HTTPException(404, "Rechnung nicht gefunden")
        png = rechnung_girocode(m, Decimal(r.brutto), f"Rechnung {r.nummer}")
        if png is None:
            raise HTTPException(404, "Keine Bankverbindung hinterlegt")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/portal/faelle/{fall_id}/bewertung")
    def portal_bewertung(fall_id: str, daten: PortalBewertung, token: str | None = None, s: Session = Depends(sitzung)) -> dict[str, Any]:
        f, m = portal_fall(s, fall_id, token)
        ergebnis = fachlich(lambda: portal.bewerten(s, m, f, daten.sterne, daten.text, app.state.uhr()))
        s.commit()
        return ergebnis

    @app.get("/bewertungen", dependencies=[Depends(recht("bearbeiten"))])
    def bewertungen_lesen(limit: int = Query(50, le=500), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return portal.bewertungen(s, m, limit)

    @app.get("/portal/faelle/{fall_id}/angebote/{angebot_id}.pdf")
    def portal_angebot_pdf(fall_id: str, angebot_id: str, token: str | None = None, s: Session = Depends(sitzung)) -> Response:
        f, m = portal_fall(s, fall_id, token)
        a = s.get(Angebot, angebot_id)
        if a is None or a.servicefall_id != f.id or a.status not in ("versendet", "angenommen"):
            raise HTTPException(404, "Angebot nicht gefunden")
        return Response(angebote.pdf(a, m), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{a.nummer}.pdf"'})

    @app.post("/portal/faelle/{fall_id}/rechnungen/{rechnung_id}/zahlung")
    def portal_zahlung(fall_id: str, rechnung_id: str, request: Request, token: str | None = None, s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Stripe-Checkout für eine offene Rechnung; Erfolg führt zurück ins Portal (?bezahlt=1), der Webhook bucht die Zahlung."""
        f, m = portal_fall(s, fall_id, token)
        r = s.get(Rechnung, rechnung_id)
        if r is None or r.servicefall_id != f.id or r.art != "rechnung":
            raise HTTPException(404, "Rechnung nicht gefunden")
        if r.status in ("bezahlt", "storniert"):
            raise HTTPException(409, "Diese Rechnung ist bereits bezahlt oder storniert")
        adapter = hub.adapter_fuer(m, "zahlung", "stripe", app.state.adapter_zusatz)
        if adapter is None:
            raise HTTPException(409, "Online-Zahlung ist für diesen Betrieb nicht eingerichtet")
        zurueck = portal.link(m, f.id) or f"{str(request.base_url).rstrip('/')}/portal/#{f.id}/{token}"
        def erzeugen():
            try:
                return adapter.checkout(r, m, zurueck.replace("/portal/#", "/portal/?bezahlt=1#"), zurueck, f.kunde.email)
            except (AdapterFehler, KeyError) as e:
                raise domaene.Domaenenfehler(f"Zahlungsdienst nicht erreichbar: {e}")
        sess = fachlich(erzeugen)
        ref = s.scalar(select(ExternalRef).where(ExternalRef.mandant_id == m.id, ExternalRef.entitaet == "rechnung", ExternalRef.entitaet_id == r.id, ExternalRef.zielsystem == "stripe"))
        if ref is None:
            s.add(ExternalRef(mandant_id=m.id, entitaet="rechnung", entitaet_id=r.id, zielsystem="stripe", externe_id=sess["id"]))
        else:
            ref.externe_id, ref.letzter_sync = sess["id"], app.state.uhr()
        domaene.protokollieren(s, m.id, "rechnung.zahlung_gestartet", "rechnung", r.id, "system", {"nummer": r.nummer, "session": sess["id"]}, f.kunde.name)
        s.commit()
        return {"url": sess["url"]}

    @app.get("/portal/faelle/{fall_id}/rechnungen/{rechnung_id}.pdf")
    def portal_rechnung_pdf(fall_id: str, rechnung_id: str, token: str | None = None, s: Session = Depends(sitzung)) -> Response:
        f, m = portal_fall(s, fall_id, token)
        r = s.get(Rechnung, rechnung_id)
        if r is None or r.servicefall_id != f.id:
            raise HTTPException(404, "Rechnung nicht gefunden")
        return Response(rechnung_pdf(r, m), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{r.nummer}.pdf"'})

    # --- Betriebswebsite (öffentlich, serverseitig gerendert) -------------------------------------
    @app.get("/betrieb/website", dependencies=[Depends(recht("bearbeiten"))])
    def website_status(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return website.status(s, m)

    @app.put("/betrieb/website", dependencies=[Depends(recht("verwalten")), Depends(feature("website"))])
    def website_einstellen(daten: WebsiteEinstellungen, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        werte = daten.model_dump(exclude_unset=True)
        k = fachlich(lambda: website.speichern(s, m, werte))
        domaene.protokollieren(s, m.id, "website.geaendert", "mandant", m.id, "mensch", {n: k.get(n) for n in ("aktiv", "slug", "domain") if n in werte}, p.name)
        s.commit()
        domain_cache.clear()
        return website.status(s, m)

    @app.post("/bewertungen/{ereignis_id}/veroeffentlichen", dependencies=[Depends(recht("verwalten"))])
    def bewertung_veroeffentlichen(ereignis_id: int, daten: BewertungVeroeffentlichen, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        ergebnis = fachlich(lambda: website.veroeffentlichen(s, m, ereignis_id, daten.veroeffentlicht))
        s.commit()
        return ergebnis

    def website_mandant(s: Session, slug: str, vorschau: bool = False) -> Mandant:
        m = website.mandant_nach_slug(s, slug)
        if m is None or (not website.konfig(m)["aktiv"] and not vorschau):
            raise HTTPException(404, "Website nicht gefunden")
        mandant_binden(s, m.id)
        return m

    @app.get("/web/domain-pruefen", include_in_schema=False)
    def website_domain_pruefen(domain: str, s: Session = Depends(system_sitzung)) -> Response:
        """Für Caddy on-demand TLS: 200, wenn ein Betrieb die Domain eingetragen und die Website aktiviert hat."""
        m = website.mandant_nach_domain(s, domain)
        if m is None or not website.konfig(m)["aktiv"]:
            raise HTTPException(404, "unbekannt")
        return Response(status_code=200)

    @app.get("/web/{slug}", include_in_schema=False, response_class=HTMLResponse)
    def website_start(slug: str, gesendet: int = 0, vorschau: int = 0, s: Session = Depends(system_sitzung)) -> HTMLResponse:
        m = website_mandant(s, slug, bool(vorschau))
        return HTMLResponse(website.startseite(s, m, bool(gesendet), bool(vorschau)), headers={"Cache-Control": "public, max-age=300"} if not vorschau else {})

    @app.get("/web/{slug}/impressum", include_in_schema=False, response_class=HTMLResponse)
    def website_impressum(slug: str, s: Session = Depends(system_sitzung)) -> HTMLResponse:
        return HTMLResponse(website.impressum(s, website_mandant(s, slug, True)))

    @app.get("/web/{slug}/datenschutz", include_in_schema=False, response_class=HTMLResponse)
    def website_datenschutz(slug: str, s: Session = Depends(system_sitzung)) -> HTMLResponse:
        return HTMLResponse(website.datenschutz(s, website_mandant(s, slug, True)))

    @app.get("/web/{slug}/sitemap.xml", include_in_schema=False)
    def website_sitemap(slug: str, s: Session = Depends(system_sitzung)) -> Response:
        return Response(website.sitemap(website_mandant(s, slug)), media_type="application/xml")

    @app.get("/web/{slug}/robots.txt", include_in_schema=False)
    def website_robots(slug: str, s: Session = Depends(system_sitzung)) -> Response:
        return Response(website.robots(website_mandant(s, slug, True)), media_type="text/plain")

    website_zaehler: dict[str, list[float]] = {}

    @app.post("/web/{slug}/anfrage", include_in_schema=False)
    async def website_anfrage(slug: str, request: Request, s: Session = Depends(system_sitzung)):
        """Kontaktformular ohne JavaScript: Honeypot, 30 Anfragen je Stunde und Betrieb, danach Umleitung mit Bestätigung."""
        m = website_mandant(s, slug)
        form = {k: str(v) for k, v in (await request.form()).items()}
        jetzt = time.monotonic()
        fenster = [x for x in website_zaehler.get(slug, []) if jetzt - x < 3600]
        if len(fenster) >= 30:
            raise HTTPException(429, "Zu viele Anfragen, bitte rufen Sie uns an")
        website_zaehler[slug] = fenster + [jetzt]
        ziel = "/" if website.konfig(m).get("domain") and request.headers.get("host", "").split(":")[0] != "localhost" and "/web/" not in str(request.headers.get("referer", "/web/")) else f"/web/{slug}"
        try:
            website.anfrage(s, m, form, app.state.uhr())
        except ValueError as e:
            if "json" in (request.headers.get("accept") or ""):
                raise HTTPException(422, str(e)) from e
            s.rollback()
            return HTMLResponse(website.startseite(s, m, False, False, str(e)), status_code=422)
        s.commit()
        if "json" in (request.headers.get("accept") or ""):
            return {"status": "angenommen"}
        return RedirectResponse(f"{ziel}?gesendet=1#kontakt", status_code=303)

    # --- Upsell -----------------------------------------------------------------------------------
    @app.get("/kunden/{kunde_id}/empfehlungen", dependencies=[Depends(recht("bearbeiten"))])
    def kunde_empfehlungen(kunde_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        empf = upsell.empfehlungen(s, m, k, app.state.uhr().date())
        return {"kunde_id": k.id, "empfehlungen": empf, "potenzial": f"{upsell.potenzial(empf):.2f}"}

    @app.get("/servicefaelle/{fall_id}/empfehlungen")
    def fall_empfehlungen(fall_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        """Für den Monteur im Rapport: Empfehlungen zum Kunden des Vorgangs, passend zum Anliegen."""
        f = holen(s, Servicefall, fall_id, m)
        eigener_fall(p, s, m, f)
        empf = upsell.empfehlungen(s, m, f.kunde, app.state.uhr().date(), f)
        return {"kunde_id": f.kunde_id, "empfehlungen": empf, "potenzial": f"{upsell.potenzial(empf):.2f}"}

    @app.post("/kunden/{kunde_id}/empfehlungen/{schluessel}/angebot", status_code=201, dependencies=[Depends(recht("einsatz"))])
    def empfehlung_angebot(kunde_id: str, schluessel: str, daten: UpsellAngebot, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                           p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        if daten.servicefall_id:
            eigener_fall(p, s, m, holen(s, Servicefall, daten.servicefall_id, m))
        a = fachlich(lambda: upsell.angebot_erstellen(s, m, k, schluessel, app.state.uhr().date(), "mensch", p.name, daten.servicefall_id, daten.versenden and "bearbeiten" in p.rechte))
        s.commit()
        return {"angebot_id": a.id, "nummer": a.nummer, "brutto": a.brutto, "status": a.status, "versand_beantragt": bool(daten.versenden and "bearbeiten" in p.rechte)}

    @app.post("/kunden/{kunde_id}/empfehlungen/wartungsvertrag/vertrag", status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def empfehlung_vertrag(kunde_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        v = fachlich(lambda: upsell.vertrag_erstellen(s, m, k, app.state.uhr().date(), p.name))
        s.commit()
        return {"vertrag_id": v.id, "preis_jahr_netto": v.preis_jahr_netto, "beginn": v.beginn.isoformat()}

    @app.post("/kunden/{kunde_id}/empfehlungen/{schluessel}/ablehnen", dependencies=[Depends(recht("einsatz"))])
    def empfehlung_ablehnen(kunde_id: str, schluessel: str, daten: UpsellAblehnung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                            p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        upsell.ablehnen(s, m, k, schluessel, daten.grund, "mensch", p.name)
        s.commit()
        return {"schluessel": schluessel, "unterdrueckt_tage": upsell.UNTERDRUECKUNG_TAGE}

    @app.get("/upsell/uebersicht", dependencies=[Depends(recht("bearbeiten")), Depends(feature("upsell"))])
    def upsell_uebersicht(limit: int = Query(30, le=200), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return upsell.uebersicht(s, m, app.state.uhr().date(), limit)

    @app.get("/upsell/pakete")
    def upsell_pakete(m: Mandant = Depends(mandant_kontext)) -> list[dict[str, Any]]:
        return [vars(p) for p in upsell.pakete(branchen.branche(m))]

    @app.post("/portal/faelle/{fall_id}/empfehlungen/{schluessel}/anfordern")
    def portal_empfehlung_anfordern(fall_id: str, schluessel: str, token: str | None = None, s: Session = Depends(sitzung)) -> dict[str, Any]:
        f, m = portal_fall(s, fall_id, token)
        ergebnis = fachlich(lambda: upsell.anfordern(s, m, f, schluessel, app.state.uhr()))
        s.commit()
        return ergebnis

    @app.post("/portal/faelle/{fall_id}/angebote/{angebot_id}/annehmen")
    def portal_angebot_annehmen(fall_id: str, angebot_id: str, token: str | None = None, s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Kunde nimmt ein versendetes Angebot im Portal an: Auftrag entsteht, Auftragsbestätigung geht als Freigabe B raus."""
        f, m = portal_fall(s, fall_id, token)
        a = s.get(Angebot, angebot_id)
        if a is None or a.kunde_id != f.kunde_id or a.status != "versendet":
            raise HTTPException(404, "Angebot nicht gefunden oder nicht mehr offen")
        fall = fachlich(lambda: angebote.annehmen(s, m, a, app.state.uhr().date(), f.kunde.name))
        portal._kommunikation(s, f, f"Kunde hat Angebot {a.nummer} ({a.brutto.replace('.', ',')} €) im Portal angenommen", app.state.uhr())
        hinweises = hinweise.senden(s, m, "info", "Angebot im Portal angenommen", f"{f.kunde.name}: {a.nummer} über {a.brutto.replace('.', ',')} €", f"angenommen:{a.id}", "angebote")
        s.commit()
        return {"angebot": {"id": a.id, "nummer": a.nummer, "status": a.status}, "servicefall_id": fall.id, "hinweis": "Vielen Dank für Ihren Auftrag! Wir melden uns zur Terminabstimmung.", "benachrichtigt": hinweises}

    @app.get("/servicefaelle/{fall_id}/portal")
    def portal_link(fall_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        f = holen(s, Servicefall, fall_id, m)
        eigener_fall(p, s, m, f)
        return {"link": portal.link(m, f.id), "pfad": f"/portal/#{f.id}/{portal.token(f.id)}", "basis_url_gesetzt": bool(m.stammdaten.get("basis_url"))}

    # --- DATEV-Datenservices: Anmeldung (OAuth2 Authorization Code) ------------------------------
    def datev_konfig(m: Mandant) -> tuple[str, dict[str, Any]]:
        for ziel in ("kanzlei", "finanz"):
            konfig = (m.integrationen.get(ziel) or {}).get("datev")
            if konfig:
                return ziel, konfiguration_entschluesseln(konfig)
        raise HTTPException(409, "DATEV ist noch nicht konfiguriert (Einstellungen → Steuerkanzlei oder Buchhaltung → datev)")

    @app.get("/integrationen/datev/anmeldung", dependencies=[Depends(recht("verwalten"))])
    def datev_anmeldung(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        basis = m.stammdaten.get("basis_url")
        if not basis:
            raise HTTPException(409, "Stammdatum basis_url fehlt (öffentliche Adresse für den DATEV-Rückruf)")
        ziel, konfig = datev_konfig(m)
        verifier, challenge = datev_api.pkce()
        redirect = f"{basis.rstrip('/')}/integrationen/datev/rueckruf"
        url = fachlich(lambda: datev_api.anmelde_url(konfig, m.id, redirect, challenge))
        block = {**(m.integrationen[ziel]["datev"]), "pkce_verifier": verifier}
        m.integrationen = {**m.integrationen, ziel: {**m.integrationen[ziel], "datev": block}}
        s.commit()
        return {"url": url, "redirect_uri": redirect, "umgebung": konfig.get("umgebung", "sandbox")}

    @app.get("/integrationen/datev/rueckruf", include_in_schema=False, response_class=HTMLResponse)
    def datev_rueckruf(code: str | None = None, state: str | None = None, error: str | None = None, error_description: str | None = None,
                       s: Session = Depends(sitzung)) -> HTMLResponse:
        seite = "<!doctype html><html lang='de'><meta charset='utf-8'><title>DATEV-Verbindung</title><body style='font-family:system-ui;padding:2rem'>{}</body></html>"
        mandant_id = datev_api.zustand_pruefen(state)
        if error or not code or not mandant_id:
            return HTMLResponse(seite.format(f"<h1>DATEV-Verbindung fehlgeschlagen</h1><p>{html.escape(error_description or error or 'Ungültiger Rückruf')}</p>"), status_code=400)
        mandant_binden(s, mandant_id)
        m = s.get(Mandant, mandant_id)
        if m is None:
            return HTMLResponse(seite.format("<h1>Betrieb unbekannt</h1>"), status_code=404)
        ziel, konfig = datev_konfig(m)
        redirect = f"{m.stammdaten.get('basis_url', '').rstrip('/')}/integrationen/datev/rueckruf"
        try:
            token = datev_api.code_einloesen(konfig, code, redirect, konfig.get("pkce_verifier"), app.state.datev_transport)
        except Exception as e:  # Fehlertext dem Nutzer zeigen, kein 500
            return HTMLResponse(seite.format(f"<h1>DATEV-Verbindung fehlgeschlagen</h1><p>{html.escape(str(e))}</p>"), status_code=502)
        block = {k: v for k, v in m.integrationen[ziel]["datev"].items() if k != "pkce_verifier"}
        block.update(konfiguration_verschluesseln({"refresh_token": token["refresh_token"]}))
        m.integrationen = {**m.integrationen, ziel: {**m.integrationen[ziel], "datev": block}}
        domaene.protokollieren(s, m.id, "integration.datev_verbunden", "mandant", m.id, "mensch", {"ziel": ziel, "umgebung": konfig.get("umgebung", "sandbox")})
        s.commit()
        return HTMLResponse(seite.format("<h1>DATEV-Verbindung hergestellt</h1><p>Sie können dieses Fenster schließen. Belege und Buchungsstapel werden ab jetzt über die DATEV-Datenservices übertragen.</p>"))

    # --- Branchenprofile, Kundenkartei, No-Show ---------------------------------------------------
    @app.get("/branchen")
    def branchen_liste() -> list[dict[str, Any]]:
        return branchen.alle()

    @app.get("/betrieb/branche")
    def branche_lesen(m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        return branchen.profil(m)

    @app.put("/betrieb/branche", dependencies=[Depends(recht("verwalten"))])
    def branche_setzen(daten: BrancheSetzen, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                       p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        ergebnis = fachlich(lambda: branchen.umstellen(s, m, daten.branche.lower(), daten.leistungen_anlegen, p.name))
        s.commit()
        return {**ergebnis, "profil": branchen.profil(m)}

    @app.get("/betrieb/einrichtung", dependencies=[Depends(recht("bearbeiten"))])
    def einrichtung_status(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Fortschritt der Einrichtung mit den nächsten Schritten (für Cockpit-Banner und Assistent)."""
        return einrichtung.status(s, m)

    @app.post("/betrieb/einrichten", dependencies=[Depends(recht("verwalten"))])
    def betrieb_einrichten(daten: Einrichtung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                           p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        """Einrichtungsassistent: Branche, Betriebsdaten, Team, Sachmittel, Leistungskatalog und Online-Buchung in einem Schritt."""
        werte = daten.model_dump(exclude_none=True)
        if daten.branche:
            werte["branche"] = daten.branche.lower()
        if daten.stammdaten is not None:
            werte["stammdaten"] = daten.stammdaten.model_dump(exclude_none=True)
        ergebnis = fachlich(lambda: einrichtung.einrichten(s, m, werte, p.name))
        s.commit()
        return {**ergebnis, "profil": branchen.profil(m)}

    @app.post("/kunden/import", dependencies=[Depends(recht("verwalten"))])
    async def kunden_importieren(datei: UploadFile = File(...), pruefen: bool = Form(False), trennzeichen: str | None = Form(None),
                                 m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        """CSV-Import aus der bisherigen Software (Spalten werden erkannt; Kennzeichen legt Fahrzeuge an). pruefen=true zeigt nur die Vorschau."""
        roh = await datei.read()
        if len(roh) > 20 * 1024 * 1024:
            raise HTTPException(413, "Datei größer als 20 MB")
        try:
            text = roh.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = roh.decode("latin-1")
        ergebnis = fachlich(lambda: einrichtung.kunden_import(s, m, text, trennzeichen or None, pruefen, akteur_name=p.name))
        if pruefen:
            s.rollback()
        else:
            s.commit()
        return ergebnis

    @app.get("/voice/kontext", dependencies=[Depends(recht("bearbeiten"))])
    def voice_kontext(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Branchenkontext für den Sprachagenten: Rolle, Leistungskatalog mit Dauer, Hinweise, Begriffe."""
        kontext = branchen.prompt(m)
        kontext["leistungen"] = [{"id": a.id, "nummer": a.nummer, "bezeichnung": a.bezeichnung, "dauer_minuten": a.dauer_minuten or 60,
                                  "qualifikation": a.qualifikation} for a in branchen.leistungen(s, m)] or kontext["leistungen"]
        kontext["betrieb"] = m.name
        return kontext

    @app.put("/kunden/{kunde_id}/kartei", response_model=KundeAus, dependencies=[Depends(recht("bearbeiten"))])
    def kartei_setzen(kunde_id: str, daten: KarteiAenderung, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                      p: Prinzipal = Depends(prinzipal)):
        k = holen(s, Kunde, kunde_id, m)
        felder = fachlich(lambda: branchen.kartei_pruefen(m, k, daten.felder))
        k.kartei = felder
        domaene.protokollieren(s, m.id, "kunde.kartei", "kunde", k.id, "mensch", {"felder": sorted(felder)}, p.name)
        s.commit()
        return k

    @app.post("/termine/{termin_id}/nicht-erschienen", response_model=TerminAus, dependencies=[Depends(recht("bearbeiten"))])
    def termin_nicht_erschienen(termin_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                                p: Prinzipal = Depends(prinzipal)):
        """No-Show: Termin markieren, Zähler am Kunden erhöhen, Vorgang zurück auf „qualifiziert“ (neuer Termin nötig)."""
        t = holen(s, Termin, termin_id, m)
        if t.status == "nicht_erschienen":
            return t
        if als_utc(t.beginn) > als_utc(app.state.uhr()):
            raise HTTPException(409, "Termin liegt in der Zukunft")
        t.status = "nicht_erschienen"
        f = t.servicefall
        f.kunde.nicht_erschienen = (f.kunde.nicht_erschienen or 0) + 1
        domaene.protokollieren(s, m.id, "termin.nicht_erschienen", "termin", t.id, "mensch", {"servicefall_id": f.id, "zaehler": f.kunde.nicht_erschienen}, p.name)
        if f.status == "eingeplant" and not any(x.status in ("vorgeschlagen", "bestaetigt") for x in f.termine):
            domaene.uebergang(s, f, "qualifiziert", "mensch", "Kunde nicht erschienen", p.name)
        s.commit()
        return t

    # --- Gutscheine ---------------------------------------------------------------------------------
    @app.get("/gutscheine", dependencies=[Depends(recht("finanzen"))])
    def gutscheine_liste(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return gutscheine.uebersicht(s, m, app.state.uhr().date())

    @app.post("/gutscheine", status_code=201, dependencies=[Depends(recht("finanzen"))])
    def gutschein_ausstellen(daten: GutscheinNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                             p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        if daten.kunde_id:
            holen(s, Kunde, daten.kunde_id, m)
        g = fachlich(lambda: gutscheine.ausstellen(s, m, daten.wert, app.state.uhr().date(), daten.kunde_id, daten.empfaenger, akteur_name=p.name))
        s.commit()
        return {"id": g.id, "code": g.code, "wert": g.wert, "restwert": g.restwert, "gueltig_bis": g.gueltig_bis.isoformat(), "status": g.status}

    @app.get("/gutscheine/{code}")
    def gutschein_pruefen(code: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        g = gutscheine.finden(s, m, code)
        if g is None:
            raise HTTPException(404, "Gutschein unbekannt")
        return {"id": g.id, "code": g.code, "wert": g.wert, "restwert": g.restwert, "status": g.status, "gueltig_bis": g.gueltig_bis.isoformat(),
                "einloesungen": g.einloesungen, "empfaenger": g.empfaenger}

    @app.post("/gutscheine/{code}/einloesen", dependencies=[Depends(recht("finanzen"))])
    def gutschein_einloesen(code: str, daten: GutscheinEinloesen, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                            p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        if daten.rechnung_id:
            holen(s, Rechnung, daten.rechnung_id, m)
        g = fachlich(lambda: gutscheine.einloesen(s, m, code, daten.betrag, app.state.uhr().date(), daten.rechnung_id, p.name))
        s.commit()
        return {"code": g.code, "restwert": g.restwert, "status": g.status, "eingeloest": g.einloesungen[-1]["betrag"]}

    @app.post("/gutscheine/{code}/storno", dependencies=[Depends(recht("finanzen"))])
    def gutschein_storno(code: str, grund: str | None = None, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                         p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        g = gutscheine.finden(s, m, code)
        if g is None:
            raise HTTPException(404, "Gutschein unbekannt")
        fachlich(lambda: gutscheine.stornieren(s, m, g, grund, p.name))
        s.commit()
        return {"code": g.code, "status": g.status}

    @app.get("/gutscheine/{code}/pdf", dependencies=[Depends(recht("finanzen"))])
    def gutschein_pdf(code: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        g = gutscheine.finden(s, m, code)
        if g is None:
            raise HTTPException(404, "Gutschein unbekannt")
        return Response(gutscheine.pdf(g, m), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="gutschein-{g.code}.pdf"'})

    # --- Online-Terminbuchung (öffentlich, Betriebs-Token) ------------------------------------------
    buchung_zaehler: dict[str, list[float]] = {}

    def buchung_mandant(s: Session, token: str) -> Mandant:
        import hmac
        mandant_id = token.split(".", 1)[0]
        if not mandant_id or len(mandant_id) > 32:
            raise HTTPException(404, "Buchungsseite unbekannt")
        mandant_binden(s, mandant_id)
        m = s.get(Mandant, mandant_id)
        erwartet = (m.stammdaten.get("buchung_token") if m else None) or ""
        if not erwartet or not hmac.compare_digest(erwartet, token):
            raise HTTPException(404, "Buchungsseite unbekannt")
        return m

    @app.get("/betrieb/buchung", dependencies=[Depends(recht("verwalten"))])
    def buchung_lesen(m: Mandant = Depends(mandant_kontext)) -> dict[str, Any]:
        token = m.stammdaten.get("buchung_token")
        return {"aktiv": bool(token), "token": token, "pfad": f"/buchung/#{token}" if token else None, "link": buchung.link(m),
                "leistungen": branchen.profil(m)["leistungen"]}

    @app.put("/betrieb/buchung", dependencies=[Depends(recht("verwalten"))])
    def buchung_setzen(aktiv: bool = True, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        st = dict(m.stammdaten)
        if aktiv and not st.get("buchung_token"):
            st["buchung_token"] = f"{m.id}.{buchung.token_erzeugen()}"
        elif not aktiv:
            st.pop("buchung_token", None)
        m.stammdaten = st
        s.commit()
        return {"aktiv": bool(st.get("buchung_token")), "pfad": f"/buchung/#{st['buchung_token']}" if st.get("buchung_token") else None, "link": buchung.link(m)}

    @app.get("/buchung/", include_in_schema=False)
    def buchung_seite() -> FileResponse:
        return FileResponse(UI_VERZEICHNIS / "buchung.html", media_type="text/html")

    @app.get("/buchung/buchung.js", include_in_schema=False)
    def buchung_skript() -> FileResponse:
        return FileResponse(UI_VERZEICHNIS / "buchung.js", media_type="text/javascript")

    @app.get("/buchung/{token}")
    def buchung_angebot(token: str, s: Session = Depends(sitzung)) -> dict[str, Any]:
        return buchung.angebot(s, buchung_mandant(s, token))

    @app.get("/buchung/{token}/zeiten")
    def buchung_zeiten(token: str, leistung_id: str, ressource_id: str | None = None, ab: datetime | None = None,
                       s: Session = Depends(sitzung)) -> dict[str, Any]:
        m = buchung_mandant(s, token)
        le = fachlich(lambda: buchung.leistung_laden(s, m, leistung_id))
        return buchung.zeiten(s, m, le, ab or app.state.uhr(), ressource_id)

    @app.post("/buchung/{token}", status_code=201)
    def buchung_anlegen(token: str, daten: BuchungNeu, s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Öffentlich: Termin buchen. Erzeugt Kunde (Dublette über Rufnummer), Vorgang mit Leistung als Anliegen und den
        Termin als Vorschlag; Freigabe termin_bestaetigen (Klasse B) macht ihn verbindlich, die Terminbestätigung geht
        mit Portal-Link an den Kunden."""
        m = buchung_mandant(s, token)
        if daten.website:
            return {"status": "angenommen"}
        jetzt = time.monotonic()
        fenster = [x for x in buchung_zaehler.get(token, []) if jetzt - x < 3600]
        if len(fenster) >= 60:
            raise HTTPException(429, "Zu viele Buchungen, bitte rufen Sie uns an")
        buchung_zaehler[token] = fenster + [jetzt]
        if not (daten.telefon or daten.email):
            raise HTTPException(422, "Rufnummer oder E-Mail wird benötigt")
        le = fachlich(lambda: buchung.leistung_laden(s, m, daten.leistung_id))
        if als_utc(daten.beginn) < als_utc(app.state.uhr()) + timedelta(hours=int(m.stammdaten.get("buchung_vorlauf_stunden", 2) or 2)):
            raise HTTPException(409, "Zeit liegt zu kurzfristig oder in der Vergangenheit")
        k, neu = buchung.kunde_finden_oder_anlegen(s, m, daten.name, daten.telefon, daten.email, daten.strasse, daten.plz, daten.ort, daten.einwilligung_email)
        if neu:
            domaene.protokollieren(s, m.id, "kunde.angelegt", "kunde", k.id, "system", {"quelle": "buchung"})
        anliegen = le.bezeichnung + (f" – {daten.hinweis.strip()}" if daten.hinweis else "")
        f = servicefall_erzeugen(s, m, ServicefallNeu(kunde_id=k.id, anliegen=anliegen, quelle="web", benoetigte_qualifikation=le.qualifikation, sachmittel=le.sachmittel,
                                                      dauer_minuten=max(15, le.dauer_minuten or 60), dringlichkeit="normal"), "system")
        t, freigabe = fachlich(lambda: termin_vorschlagen(s, m, f, TerminNeu(ressource_id=daten.ressource_id, beginn=daten.beginn), "system"))
        s.commit()
        return {"servicefall_id": f.id, "termin_id": t.id, "beginn": t.beginn.isoformat(), "verbindlich": freigabe.status == "freigegeben",
                "portal": f"/portal/#{f.id}/{portal.token(f.id)}", "portal_link": portal.link(m, f.id),
                "hinweis": "Ihr Termin ist bestätigt." if freigabe.status == "freigegeben" else "Ihr Termin ist vorgemerkt; wir bestätigen ihn in Kürze."}

    # --- Runde 5: Optimierung, Warteliste, Risiko, Prognose, Schätzung, Dubletten -----------------
    @app.get("/plantafel/optimieren", dependencies=[Depends(recht("bearbeiten"))])
    def plan_optimieren(tag: date | None = None, toleranz_minuten: int = 0, iterationen: int = 1500, m: Mandant = Depends(mandant_kontext),
                        s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Tagesplan als VRPTW: Vorschlag mit Begründung; bestätigte Termine nur innerhalb der Toleranz um die zugesagte Zeit."""
        tag = tag or app.state.uhr().astimezone(ZEITZONE).date()
        return optimierung.vorschlag(s, m, tag, router_fuer(m), max(0, min(240, toleranz_minuten)), max(100, min(5000, iterationen)))

    @app.post("/plantafel/optimieren/umsetzen", dependencies=[Depends(recht("bearbeiten"))])
    def plan_umsetzen(daten: PlanUmsetzen, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        ergebnis = fachlich(lambda: optimierung.umsetzen(s, m, daten.plan, wer(p, None)))
        s.commit()
        return ergebnis

    @app.get("/warteliste", dependencies=[Depends(recht("bearbeiten"))])
    def warteliste_lesen(tage: int = 7, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        ergebnis = warteliste.uebersicht(s, m, app.state.uhr(), max(1, min(28, tage)))
        s.commit()   # Fristprüfung kann Angebote verfallen lassen und weitergeben
        return ergebnis

    @app.post("/servicefaelle/{fall_id}/warteliste", status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def warteliste_eintragen(fall_id: str, daten: WartelisteNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                             p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        f = holen(s, Servicefall, fall_id, m)
        e = fachlich(lambda: warteliste.eintragen(s, m, f, daten.wunsch_von, daten.wunsch_bis, daten.tageszeiten, daten.wochentage, daten.notiz, wer(p, None)))
        s.commit()
        return {"id": e.id, "status": e.status, "prioritaet": e.prioritaet, "dauer_minuten": e.dauer_minuten}

    @app.post("/warteliste/{eintrag_id}/zurueckziehen", dependencies=[Depends(recht("bearbeiten"))])
    def warteliste_zurueck(eintrag_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        e = holen(s, Wartelisteneintrag, eintrag_id, m)
        warteliste.zuruecknehmen(s, m, e, wer(p, None))
        s.commit()
        return {"id": e.id, "status": e.status}

    @app.post("/warteliste/anbieten", dependencies=[Depends(recht("bearbeiten"))])
    def warteliste_anbieten(ressource_id: str, beginn: datetime, ende: datetime, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        """Lücke von Hand anbieten (z. B. aus der Lückenliste)."""
        holen(s, Ressource, ressource_id, m)
        ergebnis = warteliste.nachbesetzen(s, m, ressource_id, beginn, ende, app.state.uhr())
        s.commit()
        return ergebnis or {"hinweis": "Kein passender Eintrag auf der Warteliste"}

    @app.post("/termine/{termin_id}/absagen", response_model=TerminAus, dependencies=[Depends(recht("bearbeiten"))])
    def termin_absagen_buero(termin_id: str, daten: TerminAbsage, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                             p: Prinzipal = Depends(prinzipal)):
        """Absage durch das Büro; die Lücke wird der Warteliste angeboten."""
        t = holen(s, Termin, termin_id, m)
        if t.status == "abgesagt":
            return t
        t.status = "abgesagt"
        f = t.servicefall
        domaene.protokollieren(s, m.id, "termin.abgesagt", "termin", t.id, "mensch", {"servicefall_id": f.id, "grund": daten.grund, "quelle": "buero"}, wer(p, None))
        domaene.outbox_schreiben(s, m.id, "fachsoftware", "termin_upsert", {"termin_id": t.id}, f"termin_upsert:{t.id}:abgesagt")
        if f.status == "eingeplant" and not any(x.status in ("vorgeschlagen", "bestaetigt") for x in f.termine):
            domaene.uebergang(s, f, "qualifiziert", "mensch", daten.grund or "Termin abgesagt", wer(p, None))
        if daten.nachbesetzen:
            warteliste.nachbesetzen(s, m, t.ressource_id, t.beginn, t.ende, app.state.uhr())
        s.commit()
        return t

    @app.get("/risiko", dependencies=[Depends(recht("bearbeiten"))])
    def risiko_portfolio(limit: int = 50, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return risiko.portfolio(s, m, app.state.uhr().date(), max(1, min(500, limit)))

    @app.get("/risiko/anlagen/{anlage_id}", dependencies=[Depends(recht("bearbeiten"))])
    def risiko_anlage(anlage_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        a = holen(s, Anlage, anlage_id, m)
        heute = app.state.uhr().date()
        return risiko.bewerten(s, a, heute, risiko.kalibrierung(s, m, heute))

    @app.get("/prognose/auslastung", dependencies=[Depends(recht("bearbeiten"))])
    def prognose_auslastung(wochen: int = 8, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return prognose.auslastung(s, m, app.state.uhr().date(), max(1, min(26, wochen)))

    @app.get("/schaetzung/dauer", dependencies=[Depends(recht("bearbeiten"))])
    def schaetzung_dauer(anliegen: str, anlagentyp: str | None = None, dringlichkeit: str | None = None, m: Mandant = Depends(mandant_kontext),
                         s: Session = Depends(sitzung)) -> dict[str, Any]:
        return schaetzung.schaetzen(s, m, anliegen, anlagentyp, dringlichkeit)

    @app.get("/schaetzung/uebersicht", dependencies=[Depends(recht("bearbeiten"))])
    def schaetzung_uebersicht(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        return schaetzung.uebersicht(s, m)

    @app.get("/dubletten", dependencies=[Depends(recht("verwalten"))])
    def kunden_dubletten(schwelle: float = 0.6, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        return dubletten.kandidaten(s, m, max(0.3, min(1.0, schwelle)))

    @app.post("/kunden/{kunde_id}/zusammenfuehren", dependencies=[Depends(recht("verwalten"))])
    def kunden_zusammenfuehren(kunde_id: str, daten: Zusammenfuehren, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                               p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        ziel, quelle = holen(s, Kunde, kunde_id, m), holen(s, Kunde, daten.quelle_id, m)
        ergebnis = fachlich(lambda: dubletten.zusammenfuehren(s, m, ziel, quelle, wer(p, None)))
        s.commit()
        return ergebnis

    # --- Runde 6: Lebenszyklus, Dienstplan, Zähler, Garantie, Qualifikationen ----------------
    @app.get("/anlagen/{anlage_id}/bauteile")
    def bauteile(anlage_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        a = holen(s, Anlage, anlage_id, m)
        return lebenszyklus.anlage_uebersicht(s, m, a, app.state.uhr().date())

    @app.post("/anlagen/{anlage_id}/bauteile/anlegen", dependencies=[Depends(recht("bearbeiten"))])
    def bauteile_anlegen(anlage_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        a = holen(s, Anlage, anlage_id, m)
        neu = lebenszyklus.bauteile_anlegen(s, m, a, wer(p, None))
        s.commit()
        return {"angelegt": len(neu), **lebenszyklus.anlage_uebersicht(s, m, a, app.state.uhr().date())}

    @app.post("/anlagen/{anlage_id}/bauteile", status_code=201, dependencies=[Depends(recht("bearbeiten"))])
    def bauteil_anlegen(anlage_id: str, daten: BauteilNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        a = holen(s, Anlage, anlage_id, m)
        if daten.pflicht and not daten.intervall_monate:
            raise HTTPException(422, "Prüfpflichten brauchen ein Intervall")
        if not daten.pflicht and not daten.nutzungsdauer_jahre:
            raise HTTPException(422, "Bauteile brauchen eine Nutzungsdauer")
        b = Bauteil(mandant_id=m.id, anlage_id=a.id, **daten.model_dump())
        s.add(b)
        s.commit()
        return lebenszyklus._eintrag(b, app.state.uhr().date())

    @app.post("/bauteile/{bauteil_id}/erneuert", dependencies=[Depends(recht("einsatz"))])
    def bauteil_erneuert(bauteil_id: str, daten: BauteilErneuert, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                         p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        b = holen(s, Bauteil, bauteil_id, m)
        heute = app.state.uhr().date()
        fachlich(lambda: lebenszyklus.erneuern(s, m, b, daten.am or heute, daten.kosten, daten.notiz, wer(p, None)))
        s.commit()
        return lebenszyklus._eintrag(b, heute)

    @app.delete("/bauteile/{bauteil_id}", status_code=204, dependencies=[Depends(recht("bearbeiten"))])
    def bauteil_loeschen(bauteil_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        b = holen(s, Bauteil, bauteil_id, m)
        s.delete(b)
        s.commit()
        return Response(status_code=204)

    @app.get("/kunden/{kunde_id}/erneuerungskalender")
    def erneuerungskalender(kunde_id: str, jahre: int = Query(10, ge=1, le=30), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        return lebenszyklus.kalender(s, m, k, app.state.uhr().date(), jahre)

    @app.get("/lebenszyklus/faellig", dependencies=[Depends(recht("bearbeiten"))])
    def lebenszyklus_faellig(vorlauf_tage: int = Query(90, ge=0, le=730), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        return lebenszyklus.faellig(s, m, app.state.uhr().date(), vorlauf_tage)

    @app.get("/lebenszyklus/katalog")
    def lebenszyklus_katalog(anlagentyp: str | None = None) -> dict[str, Any]:
        typen = [anlagentyp] if anlagentyp else list(lebenszyklus.KATALOG)
        return {t: [vars(e) for e in lebenszyklus.katalog(t)] for t in typen}

    @app.get("/dienstplan", dependencies=[Depends(recht("bearbeiten")), Depends(feature("dienstplan"))])
    def dienstplan_woche(von: date | None = None, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return dienstplan.woche(s, m, von or app.state.uhr().date())

    @app.get("/dienstplan/pruefung", dependencies=[Depends(recht("bearbeiten"))])
    def dienstplan_pruefung(von: date | None = None, tage: int = Query(28, ge=1, le=120), m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        start = von or app.state.uhr().date()
        return dienstplan.pruefen(s, m, start, start + timedelta(days=tage))

    @app.get("/dienstplan/vorschlag", dependencies=[Depends(recht("verwalten"))])
    def dienstplan_vorschlag(von: date | None = None, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return dienstplan.vorschlag(s, m, von or app.state.uhr().date())

    @app.post("/dienstplan/vorschlag", dependencies=[Depends(recht("verwalten"))])
    def dienstplan_uebernehmen(daten: DienstUebernahme, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        angelegt = fachlich(lambda: dienstplan.uebernehmen(s, m, daten.dienste, wer(p, None)))
        s.commit()
        return {"angelegt": len(angelegt)}

    @app.post("/dienste", status_code=201, dependencies=[Depends(recht("verwalten"))])
    def dienst_anlegen(daten: DienstNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        r = holen(s, Ressource, daten.ressource_id, m)
        d = fachlich(lambda: dienstplan.eintragen(s, m, r, daten.datum, daten.art, daten.schicht,
                                                   time_.fromisoformat(daten.beginn) if daten.beginn else None,
                                                   time_.fromisoformat(daten.ende) if daten.ende else None, daten.pause_minuten, daten.notiz, wer(p, None)))
        s.commit()
        return dienstplan._eintrag(d)

    @app.delete("/dienste/{dienst_id}", status_code=204, dependencies=[Depends(recht("verwalten"))])
    def dienst_loeschen(dienst_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> Response:
        d = holen(s, Dienst, dienst_id, m)
        s.delete(d)
        s.commit()
        return Response(status_code=204)

    @app.get("/dienstplan/schichten")
    def dienstplan_schichten() -> dict[str, Any]:
        return {k: {"beginn": b.strftime("%H:%M"), "ende": e.strftime("%H:%M"), "pause_minuten": p} for k, (b, e, p) in dienstplan.SCHICHTEN.items()}

    @app.get("/zeitkonto", dependencies=[Depends(recht("einsatz"))])
    def zeitkonto(ressource_id: str | None = None, von: date | None = None, bis: date | None = None, m: Mandant = Depends(mandant_kontext),
                  s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> list[dict[str, Any]]:
        heute = app.state.uhr().date()
        von = von or heute.replace(day=1)
        bis = bis or heute
        eigene = eigene_ressourcen(p, s, m)
        ressourcen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv, Ressource.art == "person").order_by(Ressource.name)).all()
        if ressource_id:
            ressourcen = [r for r in ressourcen if r.id == ressource_id]
        if eigene is not None:
            ressourcen = [r for r in ressourcen if r.id in eigene]
        return [dienstplan.zeitkonto(s, m, r, von, bis) for r in ressourcen]

    @app.get("/urlaub", dependencies=[Depends(recht("einsatz"))])
    def urlaubskonten(jahr: int | None = None, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> list[dict[str, Any]]:
        heute = app.state.uhr().date()
        konten = dienstplan.urlaubsuebersicht(s, m, jahr or heute.year, heute)
        eigene = eigene_ressourcen(p, s, m)
        return konten if eigene is None else [k for k in konten if k["ressource_id"] in eigene]

    @app.get("/kunden/{kunde_id}/zaehlerstaende")
    def zaehlerstaende(kunde_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        return {"staende": zaehler.liste(s, k), "arten": {a: {"bezeichnung": b, "einheit": e} for a, (b, e, _) in zaehler.ARTEN.items()}, **zaehler.analyse(s, m, k)}

    @app.post("/kunden/{kunde_id}/zaehlerstaende", status_code=201, dependencies=[Depends(recht("einsatz"))])
    def zaehlerstand_erfassen(kunde_id: str, daten: ZaehlerstandNeu, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung),
                              p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        a = holen(s, Anlage, daten.anlage_id, m) if daten.anlage_id else None
        z = fachlich(lambda: zaehler.erfassen(s, m, k, daten.art, daten.wert, daten.datum or app.state.uhr().date(), a, daten.quelle, daten.notiz, wer(p, None)))
        s.commit()
        return zaehler._aus(z)

    @app.get("/zaehler/auffaellig", dependencies=[Depends(recht("bearbeiten"))])
    def zaehler_auffaellig(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> list[dict[str, Any]]:
        return zaehler.auffaellige(s, m)

    @app.get("/servicefaelle/{fall_id}/garantie")
    def servicefall_garantie(fall_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung), p: Prinzipal = Depends(prinzipal)) -> dict[str, Any]:
        f = holen(s, Servicefall, fall_id, m)
        eigener_fall(p, s, m, f)
        return garantie.pruefen(s, m, f, app.state.uhr().date())

    @app.get("/kunden/{kunde_id}/gewaehrleistung")
    def kunde_gewaehrleistung(kunde_id: str, m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        k = holen(s, Kunde, kunde_id, m)
        heute = app.state.uhr().date()
        anlagen = s.scalars(select(Anlage).join(Objekt, Objekt.id == Anlage.objekt_id).where(Objekt.kunde_id == k.id)).all()
        return {"leistungen": garantie.eigene_leistungen(s, m, k, heute),
                "herstellergarantien": [g for g in (garantie.herstellergarantie(s, m, a, heute) for a in anlagen) if g]}

    @app.get("/qualifikationen/matrix", dependencies=[Depends(recht("bearbeiten"))])
    def qualifikationsmatrix(m: Mandant = Depends(mandant_kontext), s: Session = Depends(sitzung)) -> dict[str, Any]:
        return qualifikationen.matrix(s, m, app.state.uhr().date())

    @app.get("/qualifikationen/katalog")
    def qualifikationskatalog() -> dict[str, Any]:
        return {k: {"bezeichnung": b, "nachschulung_monate": n, "grundlage": g} for k, (b, n, g) in qualifikationen.QUALIFIKATIONEN.items()}

    @app.post("/portal/faelle/{fall_id}/zaehlerstand")
    def portal_zaehlerstand(fall_id: str, daten: ZaehlerstandNeu, token: str | None = None, s: Session = Depends(sitzung)) -> dict[str, Any]:
        f, m = portal_fall(s, fall_id, token)
        a = s.get(Anlage, daten.anlage_id) if daten.anlage_id else None
        if a is not None and a.objekt.kunde_id != f.kunde_id:
            raise HTTPException(404, "Anlage nicht gefunden")
        z = fachlich(lambda: zaehler.erfassen(s, m, f.kunde, daten.art, daten.wert, daten.datum or app.state.uhr().date(), a, "kunde", daten.notiz))
        portal._kommunikation(s, f, f"Zählerstand gemeldet: {zaehler.ARTEN[daten.art][0]} {daten.wert:g} {zaehler.ARTEN[daten.art][1]}", app.state.uhr())
        s.commit()
        return zaehler._aus(z)

    return app
