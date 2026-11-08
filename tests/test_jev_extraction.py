"""Geführter Modus ohne LLM: Jev bestimmt Absicht und Kategorien, Regeln die Zahlen, Vorlagen die Sätze."""
from types import SimpleNamespace

import pytest

import agent
import guided
import jev
from conftest import customer


@pytest.fixture
def no_llm(monkeypatch):
    """Jeder LLM-Aufruf lässt den Test scheitern."""
    def forbidden(**kwargs):
        raise AssertionError("Im LLM-freien Modus darf kein LLM aufgerufen werden")
    monkeypatch.setattr(agent, "safe_chat", forbidden)
    monkeypatch.setattr(guided, "EXTRACTOR", "jev")
    monkeypatch.setattr(guided, "FORMULATE", False)
    monkeypatch.setattr(guided, "LLM_FALLBACK", False)


@pytest.fixture
def jev_api(monkeypatch):
    """Jev-Stub für Intent und Extraktion. `turns` ist eine Liste von Extraktionsantworten je Kundenturn,
    jeweils {feld: wert} für choice-Fragen oder {feld: wahrscheinlichkeit} für noul-Fragen."""
    state = {"turns": [], "requests": [], "down": False, "confidence": 0.95}

    def fake_post(url, json=None, headers=None, timeout=None):
        if state["down"]:
            raise jev.httpx.ConnectError("Jev nicht erreichbar")
        questions = json["questions"]
        state["requests"].append(json)
        if "absicht" in questions:
            answers = {"absicht": {"choice": "antwortet", "confidence": 0.95}}
        else:
            turn = state["turns"].pop(0) if state["turns"] else {}
            answers = {}
            for key, q in questions.items():
                if q["type"] == "choice":
                    answers[key] = {"choice": turn.get(key, "unbekannt"), "confidence": state["confidence"]}
                else:
                    answers[key] = {"noul": turn.get(key, 0.05)}
        return SimpleNamespace(status_code=200, json=lambda: {"answers": answers}, raise_for_status=lambda: None)

    monkeypatch.setattr(jev.httpx, "post", fake_post)
    monkeypatch.setattr(jev, "_KEY", "test-key")
    monkeypatch.setattr(agent, "USE_JEV", True)
    return state


def test_booking_without_a_single_llm_call(con, quiet, no_llm, jev_api):
    jev_api["turns"] = [{"eigentuemer": "ja"}, {"gebaeudetyp": "einfamilienhaus"}, {}, {},
                        {"termin_wahl": "zweiter"}]
    r = guided.run_guided_call(con, 1, get_input=customer(
        "Ja, das Haus gehört uns.", "Ein Einfamilienhaus.", "So etwa 60 Quadratmeter.",
        "Ungefähr 4.500 kWh im Jahr.", "Der zweite Termin passt."))
    assert r["ergebnis"] == "termin_gebucht" and r["termin_im_system"] is True
    lead = con.execute("SELECT dachflaeche_m2, jahresverbrauch_kwh FROM leads WHERE id = 1").fetchone()
    assert lead == (60, 4500)                                   # Zahlen kamen aus den Regeln


def test_tenant_is_disqualified_by_jev_category(con, quiet, no_llm, jev_api):
    jev_api["turns"] = [{"eigentuemer": "nein"}]
    r = guided.run_guided_call(con, 1, get_input=customer("Das gehört unserem Vermieter."))
    assert r["ergebnis"] == "disqualifiziert"


def test_uncertain_jev_verdict_asks_again_instead_of_disqualifying(con, quiet, no_llm, jev_api):
    jev_api["confidence"] = 0.55
    jev_api["turns"] = [{"eigentuemer": "nein"}]
    r = guided.run_guided_call(con, 1, get_input=customer("Hm, schwierig."))
    assert r["ergebnis"] != "disqualifiziert"
    assert r["transkript"][-1]["content"] == guided.QUESTIONS["eigentuemer"]


def test_offer_phase_only_asks_jev_for_the_choice(con, quiet, no_llm, jev_api):
    jev_api["turns"] = [{"eigentuemer": "ja", "gebaeudetyp": "reihenhaus"}, {"weiss_nicht": 0.9},
                        {"weiss_nicht": 0.9}, {"termin_wahl": "erster"}]
    guided.run_guided_call(con, 1, get_input=customer(
        "Ja, das Reihenhaus gehört uns.", "Weiß ich nicht.", "Keine Ahnung.", "Der erste."))
    last = jev_api["requests"][-1]
    assert set(last["questions"]) == {"termin_wahl", "fragt_nach_preis"}
    assert "termin_1" in last["state"] and "termin_2" in last["state"]


def test_jev_outage_without_fallback_ends_in_callback(con, quiet, no_llm, jev_api):
    jev_api["down"] = True
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, das gehört mir.", "Ein Einfamilienhaus."))
    assert r["ergebnis"] == "rueckruf_vereinbart"


def test_jev_outage_falls_back_to_the_llm_when_allowed(con, quiet, jev_api, llm, monkeypatch):
    monkeypatch.setattr(guided, "EXTRACTOR", "jev")
    monkeypatch.setattr(guided, "FORMULATE", False)
    jev_api["down"] = True
    llm([], extractions=[{"eigentuemer": "nein"}])
    r = guided.run_guided_call(con, 1, get_input=customer("Wir wohnen zur Miete."))
    assert r["ergebnis"] == "disqualifiziert"

