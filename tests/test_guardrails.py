"""Guardrails: Regex-Schicht und Faktencheck gegen den Tool-Zustand."""
import pytest

import agent


@pytest.mark.parametrize("text", [
    "Ich habe kein Interesse, rufen Sie mich nicht mehr an.",
    "Hat sich erledigt, ich hab es mir anders überlegt.",
    "Löschen Sie meine Daten.",
    "Verpiss dich mit deinen Werbeanrufen!",
])
def test_opt_out_is_recognised(text):
    assert agent.is_opt_out(text)


@pytest.mark.parametrize("text", [
    "Ja, ich bin Eigentümer eines Einfamilienhauses.",
    "Was kostet das denn ungefähr?",
    "Ich habe Interesse an einem Termin.",
])
def test_normal_answers_are_not_opt_outs(text):
    assert not agent.is_opt_out(text)


def test_price_mentions_are_flagged():
    assert "preis_oder_zahl_genannt" in agent.guardrail_check("Das kostet etwa 12.000 €.")
    assert "preis_oder_zahl_genannt" in agent.guardrail_check("Die Förderung liegt bei 30 %.")
    assert agent.guardrail_check("Das klärt die Fachberatung im Termin.") == []


def test_overlong_answers_are_flagged():
    assert "antwort_zu_lang" in agent.guardrail_check("Wort " * 61)


def test_booking_claim_without_booking_is_corrected(con, no_jev):
    tools = agent.Tools(con, 1)
    correction = agent.fact_check("Super, Ihr Termin ist gebucht.", tools)
    assert correction and "book_appointment" in correction
    tools.booked = True
    assert agent.fact_check("Super, Ihr Termin ist gebucht.", tools) is None


def test_invented_times_are_corrected(con, no_jev):
    tools = agent.Tools(con, 1)
    assert agent.fact_check("Passt Ihnen Dienstag um 14 Uhr?", tools)
    tools.offered = {1}
    assert agent.fact_check("Passt Ihnen Dienstag um 14 Uhr?", tools) is None


def test_fact_check_prefers_jev_when_available(con, jev_stub):
    tools = agent.Tools(con, 1)
    jev_stub["behauptet_buchung"] = 0.95
    assert agent.fact_check("Alles klar, das habe ich für Sie eingetragen.", tools)
    jev_stub["behauptet_buchung"] = 0.05
    flags = []
    jev_stub["nennt_preis"] = 0.9
    assert agent.fact_check("Rund zehntausend Euro.", tools, flags) is None
    assert flags == ["preis_oder_zahl_genannt"]
