"""Kanal-Fallback: Nie erreichte Leads bekommen eine Nachricht mit Buchungslink, aber nur mit Einwilligung,
nie gegen ein Opt-out, höchstens einmal im Sperrfenster und nie nachts."""
from datetime import datetime, time

import pytest

from integrations.aufbewahrung import Aufbewahrung
from integrations.modelle import AutoDialerEreignis
from integrations.nachbereitung import Nachbereitung
from integrations.nachfass import Nachfass, Nachfassregel

LINK = "https://termine.sonnenwerk.example/beratung"


@pytest.fixture
def crm(con):
    from integrations.crm import LocalCRM
    from integrations.uhr import Testuhr
    return LocalCRM(con, uhr=Testuhr("2026-10-13T10:00:00Z"))         # Dienstag, 12 Uhr in Berlin


@pytest.fixture
def nf(crm):
    return Nachfass(crm, Nachfassregel(link=LINK))


def aufgegeben(lead_id=4, grund="scheduler_gave_up_smart"):
    return AutoDialerEreignis.model_validate({"event": "auto_dialer_status_changed",
                                              "auto_dialer": {"status": "not_in_dialer", "reason": grund},
                                              "contact": {"id": f"ct_{lead_id}", "externalId": str(lead_id)}})


def test_rule_rejects_unknown_channel_missing_link_and_empty_window():
    with pytest.raises(ValueError):
        Nachfassregel(kanal="fax", link=LINK)
    with pytest.raises(ValueError):
        Nachfassregel(link="")
    with pytest.raises(ValueError):
        Nachfassregel(link=LINK, von=time(20), bis=time(9))


@pytest.mark.parametrize("jetzt, erwartet", [
    ("2026-10-13T10:00:00Z", "2026-10-13T10:00:00+00:00"),   # Dienstag 12 Uhr: sofort
    ("2026-10-13T05:00:00Z", "2026-10-13T07:00:00+00:00"),   # Dienstag 7 Uhr: um 9 Uhr Ortszeit
    ("2026-10-13T19:30:00Z", "2026-10-14T07:00:00+00:00"),   # Dienstag 21:30: Mittwoch 9 Uhr
    ("2026-10-17T19:00:00Z", "2026-10-19T07:00:00+00:00"),   # Samstag 21 Uhr: Sonntag gesperrt, Montag 9 Uhr
    ("2026-10-24T20:00:00Z", "2026-10-26T08:00:00+00:00"),   # über die Zeitumstellung: Montag 9 Uhr Winterzeit
])
def test_delivery_window_respects_local_time_weekdays_and_dst(jetzt, erwartet):
    regel = Nachfassregel(link=LINK)
    t = datetime.fromisoformat(jetzt.replace("Z", "+00:00"))
    assert regel.naechster_zeitpunkt(t).isoformat() == erwartet


def test_no_message_without_explicit_consent_for_the_channel(crm, nf):
    lead = crm.lead(4)
    assert nf.planen(lead, "test", crm.uhr()) == {"nachfass": "uebersprungen", "grund": "keine_einwilligung"}
    nf.einwilligung_setzen(4, ["whatsapp"])                              # andere Einwilligung zählt nicht
    assert nf.planen(lead, "test", crm.uhr())["grund"] == "keine_einwilligung"
    nf.einwilligung_setzen(4, ["sms", "whatsapp"])
    r = nf.planen(lead, "test", crm.uhr())
    assert r["nachfass"] == "geplant" and r["kanal"] == "sms" and LINK in r["text"]
    assert r["text"].startswith(f"Hallo {lead['name'].split()[0]},") and "STOP" in r["text"]
    with pytest.raises(ValueError):
        nf.einwilligung_setzen(4, ["brieftaube"])


def test_opt_out_beats_consent_and_closed_leads_get_nothing(crm, nf):
    nf.einwilligung_setzen(4, ["sms"])
    crm.sperren(crm.lead(4)["telefon"], "Opt-out am Telefon")
    assert nf.planen(crm.lead(4), "test", crm.uhr())["grund"] == "gesperrt"
    nf.einwilligung_setzen(3, ["sms"])
    crm.con.execute("UPDATE leads SET status = 'disqualifiziert' WHERE id = 3")
    assert nf.planen(crm.lead(3), "test", crm.uhr())["grund"] == "status_disqualifiziert"


def test_at_most_one_message_per_lead_within_the_block_window(crm, nf):
    nf.einwilligung_setzen(4, ["sms"])
    assert nf.planen(crm.lead(4), "a", crm.uhr())["nachfass"] == "geplant"
    assert nf.planen(crm.lead(4), "b", crm.uhr())["grund"] == "bereits_nachgefasst"
    crm.uhr.vor(days=15)
    assert nf.planen(crm.lead(4), "c", crm.uhr())["nachfass"] == "geplant"
    assert [v["grund"] for v in nf.verlauf(4)] == ["a", "c"]


def test_dialer_gave_up_plans_the_message_and_emits_the_event(crm, nf):
    gesendet = []

    class Outbox:
        def einreihen(self, ereignis, url=None):
            gesendet.append(ereignis)

    nb = Nachbereitung(crm, Outbox(), jetzt=crm.uhr, nachfass=nf)
    nf.einwilligung_setzen(4, ["sms"])
    r = nb.dialer_status(aufgegeben(), "msg_1")
    assert r["nachfass"] == "geplant" and r["aufgabe_id"]                 # Aufgabe für den Vertrieb bleibt
    assert [e.typ for e in gesendet] == ["aufgabe.erstellt", "nachricht.nachfassen", "dialer.beendet"]
    daten = gesendet[1].daten
    assert daten["kanal"] == "sms" and daten["link"] == LINK and daten["senden_ab"].endswith("Z")
    assert any("Kanal-Fallback" in n for n in crm.notizen(4))
    assert nb.dialer_status(aufgegeben(), "msg_1")["status"] == "duplikat"     # Replay: keine zweite Nachricht
    r = nb.dialer_status(aufgegeben(grund="no_next_opportunity"), "msg_2")    # neues Ereignis, gleicher Lead
    assert r["nachfass"] == "uebersprungen:bereits_nachgefasst" and len(nf.verlauf(4)) == 1


def test_without_fallback_the_dialer_handling_is_unchanged(crm):
    r = Nachbereitung(crm, jetzt=crm.uhr).dialer_status(aufgegeben(), "msg_1")
    assert "nachfass" not in r and r["aufgabe_id"]


def test_recovery_rate_counts_bookings_after_the_message(crm, nf):
    for lid in (3, 4):
        nf.einwilligung_setzen(lid, ["sms"])
        nf.planen(crm.lead(lid), "test", crm.uhr())
    crm.uhr.vor(hours=2)
    tools = crm.tools(4)
    tools.update_lead(status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    assert tools.book_appointment(slot_id=tools.check_slots()["freie_termine"][0]["slot_id"])["ok"]
    assert nf.kennzahlen() == {"nachgefasst": 2, "danach_gebucht": 1, "rueckgewinnungsquote": 0.5}


def test_erasure_removes_fallback_history_and_consent(crm, nf):
    nf.einwilligung_setzen(4, ["sms"])
    nf.planen(crm.lead(4), "test", crm.uhr())
    r = Aufbewahrung(crm, uhr=crm.uhr).loeschen(crm.lead(4)["telefon"], "Art. 17")
    assert r["entfernt"]["nachfass"] == 1 and nf.verlauf(4) == [] and nf.einwilligungen(4) == set()


def test_server_wires_the_fallback_from_config(con):
    from integrations.konfig import Konfig
    from integrations.server import erstelle_app
    app = erstelle_app(Konfig(nachfass_kanal="whatsapp", nachfass_link=LINK), con=con, planer_starten=False)
    nf = app.state.ctx.nachfass
    assert nf.regel.kanal == "whatsapp" and nf.regel.kunde == "SonnenWerk Energie"
    assert app.state.ctx.nachbereitung.nachfass is nf
    assert erstelle_app(Konfig(), con=con, planer_starten=False).state.ctx.nachfass is None
