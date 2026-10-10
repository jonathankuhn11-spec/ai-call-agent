"""Nachbereitung: Das Gesprächsergebnis kommt aus dem Backend-Zustand, nicht aus der Behauptung der Plattform."""
from datetime import datetime, timedelta, timezone

import pytest

from integrations.crm import LocalCRM
from integrations.modelle import CallEndedEreignis, iso_utc
from integrations.nachbereitung import Nachbereitung

TELEFON = "+49 251 000001"


def ereignis(ergebnis=None, status="connected", external_id="1", attempt=1, appointments=None, extra_outcomes=None,
             transkript=None, flach=False, ended_reason="agent-ended-call", follow_up=None, telefon=TELEFON):
    outcomes = [] if ergebnis is None else [{"key": "ergebnis", "dataType": "category", "value": ergebnis}]
    outcomes += [{"key": k, "dataType": "string", "value": v} for k, v in (extra_outcomes or {}).items()]
    call = {"call_id": f"call_{ergebnis}_{external_id}_{attempt}", "attempt": attempt, "external_contact_id": external_id,
            "direction": "outbound", "to_number": telefon, "state": "ended", "status": status, "ended_reason": ended_reason,
            "started_at_iso": "2026-10-10T09:00:00.000Z", "ended_at_iso": "2026-10-10T09:03:00.000Z",
            "transcriptObject": transkript if transkript is not None else [
                {"role": "agent", "content": "Guten Tag"}, {"role": "user", "content": "Hallo"},
                {"role": "agent", "toolActivity": "update_lead", "toolParameters": {"status": "x"}},
                {"role": "user", "content": "Ja"}],
            "outcomes": outcomes, "collected_data": None, "appointments": appointments or [], "follow_up": follow_up,
            "recording_url": "https://telli.example/rec/1"}
    if flach:
        return CallEndedEreignis.lesen({"event": "call_ended", **call})
    return CallEndedEreignis.lesen({"event": "call_ended", "call": call, "contact": {"phone_number": telefon}})


@pytest.fixture
def crm(con):
    return LocalCRM(con)


@pytest.fixture
def nb(crm):
    return Nachbereitung(crm, jetzt=lambda: datetime(2026, 10, 10, 9, 5, tzinfo=timezone.utc))


def qualifizieren_und_buchen(crm, lead_id=1):
    tools = crm.tools(lead_id)
    tools.update_lead(status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")
    slot = tools.check_slots()["freie_termine"][0]["slot_id"]
    assert tools.book_appointment(slot_id=slot)["ok"]


def test_booking_in_backend_is_the_ground_truth(crm, nb):
    qualifizieren_und_buchen(crm)
    r = nb.verarbeiten(ereignis("rueckruf_vereinbart"), "msg_1")       # Agent behauptet etwas anderes
    assert r["ergebnis"] == "termin_gebucht" and "ergebnis_korrigiert" in r["flags"]
    assert r["termin"].endswith("Z") and crm.anrufe(1)[0]["ergebnis"] == "termin_gebucht"
    assert crm.anrufe(1)[0]["turns"] == 2
    assert "Aufzeichnung" in crm.notizen(1)[0]


def test_claimed_booking_without_backend_booking_becomes_a_review_task(crm, nb):
    r = nb.verarbeiten(ereignis("termin_gebucht"), "msg_2")
    assert r["ergebnis"] == "rueckruf_vereinbart" and "termin_behauptet_ohne_buchung" in r["flags"]
    aufgabe = crm.aufgaben(1)[0]
    assert aufgabe["titel"].startswith("Prüfen") and crm.lead(1)["status"] == "rueckruf"


def test_platform_calendar_booking_counts_as_external_appointment(crm, nb):
    crm.lead_aktualisieren(1, status="in_qualifizierung", eigentuemer=True, gebaeudetyp="reihenhaus")
    termine = [{"id": "apt_1", "starts_at": "2026-10-14T08:00:00Z", "timezone": "Europe/Berlin", "status": "booked",
                "hosts": [{"name": "Berater", "email": "b@example.com"}], "reference": None},
               {"id": "apt_0", "starts_at": "2026-10-13T08:00:00Z", "status": "pending"}]
    r = nb.verarbeiten(ereignis("termin_gebucht", appointments=termine), "msg_3")
    assert r["ergebnis"] == "termin_gebucht" and "termin_extern" in r["flags"]
    assert r["termin"] == "2026-10-14T08:00:00Z" and crm.lead(1)["status"] == "qualifiziert"


def test_external_booking_without_facts_is_flagged_not_silently_qualified(crm, nb):
    termine = [{"id": "apt_1", "starts_at": "2026-10-14T08:00:00Z", "status": "booked"}]
    r = nb.verarbeiten(ereignis("termin_gebucht", appointments=termine), "msg_3b")
    assert r["ergebnis"] == "termin_gebucht" and "status_verworfen:qualifiziert" in r["flags"]
    assert crm.lead(1)["status"] == "offen"


def test_disqualification_needs_a_reason_in_the_crm(crm, nb):
    r = nb.verarbeiten(ereignis("disqualifiziert"), "msg_4")
    assert r["ergebnis"] == "rueckruf_vereinbart" and "disqualifiziert_ohne_grund" in r["flags"]
    r = nb.verarbeiten(ereignis("disqualifiziert", external_id="2", extra_outcomes={"eigentuemer": "false"}), "msg_5")
    assert r["ergebnis"] == "disqualifiziert" and crm.lead(2)["status"] == "disqualifiziert"
    assert crm.lead(2)["eigentuemer"] is False


def test_opt_out_blocks_the_number(crm, nb):
    r = nb.verarbeiten(ereignis("opt_out"), "msg_6")
    assert r["ergebnis"] == "opt_out" and r["gesperrt"] is True
    assert crm.gesperrt(TELEFON) and crm.lead(1)["status"] == "opt_out"


def test_callback_task_uses_the_requested_time_or_a_default(crm, nb):
    r = nb.verarbeiten(ereignis("rueckruf_vereinbart", extra_outcomes={"rueckruf_zeitpunkt": "2026-10-12T14:00:00Z"}), "msg_7")
    assert crm.aufgaben(1)[0]["faellig_am"] == datetime(2026, 10, 12, 16)          # 14:00Z als Berliner Wanduhrzeit
    assert r["aufgabe_id"] == 1 and crm.lead(1)["status"] == "rueckruf"
    nb.verarbeiten(ereignis("rueckruf_vereinbart", external_id="2", extra_outcomes={"rueckruf_zeitpunkt": "morgen nachmittag"}), "msg_8")
    assert crm.aufgaben(2)[0]["faellig_am"] == datetime(2026, 10, 11, 11, 5)         # jetzt + 24 h, Wanduhrzeit
    nb.verarbeiten(ereignis("rueckruf_vereinbart", external_id="3", follow_up={"type": "agent_follow_up",
                                                                                "scheduled_at": "2026-10-11T07:30:00Z"}), "msg_9")
    assert crm.aufgaben(3)[0]["faellig_am"] == datetime(2026, 10, 11, 9, 30)


def test_facts_from_outcomes_go_through_validation(crm, nb):
    nb.verarbeiten(ereignis("rueckruf_vereinbart", extra_outcomes={"eigentuemer": "ja", "gebaeudetyp": "DHH",
                                                                    "dachflaeche_m2": "70", "jahresverbrauch_kwh": "999999"}), "msg_10")
    lead = crm.lead(1)
    assert lead["eigentuemer"] is True and lead["gebaeudetyp"] == "doppelhaushaelfte" and lead["dachflaeche_m2"] == 70
    assert lead["jahresverbrauch_kwh"] is None
    assert "feld_verworfen:jahresverbrauch_kwh" in crm.anrufe(1)[0]["flags"]


def test_not_connected_calls_are_logged_without_touching_the_lead(crm, nb):
    r = nb.verarbeiten(ereignis(None, status="voicemail", attempt=3, transkript=[]), "msg_11")
    assert r["ergebnis"] == "nicht_erreicht" and "mehrfach_nicht_erreicht" in r["flags"]
    assert crm.lead(1)["status"] == "offen" and crm.aufgaben(1) == [] and len(crm.notizen(1)) == 1


def test_missing_outcome_and_early_hangup(crm, nb):
    r = nb.verarbeiten(ereignis(None, ended_reason="contact-ended-call", transkript=[{"role": "user", "content": "Hallo"}]), "msg_12")
    assert r["ergebnis"] == "sonstiges" and {"ergebnis_fehlt", "frueh_aufgelegt"} <= set(r["flags"])


def test_duplicates_and_unknown_leads(crm, nb):
    r1 = nb.verarbeiten(ereignis("opt_out"), "msg_13")
    assert nb.verarbeiten(ereignis("opt_out"), "msg_13")["status"] == "duplikat"               # gleiche Nachricht
    assert nb.verarbeiten(ereignis("opt_out"), "msg_14")["status"] == "duplikat"               # gleicher Call, neue ID
    assert len(crm.anrufe(1)) == 1 and r1["status"] == "verarbeitet"
    r = nb.verarbeiten(ereignis("opt_out", external_id="999", telefon="+49 170 1"), "msg_15")
    assert r["status"] == "lead_unbekannt"
    r = nb.verarbeiten(ereignis("kein_interesse", external_id=None, telefon="0251 000003"), "msg_16")
    assert r["status"] == "verarbeitet" and r["lead_id"] == 3 and crm.lead(3)["status"] == "kein_interesse"


def test_flat_payload_variant_is_read_too(crm, nb):
    e = ereignis("falsche_person", flach=True)
    assert e.call.call_id == "call_falsche_person_1_1" and e.telefon() == "+49251000001"
    r = nb.verarbeiten(e, "msg_17")
    assert r["ergebnis"] == "falsche_person" and crm.lead(1)["status"] == "offen"


def test_mirror_jobs_are_queued_in_the_transaction_and_run_outside_the_lock(crm):
    class Spiegel:
        def __init__(self):
            self.aufrufe = []

        def lead_aktualisieren(self, lead_id, **felder):
            self.aufrufe.append(("update", lead_id, felder)); return {"ok": True}

        def notiz(self, lead_id, text):
            self.aufrufe.append(("notiz", lead_id, text))

        def aufgabe(self, lead_id, titel, faellig, text=""):
            self.aufrufe.append(("aufgabe", lead_id, titel, faellig))

    spiegel = Spiegel()
    crm.con.execute("UPDATE leads SET crm_id = 'hs_501' WHERE id = 1")
    nb = Nachbereitung(crm, spiegel=spiegel, jetzt=lambda: datetime(2026, 10, 10, 9, 5, tzinfo=timezone.utc))
    r = nb.verarbeiten(ereignis("rueckruf_vereinbart", extra_outcomes={"eigentuemer": "true",
                                                                       "rueckruf_zeitpunkt": "2026-10-12T14:00:00Z"}), "msg_18")
    assert r["spiegel_auftrag"] == 1 and spiegel.aufrufe == []                     # vorgemerkt, noch nichts gesendet
    assert crm.spiegel_status() == {"offen": 1}
    assert nb.spiegeln() == {"erledigt": 1, "wiederholen": 0, "tot": 0}
    arten = [a[0] for a in spiegel.aufrufe]
    assert arten == ["update", "notiz", "aufgabe"] and all(a[1] == "hs_501" for a in spiegel.aufrufe)
    assert spiegel.aufrufe[0][2]["status"] == "rueckruf" and spiegel.aufrufe[0][2]["ergebnis"] == "rueckruf_vereinbart"
    assert spiegel.aufrufe[0][2]["eigentuemer"] is True
    assert spiegel.aufrufe[2][2] == "Rückruf" and spiegel.aufrufe[2][3] == datetime(2026, 10, 12, 14, tzinfo=timezone.utc)
    assert crm.spiegel_status() == {"erledigt": 1} and nb.spiegeln() == {"erledigt": 0, "wiederholen": 0, "tot": 0}


def test_mirror_failures_are_retried_and_never_break_processing(crm):
    class Kaputt:
        def __init__(self):
            self.versuche = 0

        def lead_aktualisieren(self, *a, **k):
            self.versuche += 1
            raise ConnectionError("HubSpot down")

    spiegel = Kaputt()
    nb = Nachbereitung(crm, spiegel=spiegel)
    r = nb.verarbeiten(ereignis("opt_out"), "msg_19")
    assert r["status"] == "verarbeitet" and crm.lead(1)["status"] == "opt_out"
    assert nb.spiegeln() == {"erledigt": 0, "wiederholen": 1, "tot": 0} and spiegel.versuche == 1
    assert nb.spiegeln()["wiederholen"] == 0                                        # erst nach dem Wiederholungsplan fällig
    eintrag = crm.con.execute("SELECT status, versuche, letzte_antwort FROM spiegel_auftraege").fetchone()
    assert eintrag == ("offen", 1, "update: ConnectionError: HubSpot down")


def test_processing_is_atomic_so_platform_retries_never_duplicate(crm, nb, monkeypatch):
    """Fehler mitten in der Nachbereitung: nichts bleibt zurück, die Wiederholung läuft sauber durch."""
    original = crm.notiz
    aufrufe = {"n": 0}

    def kaputte_notiz(lead_id, text):
        aufrufe["n"] += 1
        if aufrufe["n"] == 1:
            raise RuntimeError("Platte voll")
        return original(lead_id, text)

    monkeypatch.setattr(crm, "notiz", kaputte_notiz)
    with pytest.raises(RuntimeError):
        nb.verarbeiten(ereignis("rueckruf_vereinbart"), "msg_20")
    assert crm.aufgaben(1) == [] and crm.anrufe(1) == [] and crm.lead(1)["status"] == "offen"
    assert crm.webhook_bekannt("msg_20") is False
    r = nb.verarbeiten(ereignis("rueckruf_vereinbart"), "msg_20")                    # telli wiederholt dieselbe Nachricht
    assert r["status"] == "verarbeitet" and len(crm.aufgaben(1)) == 1 and len(crm.anrufe(1)) == 1


def test_an_earlier_appointment_never_overrides_a_later_opt_out_or_disqualification(crm, nb):
    qualifizieren_und_buchen(crm)
    crm.con.execute("UPDATE appointments SET gebucht_am = gebucht_am - INTERVAL 3 DAY")      # Buchung aus früherem Anruf
    r = nb.verarbeiten(ereignis("opt_out"), "msg_21")
    assert r["ergebnis"] == "opt_out" and crm.gesperrt(TELEFON) and crm.lead(1)["status"] == "opt_out"
    assert "ergebnis_korrigiert" not in r["flags"]
    qualifizieren_und_buchen(crm, 2)
    crm.con.execute("UPDATE appointments SET gebucht_am = gebucht_am - INTERVAL 3 DAY WHERE lead_id = 2")
    r = nb.verarbeiten(ereignis("disqualifiziert", external_id="2", extra_outcomes={"eigentuemer": "false"}), "msg_22")
    assert r["ergebnis"] == "disqualifiziert" and crm.lead(2)["status"] == "disqualifiziert"


def test_opt_out_wins_even_over_a_booking_in_the_same_call(crm, nb):
    qualifizieren_und_buchen(crm)
    r = nb.verarbeiten(ereignis("opt_out"), "msg_23")
    assert r["ergebnis"] == "opt_out" and "buchung_trotz_opt_out" in r["flags"] and r["gesperrt"] is True
    assert crm.aufgaben(1)[0]["titel"] == "Prüfen: buchung_trotz_opt_out"


def test_unreadable_timestamps_and_null_lists_do_not_crash(crm, nb):
    e = CallEndedEreignis.lesen({"event": "call_ended", "call": {
        "call_id": "call_null", "external_contact_id": 1, "status": "connected", "state": "ended",
        "started_at_iso": "2026-10-10 09:00:00 UTC", "ended_at_iso": None, "transcriptObject": None,
        "appointments": None, "outcomes": [{"key": "ergebnis", "value": "kein_interesse"}], "collected_data": None,
        "contact_details": None}, "contact": {"contact_details": None}})
    r = nb.verarbeiten(e, "msg_24")
    assert r["ergebnis"] == "kein_interesse" and "zeitstempel_unlesbar" in r["flags"]
    assert crm.anrufe(1)[0]["turns"] == 0 and "Aufzeichnung" not in crm.notizen(1)[0]


def test_recording_links_are_not_stored(crm, nb):
    nb.verarbeiten(ereignis("kein_interesse"), "msg_25")
    notiz = crm.notizen(1)[0]
    assert "https://telli.example/rec/1" not in notiz and "Aufzeichnung vorhanden" in notiz


def test_past_follow_up_times_are_ignored(crm, nb):
    nb.verarbeiten(ereignis("rueckruf_vereinbart", follow_up={"type": "agent_follow_up",
                                                               "scheduled_at": "2020-01-01T07:30:00Z"}), "msg_26")
    assert crm.aufgaben(1)[0]["faellig_am"] == datetime(2026, 10, 11, 11, 5)


def test_mirror_retry_skips_steps_that_already_succeeded(crm):
    class Wacklig:
        def __init__(self):
            self.aufrufe = []
            self.notiz_fehler = 1

        def lead_aktualisieren(self, lead_id, **felder):
            self.aufrufe.append("update")

        def notiz(self, lead_id, text):
            self.aufrufe.append("notiz")

        def aufgabe(self, lead_id, titel, faellig, text=""):
            self.aufrufe.append("aufgabe")
            if self.notiz_fehler:
                self.notiz_fehler -= 1
                raise TimeoutError("langsam")

    spiegel = Wacklig()
    nb = Nachbereitung(crm, spiegel=spiegel)
    nb.verarbeiten(ereignis("rueckruf_vereinbart"), "msg_30")
    assert nb.spiegeln()["wiederholen"] == 1 and spiegel.aufrufe == ["update", "notiz", "aufgabe"]
    crm.con.execute("UPDATE spiegel_auftraege SET naechster_versuch = now()::TIMESTAMP")      # fällig stellen
    assert nb.spiegeln()["erledigt"] == 1
    assert spiegel.aufrufe == ["update", "notiz", "aufgabe", "aufgabe"]                   # keine zweite Notiz, kein zweites Update


def test_external_booking_event_is_not_confused_with_an_old_local_appointment(crm, nb):
    qualifizieren_und_buchen(crm)
    crm.con.execute("UPDATE appointments SET gebucht_am = gebucht_am - INTERVAL 3 DAY")
    termine = [{"id": "apt_9", "starts_at": "2026-10-20T08:00:00Z", "status": "booked"}]
    r = nb.verarbeiten(ereignis("termin_gebucht", appointments=termine), "msg_31")
    assert r["ergebnis"] == "termin_gebucht" and "termin_extern" in r["flags"] and r["termin"] == "2026-10-20T08:00:00Z"


def test_a_previous_opt_out_is_never_overwritten_by_a_later_result(crm, nb):
    nb.verarbeiten(ereignis("opt_out"), "msg_32")
    r = nb.verarbeiten(ereignis("kein_interesse", attempt=2), "msg_33")
    assert crm.lead(1)["status"] == "opt_out" and "status_bleibt_opt_out" in r["flags"]


def test_nested_transactions_share_the_outer_one(crm):
    with crm.transaktion():
        crm.notiz(1, "außen")
        with crm.transaktion():
            crm.notiz(1, "innen")
    assert crm.notizen(1) == ["außen", "innen"]
    with pytest.raises(RuntimeError):
        with crm.transaktion():
            crm.notiz(1, "weg")
            with crm.transaktion():
                raise RuntimeError("innen kaputt")
    assert crm.notizen(1) == ["außen", "innen"] and crm._tiefe == 0
