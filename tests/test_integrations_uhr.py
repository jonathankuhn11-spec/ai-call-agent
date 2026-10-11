"""Zeit als injizierte Abhängigkeit: Die Schicht hat eine Uhr, die Tests stellen sie.

Was hier geprüft wird, ging mit der Rechneruhr nicht: dass eine Erinnerung genau zum fälligen
Zeitpunkt rausgeht, dass die Suite am Sonntagabend und in der Nacht der Zeitumstellung dasselbe
ergibt wie am Dienstagvormittag, und dass UTC-Zeitpunkte der Plattform über die Zeitumstellung
hinweg auf die richtige Wanduhrzeit fallen.
"""
from datetime import datetime, timedelta, timezone

import pytest

from integrations.modelle import iso_lesen, iso_utc
from integrations.uhr import Testuhr, Uhr


def test_the_system_clock_is_timezone_aware_utc():
    jetzt = Uhr()()
    assert jetzt.tzinfo is timezone.utc and abs(Uhr().sekunden() - jetzt.timestamp()) < 5


def test_the_test_clock_only_moves_when_told():
    uhr = Testuhr("2026-10-13T08:00:00Z")
    assert uhr() == datetime(2026, 10, 13, 8, tzinfo=timezone.utc) and uhr() == uhr()
    assert uhr.vor(hours=1, minutes=30) == datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)
    assert uhr.sekunden() == uhr().timestamp()
    uhr.stellen(datetime(2026, 10, 13, 12, 0, tzinfo=timezone(timedelta(hours=2))))         # 12:00 CEST
    assert uhr() == datetime(2026, 10, 13, 10, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        Testuhr(datetime(2026, 10, 13, 8))                                                  # naiv: welche Zone?


def test_every_timestamp_of_the_layer_comes_from_the_one_clock(app, client, uhr, sim, kundensystem):
    crm = app.state.ctx.crm
    uhr.stellen("2026-10-13T12:34:56Z")
    assert crm.jetzt() == datetime(2026, 10, 13, 14, 34, 56)                                  # Wanduhrzeit Berlin (CEST)
    sim.tool("update_lead", "c1", "1", status="in_qualifizierung", eigentuemer=True)
    crm.notiz(1, "x"); crm.sperren("+49 251 000005", "Test"); crm.ticket_anlegen(1, "c1", "sonstiges", "t")
    crm.verifizierung_merken("c1", 1, True); crm.telli_kontakt_speichern(1, "ct_1", "loop_1"); crm.webhook_merken("m1", "c1", "x")
    zeiten = [crm.con.execute(sql).fetchone()[0] for sql in (
        "SELECT erstellt_am FROM notizen", "SELECT seit FROM sperrliste", "SELECT erstellt_am FROM tickets",
        "SELECT versuch_am FROM verifizierungen", "SELECT erstellt_am FROM telli_kontakte", "SELECT empfangen_am FROM webhook_eingang")]
    assert zeiten == [datetime(2026, 10, 13, 14, 34, 56)] * 6
    b = sim.gespraech(crm.lead(2), "keine_zeit")                                              # Plattform-Zeitstempel: dieselbe Uhr
    assert b["call_ended"]["status"] == "verarbeitet"
    app.state.ctx.planer.tick()
    ereignis = next(e for e in kundensystem["empfangen"] if e["typ"] == "anruf.beendet")
    assert ereignis["body"]["zeit"] == "2026-10-13T12:34:56.000Z"
    assert crm.aufgaben(2)[0]["faellig_am"] == datetime(2026, 10, 14, 14, 34, 56)             # Rückruf "morgen": jetzt + 24 h
    assert client.get("/health").json()["zeit"] == "2026-10-13T12:34:56.000Z"


JEDERZEIT = ["2026-10-11T18:30:00Z",   # Sonntagabend: nächster Werktag-Slot liegt unter 24 h voraus
             "2026-10-16T21:59:00Z",   # Freitagnacht: Slots erst am Montag
             "2026-10-24T23:30:00Z",   # Nacht der Zeitumstellung (Sommerzeit endet am 25.10.2026 um 03:00)
             "2026-12-31T23:30:00Z",   # Jahreswechsel, 00:30 Uhr Berlin
             "2027-03-27T22:00:00Z"]   # Samstag vor Beginn der Sommerzeit 2027


@pytest.mark.parametrize("uhr", JEDERZEIT, indirect=True)
def test_booking_and_reminder_behave_the_same_at_any_time_of_week(app, uhr, sim, kundensystem):
    """Der Pilot bucht, die Erinnerung ist in der Zukunft fällig und geht genau dann raus, nie vorher, nie doppelt."""
    crm = app.state.ctx.crm
    b = sim.gespraech(crm.lead(1), "happy_path")
    assert b["call_ended"]["ergebnis"] == "termin_gebucht" and b["book"] == {"status": "success"}
    termin = crm.termin(1, zukunft=True)
    assert termin["start"] > crm.jetzt() and termin["start"].weekday() < 5 and termin["start"].hour in (9, 11, 14, 16)
    erinnerung = crm.erinnerungen(1)[0]
    assert erinnerung["status"] == "offen" and crm.jetzt() < erinnerung["faellig_am"] < termin["start"]
    assert erinnerung["faellig_am"] in (termin["start"] - timedelta(hours=24), termin["start"] - timedelta(hours=2))
    assert app.state.ctx.planer.tick()["erinnerungen"] == {"gesendet": 0}
    uhr.stellen(crm.start_utc(erinnerung["faellig_am"]) - timedelta(seconds=1))
    assert app.state.ctx.planer.tick()["erinnerungen"] == {"gesendet": 0}
    uhr.vor(seconds=1)
    assert app.state.ctx.planer.tick()["erinnerungen"] == {"gesendet": 1}
    assert app.state.ctx.planer.tick()["erinnerungen"] == {"gesendet": 0}
    gesendet = next(e for e in kundensystem["empfangen"] if e["typ"] == "termin.erinnerung")
    assert iso_lesen(gesendet["body"]["daten"]["start"]) == crm.start_utc(termin["start"])
    assert crm.erinnerungen(1)[0]["status"] == "gesendet"


@pytest.mark.parametrize("uhr", ["2026-10-23T08:00:00Z"], indirect=True)
def test_utc_slots_land_on_the_right_wall_clock_time_across_the_dst_change(app, uhr, sim):
    """Freitag vor der Zeitumstellung: Montagstermine liegen in der Winterzeit. 9 Uhr bleibt 9 Uhr, in UTC eine Stunde später."""
    crm = app.state.ctx.crm
    sim.tool("update_lead", "c", "1", status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    frei = sim.available("1", crm.lead(1)["telefon"])["available"]
    starts = [s["start_iso"] for s in frei]
    assert starts[:2] == ["2026-10-26T08:00:00.000Z", "2026-10-26T10:00:00.000Z"]              # Mo 09:00 und 11:00 CET
    assert all(iso_lesen(s["end_iso"]) - iso_lesen(s["start_iso"]) == timedelta(hours=1) for s in frei)
    assert sim.book("1", crm.lead(1)["telefon"], starts[0]) == {"status": "success"}
    assert crm.termin(1)["start"] == datetime(2026, 10, 26, 9, 0)
    assert crm.erinnerungen(1)[0]["faellig_am"] == datetime(2026, 10, 25, 9, 0)                  # Wanduhr: 24 h vorher
    assert crm.start_utc(datetime(2026, 10, 25, 9, 0)) == datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
    assert crm.start_utc(datetime(2026, 10, 23, 9, 0)) == datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)   # noch CEST
    assert sim.tool("lookup_lead", "c", "1")["naechster_termin"] == "26.10.2026 09:00 Uhr"
    assert iso_utc(crm.start_utc(crm.termin(1)["start"])) == starts[0]                            # Rundreise UTC -> Wand -> UTC
