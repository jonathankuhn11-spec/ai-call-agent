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
from integrations.signatur import SignaturFehler, bearer_pruefen, svix_pruefen, telli_pruefen
from integrations.telli import LeadPush, TelliClient

ANGEBOTE_KALENDER = 4          # Slots je /available-Antwort; der Agent im Gespräch bekommt weiterhin zwei
HTTP_TOOLS = ("lookup_lead", "update_lead", "check_slots", "book_appointment")   # end_call bleibt bei der Plattform

LOOKUP_SCHEMA = {"type": "function", "function": {
    "name": "lookup_lead",
    "description": "Liest Name, Status und bekannte Qualifizierungsangaben des Leads aus dem CRM. "
                   "Zu Beginn des Gesprächs aufrufen, um Bekanntes nicht erneut zu fragen.",
    "parameters": {"type": "object", "properties": {}}}}


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
        self.letzter_bericht: dict[str, Any] = {}
        self.letzter_push_bericht: dict[str, Any] = {}

    def tick(self, push: Optional[bool] = None, warten: bool = False) -> dict[str, Any]:
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
            faellig = push if push is not None else time.monotonic() - self.letzter_push >= ctx.konfig.push_takt_s
            if faellig and ctx.lead_push is not None:
                schritte.append(("push", ctx.lead_push.synchronisieren))
            for name, schritt in schritte:
                try:
                    bericht[name] = schritt()
                except Exception as e:  # noqa: BLE001 - ein Schritt darf die anderen nicht mitreißen
                    bericht[name] = {"fehler": f"{type(e).__name__}: {str(e)[:200]}"}
                    agent.log(f"[PLANER] {name}: {bericht[name]['fehler']}")
                if name == "push":
                    self.letzter_push = time.monotonic()
                    self.letzter_push_bericht = {"zeit": iso_utc(datetime.now(timezone.utc)), **bericht[name]}
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
    metriken: Metriken = field(default_factory=Metriken)
    sperre: threading.Lock = field(default_factory=threading.Lock)
    planer: Optional[Planer] = None


def tool_definitionen(basis_url: str = "https://<backend>") -> dict[str, Any]:
    """Was in telli unter Agent -> Tools je Custom Tool einzutragen ist, plus das OpenAI-Format
    für andere Plattformen. Systemfelder kommen aus System Variables, der Rest sind LLM-Parameter."""
    typen = {"string": "String", "number": "Number", "integer": "Number", "boolean": "Boolean"}
    aus = []
    for schema in [LOOKUP_SCHEMA, *agent.TOOL_SCHEMAS]:
        fn = schema["function"]
        if fn["name"] not in HTTP_TOOLS:
            continue
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
    return {"telli_custom_tools": aus,
            "openai_tools": [s for s in [LOOKUP_SCHEMA, *agent.TOOL_SCHEMAS] if s["function"]["name"] in HTTP_TOOLS]}


# ---------------------------------------------------------------- App
def erstelle_app(konfig: Optional[Konfig] = None, con=None, outbox: Optional[Outbox] = None, spiegel=None,
                 telli_client: Optional[TelliClient] = None, planer_starten: bool = True) -> FastAPI:
    konfig = konfig or Konfig.aus_umgebung()
    if konfig.produktiv and konfig.fehlende_geheimnisse():
        raise RuntimeError(f"UMGEBUNG=prod, aber es fehlen: {', '.join(konfig.fehlende_geheimnisse())}")
    con = con if con is not None else agent.init_db(path=konfig.db_pfad)
    crm = LocalCRM(con, konfig.zeitzone)
    if outbox is None and konfig.kunde_webhook_url:
        outbox = Outbox(con, konfig.kunde_webhook_url, konfig.kunde_webhook_secret)
    if spiegel is None and konfig.hubspot_token:
        spiegel = HubSpotCRM(konfig.hubspot_token, konfig.hubspot_url)
    if telli_client is None and konfig.telli_api_key and konfig.telli_agent_id:
        telli_client = TelliClient(konfig.telli_api_key, konfig.telli_api_url)
    ctx = Kontext(konfig, crm, outbox, Nachbereitung(crm, outbox, spiegel))
    if telli_client is not None and konfig.telli_agent_id:
        ctx.lead_push = LeadPush(crm, telli_client, konfig.telli_agent_id, sperre=ctx.sperre)
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
                "push": ctx.planer.letzter_push_bericht if ctx.planer else None, **status}

    @app.get("/tools/definitions")
    def definitions(request: Request):
        betrieb_pruefen(request)
        return tool_definitionen(str(request.base_url).rstrip("/"))

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

    # ------------------------------------------------------------ Custom Tools
    @app.post("/tools/{name}")
    async def tool(name: str, request: Request):
        t0 = time.perf_counter()
        if konfig.tool_secret:
            try:
                bearer_pruefen(konfig.tool_secret, request.headers)
            except SignaturFehler as e:
                raise HTTPException(401, str(e)) from e
        if name not in HTTP_TOOLS:
            raise HTTPException(404, f"Unbekanntes Tool: {name}. Verfügbar: {', '.join(HTTP_TOOLS)}")
        aufruf = lesen(ToolAufruf, json_lesen(await request.body()))

        def arbeit():
            lead = lead_aufloesen(KontaktReferenz(external_contact_id=aufruf.external_id,
                                                  contact_details={"phone_number": aufruf.phone_number}))
            if lead is None:
                return {"ok": False, "fehler": "Lead nicht im CRM gefunden. Name und Telefonnummer notieren, "
                                               "ein Mitarbeiter meldet sich."}
            if name == "lookup_lead":
                return {"ok": True, **{k: lead.get(k) for k in ("name", "status", "quelle", "plz", "eigentuemer",
                                                                "gebaeudetyp", "dachflaeche_m2", "jahresverbrauch_kwh")},
                        "gesperrt": ctx.crm.gesperrt(lead["telefon"])}
            return ctx.crm.tools(lead["id"]).run(name, aufruf.argumente())

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
            if lead["status"] != "qualifiziert":
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
        eigenschaften = {k: lead[k] for k in ("status", "quelle", "plz", "eigentuemer", "gebaeudetyp",
                                               "dachflaeche_m2", "jahresverbrauch_kwh") if lead.get(k) is not None}
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
        ereignis = daten.get("event") or ("call_ended" if "call" in daten or "call_id" in daten else None)
        if ereignis != "call_ended":
            messen("webhooks/telli", t0, True)
            return {"status": "ignoriert", "event": ereignis}
        try:
            call_ended = CallEndedEreignis.lesen(daten)
        except ValidationError as e:
            raise HTTPException(400, f"call_ended unlesbar: {e.errors()[0]['msg']}") from e
        ergebnis = await gesperrt(lambda: ctx.nachbereitung.verarbeiten(call_ended, message_id))
        if ctx.planer is not None:
            hintergrund.add_task(ctx.planer.tick)          # erst 2xx an telli, dann Zustellung und Spiegelung
        messen("webhooks/telli", t0, ergebnis.get("status") in ("verarbeitet", "duplikat"))
        return ergebnis

    return app
