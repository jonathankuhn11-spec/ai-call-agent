"""Geführter Modus: Leitfaden im Code, LLM nur für Extraktion und Formulierung."""
import pytest

import agent
import guided
from conftest import customer


@pytest.fixture
def templates(monkeypatch):
    monkeypatch.setattr(guided, "FORMULATE", False)


def test_happy_path_in_two_turns(con, no_jev, quiet, templates, llm):
    llm([], extractions=[
        {"eigentuemer": True, "gebaeudetyp": "einfamilienhaus", "dachflaeche_m2": 70, "jahresverbrauch_kwh": 4200},
        {"termin_wahl": "erster"},
    ])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, Eigentümer, Einfamilienhaus, 70 m², 4200 kWh.",
                                                          "Der erste passt."))
    assert r["ergebnis"] == "termin_gebucht" and r["termin_im_system"] is True
    assert r["turns"] == 2 and r["tool_fehler"] == 0 and r["modus"] == "gefuehrt"
    said = [m["content"] for m in r["transkript"] if m["role"] == "assistant"]
    assert "Beratungsgespräch" in said[-2]                            # Terminangebot kam vor der Wahl
    logged = [m["content"] for m in r["transkript"] if m["role"] == "tool"]
    assert len(logged) == 2 and '"termin_wahl": "erster"' in logged[-1]


def test_extraction_schema_forces_every_field():
    schema = guided.EXTRACTION_SCHEMA
    assert set(schema["required"]) == set(schema["properties"])
    assert "eigentuemer" in schema["required"] and "termin_wahl" in schema["required"]


def test_tenant_is_disqualified_by_code(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"eigentuemer": False}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ich wohne zur Miete, geht das trotzdem?"))
    assert r["ergebnis"] == "disqualifiziert" and r["status"] == "disqualifiziert"
    assert r["termin_im_system"] is False and r["turns"] == 1


def test_apartment_building_is_disqualified_even_for_owners(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"eigentuemer": True, "gebaeudetyp": "mehrfamilienhaus"}])
    r = guided.run_guided_call(con, 1, get_input=customer("Mir gehört ein Mehrfamilienhaus mit 6 Parteien."))
    assert r["ergebnis"] == "disqualifiziert"
    assert "Eigentümergemeinschaft" in r["transkript"][-1]["content"]


def test_vague_answers_still_lead_to_a_booking(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"eigentuemer": True}, {"gebaeudetyp": "reihenhaus"},
                         {"weiss_nicht": True}, {"weiss_nicht": True}, {"termin_wahl": "zweiter"}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, gehört mir.", "Reihenhaus.", "Keine Ahnung.",
                                                          "Normal halt.", "Der zweite."))
    assert r["ergebnis"] == "termin_gebucht" and r["termin_im_system"] is True
    assert r["turns"] == 5
    row = con.execute("SELECT dachflaeche_m2, jahresverbrauch_kwh FROM leads WHERE id = 1").fetchone()
    assert row == (None, None)
    slot = con.execute("SELECT slot_id FROM appointments WHERE lead_id = 1").fetchone()[0]
    second = con.execute("SELECT id FROM slots WHERE start > now() ORDER BY start LIMIT 2").fetchall()[1][0]
    assert slot == second


def test_price_question_is_deflected_without_numbers(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"eigentuemer": True, "fragt_nach_preis": True}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, Eigentümer. Was kostet das denn?"))
    reply = r["transkript"][-1]["content"]
    assert reply.startswith(guided.PRICE_LINE)
    assert guided.QUESTIONS["gebaeudetyp"] in reply
    assert agent.guardrail_check(reply) == []


def test_no_time_is_handled_through_extraction_without_jev(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"keine_zeit": True}])
    r = guided.run_guided_call(con, 1, get_input=customer("Bin gerade im Auto, morgen Nachmittag bitte."))
    assert r["ergebnis"] == "rueckruf_vereinbart" and r["status"] == "rueckruf"
    assert "entscheidung_extraktion" in r["flags"]


def test_unanswered_must_question_ends_in_callback(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{}, {}, {}])
    r = guided.run_guided_call(con, 1, get_input=customer("Hä?", "Wie bitte?", "Was?"))
    assert r["ergebnis"] == "rueckruf_vereinbart" and r["turns"] == 3
    asked = [m["content"] for m in r["transkript"] if m["content"] == guided.QUESTIONS["eigentuemer"]]
    assert len(asked) == guided.MAX_ASKS


def test_opt_out_is_decided_before_any_llm_call(con, no_jev, quiet, templates, llm):
    scripted = llm([])
    r = guided.run_guided_call(con, 1, get_input=customer("Kein Interesse, nicht mehr anrufen."))
    assert r["ergebnis"] == "opt_out" and scripted.calls == []


def test_formulation_is_used_when_it_passes_the_guardrails(con, no_jev, quiet, llm):
    llm(["Darf ich fragen, sind Sie Eigentümer der Immobilie?"], extractions=[{}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, hallo?"))
    assert r["transkript"][-1]["content"] == "Darf ich fragen, sind Sie Eigentümer der Immobilie?"
    assert "vorlage_gesprochen" not in r["flags"]


def test_formulation_with_a_price_falls_back_to_the_template(con, no_jev, quiet, llm):
    llm(["Gern, so eine Anlage kostet etwa 12.000 €. Sind Sie Eigentümer?"], extractions=[{}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, hallo?"))
    assert r["transkript"][-1]["content"] == guided.QUESTIONS["eigentuemer"]
    assert "vorlage_gesprochen" in r["flags"]


def test_offer_must_keep_both_times_or_the_template_is_spoken(con, no_jev, quiet, llm):
    llm(["Ich hätte da was nächste Woche, passt das?"],                 # Zeiten verschluckt
        extractions=[{"eigentuemer": True, "gebaeudetyp": "einfamilienhaus",
                      "dachflaeche_m2": 60, "jahresverbrauch_kwh": 3000}])
    r = guided.run_guided_call(con, 1, get_input=customer("Eigentümer, EFH, 60 m², 3000 kWh."))
    reply = r["transkript"][-1]["content"]
    assert "vorlage_gesprochen" in r["flags"] and "Welcher passt Ihnen besser?" in reply
    assert len(con.execute("SELECT id FROM slots WHERE start > now()").fetchall()) >= 2


def test_llm_outage_ends_the_call_with_a_callback(con, no_jev, quiet, monkeypatch):
    monkeypatch.setattr(agent, "safe_chat", lambda **kw: None)
    r = guided.run_guided_call(con, 1, get_input=customer("Ja?", "Hallo?", "Noch da?"))
    assert r["ergebnis"] == "rueckruf_vereinbart" and r["flags"].count("llm_fehler") == 2
    assert r["turns"] == 2


def test_turn_cap_in_guided_mode(con, no_jev, quiet, templates, llm, monkeypatch):
    monkeypatch.setattr(guided, "MAX_ASKS", 99)
    llm([], extractions=[{}] * 20)
    r = guided.run_guided_call(con, 1, get_input=customer(*["..."] * 20))
    assert r["turns"] == guided.MAX_TURNS and r["ergebnis"] == "timeout"


def test_extraction_tolerates_small_model_quirks():
    a = guided.Antwort.model_validate_json(
        '{"eigentuemer": "Ja", "gebaeudetyp": "Einfamilienhaus", "dachflaeche_m2": 0, '
        '"jahresverbrauch_kwh": "4200 kWh", "weiss_nicht": "false", "termin_wahl": "", "keine_zeit": 0}')
    assert a.eigentuemer == "ja" and a.gebaeudetyp == "einfamilienhaus"
    assert a.facts() == {"eigentuemer": True, "gebaeudetyp": "einfamilienhaus",
                         "dachflaeche_m2": None, "jahresverbrauch_kwh": 4200}
    assert a.weiss_nicht is False and a.termin_wahl == "unbekannt" and a.keine_zeit is False
    b = guided.Antwort.model_validate_json('{"eigentuemer": null, "gebaeudetyp": "Schloss", "termin_wahl": "1"}')
    assert b.eigentuemer == "unbekannt" and b.gebaeudetyp == "unbekannt" and b.termin_wahl == "erster"
    assert guided.Antwort().facts()["eigentuemer"] is None


def test_unprompted_no_to_ownership_is_ignored_without_context(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"eigentuemer": False}, {"eigentuemer": False}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, hallo, gern!", "Nein, ich wohne zur Miete."))
    said = [m["content"] for m in r["transkript"] if m["role"] == "assistant"]
    assert said[-2] == guided.QUESTIONS["eigentuemer"]        # erstes "nein" ignoriert, nachgefragt
    assert r["ergebnis"] == "disqualifiziert"                   # auf die Frage hin zählt das "nein"


def test_unprompted_no_with_ownership_words_counts(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"eigentuemer": False}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ich wohne zur Miete, geht das trotzdem?"))
    assert r["ergebnis"] == "disqualifiziert" and r["turns"] == 1


def test_greeting_does_not_make_the_customer_an_owner(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"eigentuemer": True, "gebaeudetyp": "einfamilienhaus", "dachflaeche_m2": 70}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, hallo, gern!"))
    assert r["transkript"][-1]["content"] == guided.QUESTIONS["eigentuemer"]
    row = con.execute("SELECT eigentuemer, gebaeudetyp, dachflaeche_m2 FROM leads WHERE id = 1").fetchone()
    assert row == (None, None, None)


def test_volunteered_facts_with_context_are_kept(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"eigentuemer": True, "gebaeudetyp": "reihenhaus", "dachflaeche_m2": 45,
                          "jahresverbrauch_kwh": 3500}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, mir gehört ein Reihenhaus, 45 m² Dach, 3500 kWh."))
    assert "Beratungsgespräch" in r["transkript"][-1]["content"]     # direkt zum Terminangebot


def test_insult_is_an_opt_out_even_if_jev_hears_no_time(con, quiet, templates, llm, jev_stub):
    jev_stub["intent"], jev_stub["confidence"] = "keine_zeit", 0.95
    llm([])
    r = guided.run_guided_call(con, 1, get_input=customer("Verpiss dich, ich hab keine Zeit für so was!"))
    assert r["ergebnis"] == "opt_out" and "entscheidung_regex" in r["flags"]


def test_invalid_json_counts_as_nothing_understood_not_as_outage(con, no_jev, quiet, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(agent, "safe_chat", lambda **kw: SimpleNamespace(message=SimpleNamespace(content="kaputt")))
    monkeypatch.setattr(guided, "FORMULATE", False)
    r = guided.run_guided_call(con, 1, get_input=customer("Ja?", "Hm?"))
    assert "llm_fehler" not in r["flags"]
    assert guided.QUESTIONS["eigentuemer"] in r["transkript"][-1]["content"]


def test_dont_know_on_a_must_question_asks_again_instead_of_skipping(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{}, {"weiss_nicht": True}, {"eigentuemer": True}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, hallo?", "Ähm, weiß nicht?", "Ja, mir."))
    questions = [m["content"] for m in r["transkript"] if m["role"] == "assistant"]
    assert questions.count(guided.QUESTIONS["eigentuemer"]) == 2
    assert questions[-1] == guided.QUESTIONS["gebaeudetyp"]


def test_jev_verdict_outranks_extraction_intent_flags(con, quiet, templates, llm, jev_stub):
    jev_stub["intent"] = "antwortet"
    llm([], extractions=[{"eigentuemer": True, "keine_zeit": True}])     # Extraktion irrt
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, gehört mir. Wann hätten Sie denn Zeit?"))
    assert r["ergebnis"] != "rueckruf_vereinbart"
    assert r["transkript"][-1]["content"] == guided.QUESTIONS["gebaeudetyp"]


def test_without_jev_the_extraction_may_end_the_call(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{"falsche_person": True}])
    r = guided.run_guided_call(con, 1, get_input=customer("Mein Mann ist nicht da."))
    assert r["ergebnis"] == "falsche_person" and "entscheidung_extraktion" in r["flags"]


@pytest.mark.parametrize("text, expected", [
    ("Ja, ich bin Eigentümer.", True), ("Ja.", True), ("Das Haus gehört mir.", True),
    ("Nein, ich wohne zur Miete.", False), ("Nee.", False), ("Nein, ich bin kein Eigentümer.", None),
    ("Wie meinen Sie das?", None), ("Weiß ich nicht.", None), ("Da bin ich nicht sicher.", None),
])
def test_parse_owner(text, expected):
    assert guided.parse_owner(text) is expected


@pytest.mark.parametrize("text, expected", [
    ("Ein Einfamilienhaus.", "einfamilienhaus"), ("Doppelhaushälfte.", "doppelhaushaelfte"),
    ("Reihenmittelhaus.", "reihenhaus"), ("Eine Wohnung im dritten Stock.", "mehrfamilienhaus"),
    ("Unsere Firma, eine Halle.", "gewerbe"), ("Freistehendes Haus.", "einfamilienhaus"), ("Hm?", None),
])
def test_parse_building(text, expected):
    assert guided.parse_building(text) == expected


def test_parse_number_and_dont_know():
    assert guided.parse_number("Etwa 70 m², denke ich.", 5, 2000) == 70
    assert guided.parse_number("So 4.200 kWh im Jahr.", 500, 100_000) == 4200
    assert guided.parse_number("4200", 500, 100_000) == 4200
    assert guided.parse_number("Drei Personen.", 500, 100_000) is None
    assert guided.parse_pending("dachflaeche_m2", "Keine Ahnung, normal halt.", []) == {"weiss_nicht": True}


def test_parse_choice_matches_offered_times():
    offered = [(1, "Di 07.10. 09:00 Uhr"), (2, "Di 07.10. 11:00 Uhr")]
    assert guided.parse_choice("Dienstag um 9 passt mir.", offered) == "erster"
    assert guided.parse_choice("11 Uhr wäre besser.", offered) == "zweiter"
    assert guided.parse_choice("Der zweite.", offered) == "zweiter"
    assert guided.parse_choice("Ja, gern.", offered) == "erster"
    assert guided.parse_choice("Beide nicht, leider.", offered) == "keiner"
    assert guided.parse_choice("Muss ich erst nachsehen.", offered) is None


def test_rules_carry_the_pending_question_even_if_extraction_fails(con, no_jev, quiet, templates, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(agent, "safe_chat", lambda **kw: SimpleNamespace(message=SimpleNamespace(content="{}")))
    r = guided.run_guided_call(con, 1, get_input=customer("Hallo?", "Ja, Eigentümer.", "Reihenhaus.",
                                                          "Etwa 50 m².", "3000 kWh.", "Der erste passt."))
    assert r["ergebnis"] == "termin_gebucht" and r["termin_im_system"] is True
    assert "regel_gedeutet" in r["flags"]


def test_jev_no_time_is_ignored_during_the_offer_and_without_request_words(con, quiet, templates, llm, jev_stub):
    jev_stub["intent"], jev_stub["confidence"] = "keine_zeit", 0.9
    llm([], extractions=[{"eigentuemer": True, "gebaeudetyp": "einfamilienhaus", "dachflaeche_m2": 60,
                          "jahresverbrauch_kwh": 3000}, {}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, mir gehört ein Einfamilienhaus, 60 m², 3000 kWh.",
                                                          "Wann hätten Sie denn Zeit? Der erste passt."))
    assert r["ergebnis"] == "termin_gebucht"


def test_tenant_saying_no_interest_is_disqualified_not_opted_out(con, no_jev, quiet, templates, llm):
    llm([], extractions=[{}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ich wohne zur Miete, dann hab ich wohl kein Interesse."))
    assert r["ergebnis"] == "disqualifiziert" and r["status"] == "disqualifiziert"


@pytest.mark.parametrize("text", [
    "Nein, ich muss fragen, sind Sie Eigentümer der Immobilie?",                 # Ja/Nein-Auftakt
    "Egal ob Eigentümer oder Mieter, Solarpanele gehen fast immer. Tschüss.",     # Zusatzinfo und Verabschiedung
    "Wir können den Termin leider nicht vereinbaren, da der Satz keinen Termintext enthält.",  # Meta-Sprache
    "Haben Sie eine Wohnung oder ein Haus?",                                      # Kern der Vorlage fehlt
    "",
])
def test_bad_formulations_fall_back_to_the_template(con, text):
    tools = agent.Tools(con, 1)
    assert not guided.formulation_ok(text, guided.QUESTIONS["eigentuemer"], tools, ("Eigentümer",), ending=False)


def test_good_formulation_passes(con):
    tools = agent.Tools(con, 1)
    assert guided.formulation_ok("Darf ich fragen, sind Sie Eigentümer der Immobilie?",
                                 guided.QUESTIONS["eigentuemer"], tools, ("Eigentümer",), ending=False)
    tools.booked, tools.offered = True, {1}
    assert guided.formulation_ok("Wunderbar, der Termin am Di 06.10. 09:00 Uhr ist eingetragen. Auf Wiederhören!",
                                 "Perfekt, ich habe den Termin am Di 06.10. 09:00 Uhr für Sie eingetragen.",
                                 tools, ("Di 06.10. 09:00 Uhr",), ending=True)


@pytest.mark.parametrize("text", ["Das kann ich Ihnen nicht sagen.", "Ich kann das nicht sagen.",
                                  "Weiß ich leider nicht.", "Hab ich nicht im Kopf."])
def test_dont_know_variants_skip_optional_questions(text):
    assert guided.parse_pending("dachflaeche_m2", text, []) == {"weiss_nicht": True}
