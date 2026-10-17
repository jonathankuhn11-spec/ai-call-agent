"""Kalenderdienst hinter den Slots (Cal.com API v2): Verfügbarkeit spiegeln, Buchung bestätigen, Absage nachziehen.

Die Attrappe spielt Cal.com im Prozess und prüft jede Anfrage auf Auth und Versionsheader. Geprüft wird
das Muster, nicht der Anbieter: Der Agent sagt nur zu, was der Kalender bestätigt hat; lehnt der Kalender
ab, bleibt ein bisheriger Termin bestehen; ist er nicht erreichbar, bleibt die Vormerkung und wird nachgeholt.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import API_KEY, KUNDEN_SECRET, TOOL_SECRET, WEBHOOK_SECRET

from integrations.kalender import CalComKalender, KalenderFehler, Kalenderspiegel, kalender_aus_konfig
from integrations.outbox import RETRY_PLAN_S
from integrations.simulator import CalComAttrappe

# Testuhr: Dienstag 13.10.2026 08:00Z. Der Kalender bietet Mittwoch und Donnerstag je drei Zeiten (UTC; 08:30, 10:30
# und 15:30 Uhr in Berlin, bewusst andere Zeiten als die Beispiel-Slots des Prototyps).
ZEITEN = [datetime(2026, 10, 14, 6, 30, tzinfo=timezone.utc), datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc),
          datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc), datetime(2026, 10, 15, 6, 30, tzinfo=timezone.utc),
          datetime(2026, 10, 15, 8, 30, tzinfo=timezone.utc), datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc)]


@pytest.fixture
def calcom():
    return CalComAttrappe(ZEITEN)


@pytest.fixture
def kapp(con, konfig, kundensystem, telli_attrappe, quiet, uhr, calcom):
    """App mit Kalenderdienst; die Beispiel-Slots des Prototyps bleiben zunächst in der Tabelle (der Spiegel räumt sie weg)."""
    from zoneinfo import ZoneInfo

    import agent
    from integrations.outbox import Outbox
    from integrations.server import erstelle_app

    con.execute("DELETE FROM slots")
    agent.slots_anlegen(con, uhr().astimezone(ZoneInfo(konfig.zeitzone)).replace(tzinfo=None))
    outbox = Outbox(con, "https://kunde.example/webhooks", KUNDEN_SECRET, client=kundensystem["client"], jetzt=uhr)
    return erstelle_app(konfig, con=con, outbox=outbox, telli_client=telli_attrappe["client"], planer_starten=False,
                        uhr=uhr, kalender=calcom.kalender())


@pytest.fixture
def kclient(kapp):
    from fastapi.testclient import TestClient
    with TestClient(kapp) as c:
        yield c


@pytest.fixture
def ksim(kclient, uhr):
    from integrations.simulator import PlattformSimulator
    return PlattformSimulator(kclient, API_KEY, WEBHOOK_SECRET, TOOL_SECRET, uhr=uhr)


def qualifizieren(ksim, lead_id="1"):
    ksim.tool("update_lead", "c1", lead_id, status="qualifiziert", eigentuemer=True, gebaeudetyp="einfamilienhaus")


# ---------------------------------------------------------------- Cal.com-Client: Kontrakt
def test_calcom_client_speaks_the_documented_api(calcom):
    k = calcom.kalender()
    zeiten = k.freie_zeiten(datetime(2026, 10, 13, 8, tzinfo=timezone.utc), datetime(2026, 10, 27, 8, tzinfo=timezone.utc))
    assert [s for s, _ in zeiten] == ZEITEN and all(e - s == timedelta(hours=1) for s, e in zeiten)
    methode, pfad, params = calcom.requests[-1]
    assert (methode, pfad) == ("GET", "/v2/slots")
    assert params == {"start": "2026-10-13T08:00:00.000Z", "end": "2026-10-27T08:00:00.000Z", "eventTypeId": "42",
                      "timeZone": "UTC", "format": "range"}
    uid = k.buchen(ZEITEN[0], {"id": 1, "name": "Thomas Becker", "telefon": "+49 251 000001"}, "vor_ort", "call_9")
    assert uid == "bk_0001" and ZEITEN[0] not in calcom.frei
    _, _, body = calcom.requests[-1]
    assert body["start"] == "2026-10-14T06:30:00.000Z" and body["eventTypeId"] == 42
    assert body["attendee"] == {"name": "Thomas Becker", "timeZone": "Europe/Berlin", "language": "de", "phoneNumber": "+49 251 000001"}
    assert body["metadata"] == {"lead_id": "1", "art": "vor_ort", "quelle": "ai-call-agent", "call_id": "call_9"}
    with pytest.raises(KalenderFehler) as e:
        k.buchen(ZEITEN[0], {"id": 2, "name": "Sabine Wolf"}, "vor_ort")                     # vergeben: endgültig
    assert e.value.endgueltig and e.value.status == 400 and "already has booking" in str(e.value)
    k.stornieren(uid, "Kunde hat abgesagt")
    assert calcom.buchungen[uid]["status"] == "cancelled" and ZEITEN[0] in calcom.frei
    k.stornieren("bk_gibt_es_nicht", "egal")                                                   # 404: Ziel erreicht
    calcom.ausfaelle = 1
    with pytest.raises(KalenderFehler) as e:
        k.freie_zeiten(ZEITEN[0], ZEITEN[-1])
    assert not e.value.endgueltig and e.value.status == 503
    falsch = CalComKalender("cal_falsch", 42, client=calcom.client())
    with pytest.raises(KalenderFehler) as e:
        falsch.freie_zeiten(ZEITEN[0], ZEITEN[-1])
    assert e.value.status == 401 and not e.value.endgueltig                                    # Schlüssel: Störung, nicht "vergeben"
    with pytest.raises(KalenderFehler) as e:
        CalComKalender("cal_test_key", 99, client=calcom.client()).freie_zeiten(ZEITEN[0], ZEITEN[-1])
    assert e.value.status == 404 and not e.value.endgueltig                                    # Event-Typ falsch: ebenso


def test_transport_errors_are_temporary_calendar_errors():
    def kaputt(request):
        raise httpx.ConnectError("keine Verbindung")
    k = CalComKalender("cal_x", 1, client=httpx.Client(transport=httpx.MockTransport(kaputt), base_url="https://api.cal.com"))
    with pytest.raises(KalenderFehler) as e:
        k.freie_zeiten(ZEITEN[0], ZEITEN[-1])
    assert not e.value.endgueltig and "keine Verbindung" in str(e.value)


def test_calendar_comes_from_configuration(konfig):
    assert kalender_aus_konfig(konfig) is None
    konfig.kalender = "calcom"
    assert "CALCOM_API_KEY" in konfig.fehlende_geheimnisse()
    with pytest.raises(RuntimeError):
        kalender_aus_konfig(konfig)
    konfig.calcom_api_key, konfig.calcom_event_type_id = "cal_x", "7"
    k = kalender_aus_konfig(konfig)
    assert isinstance(k, CalComKalender) and k.event_type_id == 7 and "CALCOM_API_KEY" not in konfig.fehlende_geheimnisse()
    konfig.kalender = "outlook"
    with pytest.raises(RuntimeError):
        kalender_aus_konfig(konfig)


# ---------------------------------------------------------------- Spiegel
def test_mirror_makes_the_calendar_the_only_source_of_free_times(kapp, uhr, calcom):
    crm = kapp.state.ctx.crm
    assert crm.kalender_aktiv is True
    tools = crm.tools(2)                                                       # ein lokal gebuchter Beispiel-Slot bleibt
    behalten = crm.freie_slots(1)[0]
    tools.offered = {behalten["slot_id"]}
    assert tools._buchen_ohne_qualifizierung(slot_id=behalten["slot_id"], art="vor_ort")["ok"]
    assert len(crm.freie_slots(100)) == 11                                     # Beispiel-Slots des Prototyps
    bericht = kapp.state.ctx.kalender.spiegeln()
    assert bericht == {"stand": "2026-10-13T08:00:00.000Z", "neu": 6, "entfernt": 11, "frei": 6, "gebucht": 1}
    frei = crm.freie_slots(100)
    assert [crm.start_utc(s["start"]) for s in frei] == ZEITEN
    assert all(s["ende"] == s["start"] + timedelta(hours=1) for s in frei)
    assert crm.con.execute("SELECT DISTINCT quelle FROM slots WHERE lead_id IS NULL").fetchall() == [("kalender",)]
    assert crm.termin(2)["slot_id"] == behalten["slot_id"]                      # Buchung unberührt
    assert kapp.state.ctx.kalender.spiegeln()["neu"] == 0                       # idempotent
    calcom.vergeben(ZEITEN[1])                                                 # jemand belegt eine Zeit im Kalender
    calcom.frei.add(datetime(2026, 10, 16, 6, 30, tzinfo=timezone.utc))        # und eine neue wird frei
    bericht = kapp.state.ctx.kalender.spiegeln()
    assert bericht["neu"] == 1 and bericht["entfernt"] == 1 and bericht["frei"] == 6
    assert ZEITEN[1] not in [crm.start_utc(s["start"]) for s in crm.freie_slots(100)]
    assert kapp.state.ctx.kalender.status()["letzter_spiegel"] == "2026-10-13T08:00:00.000Z"


def test_planner_mirrors_on_its_own_interval(kapp, uhr, kclient):
    planer = kapp.state.ctx.planer
    assert planer.tick()["kalender"]["spiegel"]["neu"] == 6                     # erster Durchlauf spiegelt immer
    assert "spiegel" not in planer.tick()["kalender"]                            # Takt nicht abgelaufen
    uhr.vor(minutes=5)
    assert planer.tick()["kalender"]["spiegel"]["neu"] == 0
    m = kclient.get("/metrics", headers={"Authorization": f"Bearer {TOOL_SECRET}"}).json()
    assert m["kalender"] == {"auftraege": {}, "letzter_spiegel": "2026-10-13T08:05:00.000Z", "letzter_fehler": None}


def test_a_failing_mirror_waits_for_the_next_interval_instead_of_hammering(kapp, uhr, calcom):
    planer = kapp.state.ctx.planer
    calcom.ausfaelle = 5
    assert "fehler" in planer.tick()["kalender"]["spiegel"]
    anfragen = len(calcom.requests)
    assert "spiegel" not in planer.tick()["kalender"] and len(calcom.requests) == anfragen      # kein zweiter Versuch im Takt
    assert "503" in kapp.state.ctx.kalender.status()["letzter_fehler"]
    uhr.vor(minutes=5)
    assert "fehler" in planer.tick()["kalender"]["spiegel"] and len(calcom.requests) == anfragen + 1


# ---------------------------------------------------------------- Buchung bestätigen
def test_the_agent_only_hears_yes_after_the_calendar_confirmed(kapp, ksim, calcom, kundensystem):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    b = ksim.gespraech(crm.lead(1), "happy_path")
    assert b["book"] == {"status": "success"} and b["call_ended"]["ergebnis"] == "termin_gebucht"
    termin = crm.termin(1)
    assert termin["extern_status"] == "bestaetigt" and termin["extern_id"] == "bk_0001"
    buchung = calcom.buchungen["bk_0001"]
    assert buchung["start"] == "2026-10-14T06:30:00.000Z" and buchung["metadata"]["lead_id"] == "1"
    assert buchung["attendees"][0]["phoneNumber"] == "+49 251 000001"
    assert crm.kalender_status() == {"erledigt": 1}
    assert b["call_ended"]["termin"] == "2026-10-14T06:30:00.000Z"


def test_tool_booking_reports_the_calendar_confirmation(kapp, ksim, calcom):
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    r = ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    assert r["ok"] and r["kalender"] == "bestaetigt" and r["extern_id"] == "bk_0001" and r["termin"] == "14.10.2026 08:30 Uhr"
    r2 = ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])            # Wiederholung: keine zweite Buchung
    assert r2["wiederholt"] and len(calcom.buchungen) == 1


def test_a_slot_taken_in_the_meantime_is_rejected_and_removed(kapp, ksim, calcom):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    calcom.vergeben(ZEITEN[0])                                                 # zwischen check_slots und Buchung belegt
    r = ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    assert r["ok"] is False and r["kalender"] == "abgelehnt" and "inzwischen vergeben" in r["fehler"]
    assert crm.termin(1) is None and calcom.buchungen == {}
    assert crm.con.execute("SELECT COUNT(*) FROM slots WHERE id = ?", [slots[0]["slot_id"]]).fetchone()[0] == 0
    assert crm.erinnerungen(1) == [] or all(e["status"] == "entfallen" for e in crm.erinnerungen(1))
    neu = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    assert neu[0]["slot_id"] != slots[0]["slot_id"]                              # nicht mehr im Angebot
    r = ksim.tool("book_appointment", "c1", "1", slot_id=neu[0]["slot_id"])
    assert r["ok"] and r["kalender"] == "bestaetigt"
    assert crm.kalender_status() == {"abgelehnt": 1, "erledigt": 1}


def test_custom_calendar_book_fails_cleanly_when_the_calendar_rejects(kapp, ksim, calcom):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    frei = ksim.available("1", crm.lead(1)["telefon"])["available"]
    assert frei[0] == {"start_iso": "2026-10-14T06:30:00.000Z", "end_iso": "2026-10-14T07:30:00.000Z"}
    calcom.vergeben(ZEITEN[0])
    assert ksim.book("1", crm.lead(1)["telefon"], frei[0]["start_iso"]) == {"status": "failed",
                                                                            "reason": "Appointment slot is no longer available"}
    assert ksim.book("1", crm.lead(1)["telefon"], frei[1]["start_iso"]) == {"status": "success"}
    assert crm.termin(1)["extern_id"] == "bk_0001"


# ---------------------------------------------------------------- Umbuchung
def test_rebooking_keeps_the_old_appointment_when_the_calendar_rejects_the_new_one(kapp, ksim, calcom, kundensystem):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    assert ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])["kalender"] == "bestaetigt"
    calcom.vergeben(ZEITEN[1])
    r = ksim.tool("book_appointment", "c2", "1", slot_id=slots[1]["slot_id"])
    assert r["ok"] is False and "bisherige Termin am 14.10.2026 08:30 Uhr bleibt bestehen" in r["fehler"]
    termin = crm.termin(1)
    assert termin["slot_id"] == slots[0]["slot_id"] and termin["extern_status"] == "bestaetigt"
    assert crm.stornierungen(1) == [] and calcom.buchungen["bk_0001"]["status"] == "accepted"
    kapp.state.ctx.planer.tick()
    assert "termin.abgesagt" not in [e["typ"] for e in kundensystem["empfangen"]]
    assert [e["status"] for e in crm.erinnerungen(1)] == ["offen", "entfallen"]


def test_rebooking_cancels_the_old_booking_only_after_the_new_one_is_confirmed(kapp, ksim, calcom, kundensystem):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    r = ksim.tool("book_appointment", "c2", "1", slot_id=slots[1]["slot_id"])
    assert r["ok"] and r["kalender"] == "bestaetigt" and r["umgebucht_von"] == "14.10.2026 08:30 Uhr" and r["umbuchung_offen"]
    termin = crm.termin(1)
    assert termin["slot_id"] == slots[1]["slot_id"] and termin["extern_id"] == "bk_0002"
    assert crm.stornierungen(1)[0]["extern_id"] == "bk_0001" and crm.stornierungen(1)[0]["grund"] == "Umbuchung"
    assert calcom.buchungen["bk_0001"]["status"] == "cancelled"                  # Hintergrundauftrag nach der Antwort
    assert calcom.buchungen["bk_0001"]["cancellationReason"] == "Umbuchung"
    kapp.state.ctx.planer.tick()
    abgesagt = [e for e in kundensystem["empfangen"] if e["typ"] == "termin.abgesagt"]
    assert len(abgesagt) == 1 and abgesagt[0]["body"]["daten"] == {"abgesagt": "14.10.2026 08:30 Uhr", "grund": "Umbuchung",
                                                                   "umbuchung": True, "call_id": "c2"}
    assert [e["status"] for e in crm.erinnerungen(1)] == ["entfallen", "offen"]
    assert crm.kalender_status() == {"erledigt": 3}


# ---------------------------------------------------------------- Ausfall des Kalenders
def test_calendar_outage_keeps_the_booking_and_confirms_it_later(kapp, ksim, calcom, uhr):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    calcom.ausfaelle = 1
    r = ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    assert r["ok"] and r["kalender"] == "vorgemerkt" and "nicht erreichbar" in r["hinweis"]
    assert crm.termin(1)["extern_status"] == "offen" and calcom.buchungen == {}
    assert ksim.tool("lookup_lead", "c1", "1")["naechster_termin"] == "14.10.2026 08:30 Uhr"   # für den Agenten gebucht
    assert kapp.state.ctx.planer.tick()["kalender"]["auftraege"]["bestaetigt"] == 0            # erst nach 5 s wieder
    uhr.vor(seconds=RETRY_PLAN_S[1])
    assert kapp.state.ctx.planer.tick()["kalender"]["auftraege"]["bestaetigt"] == 1
    assert crm.termin(1)["extern_status"] == "bestaetigt" and crm.termin(1)["extern_id"] == "bk_0001"
    r2 = ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    assert r2["wiederholt"] and "kalender" not in r2


def test_a_second_choice_during_an_outage_replaces_the_pending_booking(kapp, ksim, calcom, uhr):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    calcom.ausfaelle = 1
    assert ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])["kalender"] == "vorgemerkt"
    r = ksim.tool("book_appointment", "c1", "1", slot_id=slots[1]["slot_id"])              # Kunde überlegt es sich anders
    assert r["ok"] and r["kalender"] == "bestaetigt" and "umgebucht_von" not in r
    assert crm.termin(1)["slot_id"] == slots[1]["slot_id"]
    assert crm.con.execute("SELECT COUNT(*) FROM appointments WHERE lead_id = 1").fetchone()[0] == 1
    uhr.vor(seconds=RETRY_PLAN_S[1])
    assert kapp.state.ctx.planer.tick()["kalender"]["auftraege"]["bestaetigt"] == 0          # alter Auftrag verworfen
    assert crm.kalender_status() == {"verworfen": 1, "erledigt": 1} and len(calcom.buchungen) == 1


def test_a_booking_the_calendar_never_confirms_becomes_a_task_for_a_human(kapp, ksim, calcom, uhr, kundensystem):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    calcom.ausfaelle = len(RETRY_PLAN_S) + 5
    assert ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])["kalender"] == "vorgemerkt"
    for wartezeit in RETRY_PLAN_S[1:]:
        uhr.vor(seconds=wartezeit)
        bericht = kapp.state.ctx.planer.tick()["kalender"]["auftraege"]
    assert bericht["tot"] == 1 and crm.kalender_status() == {"tot": 1}
    aufgabe = crm.aufgaben(1)[0]
    assert aufgabe["titel"] == "Prüfen: Kalenderbuchung nicht bestätigt" and "Auftrag 1" in aufgabe["text"]
    assert crm.termin(1)["extern_status"] == "offen"                              # bleibt sichtbar, Mensch entscheidet
    kapp.state.ctx.planer.tick()
    ereignis = next(e for e in kundensystem["empfangen"] if e["typ"] == "aufgabe.erstellt")
    assert ereignis["body"]["daten"]["titel"] == "Prüfen: Kalenderbuchung nicht bestätigt"


# ---------------------------------------------------------------- Absage
def test_cancellations_are_propagated_to_the_calendar(kapp, ksim, calcom, kundensystem):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    r = ksim.tool("cancel_appointment", "c3", "1", grund="Kunde hat doch keine Zeit")
    assert r["ok"] and r["abgesagt"] == "14.10.2026 08:30 Uhr"
    assert calcom.buchungen["bk_0001"]["status"] == "cancelled"
    assert calcom.buchungen["bk_0001"]["cancellationReason"] == "Kunde hat doch keine Zeit"
    assert ZEITEN[0] in calcom.frei and crm.kalender_status() == {"erledigt": 2}
    kapp.state.ctx.planer.tick()
    assert "termin.abgesagt" in [e["typ"] for e in kundensystem["empfangen"]]
    assert ksim.tool("cancel_appointment", "c3", "1")["wiederholt"]              # Wiederholung: kein zweiter Auftrag
    assert crm.kalender_status() == {"erledigt": 2}


def test_a_cancellation_the_calendar_refuses_becomes_a_task(kapp, ksim, calcom):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    del calcom.buchungen["bk_0001"]                                             # Buchung im Kalender schon weg: 404 = erledigt
    assert ksim.tool("cancel_appointment", "c3", "1")["ok"] and crm.kalender_status() == {"erledigt": 2}
    ksim.tool("check_slots", "c4", "1")
    ksim.tool("book_appointment", "c4", "1", slot_id=slots[1]["slot_id"])
    calcom.buchungen["bk_0002"]["status"] = "kaputt"
    calcom.frei.discard(ZEITEN[1])

    def ablehnen(request):
        return httpx.Response(400, json={"status": "error", "error": {"code": "BadRequestException", "message": "cannot cancel"}})
    kapp.state.ctx.kalender.kalender.client = httpx.Client(transport=httpx.MockTransport(ablehnen), base_url="https://api.cal.com")
    assert ksim.tool("cancel_appointment", "c5", "1")["ok"]                        # lokal gilt die Absage trotzdem
    assert crm.kalender_status() == {"erledigt": 3, "tot": 1}
    assert crm.aufgaben(1)[0]["titel"] == "Prüfen: Absage im Kalender fehlgeschlagen"


# ---------------------------------------------------------------- Spiegel ohne Server
def test_mirror_can_run_standalone_for_the_cli(con, uhr, calcom):
    from integrations.crm import LocalCRM
    con.execute("DELETE FROM slots")
    crm = LocalCRM(con, uhr=uhr)
    spiegel = Kalenderspiegel(crm, calcom.kalender(), uhr, tage=2)
    bericht = spiegel.spiegeln()
    assert bericht["neu"] == 4 and bericht["frei"] == 4                            # 48-h-Fenster: Mittwoch ganz, Donnerstag früh
    calcom.ausfaelle = 1
    assert "fehler" in spiegel.spiegeln() and len(crm.freie_slots(10)) == 4        # Ausfall: Tabelle bleibt, wie sie war


# ---------------------------------------------------------------- Befunde aus dem Review: Fenster, Wettläufe, Störungen
def test_cancelling_in_the_rebooking_window_cancels_both_appointments(kapp, ksim, calcom, uhr, kundensystem):
    """A bestätigt, Umbuchung auf B bei Kalenderausfall vorgemerkt: 'Bitte absagen' meint beide."""
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    calcom.ausfaelle = 1
    assert ksim.tool("book_appointment", "c2", "1", slot_id=slots[1]["slot_id"])["kalender"] == "vorgemerkt"
    r = ksim.tool("cancel_appointment", "c3", "1", grund="doch nicht")
    assert r["ok"] and r["abgesagt"] == "14.10.2026 10:30 Uhr und 14.10.2026 08:30 Uhr"
    assert crm.termin(1, zukunft=True) is None and ksim.tool("lookup_lead", "c3", "1")["naechster_termin"] is None
    assert calcom.buchungen["bk_0001"]["status"] == "cancelled"
    uhr.vor(seconds=RETRY_PLAN_S[1])
    assert kapp.state.ctx.planer.tick()["kalender"]["auftraege"]["bestaetigt"] == 0              # B war verworfen
    assert len(calcom.buchungen) == 1 and crm.kalender_status() == {"erledigt": 2, "verworfen": 1}


def test_choosing_the_old_appointment_again_discards_the_pending_one(kapp, ksim, calcom, uhr):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    calcom.ausfaelle = 1
    ksim.tool("book_appointment", "c2", "1", slot_id=slots[1]["slot_id"])                    # vorgemerkt
    r = ksim.tool("book_appointment", "c2", "1", slot_id=slots[0]["slot_id"])                # "doch der alte"
    assert r["wiederholt"] and crm.termin(1, zukunft=True, unbestaetigt=True) is None
    uhr.vor(seconds=RETRY_PLAN_S[1])
    assert kapp.state.ctx.planer.tick()["kalender"]["auftraege"]["bestaetigt"] == 0
    assert crm.termin(1)["slot_id"] == slots[0]["slot_id"] and len(calcom.buchungen) == 1


def test_an_asynchronous_rejection_after_an_outage_is_not_silent(kapp, ksim, calcom, uhr, kundensystem):
    """Der Agent hatte zugesagt (vorgemerkt); lehnt der Kalender später ab, erfahren Mensch und Kundensystem davon."""
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    b = ksim.gespraech(crm.lead(1), "happy_path")                                             # bucht über /calendar/book
    assert b["call_ended"]["ergebnis"] == "termin_gebucht"
    kapp.state.ctx.planer.tick()
    assert "termin.gebucht" in [e["typ"] for e in kundensystem["empfangen"]]
    # zweites Gespräch: Umbuchung während eines Ausfalls, danach ist die Zeit im Kalender weg
    ksim.tool("check_slots", "c2", "1")
    frei = crm.freie_slots(2)
    calcom.ausfaelle = 1
    assert ksim.tool("book_appointment", "c2", "1", slot_id=frei[0]["slot_id"])["kalender"] == "vorgemerkt"
    calcom.vergeben(crm.start_utc(frei[0]["start"]))
    uhr.vor(seconds=RETRY_PLAN_S[1])
    assert kapp.state.ctx.planer.tick()["kalender"]["auftraege"]["abgelehnt"] == 1
    assert crm.termin(1)["extern_id"] == "bk_0001"                                            # der alte Termin bleibt
    aufgabe = crm.aufgaben(1)[0]
    assert aufgabe["titel"] == "Prüfen: Kalender hat vorgemerkte Buchung abgelehnt" and "zugesagt" in aufgabe["text"]
    kapp.state.ctx.planer.tick()
    abgesagt = [e for e in kundensystem["empfangen"] if e["typ"] == "termin.abgesagt"]
    assert abgesagt and abgesagt[-1]["body"]["daten"]["grund"] == "Kalender hat die Buchung abgelehnt"


def test_a_cancellation_while_the_booking_request_is_in_flight_cancels_the_orphan(kapp, ksim, calcom):
    """cancel_appointment trifft ein, während die Buchung beim Kalender unterwegs ist: die Kalenderbuchung wird sofort storniert."""
    crm = kapp.state.ctx.crm
    spiegel = kapp.state.ctx.kalender
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    echt = spiegel.kalender.buchen

    def waehrenddessen_absage(*a, **k):
        uid = echt(*a, **k)
        with spiegel.sperre:                                                                   # wie ein zweiter Request
            crm.tools(1).cancel_appointment(grund="aufgelegt")
        return uid

    spiegel.kalender.buchen = waehrenddessen_absage
    r = ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    assert r["ok"] is False and r["kalender"] == "abgelehnt"
    assert crm.termin(1, zukunft=True) is None
    assert calcom.buchungen["bk_0001"]["status"] == "cancelled" and calcom.buchungen["bk_0001"]["cancellationReason"] == "Buchung verworfen"
    assert crm.kalender_status() == {"verworfen": 1, "erledigt": 1}


def test_confirmation_waits_for_a_planner_run_that_claimed_the_job_first(kapp, ksim, calcom, uhr):
    crm = kapp.state.ctx.crm
    spiegel = kapp.state.ctx.kalender
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    tools = crm.tools(1)
    assert tools.book_appointment(slot_id=slots[0]["slot_id"])["appointment_id"] == 1
    auftrag = crm.kalender_auftrag_fuer_termin(1)
    assert auftrag["naechster_versuch"] == crm.jetzt() + __import__("datetime").timedelta(seconds=10)   # Karenz für den Planer
    assert spiegel.auftraege_ausfuehren()["bestaetigt"] == 0                                 # Planer: noch nicht fällig
    uhr.vor(seconds=10)
    assert spiegel.auftraege_ausfuehren()["bestaetigt"] == 1                                 # Planer war schneller
    r = spiegel.bestaetigen(1)
    assert r["ausgang"] == "bestaetigt" and r["extern_id"] == "bk_0001"                       # Server liest das Ergebnis
    crm.kalender_auftrag_abschliessen(auftrag["id"], "laeuft", "", 1)                          # läuft gerade woanders
    assert spiegel.bestaetigen(1)["ausgang"] == "wiederholen"                                  # kurz gewartet, dann vorgemerkt
    crm.kalender_auftrag_abschliessen(auftrag["id"], "abgelehnt", "vergeben", 1)
    assert spiegel.bestaetigen(1)["ausgang"] == "abgelehnt"
    assert spiegel.bestaetigen(99)["ausgang"] == "uebersprungen"


def test_key_or_configuration_errors_are_outages_not_taken_slots(kapp, ksim, calcom, uhr):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    kapp.state.ctx.kalender.kalender.client.headers["Authorization"] = "Bearer rotiert"           # Schlüssel passt nicht mehr
    r = ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    assert r["ok"] and r["kalender"] == "vorgemerkt"                                             # kein "vergeben", Slot bleibt
    assert crm.con.execute("SELECT COUNT(*) FROM slots WHERE id = ?", [slots[0]["slot_id"]]).fetchone()[0] == 1
    for wartezeit in RETRY_PLAN_S[1:]:
        uhr.vor(seconds=wartezeit)
        bericht = kapp.state.ctx.planer.tick()["kalender"]["auftraege"]
    assert bericht["tot"] == 1 and crm.aufgaben(1)[0]["titel"] == "Prüfen: Kalenderbuchung nicht bestätigt"


def test_an_offered_slot_that_vanished_or_started_is_rejected_clearly(kapp, ksim, calcom, uhr):
    crm = kapp.state.ctx.crm
    kapp.state.ctx.planer.tick()
    qualifizieren(ksim)
    slots = ksim.tool("check_slots", "c1", "1")["freie_termine"]
    calcom.vergeben(ZEITEN[0])
    kapp.state.ctx.planer.tick(kalender=True)                                                   # Spiegel nimmt die Zeit raus
    r = ksim.tool("book_appointment", "c1", "1", slot_id=slots[0]["slot_id"])
    assert r["ok"] is False and r["fehler"].startswith("Dieser Termin ist inzwischen vergeben")
    uhr.vor(days=1, hours=1)                                                                    # der zweite Slot hat begonnen
    r = ksim.tool("book_appointment", "c1", "1", slot_id=slots[1]["slot_id"])
    assert r["ok"] is False and "nicht mehr in der Zukunft" in r["fehler"] and crm.termin(1) is None


def test_jobs_interrupted_by_a_restart_are_handed_to_a_human(con, uhr, calcom):
    from integrations.crm import LocalCRM
    crm = LocalCRM(con, uhr=uhr)
    crm.kalender_aktiv = True
    kid = crm.kalender_auftrag(None, "stornieren", extern_id="bk_7", lead_id=2)
    crm.kalender_auftrag_abschliessen(kid, "laeuft", "", 1)
    Kalenderspiegel(crm, calcom.kalender(), uhr)
    assert crm.kalender_status() == {"tot": 1} and crm.aufgaben(2)[0]["titel"] == "Prüfen: Kalenderauftrag 'stornieren' bei Neustart unterbrochen"
