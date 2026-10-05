"""Bewertungslogik des UAT: Was zählt als bestanden?"""
import simulate

HAPPY = next(p for p in simulate.PERSONAS if p["id"] == "happy_path")
PRICE = next(p for p in simulate.PERSONAS if p["id"] == "preisfrager")
TENANT = next(p for p in simulate.PERSONAS if p["id"] == "mieter")


def _result(**kw):
    base = {"ergebnis": "termin_gebucht", "termin_im_system": True, "flags": [], "tool_fehler": 0}
    return {**base, **kw}


def test_every_persona_has_an_expected_outcome():
    assert len(simulate.PERSONAS) == 10
    assert all(p["erwartet"] in {"termin_gebucht", "disqualifiziert", "rueckruf_vereinbart",
                                 "opt_out", "falsche_person"} for p in simulate.PERSONAS)


def test_booking_persona_passes_only_with_appointment_in_system():
    assert simulate.evaluate(HAPPY, _result()) == []
    assert "kein Termin im System" in simulate.evaluate(HAPPY, _result(termin_im_system=False))


def test_booking_for_a_disqualified_persona_is_a_failure():
    errors = simulate.evaluate(TENANT, _result(ergebnis="disqualifiziert", termin_im_system=True))
    assert "Termin gebucht, obwohl nicht erlaubt" in errors
    assert simulate.evaluate(TENANT, _result(ergebnis="disqualifiziert", termin_im_system=False)) == []


def test_price_mention_fails_the_price_persona_only():
    assert "Preis genannt" in simulate.evaluate(PRICE, _result(flags=["preis_oder_zahl_genannt"]))
    assert simulate.evaluate(HAPPY, _result(flags=["preis_oder_zahl_genannt"])) == []


def test_too_many_tool_errors_fail():
    assert "3 Tool-Fehler" in simulate.evaluate(HAPPY, _result(tool_fehler=3))
