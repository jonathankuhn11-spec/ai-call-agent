"""Geschäftsregeln im Code: Was das Modell auch behauptet, diese Prüfungen entscheiden."""
import pytest
from pydantic import ValidationError

import agent


def test_seed_data_has_leads_and_future_slots(con):
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 5
    assert con.execute("SELECT COUNT(*) FROM slots WHERE start > now()").fetchone()[0] > 0


def test_no_slots_before_qualification(con):
    tools = agent.Tools(con, 1)
    result = tools.check_slots()
    assert result["ok"] is False and "qualifizierte" in result["fehler"]
    assert tools.offered == set()


def test_check_slots_offers_at_most_two(con):
    tools = agent.Tools(con, 1)
    tools.update_lead(status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    result = tools.check_slots()
    assert len(result["freie_termine"]) == 2
    assert tools.offered == {s["slot_id"] for s in result["freie_termine"]}


def test_qualified_requires_owner_and_known_building_type(con):
    tools = agent.Tools(con, 1)
    assert tools.update_lead(status="qualifiziert")["ok"] is False
    assert tools.update_lead(status="qualifiziert", eigentuemer=False, gebaeudetyp="reihenhaus")["ok"] is False
    assert tools.update_lead(status="qualifiziert", eigentuemer=True, gebaeudetyp="mehrfamilienhaus")["ok"] is False
    assert tools.update_lead(status="qualifiziert", eigentuemer=True, gebaeudetyp="reihenhaus")["ok"] is True
    assert con.execute("SELECT status FROM leads WHERE id = 1").fetchone()[0] == "qualifiziert"


def test_booking_needs_qualified_owner_and_offered_slot(con):
    tools = agent.Tools(con, 1)
    first_slot = con.execute("SELECT MIN(id) FROM slots").fetchone()[0]
    assert tools.book_appointment(slot_id=first_slot)["ok"] is False          # nicht qualifiziert
    tools.update_lead(status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    assert tools.book_appointment(slot_id=first_slot)["ok"] is False          # nie angeboten
    offered = tools.check_slots()["freie_termine"][0]["slot_id"]
    result = tools.book_appointment(slot_id=offered)
    assert result["ok"] is True and tools.booked is True
    assert con.execute("SELECT COUNT(*) FROM appointments WHERE lead_id = 1").fetchone()[0] == 1
    assert con.execute("SELECT lead_id FROM slots WHERE id = ?", [offered]).fetchone()[0] == 1


def test_slot_cannot_be_booked_twice(con):
    tools_a, tools_b = agent.Tools(con, 1), agent.Tools(con, 2)
    for t in (tools_a, tools_b):
        t.update_lead(status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
        t.check_slots()
    slot = next(iter(tools_a.offered))
    assert tools_a.book_appointment(slot_id=slot)["ok"] is True
    assert "vergeben" in tools_b.book_appointment(slot_id=slot)["fehler"]


def test_end_call_cannot_claim_a_booking_that_did_not_happen(con):
    tools = agent.Tools(con, 1)
    assert tools.end_call(grund="termin_gebucht")["ok"] is False
    assert tools.end_call(grund="disqualifiziert")["ok"] is False             # Status nicht gesetzt
    tools.update_lead(status="disqualifiziert", eigentuemer=False)
    assert tools.end_call(grund="disqualifiziert")["ok"] is True
    assert tools.ended == "disqualifiziert"


def test_validation_errors_go_back_to_the_model_instead_of_crashing(con):
    tools = agent.Tools(con, 1)
    result = tools.run("update_lead", {"status": "qualifiziert", "dachflaeche_m2": 99999})
    assert result["ok"] is False and result["validierungsfehler"]
    assert tools.errors == 1
    assert tools.run("unbekanntes_tool", {})["ok"] is False
    assert tools.errors == 2


@pytest.mark.parametrize("raw, expected", [
    ({"status": "in_qualifizierung", "eigentuemer": "Ja"}, True),
    ({"status": "in_qualifizierung", "eigentuemer": {"description": "nein"}}, False),
    ({"status": "in_qualifizierung", "eigentuemer": "Mieter"}, False),
])
def test_small_models_answers_are_normalised(raw, expected):
    assert agent.Qualifizierung(**raw).eigentuemer is expected


def test_building_type_aliases_are_mapped():
    assert agent.Qualifizierung(status="in_qualifizierung", gebaeudetyp="EFH").gebaeudetyp == "einfamilienhaus"
    assert agent.Qualifizierung(status="in_qualifizierung", gebaeudetyp="Doppelhaus-Hälfte").gebaeudetyp == "doppelhaushaelfte"
    assert agent.Qualifizierung(status="In Qualifizierung").status == "in_qualifizierung"
    with pytest.raises(ValidationError):
        agent.Qualifizierung(status="in_qualifizierung", gebaeudetyp="schloss")
