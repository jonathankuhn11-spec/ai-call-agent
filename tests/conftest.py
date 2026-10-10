"""Gemeinsame Testbausteine.

Alle Tests laufen ohne Ollama und ohne Jev-Zugang: Das LLM wird durch ein Skript
ersetzt, die Jev-API durch einen Stub. Geprüft wird damit genau der Teil, der in
Produktion deterministisch sein muss: Geschäftsregeln, Guardrails, Routing.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
import jev  # noqa: E402


@pytest.fixture
def con(tmp_path):
    """Frisches Mock-CRM mit Seed-Leads und freien Slots."""
    c = agent.init_db(reset=True, path=str(tmp_path / "test.duckdb"))
    yield c
    c.close()


@pytest.fixture
def no_jev(monkeypatch):
    """Jev abgeschaltet: Nur Regeln und LLM."""
    monkeypatch.setattr(jev, "_KEY", None)
    monkeypatch.setattr(agent, "USE_JEV", False)


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(agent, "VERBOSE", False)


class ScriptedLLM:
    """Ersetzt agent.safe_chat: liefert vorbereitete Antworten in fester Reihenfolge.

    Ein Eintrag ist entweder ein String (Textantwort) oder eine Liste von
    (tool_name, argumente)-Tupeln (Tool-Aufrufe). Strukturierte Extraktion
    (format=...) liefert immer ein leeres JSON-Objekt.
    """

    def __init__(self, script, extractions=None):
        self.script = list(script)
        self.extractions = list(extractions or [])
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if "format" in kwargs:
            payload = self.extractions.pop(0) if self.extractions else {}
            return SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))
        if not self.script:
            raise AssertionError("Das LLM-Skript ist aufgebraucht, der Agent fragt öfter als erwartet.")
        step = self.script.pop(0)
        if isinstance(step, str):
            return SimpleNamespace(message=SimpleNamespace(role="assistant", content=step, tool_calls=None))
        calls = [SimpleNamespace(function=SimpleNamespace(name=n, arguments=a)) for n, a in step]
        return SimpleNamespace(message=SimpleNamespace(role="assistant", content="", tool_calls=calls))


@pytest.fixture
def llm(monkeypatch):
    """Factory: llm([...]) installiert ein Skript und gibt es zurück."""
    def install(script, extractions=None):
        scripted = ScriptedLLM(script, extractions)
        monkeypatch.setattr(agent, "safe_chat", scripted)
        return scripted
    return install


def customer(*lines):
    """Skriptierter Kunde für run_call: gibt die Zeilen nacheinander zurück."""
    it = iter(list(lines) + ["/q"] * 20)

    def get_input(_messages):
        return next(it)
    return get_input


@pytest.fixture
def jev_stub(monkeypatch):
    """Jev-API-Stub: Antworten werden über ein veränderbares Dict gesteuert."""
    state = {"intent": "antwortet", "confidence": 0.95,
             "behauptet_buchung": 0.05, "nennt_preis": 0.05, "nennt_termin": 0.05, "down": False}

    def fake_post(url, json=None, headers=None, timeout=None):
        if state["down"]:
            raise jev.httpx.ConnectError("Jev nicht erreichbar")
        if "absicht" in json["questions"]:
            answers = {"absicht": {"type": "choice", "choice": state["intent"],
                                   "confidence": state["confidence"]}}
        else:
            answers = {k: {"noul": state[k]} for k in ("behauptet_buchung", "nennt_preis", "nennt_termin")}
        return SimpleNamespace(status_code=200, json=lambda: {"answers": answers},
                               raise_for_status=lambda: None)

    monkeypatch.setattr(jev.httpx, "post", fake_post)
    monkeypatch.setattr(jev, "_KEY", "test-key")
    monkeypatch.setattr(agent, "USE_JEV", True)
    monkeypatch.setitem(jev.STATS, "calls", 0)
    monkeypatch.setitem(jev.STATS, "fehler", 0)
    monkeypatch.setitem(jev.STATS, "latenzen", [])
    return state


# ---------------------------------------------------------------- Integrationsschicht
# Die Plattform (telli) wird durch den Simulator ersetzt, das Kundensystem durch einen
# httpx.MockTransport, der jede Zustellung signaturgeprüft entgegennimmt. Kein Account, kein Netz.
API_KEY = "test-api-key"
WEBHOOK_SECRET = "whsec_dGVzdC13ZWJob29rLXNlY3JldA=="
TOOL_SECRET = "test-tool-secret"
KUNDEN_SECRET = "whsec_a3VuZGVuLXNlY3JldA=="


@pytest.fixture
def konfig():
    from integrations.konfig import Konfig
    return Konfig(db_pfad=":memory:", telli_api_key=API_KEY, telli_webhook_secret=WEBHOOK_SECRET,
                  tool_secret=TOOL_SECRET, telli_agent_id="agent_test")


@pytest.fixture
def kundensystem():
    """Attrappe des Kundensystems: prüft die Signatur jeder Zustellung und merkt sich die Ereignisse."""
    import httpx

    from integrations.signatur import svix_pruefen

    zustand = {"empfangen": [], "ausfaelle": 0, "antwort": 200, "jetzt": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if zustand["ausfaelle"] > 0:
            zustand["ausfaelle"] -= 1
            return httpx.Response(503)
        jetzt = zustand["jetzt"]().timestamp() if zustand["jetzt"] else None
        svix_pruefen(KUNDEN_SECRET, request.content, request.headers, jetzt=jetzt)
        zustand["empfangen"].append({"typ": request.headers["x-ereignis-typ"], "id": request.headers["idempotency-key"],
                                     "body": json.loads(request.content)})
        return httpx.Response(zustand["antwort"])

    zustand["client"] = httpx.Client(transport=httpx.MockTransport(handler))
    return zustand


@pytest.fixture
def telli_attrappe():
    """Attrappe der telli-API (Create Contact v2, Schedule Call v1) für den Lead-Push."""
    import httpx

    from integrations.telli import TelliClient

    zustand = {"requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        zustand["requests"].append((request.method, request.url.path, body))
        if request.url.path == "/v2/contacts":
            return httpx.Response(201, json={"id": f"ct_{body['externalId']}", "type": "Contact"})
        if request.url.path == "/v1/schedule-call":
            return httpx.Response(200, json={"status": "success", "contact_id": body["contact_id"],
                                             "loop_id": f"loop_{body['contact_id']}"})
        return httpx.Response(404, json={"message": "nicht da"})

    zustand["client"] = TelliClient("test-api-key", client=httpx.Client(transport=httpx.MockTransport(handler),
                                                                        base_url="https://api.telli.com"))
    return zustand


@pytest.fixture
def app(con, konfig, kundensystem, telli_attrappe, quiet):
    from integrations.outbox import Outbox
    from integrations.server import erstelle_app

    outbox = Outbox(con, "https://kunde.example/webhooks", KUNDEN_SECRET, client=kundensystem["client"])
    return erstelle_app(konfig, con=con, outbox=outbox, telli_client=telli_attrappe["client"],
                        planer_starten=False)                                   # Planer-Takt im Test von Hand


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sim(client):
    from integrations.simulator import PlattformSimulator
    return PlattformSimulator(client, API_KEY, WEBHOOK_SECRET, TOOL_SECRET)
