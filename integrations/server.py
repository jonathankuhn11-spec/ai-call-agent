"""HTTP-Schnittstelle für die Voice-Plattform: Custom Tools, Custom Calendar, Contact Lookup, Webhooks.

Start:  python -m integrations serve            (uvicorn, Port 8000)
        python -m integrations definitions      (Tool-Definitionen für die Konfiguration in telli)

Jeder Endpunkt folgt derselben Reihenfolge: Signatur oder Token prüfen, rohen Body lesen,
Payload tolerant parsen, Lead auflösen, Geschäftsregel über agent.Tools ausführen, nach
Kontrakt antworten. Fehler, die der Agent im Gespräch verwerten kann (Lead nicht gefunden,
Termin nicht mehr frei), kommen mit HTTP 200 und einer Fehlermeldung im Body zurück: So
erreicht die Information das Sprachmodell, statt als HTTP-Fehler im Plattform-Log zu enden.
Konfigurationsfehler (unbekanntes Tool, kaputtes JSON, falsche Signatur) sind 4xx.

Nebenläufigkeit: Die DuckDB-Verbindung wird geteilt und ist nicht threadsicher, deshalb
serialisiert eine Sperre alle Datenbankzugriffe. Die Handler sind asynchron, halten die Sperre
aber nur in einem Arbeitsthread (run_in_threadpool), damit die Ereignisschleife frei bleibt.
Jeder ausgehende HTTP-Verkehr (Outbox an Kundensysteme, Spiegelung ins CRM, Lead-Push zu telli)
läuft im Hintergrundplaner und nie unter der Sperre: Ein langsames Kundensystem darf keinen
Tool-Aufruf der Plattform verzögern. DuckDB erlaubt nur einen schreibenden Prozess; darum laufen
Outbox-Wiederholung, Spiegelung und Lead-Push als Threads in diesem Prozess, nicht als Cron.
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

import agent
from integrations import __version__
from integrations.crm import LocalCRM, slot_ende
from integrations.hubspot import HubSpotCRM
from integrations.konfig import Konfig
from integrations.modelle import (BuchenAnfrage, BuchenAntwort, CallEndedEreignis, KontaktLookupAnfrage,
                                  KontaktLookupAntwort, KontaktLookupKontakt, KontaktReferenz, Slot, ToolAufruf,
                                  VerfuegbarAnfrage, VerfuegbarAntwort, iso_lesen, iso_utc, telefon_normalisieren)
from integrations.nachbereitung import Nachbereitung
from integrations.outbox import Outbox
from integrations.salesforce import SalesforceCRM
from integrations.signatur import SignaturFehler, bearer_pruefen, geheimnisse, svix_pruefen, telli_pruefen
from integrations.abgleich import Abgleich, anruf_nachholen
from integrations.telli import LeadPush, TelliClient, TelliFehler

ANGEBOTE_KALENDER = 4          # Slots je /available-Antwort; der Agent im Gespräch bekommt weiterhin zwei
HTTP_TOOLS = ("lookup_lead", "update_lead", "check_slots", "book_appointment", "cancel_appointment",
              "verify_customer", "create_ticket")                                 # end_call bleibt bei der Plattform
VERIFIZIERUNG_FREI = ("verify_customer",)                                          # auch ohne Verifizierung erlaubt

LOOKUP_SCHEMA = {"type": "function", "function": {
    "name": "lookup_lead",
    "description": "Liest Name, Status, bekannte Qualifizierungsangaben und den nächsten Termin des Leads aus dem CRM. "
                   "Zu Beginn des Gesprächs aufrufen, um Bekanntes nicht erneut zu fragen.",
    "parameters": {"type": "object", "properties": {}}}}
CANCEL_SCHEMA = {"type": "function", "function": {
    "name": "cancel_appointment",
    "description": "Sagt den bevorstehenden Beratungstermin des Kunden ab. Nur aufrufen, wenn der Kunde das "
                   "ausdrücklich will. Danach bei Interesse mit check_slots einen neuen Termin anbieten.",
    "parameters": {"type": "object", "properties": {
        "grund": {"type": "string", "description": "Kurzer Grund in den Worten des Kunden"}}}}}
VERIFY_SCHEMA = {"type": "function", "function": {
    "name": "verify_customer",
    "description": "Prüft die Identität des Anrufers über die Postleitzahl der Anschlussadresse. Vor jeder Auskunft zu "
                   "Terminen aufrufen; höchstens drei Versuche, danach weiterleiten.",
    "parameters": {"type": "object", "properties": {
        "plz": {"type": "string", "description": "Postleitzahl, die der Kunde genannt hat"}}, "required": ["plz"]}}}
TICKET_SCHEMA = {"type": "function", "function": {
    "name": "create_ticket",
    "description": "Nimmt eine Störung oder ein Anliegen als Ticket auf und liefert die Ticketnummer zum Vorlesen.",
    "parameters": {"type": "object", "properties": {
        "kategorie": {"type": "string", "enum": ["stoerung_internet", "stoerung_tv", "stoerung_telefon", "rechnung", "sonstiges"]},
        "beschreibung": {"type": "string", "description": "Was funktioniert nicht, seit wann, in den Worten des Kunden"}},
        "required": ["kategorie", "beschreibung"]}}}
EIGENE_SCHEMAS = [LOOKUP_SCHEMA, CANCEL_SCHEMA, VERIFY_SCHEMA, TICKET_SCHEMA]


# ---------------------------------------------------------------- Metriken (für Runbook und Dashboard)
class Metriken:
    def __init__(self):
        self._daten: dict[str, list[tuple[float, bool]]] = {}
        self._sperre = threading.Lock()

    def erfassen(self, endpunkt: str, ms: float, ok: bool):
        with self._sperre:
            self._daten.setdefault(endpunkt, []).append((ms, ok))
            if len(self._daten[endpunkt]) > 5000:
                self._daten[endpunkt] = self._daten[endpunkt][-5000:]

    def bericht(self) -> dict[str, dict[str, float]]:
        with self._sperre:
            aus = {}
            for ep, werte in self._daten.items():
                ms = sorted(w for w, _ in werte)
                aus[ep] = {"anzahl": len(ms), "fehler": sum(1 for _, ok in werte if not ok),
                           "p50_ms": round(statistics.median(ms), 1),
                           "p95_ms": round(ms[max(0, int(len(ms) * 0.95) - 1)], 1), "max_ms": round(ms[-1], 1)}
            return aus


# ---------------------------------------------------------------- Hintergrundplaner
class Planer:
    """Führt alles aus, was nach außen telefoniert: Outbox, Spiegelung, Lead-Push. HTTP ohne Sperre."""

    def __init__(self, ctx: "Kontext"):
        self.ctx = ctx
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._laeuft = threading.Lock()                       # ein Tick zur Zeit, auch über Threads hinweg
        self.letzter_push = 0.0
        self.letzter_abgleich = time.monotonic()           # erster Abgleich erst nach einem vollen Intervall
        self.letzter_bericht: dict[str, Any] = {}
        self.letzter_push_bericht: dict[str, Any] = {}
        self.letzter_abgleich_bericht: dict[str, Any] = {}

    def tick(self, push: Optional[bool] = None, warten: bool = False, abgleich: Optional[bool] = None) -> dict[str, Any]:
        """Ein Durchlauf. `push=None`: Lead-Push nur, wenn sein Intervall abgelaufen ist.
        `warten=False` (Hintergrund): läuft gerade ein Tick, wird dieser übersprungen, der nächste kommt im Takt.
        `warten=True` (ausdrückliche Anforderung über die API): auf den laufenden Tick warten, dann ausführen.
        Jeder Schritt ist für sich abgesichert; ein Fehler in der Outbox hält Spiegelung und Push nicht auf."""
        if not self._laeuft.acquire(blocking=warten):
            return {"uebersprungen": "läuft bereits"}
        try:
            bericht: dict[str, Any] = {}
            ctx = self.ctx
            schritte = []
            if ctx.outbox is not None:
                schritte.append(("outbox", lambda: ctx.outbox.zustellen(sperre=ctx.sperre)))
            if ctx.nachbereitung.spiegel is not None:
                schritte.append(("spiegel", lambda: ctx.nachbereitung.spiegeln(sperre=ctx.sperre)))
            if ctx.outbox is not None:
                schritte.append(("erinnerungen", lambda: ctx.nachbereitung.erinnerungen_senden(sperre=ctx.sperre)))
            faellig = push if push is not None else time.monotonic() - self.letzter_push >= ctx.konfig.push_takt_s
            if faellig and ctx.lead_push is not None:
                schritte.append(("push", ctx.lead_push.synchronisieren))
            abgleich_faellig = abgleich if abgleich is not None else \
                time.monotonic() - self.letzter_abgleich >= ctx.konfig.abgleich_takt_s
            if abgleich_faellig and ctx.abgleich is not None:
                schritte.append(("abgleich", ctx.abgleich.ausfuehren))
            for name, schritt in schritte:
                try:
                    bericht[name] = schritt()
                except Exception as e:  # noqa: BLE001 - ein Schritt darf die anderen nicht mitreißen
                    bericht[name] = {"fehler": f"{type(e).__name__}: {str(e)[:200]}"}
                    agent.log(f"[PLANER] {name}: {bericht[name]['fehler']}")
                if name == "push":
                    self.letzter_push = time.monotonic()
                    self.letzter_push_bericht = {"zeit": iso_utc(datetime.now(timezone.utc)), **bericht[name]}
                if name == "abgleich":
                    self.letzter_abgleich = time.monotonic()
                    self.letzter_abgleich_bericht = {"zeit": iso_utc(datetime.now(timezone.utc)), **bericht[name]}
            self.letzter_bericht = {"zeit": iso_utc(datetime.now(timezone.utc)), **bericht}
            return bericht
        finally:
            self._laeuft.release()

    def _schleife(self):
        while not self._stop.wait(self.ctx.konfig.takt_s):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - der Planer darf nie sterben
                agent.log(f"[PLANER] Fehler im Durchlauf: {type(e).__name__}: {str(e)[:200]}")
                self.letzter_bericht = {"zeit": iso_utc(datetime.now(timezone.utc)), "fehler": f"{type(e).__name__}: {str(e)[:200]}"}

    def starten(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._schleife, name="integrations-planer", daemon=True)
            self._thread.start()

    def stoppen(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


@dataclass
class Kontext:
    konfig: Konfig
    crm: LocalCRM
    outbox: Optional[Outbox]
    nachbereitung: Nachbereitung
    lead_push: Optional[LeadPush] = None
    abgleich: Optional[Abgleich] = None
    telli_client: Optional[TelliClient] = None
    usecase: Any = None
    verifizierung_pflicht: bool = False
    metriken: Metriken = field(default_factory=Metriken)
    sperre: threading.Lock = field(default_factory=threading.Lock)
    planer: Optional[Planer] = None


def tool_definitionen(basis_url: str = "https://<backend>", nur: Optional[list[str]] = None) -> dict[str, Any]:
    """Was in telli unter Agent -> Tools je Custom Tool einzutragen ist, plus das OpenAI-Format
    für andere Plattformen. Systemfelder kommen aus System Variables, der Rest sind LLM-Parameter.
    `nur` beschränkt auf die Tools eines Use Case (Reihenfolge wie dort)."""
    typen = {"string": "String", "number": "Number", "integer": "Number", "boolean": "Boolean"}
    aus = []
    schemata = [s for s in [*EIGENE_SCHEMAS, *agent.TOOL_SCHEMAS] if s["function"]["name"] in HTTP_TOOLS]
    if nur is not None:
        schemata = sorted((s for s in schemata if s["function"]["name"] in nur), key=lambda s: nur.index(s["function"]["name"]))
    for schema in schemata:
        fn = schema["function"]
        body = [{"key": "call_id", "value_type": "System Variable", "value": "call.id"},
                {"key": "external_id", "value_type": "System Variable", "value": "contact.externalId"},
                {"key": "phone_number", "value_type": "System Variable", "value": "contact.phoneNumber"}]
        for name, p in fn["parameters"].get("properties", {}).items():
            beschreibung = p.get("description", "")
            if "enum" in p:
                beschreibung = (beschreibung + " " if beschreibung else "") + "Einer von: " + ", ".join(p["enum"])
            body.append({"key": name, "value_type": "LLM Parameter", "data_type": typen.get(p.get("type"), "String"),
                         "description": beschreibung.strip(),
                         "required": name in fn["parameters"].get("required", [])})
        aus.append({"name": fn["name"], "description": fn["description"], "method": "POST",
                    "url": f"{basis_url}/tools/{fn['name']}", "response_timeout_s": 3,
                    "headers": [{"key": "Authorization", "value_type": "Secret", "value": "Bearer <TOOL_SECRET>"}],
                    "body": body})
    return {"telli_custom_tools": aus, "openai_tools": schemata}


# ---------------------------------------------------------------- Bausteine aus der Konfiguration (Server und CLI teilen sie)
def spiegel_aus_konfig(konfig: Konfig):
    """Genau ein Kunden-CRM als Spiegel; HubSpot hat Vorrang, wenn beide konfiguriert sind."""
    if konfig.hubspot_token:
        return HubSpotCRM(geheimnisse(konfig.hubspot_token)[-1], konfig.hubspot_url)
    if konfig.salesforce_konfiguriert:
        return SalesforceCRM(konfig.salesforce_instance_url, konfig.salesforce_token, konfig.salesforce_login_url,
                             konfig.salesforce_client_id, konfig.salesforce_client_secret)
    return None


def telli_client_aus_konfig(konfig: Konfig) -> Optional[TelliClient]:
    """Bei Schlüsselrotation ('alt,neu') authentifiziert der Client mit dem neuesten Schlüssel."""
    if not konfig.telli_api_key:
        return None
    return TelliClient(geheimnisse(konfig.telli_api_key)[-1], konfig.telli_api_url)


def outbox_aus_konfig(konfig: Konfig, con) -> Optional[Outbox]:
    return Outbox(con, konfig.kunde_webhook_url, konfig.kunde_webhook_secret) if konfig.kunde_webhook_url else None


# ---------------------------------------------------------------- App
def erstelle_app(konfig: Optional[Konfig] = None, con=None, outbox: Optional[Outbox] = None, spiegel=None,
                 telli_client: Optional[TelliClient] = None, planer_starten: bool = True) -> FastAPI:
    konfig = konfig or Konfig.aus_umgebung()
    if konfig.produktiv and konfig.fehlende_geheimnisse():
        raise RuntimeError(f"UMGEBUNG=prod, aber es fehlen: {', '.join(konfig.fehlende_geheimnisse())}")
    con = con if con is not None else agent.init_db(path=konfig.db_pfad)
    from integrations.usecases import alle as alle_usecases
    usecase = alle_usecases().get(konfig.usecase)
    if usecase is None:
        raise RuntimeError(f"Unbekannter USECASE '{konfig.usecase}'. Bekannt: {', '.join(alle_usecases())}")
    verifizierung = usecase.verifizierung_pflicht if konfig.verifizierung_pflicht is None else konfig.verifizierung_pflicht
    mindestvorlauf = usecase.mindestvorlauf_h if konfig.mindestvorlauf_h is None else konfig.mindestvorlauf_h
    crm = LocalCRM(con, konfig.zeitzone, mindestvorlauf_h=mindestvorlauf,
                   qualifizierung_noetig=usecase.buchung_erfordert_qualifizierung)
    if outbox is None:
        outbox = outbox_aus_konfig(konfig, con)
    if spiegel is None:
        spiegel = spiegel_aus_konfig(konfig)
    if telli_client is None and konfig.telli_api_key:
        telli_client = telli_client_aus_konfig(konfig)
    ctx = Kontext(konfig, crm, outbox, Nachbereitung(crm, outbox, spiegel))
    ctx.usecase, ctx.verifizierung_pflicht = usecase, verifizierung
    if telli_client is not None and konfig.telli_agent_id:
        ctx.lead_push = LeadPush(crm, telli_client, konfig.telli_agent_id, sperre=ctx.sperre)
    if telli_client is not None:
        ctx.telli_client = telli_client
        ctx.abgleich = Abgleich(crm, telli_client, ctx.nachbereitung, sperre=ctx.sperre)
    ctx.planer = Planer(ctx)

    @asynccontextmanager
    async def lebenszyklus(app: FastAPI):
        if planer_starten:
            ctx.planer.starten()
        yield
        ctx.planer.stoppen()

    app = FastAPI(title="ai-call-agent Integrationsschicht", version=__version__, lifespan=lebenszyklus,
                  description="Custom Tools, Custom Calendar, Contact Lookup und Webhooks für die Voice-Plattform.")
    app.state.ctx = ctx

    def messen(endpunkt: str, t0: float, ok: bool):
        ctx.metriken.erfassen(endpunkt, (time.perf_counter() - t0) * 1000, ok)

    def json_lesen(body: bytes) -> dict:
        try:
            daten = json.loads(body or b"{}")
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"Body ist kein JSON: {e.msg}") from e
        if not isinstance(daten, dict):
            raise HTTPException(400, "Body muss ein JSON-Objekt sein")
        return daten

    def lesen(modell, daten: dict):
        """Payload nach Modell; Formfehler sind Konfigurationsfehler der Plattform, also 400."""
        try:
            return modell.model_validate(daten)
        except ValidationError as e:
            fehler = e.errors()[0]
            raise HTTPException(400, f"Payload ungültig: {'.'.join(map(str, fehler['loc']))}: {fehler['msg']}") from e

    def telli_signatur_pruefen(body: bytes, headers):
        if konfig.telli_api_key:
            try:
                telli_pruefen(konfig.telli_api_key, body, headers)
            except SignaturFehler as e:
                raise HTTPException(401, str(e)) from e

    def betrieb_pruefen(request: Request):
        """Betriebsendpunkte: mit dem Tool-Secret geschützt, sobald eines gesetzt ist."""
        if konfig.tool_secret:
            try:
                bearer_pruefen(konfig.tool_secret, request.headers)
            except SignaturFehler as e:
                raise HTTPException(401, str(e)) from e

    def lead_aufloesen(ref: KontaktReferenz) -> Optional[dict]:
        ext = ref.external_id()
        lead = ctx.crm.lead(ext) if ext else None
        if lead is None and ref.telefon():
            lead = ctx.crm.lead_per_telefon(ref.telefon())
        return lead

    async def gesperrt(arbeit: Callable[[], Any]) -> Any:
        """Datenbankarbeit unter der Sperre in einem Arbeitsthread; die Ereignisschleife bleibt frei."""
        def _laufen():
            with ctx.sperre:
                return arbeit()
        return await run_in_threadpool(_laufen)

    # ------------------------------------------------------------ Betrieb
    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__, "geheimnisse_vollstaendig": not konfig.fehlende_geheimnisse(),
                "planer": bool(ctx.planer and ctx.planer._thread and ctx.planer._thread.is_alive()),
                "zeit": iso_utc(datetime.now(timezone.utc))}

    @app.get("/metrics")
    async def metrics(request: Request):
        betrieb_pruefen(request)
        status = await gesperrt(lambda: {"outbox": ctx.outbox.status() if ctx.outbox else None,
                                         "spiegel": ctx.crm.spiegel_status() if ctx.nachbereitung.spiegel else None,
                                         "zuordnung_offen": ctx.crm.zuordnung_offen()})
        return {"endpunkte": ctx.metriken.bericht(), "fehlende_geheimnisse": konfig.fehlende_geheimnisse(),
                "planer": ctx.planer.letzter_bericht if ctx.planer else None,
                "push": ctx.planer.letzter_push_bericht if ctx.planer else None,
                "abgleich": ctx.planer.letzter_abgleich_bericht if ctx.planer else None, **status}

    @app.get("/tools/definitions")
    def definitions(request: Request):
        betrieb_pruefen(request)
        return tool_definitionen(str(request.base_url).rstrip("/"), nur=ctx.usecase.tool_namen())

    @app.get("/outbox")
    async def outbox_status(request: Request):
        betrieb_pruefen(request)
        if ctx.outbox is None:
            return {"konfiguriert": False}
        return await gesperrt(lambda: {
            "konfiguriert": True, "status": ctx.outbox.status(),
            "tot": [{k: e[k] for k in ("id", "ereignis_id", "typ", "versuche", "letzte_antwort")} for e in ctx.outbox.eintraege("tot")]})

    @app.post("/outbox/zustellen")
    async def outbox_zustellen(request: Request):
        """Über den Planer, damit nie zwei Zustellläufe gleichzeitig dieselben Einträge versenden."""
        betrieb_pruefen(request)
        if ctx.outbox is None:
            return {"konfiguriert": False}
        bericht = await run_in_threadpool(lambda: ctx.planer.tick(push=False, warten=True))
        return bericht.get("outbox", bericht)

    @app.post("/outbox/{outbox_id}/erneut")
    async def outbox_erneut(outbox_id: int, request: Request):
        betrieb_pruefen(request)
        if ctx.outbox is None:
            return {"konfiguriert": False}
        await gesperrt(lambda: ctx.outbox.erneut(outbox_id))
        return {"ok": True, "id": outbox_id}

    @app.post("/push")
    async def push(request: Request):
        """Lead-Push sofort auslösen (z. B. aus einem CRM-Webhook heraus), statt auf den Takt zu warten."""
        betrieb_pruefen(request)
        if ctx.lead_push is None:
            return {"konfiguriert": False, "hinweis": "TELLI_API_KEY und TELLI_AGENT_ID setzen"}
        return await run_in_threadpool(lambda: ctx.planer.tick(push=True, warten=True))

    @app.post("/abgleich")
    async def abgleich_jetzt(request: Request):
        """Abgleich mit List Calls sofort ausführen (Runbook: nach einer Störung des Webhook-Endpunkts)."""
        betrieb_pruefen(request)
        if ctx.abgleich is None:
            return {"konfiguriert": False, "hinweis": "TELLI_API_KEY setzen"}
        bericht = await run_in_threadpool(lambda: ctx.planer.tick(push=False, abgleich=True, warten=True))
        return bericht.get("abgleich", bericht)

    @app.post("/abgleich/{call_id}")
    async def anruf_nachholen_endpunkt(call_id: str, request: Request):
        """Einen bestimmten Anruf per Get Call nachträglich verarbeiten."""
        betrieb_pruefen(request)
        if ctx.telli_client is None:
            return {"konfiguriert": False, "hinweis": "TELLI_API_KEY setzen"}
        try:
            return await run_in_threadpool(lambda: anruf_nachholen(ctx.crm, ctx.telli_client, ctx.nachbereitung, call_id, ctx.sperre))
        except TelliFehler as e:
            raise HTTPException(404 if e.status == 404 else 502, f"telli: {e}") from e

    # ------------------------------------------------------------ Custom Tools
    @app.post("/tools/{name}")
    async def tool(name: str, request: Request):
        t0 = time.perf_counter()
        if konfig.tool_secret:
            try:
                bearer_pruefen(konfig.tool_secret, request.headers)
            except SignaturFehler as e:
                raise HTTPException(401, str(e)) from e
        if name not in ctx.usecase.tool_namen():
            raise HTTPException(404, f"Unbekanntes Tool: {name}. Verfügbar: {', '.join(ctx.usecase.tool_namen())}")
        aufruf = lesen(ToolAufruf, json_lesen(await request.body()))

        def arbeit():
            lead = lead_aufloesen(KontaktReferenz(external_contact_id=aufruf.external_id,
                                                  contact_details={"phone_number": aufruf.phone_number}))
            if lead is None:
                return {"ok": False, "fehler": "Lead nicht im CRM gefunden. Name und Telefonnummer notieren, "
                                               "ein Mitarbeiter meldet sich."}
            if ctx.verifizierung_pflicht and name not in VERIFIZIERUNG_FREI and not ctx.crm.verifiziert(aufruf.call_id, lead["id"]):
                return {"ok": False, "fehler": "Der Anrufer ist noch nicht verifiziert. Erst die Postleitzahl erfragen und mit "
                                               "verify_customer prüfen; vorher keine Auskunft.", "verifizierung_noetig": True}
            if name == "lookup_lead":
                termin = ctx.crm.termin(lead["id"], zukunft=True)
                return {"ok": True, **{k: lead.get(k) for k in ("name", "status", "quelle", "plz", "eigentuemer",
                                                                "gebaeudetyp", "dachflaeche_m2", "jahresverbrauch_kwh")},
                        "gesperrt": ctx.crm.gesperrt(lead["telefon"]),
                        "naechster_termin": f"{termin['start']:%d.%m.%Y %H:%M} Uhr" if termin else None}
            tools = ctx.crm.tools(lead["id"])
            tools.call_id = aufruf.call_id
            ergebnis = tools.run(name, aufruf.argumente())
            if ergebnis.get("ok") and not ergebnis.get("wiederholt") and ctx.outbox is not None:   # Wiederholungen erzeugen keine Ereignisse
                if name == "book_appointment" and ergebnis.get("umgebucht_von"):   # Umbuchung: alter Termin ist abgesagt
                    ctx.nachbereitung._ereignis("termin.abgesagt", lead["id"], {"abgesagt": ergebnis["umgebucht_von"],
                                                                                "grund": "Umbuchung", "umbuchung": True,
                                                                                "call_id": aufruf.call_id}, datetime.now(timezone.utc))
                if name == "cancel_appointment":
                    ctx.nachbereitung._ereignis("termin.abgesagt", lead["id"], {"abgesagt": ergebnis["abgesagt"],
                                                                                "grund": ergebnis["grund"], "call_id": aufruf.call_id},
                                                datetime.now(timezone.utc))
                if name == "create_ticket":
                    ctx.nachbereitung._ereignis("ticket.erstellt", lead["id"], {"ticket_nummer": ergebnis["ticket_nummer"],
                                                                                "kategorie": ergebnis["kategorie"],
                                                                                "beschreibung": aufruf.argumente().get("beschreibung"),
                                                                                "telefon": lead["telefon"], "name": lead["name"],
                                                                                "call_id": aufruf.call_id},
                                                datetime.now(timezone.utc))
            return ergebnis

        ergebnis = await gesperrt(arbeit)
        ms = (time.perf_counter() - t0) * 1000
        ctx.metriken.erfassen(f"tools/{name}", ms, bool(ergebnis.get("ok", True)))
        if ms > konfig.tool_latenzbudget_ms:
            agent.log(f"[INTEGRATION] Tool {name} über Latenzbudget: {ms:.0f} ms")
        return ergebnis

    # ------------------------------------------------------------ Custom Calendar (telli-Kontrakt)
    @app.post("/calendar/available", response_model=VerfuegbarAntwort)
    async def available(request: Request):
        t0 = time.perf_counter()
        body = await request.body()
        telli_signatur_pruefen(body, request.headers)
        anfrage = lesen(VerfuegbarAnfrage, json_lesen(body))

        def arbeit():
            lead = lead_aufloesen(anfrage)
            if lead is None:
                return [], "Lead nicht im CRM gefunden"
            if ctx.crm.qualifizierung_noetig and lead["status"] != "qualifiziert":
                return [], f"Lead {lead['id']} hat Status '{lead['status']}': Termine nur für qualifizierte Eigentümer"
            slots = ctx.crm.freie_slots(ANGEBOTE_KALENDER)
            ctx.crm.angebote_merken(lead["id"], [s["slot_id"] for s in slots])
            return [Slot(start_iso=iso_utc(ctx.crm.start_utc(s["start"])),
                         end_iso=iso_utc(ctx.crm.start_utc(slot_ende(s["start"])))) for s in slots], None

        slots, grund = await gesperrt(arbeit)
        messen("calendar/available", t0, grund is None)
        if grund:
            agent.log(f"[INTEGRATION] /calendar/available leer: {grund}")
        return VerfuegbarAntwort(available=slots)

    @app.post("/calendar/book", response_model=BuchenAntwort, response_model_exclude_none=True)
    async def book(request: Request):
        t0 = time.perf_counter()
        body = await request.body()
        telli_signatur_pruefen(body, request.headers)
        anfrage = lesen(BuchenAnfrage, json_lesen(body))

        def arbeit():
            lead = lead_aufloesen(anfrage)
            if lead is None:
                return BuchenAntwort(status="failed", reason="Lead nicht im CRM gefunden")
            try:
                start = iso_lesen(anfrage.start_iso)
            except ValueError:
                return BuchenAntwort(status="failed", reason="start_iso ist kein ISO-8601-Zeitpunkt")
            slot = ctx.crm.slot_per_start(start)
            if slot is None or (slot["lead_id"] is not None and slot["lead_id"] != lead["id"]):
                return BuchenAntwort(status="failed", reason="Appointment slot is no longer available")
            ergebnis = ctx.crm.tools(lead["id"]).run("book_appointment", {"slot_id": slot["slot_id"]})
            if ergebnis.get("ok"):
                return BuchenAntwort(status="success")
            grund = ergebnis.get("fehler") or "; ".join(ergebnis.get("validierungsfehler", [])) or "Buchung abgelehnt"
            return BuchenAntwort(status="failed", reason=grund)

        antwort = await gesperrt(arbeit)
        messen("calendar/book", t0, antwort.status == "success")
        return antwort

    # ------------------------------------------------------------ Contact Lookup Webhook
    @app.post("/webhooks/contact-lookup")
    async def contact_lookup(request: Request):
        """Antwort nach Kontrakt: {"contact": null} für Unbekannte, sonst der Kontakt ohne leere Felder."""
        t0 = time.perf_counter()
        body = await request.body()
        telli_signatur_pruefen(body, request.headers)
        anfrage = lesen(KontaktLookupAnfrage, json_lesen(body))
        lead, ist_gesperrt = await gesperrt(lambda: (ctx.crm.lead_per_telefon(anfrage.phone_number),
                                                     ctx.crm.gesperrt(anfrage.phone_number)))
        messen("webhooks/contact-lookup", t0, lead is not None)
        if lead is None:
            return KontaktLookupAntwort(contact=None).model_dump()
        vorname, _, nachname = (lead["name"] or "").partition(" ")
        felder = ("status", "quelle", "plz", "eigentuemer", "gebaeudetyp", "dachflaeche_m2", "jahresverbrauch_kwh")
        if ctx.verifizierung_pflicht:
            felder = tuple(f for f in felder if f != "plz")        # das Prüfmerkmal bleibt im Backend
        eigenschaften = {k: lead[k] for k in felder if lead.get(k) is not None}
        eigenschaften["gesperrt"] = ist_gesperrt
        kontakt = KontaktLookupKontakt(first_name=(vorname or "Unbekannt")[:50], last_name=(nachname or "Unbekannt")[:50],
                                       external_id=str(lead["id"]), phone_number=telefon_normalisieren(lead["telefon"]),
                                       properties=eigenschaften)
        return {"contact": kontakt.model_dump(exclude_none=True)}

    # ------------------------------------------------------------ Webhook-Ereignisse (Svix-signiert)
    @app.post("/webhooks/telli")
    async def telli_webhook(request: Request, hintergrund: BackgroundTasks):
        t0 = time.perf_counter()
        body = await request.body()
        if konfig.telli_webhook_secret:
            try:
                message_id = svix_pruefen(konfig.telli_webhook_secret, body, request.headers,
                                          toleranz_s=konfig.signatur_toleranz_s)
            except SignaturFehler as e:
                raise HTTPException(401, str(e)) from e
        else:
            message_id = request.headers.get("svix-id") or f"unsigniert_{int(time.time() * 1000)}"
        daten = json_lesen(body)
        # Payload-Modelle werden vor der Sperre geprüft: Formfehler sind ein 400, keine halbe Verarbeitung
        try:
            ergebnis = await gesperrt(lambda: ctx.nachbereitung.ereignis_verarbeiten(daten, message_id))
        except ValidationError as e:
            fehler = e.errors()[0]
            raise HTTPException(400, f"Ereignis unlesbar: {'.'.join(map(str, fehler['loc']))}: {fehler['msg']}") from e
        if ctx.planer is not None and ergebnis.get("status") == "verarbeitet":
            hintergrund.add_task(ctx.planer.tick)          # erst 2xx an telli, dann Zustellung und Spiegelung
        messen("webhooks/telli", t0, ergebnis.get("status") in ("verarbeitet", "duplikat", "ignoriert"))
        return ergebnis

    return app
