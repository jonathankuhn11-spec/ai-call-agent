"""Gemeinsame Testbausteine.

Alle Tests laufen ohne Ollama und ohne Jev-Zugang: Das LLM wird durch ein Skript
ersetzt, die Jev-API durch einen Stub. Geprüft wird damit genau der Teil, der in
Produktion deterministisch sein muss: Geschäftsregeln, Guardrails, Routing.
"""
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

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if "format" in kwargs:
            return SimpleNamespace(message=SimpleNamespace(content="{}"))
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
    def install(script):
        scripted = ScriptedLLM(script)
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
