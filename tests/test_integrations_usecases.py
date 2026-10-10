"""Eine Quelle: Use-Case-Definition, Prompt, Outcomes, Tools, Nachbereitung und Doku dürfen nicht driften."""
import json
import re
from pathlib import Path

import pytest

import agent
import guided
from integrations import konfigpaket
from integrations.modelle import ERGEBNISSE
from integrations.nachbereitung import OUTCOME_SCHLUESSEL
from integrations.server import HTTP_TOOLS
from integrations.usecases import alle

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("uid", list(alle()))
def test_committed_config_package_matches_the_code(uid):
    """docs/telli-konfig/<usecase>/ ist mit `python -m integrations konfigpaket --alle --out docs/telli-konfig` erzeugt."""
    u = alle()[uid]
    erzeugt = konfigpaket.erzeugen(u, "https://backend.kunde.example")
    ordner = ROOT / "docs" / "telli-konfig" / uid
    assert sorted(p.name for p in ordner.iterdir()) == sorted(erzeugt)
    for name, inhalt in erzeugt.items():
        assert (ordner / name).read_text(encoding="utf-8") == inhalt, f"{uid}/{name} weicht ab: konfigpaket neu erzeugen"


def test_pv_prompt_uses_exactly_the_questions_and_texts_of_the_local_agent():
    prompt = alle()["pv_outbound"].prompt()
    for frage in guided.QUESTIONS.values():
        assert frage in prompt
    for text in guided.DISQUALIFY.values():
        assert text in prompt
    assert guided.PRICE_LINE in prompt and guided.CALLBACK in prompt and agent.greeting_text() in prompt
    assert prompt.index("# 5. Einwände") > prompt.index("# 4. Gesprächsleitfaden")       # telli-Struktur: Einwände nach dem Ablauf


def test_handwritten_agent_doc_quotes_the_same_texts():
    doc = re.sub(r"\s+", " ", (ROOT / "docs" / "telli-agent-konfiguration.md").read_text(encoding="utf-8"))   # Zeilenumbrüche egal
    for frage in guided.QUESTIONS.values():
        assert frage in doc
    assert guided.PRICE_LINE in doc and agent.greeting_text() in doc
    for key in OUTCOME_SCHLUESSEL.values():
        assert f"`{key}`" in doc


@pytest.mark.parametrize("uid", list(alle()))
def test_outcomes_tools_and_results_are_consistent_with_backend(uid):
    u = alle()[uid]
    assert "ergebnis" in u.outcome_keys()
    ergebnis = next(o for o in u.outcomes if o.key == "ergebnis")
    assert set(ergebnis.optionen) <= set(ERGEBNISSE), "jeder Ergebniswert muss der Nachbereitung bekannt sein"
    assert set(u.tool_namen()) <= set(HTTP_TOOLS)
    for t in u.tools:
        assert re.search(rf"\b{t.name}\b", u.prompt()), f"{t.name} kommt im Prompt nicht vor"
    for o in u.outcomes:
        assert o.typ in ("boolean", "string", "number", "category")
        assert re.fullmatch(r"[a-z][a-z0-9_]*", o.key), "Outcome-Schlüssel in snake_case"
    for e in u.eigenschaften:
        assert re.fullmatch(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", e.key)          # Regel der Create-Contact-Property-API
        assert e.typ in ("string", "number", "boolean", "date", "datetime", "select", "multi_select", "phone_number", "email")


def test_pv_outcomes_cover_everything_the_postprocessing_reads():
    keys = set(alle()["pv_outbound"].outcome_keys())
    assert set(OUTCOME_SCHLUESSEL.values()) <= keys


def test_generated_json_has_the_api_shapes():
    paket = konfigpaket.erzeugen(alle()["telko_inbound"], "https://b.example/")
    props = json.loads(paket["contact_properties.json"])
    assert all(set(p) >= {"key", "dataType", "label"} for p in props)
    tools = json.loads(paket["custom_tools.json"])
    assert [t["name"] for t in tools] == alle()["telko_inbound"].tool_namen()
    assert all(t["url"].startswith("https://b.example/tools/") for t in tools)
    assert "WANN:" in tools[0]["description"] and "SAGEN:" in tools[0]["description"]
    assert json.loads(paket["webhook.json"])["events"] == ["call_ended", "call_rescheduled", "auto_dialer_status_changed"]
    assert "VERIFIZIERUNG_PFLICHT" in paket["README.md"] and "24 Stunden" in paket["README.md"]
    kalender = json.loads(konfigpaket.erzeugen(alle()["pv_outbound"])["calendar.json"])
    assert kalender["variante"] == "tools"
