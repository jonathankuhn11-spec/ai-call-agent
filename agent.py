"""
Voice-Agent-Prototyp (Tag 1: Text-Version)
Use Case: Outbound-Lead-Qualifizierung für PV-Anlagen + Terminbuchung
Stack: Ollama (lokales LLM) · Pydantic (Validierung) · DuckDB (Mock-CRM)

Start:  python agent.py            -> ruft den nächsten offenen Lead an
        python agent.py --lead 3   -> bestimmten Lead anrufen
        python agent.py --reset    -> Datenbank neu aufsetzen
"""
import argparse
import json
import re
import time
from datetime import datetime, timedelta
from typing import Literal, Optional

import duckdb
import ollama
from pydantic import BaseModel, Field, ValidationError, field_validator

MODEL = "qwen2.5:7b"
DB_PATH = "crm.duckdb"
FIRMA = "SonnenWerk Energie"   # fiktiver Kunde
AGENT_NAME = "Lena"
MAX_TOOL_STEPS = 5             # Schutz gegen Tool-Endlosschleifen pro Turn
VERBOSE = True

def log(*a, **k):
    if VERBOSE:
        print(*a, **k)


# ---------------------------------------------------------------- Mock-CRM
def init_db(reset: bool = False, path: str = DB_PATH):
    con = duckdb.connect(path)
    if reset:
        for t in ("appointments", "calls", "slots", "leads"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.execute("""CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY, name TEXT, telefon TEXT, plz TEXT,
        quelle TEXT, status TEXT DEFAULT 'offen',
        eigentuemer BOOLEAN, gebaeudetyp TEXT, dachflaeche_m2 DOUBLE,
        jahresverbrauch_kwh INTEGER, notiz TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS slots (
        id INTEGER PRIMARY KEY, start TIMESTAMP, lead_id INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER, lead_id INTEGER, slot_id INTEGER, art TEXT, gebucht_am TIMESTAMP)""")
    con.execute("""CREATE TABLE IF NOT EXISTS calls (
        id INTEGER, lead_id INTEGER, start TIMESTAMP, ende TIMESTAMP,
        ergebnis TEXT, turns INTEGER, tool_fehler INTEGER, flags TEXT, transkript TEXT)""")

    if con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0:
        con.executemany(
            "INSERT INTO leads (id, name, telefon, plz, quelle) VALUES (?, ?, ?, ?, ?)",
            [(1, "Thomas Becker", "+49 251 000001", "48149", "Website-Formular"),
             (2, "Sabine Wolf", "+49 251 000002", "48155", "Solarrechner"),
             (3, "Mehmet Yilmaz", "+49 251 000003", "48161", "Facebook-Ad"),
             (4, "Anna Schröder", "+49 251 000004", "48143", "Website-Formular"),
             (5, "Klaus Hoffmann", "+49 251 000005", "48167", "Empfehlung")])
    if con.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 0:
        base = (datetime.now() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        rows, sid = [], 1
        for d in range(5):
            day = base + timedelta(days=d)
            if day.weekday() >= 5:
                continue
            for h in (9, 11, 14, 16):
                rows.append((sid, day.replace(hour=h), None)); sid += 1
        con.executemany("INSERT INTO slots VALUES (?, ?, ?)", rows)
    return con


# ---------------------------------------------------------------- Pydantic-Schemas
def _unwrap(v):
    """{"description": "Ja"} -> "Ja" (kleine Modelle kopieren oft die Schema-Struktur)"""
    if isinstance(v, dict) and v:
        return next(iter(v.values()))
    return v


class Qualifizierung(BaseModel):
    eigentuemer: Optional[bool] = Field(None, description="Ist die Person Eigentümer der Immobilie?")
    gebaeudetyp: Optional[Literal["einfamilienhaus", "doppelhaushaelfte", "reihenhaus",
                                  "mehrfamilienhaus", "gewerbe", "sonstiges"]] = None
    dachflaeche_m2: Optional[float] = Field(None, ge=5, le=2000)
    jahresverbrauch_kwh: Optional[int] = Field(None, ge=500, le=100_000)
    status: Literal["in_qualifizierung", "qualifiziert", "disqualifiziert",
                    "rueckruf", "kein_interesse", "opt_out"]
    notiz: Optional[str] = Field(None, max_length=500)

    # Kleine Modelle liefern oft "Ja", {"description": "Ja"} oder "Einfamilienhaus".
    # Statt abzulehnen: normalisieren, was eindeutig ist.
    @field_validator("dachflaeche_m2", "jahresverbrauch_kwh", "notiz", mode="before")
    @classmethod
    def _plain(cls, v):
        return _unwrap(v)

    @field_validator("eigentuemer", mode="before")
    @classmethod
    def _bool(cls, v):
        v = _unwrap(v)
        if isinstance(v, str):
            t = v.strip().lower()
            if t in ("ja", "yes", "true", "wahr", "eigentümer", "eigentuemer", "1"):
                return True
            if t in ("nein", "no", "false", "falsch", "mieter", "0"):
                return False
        return v

    @field_validator("gebaeudetyp", mode="before")
    @classmethod
    def _typ(cls, v):
        v = _unwrap(v)
        if not isinstance(v, str):
            return v
        t = v.strip().lower().replace("ä", "ae").replace("-", "").replace(" ", "")
        mapping = {"efh": "einfamilienhaus", "einzelhaus": "einfamilienhaus",
                   "dhh": "doppelhaushaelfte", "doppelhaus": "doppelhaushaelfte",
                   "rh": "reihenhaus", "reihenendhaus": "reihenhaus",
                   "mfh": "mehrfamilienhaus", "wohnung": "mehrfamilienhaus"}
        return mapping.get(t, t)

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v):
        v = _unwrap(v)
        return v.strip().lower().replace(" ", "_") if isinstance(v, str) else v


class Buchung(BaseModel):
    slot_id: int = Field(..., ge=1)
    art: Literal["vor_ort", "video"] = "video"


class Gespraechsende(BaseModel):
    grund: Literal["termin_gebucht", "disqualifiziert", "kein_interesse",
                   "rueckruf_vereinbart", "falsche_person", "opt_out", "sonstiges"]


# ---------------------------------------------------------------- Tools
class Tools:
    def __init__(self, con, lead_id: int):
        self.con, self.lead_id = con, lead_id
        self.ended: Optional[str] = None
        self.errors = 0
        self.offered: set[int] = set()
        self.booked = False

    def check_slots(self, **_):
        st = self.con.execute("SELECT status FROM leads WHERE id = ?", [self.lead_id]).fetchone()[0]
        if st != "qualifiziert":
            return {"ok": False, "fehler": f"Lead-Status ist '{st}'. Termine gibt es nur für qualifizierte Leads "
                                           "(Eigentümer, kein Mehrfamilienhaus). Erst fertig qualifizieren."}
        rows = self.con.execute(
            "SELECT id, start FROM slots WHERE lead_id IS NULL AND start > now() ORDER BY start LIMIT 6"
        ).fetchall()
        self.offered = {r[0] for r in rows[:2]}   # Agent soll max. 2 Termine anbieten
        rows = rows[:2]
        wt = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        return {"freie_termine": [{"slot_id": r[0],
                                   "zeit": f"{wt[r[1].weekday()]} {r[1]:%d.%m. %H:%M} Uhr"} for r in rows]}

    def update_lead(self, **kwargs):
        q = Qualifizierung(**kwargs)
        data = q.model_dump(exclude_none=True)
        if data.get("status") == "qualifiziert":
            cur = self.con.execute("SELECT eigentuemer, gebaeudetyp FROM leads WHERE id = ?",
                                   [self.lead_id]).fetchone()
            eig = data.get("eigentuemer", cur[0])
            typ = data.get("gebaeudetyp", cur[1])
            if eig is not True or typ is None or typ == "mehrfamilienhaus":
                return {"ok": False, "fehler": "Nicht qualifizierbar: Qualifiziert heißt Eigentümer UND bekannter "
                                               "Gebäudetyp, kein Mehrfamilienhaus. Fehlende Infos erfragen oder "
                                               "status 'disqualifiziert' setzen."}
        sets = ", ".join(f"{k} = ?" for k in data)
        self.con.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*data.values(), self.lead_id])
        return {"ok": True, "gespeichert": data}

    def book_appointment(self, **kwargs):
        b = Buchung(**kwargs)
        lead = self.con.execute("SELECT status, eigentuemer FROM leads WHERE id = ?",
                                [self.lead_id]).fetchone()
        # Geschäftsregel hart im Code, nicht nur im Prompt:
        if lead[0] != "qualifiziert" or not lead[1]:
            return {"ok": False, "fehler": "Lead ist nicht als qualifizierter Eigentümer gespeichert. "
                                           "Erst Qualifizierung abschließen und update_lead aufrufen."}
        if b.slot_id not in self.offered:
            return {"ok": False, "fehler": "Dieser Termin wurde dem Kunden nicht angeboten. "
                                           "Erst check_slots, dann die Termine vorlesen, "
                                           "dann nur den Termin buchen, den der Kunde gewählt hat."}
        slot = self.con.execute("SELECT start, lead_id FROM slots WHERE id = ?", [b.slot_id]).fetchone()
        if slot is None:
            return {"ok": False, "fehler": f"slot_id {b.slot_id} existiert nicht. check_slots aufrufen."}
        if slot[1] is not None:
            return {"ok": False, "fehler": "Termin ist bereits vergeben. Anderen Termin anbieten."}
        self.con.execute("UPDATE slots SET lead_id = ? WHERE id = ?", [self.lead_id, b.slot_id])
        aid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM appointments").fetchone()[0]
        self.con.execute("INSERT INTO appointments VALUES (?, ?, ?, ?, now())",
                         [aid, self.lead_id, b.slot_id, b.art])
        self.booked = True
        return {"ok": True, "termin": f"{slot[0]:%d.%m.%Y %H:%M} Uhr", "art": b.art}

    def end_call(self, **kwargs):
        e = Gespraechsende(**kwargs)
        st = self.con.execute("SELECT status FROM leads WHERE id = ?", [self.lead_id]).fetchone()[0]
        if e.grund == "termin_gebucht" and not self.booked:
            return {"ok": False, "fehler": "Es wurde kein Termin gebucht. end_call mit termin_gebucht ist nicht erlaubt. "
                                           "Führe das Gespräch nach Leitfaden weiter."}
        if e.grund == "disqualifiziert" and st != "disqualifiziert":
            return {"ok": False, "fehler": "Erst update_lead mit status 'disqualifiziert' aufrufen, dann end_call."}
        if e.grund == "rueckruf_vereinbart" and st != "rueckruf":
            return {"ok": False, "fehler": "Erst update_lead mit status 'rueckruf' aufrufen, dann end_call."}
        self.ended = e.grund
        return {"ok": True, "hinweis": "Verabschiede dich jetzt kurz und freundlich."}

    def run(self, name: str, args: dict):
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_") or name == "run":
            self.errors += 1
            return {"ok": False, "fehler": f"Unbekanntes Tool: {name}"}
        try:
            return fn(**(args or {}))
        except ValidationError as ve:
            # Fehler zurück an das Modell -> es korrigiert sich selbst
            self.errors += 1
            msgs = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in ve.errors()]
            return {"ok": False, "validierungsfehler": msgs}


TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "check_slots",
        "description": "Liefert zwei freie Beratungstermine. Nur aufrufen, wenn der Lead qualifiziert ist.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "update_lead",
        "description": "Speichert Infos im CRM. Nach JEDER neuen Info des Kunden sofort aufrufen. "
                       "Nur Felder mitschicken, die du kennst.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["in_qualifizierung", "qualifiziert",
                       "disqualifiziert", "rueckruf", "kein_interesse", "opt_out"]},
            "eigentuemer": {"type": "boolean", "description": "true oder false"},
            "gebaeudetyp": {"type": "string", "enum": ["einfamilienhaus", "doppelhaushaelfte",
                            "reihenhaus", "mehrfamilienhaus", "gewerbe", "sonstiges"]},
            "dachflaeche_m2": {"type": "number", "description": "Zahl in Quadratmetern, z. B. 60"},
            "jahresverbrauch_kwh": {"type": "integer", "description": "Zahl in kWh, z. B. 4500"},
            "notiz": {"type": "string"}},
            "required": ["status"]}}},
    {"type": "function", "function": {
        "name": "book_appointment",
        "description": "Bucht den Termin, den der Kunde ausdrücklich gewählt hat. slot_id aus check_slots.",
        "parameters": {"type": "object", "properties": {
            "slot_id": {"type": "integer"},
            "art": {"type": "string", "enum": ["video", "vor_ort"]}},
            "required": ["slot_id"]}}},
    {"type": "function", "function": {
        "name": "end_call",
        "description": "Beendet das Gespräch.",
        "parameters": {"type": "object", "properties": {
            "grund": {"type": "string", "enum": ["termin_gebucht", "disqualifiziert", "kein_interesse",
                      "rueckruf_vereinbart", "falsche_person", "opt_out", "sonstiges"]}},
            "required": ["grund"]}}},
]


# ---------------------------------------------------------------- Gesprächsleitfaden
def system_prompt(lead) -> str:
    return f"""Du bist {AGENT_NAME}, Telefon-Agentin von {FIRMA}, einem Anbieter für Photovoltaikanlagen.
Du rufst {lead['name']} (PLZ {lead['plz']}) an. Die Person hat über "{lead['quelle']}" Interesse an einer PV-Anlage angemeldet.

ZIEL: Lead qualifizieren und, falls qualifiziert, einen kostenlosen Beratungstermin buchen.

ABLAUF:
1. Begrüßung in GENAU zwei kurzen Sätzen, z. B.: "Guten Tag, hier ist {AGENT_NAME} von {FIRMA}. Sie hatten sich für eine Solaranlage interessiert, passt es kurz?"
2. Qualifizierung, EINE Frage pro Antwort:
   a) Eigentümer der Immobilie?  b) Gebäudetyp?  c) ungefähre Dachfläche?  d) Stromverbrauch pro Jahr?
   Nach jeder Antwort update_lead mit status "in_qualifizierung" aufrufen.
3. Qualifiziert = Eigentümer UND kein Mehrfamilienhaus. Dann update_lead mit status "qualifiziert",
   danach check_slots, die zwei Termine vorlesen und AUF DIE ANTWORT DES KUNDEN WARTEN.
   Erst wenn der Kunde einen Termin gewählt hat: book_appointment mit genau dieser slot_id,
   dann Termin bestätigen und end_call mit "termin_gebucht".
4. Nicht qualifiziert (z. B. Mieter): höflich erklären, update_lead mit "disqualifiziert", end_call.
5. Keine Zeit: Rückruf anbieten, update_lead mit "rueckruf", end_call mit "rueckruf_vereinbart".
6. Opt-outs (Beleidigung, "kein Interesse", "nicht mehr anrufen") behandelt das System automatisch.

REGELN:
- Sprich wie am Telefon: kurze Sätze, maximal zwei Sätze pro Antwort, keine Listen, kein Markdown.
- Nenne NIEMALS Preise, Förderungen, Renditen oder Amortisationszeiten. Das klärt die Fachberatung im Termin.
- Erfinde keine Termine. Nur Termine aus check_slots nennen.
- Wenn die Person nicht {lead['name']} ist: nach einem besseren Zeitpunkt fragen, end_call mit "falsche_person".
- Siezen. Freundlich, aber zielstrebig."""


# Output-Guardrail: prüft jede Agentenantwort auf verbotene Inhalte
PRICE_PATTERN = re.compile(r"\d[\d.,]*\s*(€|euro|eur\b|prozent|%)", re.IGNORECASE)

def guardrail_check(text: str) -> list[str]:
    flags = []
    if PRICE_PATTERN.search(text):
        flags.append("preis_oder_zahl_genannt")
    if len(text.split()) > 60:
        flags.append("antwort_zu_lang")
    return flags


# Input-Guardrail: deterministische Opt-out-Erkennung VOR dem LLM
OPT_OUT_PATTERN = re.compile(
    r"(fick|fuck|arschloch|hure|wichser|verpiss|halt.{0,5}(maul|fresse)|"
    r"kein(e|en)? (interesse|bedarf|lust)|nicht (mehr )?an(rufen|zurufen)|"
    r"(will|möchte|brauche|brauch) (ich )?((doch|eigentlich|gar|jetzt) )*(nichts|keine (solaranlage|pv|anlage))|"
    r"doch kein interesse|hat sich erledigt|anders überlegt|doch nicht mehr|nicht mehr interessiert|"
    r"ruf(en)? sie (mich )?nicht|lass(en)? sie mich in ruhe|"
    r"(daten|nummer) l(ö|oe)schen|l(ö|oe)schen sie (meine )?(daten|nummer)|keine werbung|werbeanruf|belästig|anzeige erstatten)",
    re.IGNORECASE)

def is_opt_out(text: str) -> bool:
    return bool(OPT_OUT_PATTERN.search(text))


# Fakten-Guardrail: Behauptungen des Agenten gegen den echten Tool-Zustand prüfen
BOOKING_CLAIM = re.compile(
    r"termin\w*\W+(\w+\W+){0,6}?(gebucht|reserviert|eingetragen|vorgemerkt|fest)\b|"
    r"\b(buche|gebucht|reserviere|trage)\b\W+(\w+\W+){0,6}?termin", re.IGNORECASE)
TIME_CLAIM = re.compile(r"\b\d{1,2}([:.]\d{2})?\s*uhr\b|\b\d{1,2}:\d{2}\b", re.IGNORECASE)

def fact_check(text: str, tools: "Tools") -> Optional[str]:
    if BOOKING_CLAIM.search(text) and not tools.booked:
        return ("SYSTEM-KORREKTUR: Du hast behauptet, einen Termin zu buchen oder gebucht zu haben, "
                "aber book_appointment wurde nicht erfolgreich aufgerufen. Nichts ist gebucht. "
                "Behaupte keine Buchung. Führe das Gespräch nach Leitfaden weiter. "
                "Buchen darfst du nur bei qualifizierten Leads, nachdem der Kunde einen angebotenen Termin gewählt hat.")
    if TIME_CLAIM.search(text) and not tools.offered:
        return ("SYSTEM-KORREKTUR: Du hast Uhrzeiten genannt, ohne check_slots aufzurufen. "
                "Diese Termine sind erfunden. Rufe check_slots auf und nenne nur diese Termine.")
    return None


# Robuster LLM-Aufruf: Wiederholungsschleifen und Serverfehler dürfen den Anruf nie crashen
LLM_OPTIONS = {"temperature": 0.3, "repeat_penalty": 1.15, "num_predict": 300}
LLM_TIMEOUT = 120   # Sekunden pro Aufruf; danach Abbruch statt endlos hängen
_client = ollama.Client(timeout=LLM_TIMEOUT)

def safe_chat(**kwargs):
    opts = {**LLM_OPTIONS, **kwargs.pop("options", {})}
    for attempt in range(3):
        try:
            return _client.chat(options=opts, **kwargs)
        except Exception as e:   # ResponseError, Timeout, Verbindungsfehler
            print(f"\n   [LLM-FEHLER] {type(e).__name__}: {str(e)[:80]} -> Versuch {attempt + 2}/3", flush=True)
            opts = {**opts, "temperature": min(opts.get("temperature", 0.3) + 0.3, 1.0),
                    "repeat_penalty": opts.get("repeat_penalty", 1.1) + 0.15}
    return None


# ---------------------------------------------------------------- Agent-Loop
def agent_turn(messages, tools: Tools, log_flags: list, _retry: int = 0) -> str:
    for _ in range(MAX_TOOL_STEPS):
        resp = safe_chat(model=MODEL, messages=messages, tools=TOOL_SCHEMAS)
        if resp is None:
            log_flags.append("llm_fehler")
            return "Entschuldigung, die Verbindung ist gerade schlecht. Können Sie das bitte wiederholen?"
        msg = resp.message
        messages.append(msg)
        if not msg.tool_calls:
            text = (msg.content or "").strip()
            correction = fact_check(text, tools)
            if correction:
                log_flags.append("halluzination_blockiert")
                log(f"   [GUARDRAIL] Halluzination blockiert: {text[:70]}...")
                messages.pop()                       # falsche Antwort verwerfen
                if _retry < 2:
                    messages.append({"role": "user", "content": correction})
                    return agent_turn(messages, tools, log_flags, _retry + 1)
                return "Einen Moment bitte, ich prüfe kurz die freien Termine."
            flags = guardrail_check(text)
            if flags:
                log_flags.extend(flags)
                log(f"   [GUARDRAIL] {flags}")
            return text
        for tc in msg.tool_calls:
            result = tools.run(tc.function.name, tc.function.arguments)
            log(f"   [TOOL] {tc.function.name}({json.dumps(tc.function.arguments, ensure_ascii=False)}) "
                  f"-> {json.dumps(result, ensure_ascii=False, default=str)}")
            messages.append({"role": "tool", "content": json.dumps(result, ensure_ascii=False, default=str),
                             "tool_name": tc.function.name})
    return "Entschuldigung, einen Moment bitte."


class Extraktion(BaseModel):
    eigentuemer: Optional[bool] = None
    gebaeudetyp: Optional[Literal["einfamilienhaus", "doppelhaushaelfte", "reihenhaus",
                                  "mehrfamilienhaus", "gewerbe", "sonstiges"]] = None
    dachflaeche_m2: Optional[float] = Field(None, ge=5, le=2000)
    jahresverbrauch_kwh: Optional[int] = Field(None, ge=500, le=100_000)


def post_call_extraction(con, lead_id: int, transcript: list) -> dict:
    """Zweiter LLM-Aufruf nach dem Gespräch: liest das Transkript und füllt fehlende CRM-Felder.
    Robuster als sich darauf zu verlassen, dass das Modell während des Gesprächs jedes Mal speichert."""
    text = "\n".join(f"{'Kunde' if m['role'] == 'user' else 'Agent'}: {m['content']}"
                     for m in transcript if m["role"] in ("user", "assistant") and m["content"])
    try:
        resp = safe_chat(model=MODEL, format=Extraktion.model_json_schema(), options={"temperature": 0},
                           messages=[{"role": "system", "content":
                                      "Extrahiere aus dem Telefonat NUR Fakten, die der KUNDE selbst genannt hat. "
                                      "Unbekanntes als null. Gebäudetyp klein geschrieben. Antworte nur mit JSON."},
                                     {"role": "user", "content": text}])
        ext = Extraktion.model_validate_json(resp.message.content)
    except Exception as e:                      # Extraktion darf den Anruf nie crashen
        log(f"   [EXTRAKTION] fehlgeschlagen: {e}")
        return {}
    cols = ["eigentuemer", "gebaeudetyp", "dachflaeche_m2", "jahresverbrauch_kwh"]
    current = dict(zip(cols, con.execute(f"SELECT {', '.join(cols)} FROM leads WHERE id = ?",
                                         [lead_id]).fetchone()))
    filled = {k: v for k, v in ext.model_dump().items() if v is not None and current[k] is None}
    if filled:
        sets = ", ".join(f"{k} = ?" for k in filled)
        con.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*filled.values(), lead_id])
        log(f"   [EXTRAKTION] ergänzt: {filled}")
    return filled


def run_call(con, lead_id: int, get_input=None) -> dict:
    """get_input(messages) -> str. Standard: Tastatur. Der Simulator übergibt hier einen LLM-Kunden."""
    get_input = get_input or (lambda _msgs: input("Kunde: "))
    cols = ["id", "name", "telefon", "plz", "quelle", "status"]
    row = con.execute(f"SELECT {', '.join(cols)} FROM leads WHERE id = ?", [lead_id]).fetchone()
    if row is None:
        log(f"Lead {lead_id} nicht gefunden."); return {}
    lead = dict(zip(cols, row))
    tools, flags = Tools(con, lead_id), []
    greeting = (f"Guten Tag, hier ist {AGENT_NAME} von {FIRMA}. "
                f"Sie hatten sich für eine Solaranlage interessiert, passt es gerade kurz?")
    messages = [{"role": "system", "content": system_prompt(lead)},
                {"role": "user", "content": "(Anruf wird angenommen) Ja, hallo?"},
                {"role": "assistant", "content": greeting}]
    start, turns, latencies = datetime.now(), 0, []

    log(f"\n=== Anruf bei {lead['name']} ({lead['telefon']}) — '/q' zum Auflegen ===\n")
    log("Kunde: Ja, hallo?")
    log(f"{AGENT_NAME}: {greeting}   (fest)\n")
    while True:
        user = get_input(messages).strip()
        if get_input.__name__ != "<lambda>":   # Simulator-Eingaben sichtbar machen
            log(f"Kunde: {user}")
        if user.lower() in ("/q", "/quit") or turns >= 15:
            tools.ended = tools.ended or ("timeout" if turns >= 15 else "aufgelegt")
            break
        messages.append({"role": "user", "content": user})

        if is_opt_out(user):   # hart im Code, das LLM wird gar nicht gefragt
            tools.run("update_lead", {"status": "opt_out", "notiz": "Opt-out per Input-Guardrail"})
            tools.ended = "opt_out"
            bye = "Verstanden, ich trage Sie aus und wünsche Ihnen einen schönen Tag."
            messages.append({"role": "assistant", "content": bye})
            log("   [GUARDRAIL] Opt-out erkannt -> CRM gesperrt, Gespräch beendet")
            log(f"{AGENT_NAME}: {bye}   (fest)\n")
            turns += 1
            break

        t0 = time.time()
        reply = agent_turn(messages, tools, flags)
        latencies.append(time.time() - t0)
        turns += 1
        log(f"{AGENT_NAME}: {reply}   ({latencies[-1]:.1f}s)\n")
        if tools.ended:
            break

    transcript = [{"role": m["role"] if isinstance(m, dict) else m.role,
                   "content": m["content"] if isinstance(m, dict) else (m.content or "")}
                  for m in messages if (m["role"] if isinstance(m, dict) else m.role) != "system"]
    extracted = post_call_extraction(con, lead_id, transcript)

    # Ground Truth: Ergebnis aus CRM-Zustand, nicht aus der Behauptung des Modells
    st_now = con.execute("SELECT status FROM leads WHERE id = ?", [lead_id]).fetchone()[0]
    if tools.booked:
        truth = "termin_gebucht"
    elif st_now == "opt_out":
        truth = "opt_out"
    elif st_now == "disqualifiziert":
        truth = "disqualifiziert"
    elif st_now == "rueckruf":
        truth = "rueckruf_vereinbart"
    elif tools.ended == "falsche_person":
        truth = "falsche_person"
    else:
        truth = tools.ended or "offen"
    if tools.ended and tools.ended not in (truth, "timeout", "aufgelegt"):
        flags.append("falsches_gespraechsende")
    tools.ended = truth

    cid = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM calls").fetchone()[0]
    con.execute("INSERT INTO calls VALUES (?, ?, ?, now(), ?, ?, ?, ?, ?)",
                [cid, lead_id, start, tools.ended, turns, tools.errors,
                 json.dumps(flags), json.dumps(transcript, ensure_ascii=False)])

    final = con.execute("SELECT status, eigentuemer, gebaeudetyp, dachflaeche_m2, jahresverbrauch_kwh "
                        "FROM leads WHERE id = ?", [lead_id]).fetchone()
    booked = con.execute("SELECT COUNT(*) FROM appointments WHERE lead_id = ?", [lead_id]).fetchone()[0] > 0
    log("=== Gesprächsende ===")
    log(f"Ergebnis: {tools.ended} | Turns: {turns} | Tool-Fehler: {tools.errors} | Guardrail-Flags: {flags}")
    log(f"CRM: status={final[0]}, eigentuemer={final[1]}, typ={final[2]}, "
        f"dach={final[3]} m², verbrauch={final[4]} kWh | Termin im System: {booked}")
    return {"ergebnis": tools.ended, "turns": turns, "tool_fehler": tools.errors, "flags": flags,
            "status": final[0], "eigentuemer": final[1], "gebaeudetyp": final[2],
            "termin_im_system": booked, "extrahiert": extracted,
            "latenz_avg": sum(latencies) / len(latencies) if latencies else 0.0,
            "transkript": transcript}


def show_kpis(con):
    n = con.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    if n == 0:
        print("Noch keine Anrufe."); return
    print("\n=== KPIs ===")
    for ergebnis, anzahl in con.execute("SELECT ergebnis, COUNT(*) FROM calls GROUP BY 1 ORDER BY 2 DESC").fetchall():
        print(f"{ergebnis:<22} {anzahl:>3}  ({anzahl / n:.0%})")
    fe, avg_t = con.execute("SELECT SUM(tool_fehler), AVG(turns) FROM calls").fetchone()
    print(f"Anrufe: {n} | Ø Turns: {avg_t:.1f} | Tool-Fehler gesamt: {fe}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--kpis", action="store_true")
    a = ap.parse_args()
    con = init_db(reset=a.reset)
    if a.kpis:
        show_kpis(con)
    else:
        lid = a.lead or (con.execute("SELECT MIN(id) FROM leads WHERE status = 'offen'").fetchone()[0])
        if lid is None:
            print("Keine offenen Leads. Mit --reset neu aufsetzen.")
        else:
            run_call(con, lid)
    con.close()
