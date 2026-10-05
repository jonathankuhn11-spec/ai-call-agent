"""Agent-Loop mit skriptiertem LLM: Tool-Aufrufe, Selbstkorrektur, Intent-Routing."""
import agent
import jev
from conftest import customer


def test_happy_path_books_only_after_customer_chose_a_slot(con, no_jev, quiet, llm):
    first = con.execute("SELECT MIN(id) FROM slots WHERE start > now()").fetchone()[0]
    llm([
        [("update_lead", {"status": "in_qualifizierung", "eigentuemer": True})],
        "Welchen Gebäudetyp haben Sie?",
        [("update_lead", {"status": "qualifiziert", "gebaeudetyp": "einfamilienhaus"}),
         ("check_slots", {})],
        "Ich hätte zwei Termine für Sie. Welcher passt?",
        [("book_appointment", {"slot_id": first})],
        [("end_call", {"grund": "termin_gebucht"})],
        "Dann bis dann, auf Wiederhören.",
    ])
    r = agent.run_call(con, 1, get_input=customer("Ja, ich bin Eigentümer.", "Einfamilienhaus.", "Der erste."))
    assert r["ergebnis"] == "termin_gebucht" and r["termin_im_system"] is True
    assert r["tool_fehler"] == 0 and r["status"] == "qualifiziert"


def test_hallucinated_booking_is_rejected_and_model_corrects_itself(con, no_jev, quiet, llm):
    scripted = llm(["Ihr Termin ist gebucht, Dienstag 14 Uhr!",       # erfunden -> Korrektur
                    "Sind Sie Eigentümer der Immobilie?"])             # zweiter Versuch
    r = agent.run_call(con, 1, get_input=customer("Ja, hallo?"))
    assert "halluzination_blockiert" in r["flags"]
    assert r["termin_im_system"] is False
    assert any("SYSTEM-KORREKTUR" in str(c["messages"][-1]) for c in scripted.calls)
    assert all(m["content"] != "Ihr Termin ist gebucht, Dienstag 14 Uhr!" for m in r["transkript"])


def test_result_comes_from_crm_state_not_from_the_models_claim(con, no_jev, quiet, llm):
    llm([[("end_call", {"grund": "termin_gebucht"})],          # abgelehnt: nichts gebucht
         "Sind Sie Eigentümer?"])
    r = agent.run_call(con, 1, get_input=customer("Hallo"))
    assert r["ergebnis"] != "termin_gebucht"
    assert r["tool_fehler"] == 0            # abgelehnter Aufruf ist ein Regelverstoß, kein Validierungsfehler


def test_opt_out_ends_the_call_without_asking_the_model(con, no_jev, quiet, llm):
    scripted = llm([])                      # jeder LLM-Aufruf würde hier fehlschlagen
    r = agent.run_call(con, 1, get_input=customer("Kein Interesse, nicht mehr anrufen."))
    assert r["ergebnis"] == "opt_out" and r["status"] == "opt_out"
    assert "entscheidung_regex" in r["flags"]
    assert all("format" in c for c in scripted.calls)   # nur die Extraktion danach


def test_jev_routes_wrong_person_and_no_time_deterministically(con, quiet, llm, jev_stub):
    for intent, expected, status in (("falsche_person", "falsche_person", "offen"),
                                     ("keine_zeit", "rueckruf_vereinbart", "rueckruf")):
        c = agent.init_db(reset=True, path=str(con.execute("PRAGMA database_list").fetchone()[2]) + intent)
        jev_stub["intent"], jev_stub["confidence"] = intent, 0.9
        llm([])
        r = agent.run_call(c, 1, get_input=customer("Mein Mann ist nicht da" if intent == "falsche_person"
                                                     else "Bin im Auto, morgen bitte"))
        assert r["ergebnis"] == expected and r["status"] == status
        assert "entscheidung_jev" in r["flags"]
        c.close()


def test_uncertain_jev_hands_over_to_the_model(con, quiet, llm, jev_stub):
    jev_stub["intent"], jev_stub["confidence"] = "keine_zeit", 0.4     # unter INTENT_THRESHOLD
    scripted = llm(["Kein Problem. Sind Sie Eigentümer der Immobilie?"])
    r = agent.run_call(con, 1, get_input=customer("Hm, weiß nicht"))
    assert "entscheidung_jev" not in r["flags"]
    assert r["status"] == "offen"
    assert any("tools" in c for c in scripted.calls)      # das LLM hat übernommen


def test_jev_outage_falls_back_to_regex_and_model(con, quiet, llm, jev_stub):
    jev_stub["down"] = True
    llm(["Sind Sie Eigentümer der Immobilie?"])
    r = agent.run_call(con, 1, get_input=customer("Ja, hallo?", "Rufen Sie mich nicht mehr an."))
    assert jev.STATS["fehler"] >= 1                              # Jev wurde gefragt und war weg
    assert r["ergebnis"] == "opt_out" and "entscheidung_regex" in r["flags"]
    assert r["turns"] == 2


def test_llm_outage_never_crashes_the_call(con, no_jev, quiet, monkeypatch):
    monkeypatch.setattr(agent, "safe_chat", lambda **kw: None)
    r = agent.run_call(con, 1, get_input=customer("Hallo?"))
    assert "llm_fehler" in r["flags"]
    assert r["ergebnis"] in ("aufgelegt", "offen")


def test_tool_loop_is_capped_per_turn(con, no_jev, quiet, llm):
    llm([[("check_slots", {})]] * agent.MAX_TOOL_STEPS + ["nie erreicht"])
    r = agent.run_call(con, 1, get_input=customer("Hallo"))
    assert "tool_limit" in r["flags"]
    assert r["transkript"][-1] == {"role": "assistant", "content": "Entschuldigung, einen Moment bitte."}
