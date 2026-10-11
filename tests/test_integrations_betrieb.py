"""Betrieb: strukturiertes Logbuch (eine JSON-Zeile je Vorgang, korreliert über call_id) und Prometheus-Exposition."""
import io
import json

from conftest import TOOL_SECRET
from integrations import logbuch


def zeilen(strom: io.StringIO) -> list[dict]:
    return [json.loads(z) for z in strom.getvalue().splitlines() if z.strip()]


def test_every_request_leaves_one_json_line_with_the_call_id(sim, app, kundensystem):
    strom = io.StringIO()
    logbuch.einrichten(strom=strom)
    try:
        crm = app.state.ctx.crm
        b = sim.gespraech(crm.lead(1), "happy_path")
        sim.contact_lookup(crm.lead(1)["telefon"])
        app.state.ctx.planer.tick()
    finally:
        logbuch.einrichten()
    log = zeilen(strom)
    assert all(set(z) >= {"zeit", "stufe", "ereignis"} and z["zeit"].endswith("Z") for z in log)
    tools = [z for z in log if z["ereignis"] == "tool"]
    assert tools and all(z["call_id"] == b["call_id"] and z["lead_id"] == "1" and z["ok"] is True and z["ms"] >= 0 for z in tools)
    assert [z["endpunkt"] for z in tools] == ["tools/lookup_lead", "tools/update_lead", "tools/update_lead", "tools/update_lead"]
    kalender = [z for z in log if z["ereignis"].startswith("kalender.")]
    assert [z["ereignis"] for z in kalender] == ["kalender.available", "kalender.book"] and kalender[1]["ok"] is True
    webhook = next(z for z in log if z["ereignis"] == "webhook")
    assert webhook["call_id"] == b["call_id"] and webhook["status"] == "verarbeitet" and webhook["ergebnis"] == "termin_gebucht"
    assert "plattform:telli" in webhook["flags"] and webhook["message_id"].startswith("msg_")
    lookup = next(z for z in log if z["ereignis"] == "contact_lookup")
    assert lookup["bekannt"] is True and lookup["lead_id"] == 1 and "telefon" not in lookup
    text = strom.getvalue()
    assert "Guten Tag" not in text and "+49 251" not in text and TOOL_SECRET not in text            # nie: Transkript, Nummer, Geheimnis


def test_forbidden_fields_and_errors_are_handled(caplog):
    strom = io.StringIO()
    logbuch.einrichten(strom=strom)
    try:
        logbuch.eintrag("test", call_id="c1", transkript="geheim", api_key="x", telefon="+49", phone_number="+49", ok=None,
                        lang="x" * 400)
        logbuch.warnung("tool.latenzbudget", ms=1200.0, budget_ms=800)
        try:
            raise ValueError("kaputt")
        except ValueError:
            logbuch.LOGGER.exception("absturz", extra={"felder": {"call_id": "c2"}})
    finally:
        logbuch.einrichten()
    log = zeilen(strom)
    assert log[0] == {"zeit": log[0]["zeit"], "stufe": "info", "ereignis": "test", "call_id": "c1", "lang": "x" * 300 + "…"}
    assert log[1]["stufe"] == "warning" and log[1]["ms"] == 1200.0
    assert log[2]["stufe"] == "error" and log[2]["call_id"] == "c2" and "ValueError: kaputt" in log[2]["ausnahme"]
    assert logbuch.einrichten() is logbuch.LOGGER and len(logbuch.LOGGER.handlers) == 1 and logbuch.LOGGER.propagate is False


def test_planner_failures_are_logged_as_errors(app, uhr, kundensystem):
    strom = io.StringIO()
    logbuch.einrichten(strom=strom)
    try:
        app.state.ctx.outbox.zustellen = lambda **kw: (_ for _ in ()).throw(RuntimeError("Datenbank weg"))
        bericht = app.state.ctx.planer.tick()
    finally:
        logbuch.einrichten()
    assert bericht["outbox"]["fehler"].startswith("RuntimeError")
    log = zeilen(strom)
    assert log[0]["ereignis"] == "planer.schritt" and log[0]["stufe"] == "error" and log[0]["schritt"] == "outbox"


def test_prometheus_exposition(client, sim, app, kundensystem):
    assert client.get("/metrics/prometheus").status_code == 401
    crm = app.state.ctx.crm
    sim.gespraech(crm.lead(1), "happy_path")
    sim.tool("book_appointment", "c9", "2", slot_id=1)                                            # fachlicher Fehler (ok: False)
    app.state.ctx.planer.tick()
    r = client.get("/metrics/prometheus", headers={"Authorization": f"Bearer {TOOL_SECRET}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = r.text
    assert 'integrations_info{version="' in text and 'usecase="pv_outbound"} 1' in text
    assert 'integrations_http_requests_total{endpunkt="tools/update_lead"} 3' in text
    assert 'integrations_http_errors_total{endpunkt="tools/book_appointment"} 1' in text
    assert 'integrations_http_latency_ms{endpunkt="webhooks/telli",quantile="0.95"}' in text
    assert 'integrations_outbox_entries{status="zugestellt"} 2' in text
    assert "integrations_planer_alive 0" in text and "integrations_geheimnisse_vollstaendig 1" in text
    assert "integrations_tool_latenzbudget_ms 800" in text and "integrations_zuordnung_offen 0" in text
    assert "# TYPE integrations_http_requests_total counter" in text and text.endswith("\n")
    for zeile in text.splitlines():                                                            # jede Zeile nach Exposition-Format
        assert zeile.startswith("#") or (" " in zeile and zeile.rsplit(" ", 1)[1].replace(".", "", 1).isdigit())


def test_prometheus_includes_calendar_and_mirror_counters(con, konfig, kundensystem, telli_attrappe, quiet, uhr):
    from fastapi.testclient import TestClient

    from integrations.server import erstelle_app
    from integrations.simulator import CalComAttrappe

    class Spiegel:
        def lead_aktualisieren(self, *a, **k): return {"ok": True}
        def notiz(self, *a, **k): return 1
        def aufgabe(self, *a, **k): return 1

    app = erstelle_app(konfig, con=con, telli_client=telli_attrappe["client"], planer_starten=False, uhr=uhr,
                       kalender=CalComAttrappe([]).kalender(), spiegel=Spiegel())
    with TestClient(app) as client:
        app.state.ctx.crm.spiegel_einreihen(1, "x", {"felder": {}, "notiz": "n", "aufgabe": None})
        app.state.ctx.crm.kalender_auftrag(None, "stornieren", extern_id="bk_1", lead_id=1)
        text = client.get("/metrics/prometheus", headers={"Authorization": f"Bearer {TOOL_SECRET}"}).text
    assert 'integrations_spiegel_auftraege{status="offen"} 1' in text and 'integrations_kalender_auftraege{status="offen"} 1' in text
