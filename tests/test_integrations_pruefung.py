"""Abnahme von außen (python -m integrations check): dieselben Requests wie die Plattform, Go/No-Go als Ergebnis.

Geprüft gegen die App im Prozess. Der Prüfer darf nichts verändern: Nach dem Lauf sind Leads, Termine,
Angebote und Protokoll wie vorher.
"""
import json

import httpx
import pytest

from conftest import API_KEY, TOOL_SECRET, WEBHOOK_SECRET
from integrations.konfig import Konfig
from integrations.pruefung import Abnahme, Bericht, Pruefpunkt


def zustand(crm):
    return [crm.con.execute(sql).fetchall() for sql in (
        "SELECT id, status, eigentuemer, dachflaeche_m2 FROM leads ORDER BY id", "SELECT * FROM appointments",
        "SELECT * FROM angebote", "SELECT id FROM calls", "SELECT * FROM webhook_eingang", "SELECT id FROM aufgaben")]


def test_a_correctly_configured_backend_gets_a_go(app, client, konfig, uhr):
    crm = app.state.ctx.crm
    vorher = zustand(crm)
    bericht = Abnahme("http://testserver", konfig, client=client, uhr=uhr).ausfuehren()
    punkte = {p.name: p for p in bericht.punkte}
    assert bericht.go and bericht.zusammenfassung["fehler"] == 0
    assert punkte["planer"].ausgang == "warnung"                                   # im Test läuft kein Hintergrundplaner
    assert {n for n, p in punkte.items() if p.ausgang == "ok"} >= {
        "health", "geheimnisse", "uhrzeit", "auth tools", "auth kalender", "auth webhook", "tool lookup_lead",
        "tool fehler als 200", "tool unbekannt", "tool-definitionen", "tool-drift", "kalender available", "kalender book",
        "contact lookup", "webhook signiert", "metrics", "latenz tools", "latenz calendar", "latenz lookup", "latenz webhook"}
    assert punkte["https"].ausgang == "warnung" and "HTTPS" in punkte["https"].detail   # testserver ist kein localhost
    assert "Thomas Becker" in punkte["tool lookup_lead"].detail
    assert "5 Tools" in punkte["tool-definitionen"].detail
    assert all(p.ms is not None for n, p in punkte.items() if n.startswith("latenz"))
    assert zustand(crm) == vorher                                                   # nichts verändert
    assert bericht.zeit == "2026-10-13T08:00:00.000Z"


def test_an_unprotected_backend_is_a_warning_in_staging_and_a_no_go_in_production(con, telli_attrappe, quiet, uhr):
    from fastapi.testclient import TestClient

    from integrations.server import erstelle_app
    offen = Konfig(db_pfad=":memory:")
    app = erstelle_app(offen, con=con, telli_client=telli_attrappe["client"], planer_starten=False, uhr=uhr)
    with TestClient(app, base_url="http://localhost") as client:
        bericht = Abnahme("http://localhost", offen, client=client, uhr=uhr).ausfuehren()
        punkte = {p.name: p for p in bericht.punkte}
        assert bericht.go and punkte["geheimnisse"].ausgang == "warnung"
        assert punkte["https"].ausgang == "ok" and punkte["https"].detail == "lokal, HTTP erlaubt"
        assert [punkte[n].ausgang for n in ("auth tools", "auth kalender", "auth webhook")] == ["uebersprungen"] * 3
        assert punkte["webhook signiert"].ausgang == "ok"                            # unsigniert angenommen, Typ ignoriert
        bericht = Abnahme("http://localhost", offen, client=client, uhr=uhr, produktiv=True).ausfuehren()
        assert not bericht.go and {p.name for p in bericht.punkte if p.ausgang == "fehler"} == {"geheimnisse"}


def test_wrong_secrets_on_the_checker_side_are_a_no_go(app, client, uhr):
    falsch = Konfig(db_pfad=":memory:", tool_secret="falsch", telli_api_key="falsch", telli_webhook_secret=WEBHOOK_SECRET)
    bericht = Abnahme("http://testserver", falsch, client=client, uhr=uhr).ausfuehren()
    punkte = {p.name: p for p in bericht.punkte}
    assert not bericht.go
    assert punkte["auth tools"].ausgang == "ok"                                      # ohne Token 401 bleibt richtig
    assert punkte["tool lookup_lead"].ausgang == "fehler" and "HTTP 401" in punkte["tool lookup_lead"].detail
    assert punkte["kalender available"].ausgang == "fehler" and punkte["contact lookup"].ausgang == "fehler"
    assert punkte["tool-definitionen"].ausgang == "fehler"


def test_an_open_backend_with_secrets_on_the_checker_is_caught(con, telli_attrappe, quiet, uhr, konfig):
    """Der Server läuft ohne Geheimnisse, der Prüfer erwartet welche: Auth-Prüfpunkte schlagen fehl."""
    from fastapi.testclient import TestClient

    from integrations.server import erstelle_app
    app = erstelle_app(Konfig(db_pfad=":memory:"), con=con, telli_client=telli_attrappe["client"], planer_starten=False, uhr=uhr)
    with TestClient(app) as client:
        bericht = Abnahme("https://backend.example", konfig, client=client, uhr=uhr).ausfuehren()
    punkte = {p.name: p for p in bericht.punkte}
    assert not bericht.go
    assert punkte["auth tools"].ausgang == "fehler" and "offen" in punkte["auth tools"].detail
    assert punkte["auth kalender"].ausgang == "fehler" and punkte["auth webhook"].ausgang == "fehler"


def test_tool_drift_between_server_and_checker_is_reported(app, client, konfig, uhr):
    konfig.usecase = "telko_inbound"
    bericht = Abnahme("http://testserver", konfig, client=client, uhr=uhr).ausfuehren()
    punkte = {p.name: p for p in bericht.punkte}
    assert bericht.go and punkte["tool-drift"].ausgang == "warnung" and "telko_inbound" in punkte["tool-drift"].detail


def test_an_unreachable_backend_stops_after_health(konfig, uhr):
    def weg(request):
        raise httpx.ConnectError("Verbindung abgelehnt")
    client = httpx.Client(transport=httpx.MockTransport(weg), base_url="https://backend.example")
    bericht = Abnahme("https://backend.example", konfig, client=client, uhr=uhr).ausfuehren()
    assert not bericht.go and [p.name for p in bericht.punkte] == ["https", "health"]
    assert "ConnectError" in bericht.punkte[1].detail


def test_clock_skew_and_dead_letters_are_findings(app, client, konfig, uhr, kundensystem, sim):
    from integrations.uhr import Testuhr
    pruefer = Testuhr("2026-10-13T08:02:00Z")                                      # zwei Minuten neben der Serveruhr
    bericht = Abnahme("http://testserver", konfig, client=client, uhr=pruefer).ausfuehren()
    punkte = {p.name: p for p in bericht.punkte}
    assert punkte["uhrzeit"].ausgang == "fehler" and "120 s" in punkte["uhrzeit"].detail
    assert punkte["auth webhook"].ausgang == "ok" and punkte["webhook signiert"].ausgang == "ok"   # 2 min liegen in der Toleranz
    kundensystem["ausfaelle"] = 99
    sim.gespraech(app.state.ctx.crm.lead(4), "opt_out")
    for _ in range(9):
        app.state.ctx.planer.tick()
        uhr.vor(hours=11)
    assert app.state.ctx.outbox.status()["tot"] >= 1
    bericht = Abnahme("http://testserver", konfig, client=client, uhr=uhr).ausfuehren()
    punkte = {p.name: p for p in bericht.punkte}
    assert punkte["metrics"].ausgang == "warnung" and "tote Einträge: Outbox" in punkte["metrics"].detail


def test_report_formats(konfig):
    b = Bericht("https://b.example", "2026-10-13T08:00:00.000Z",
                [Pruefpunkt("health", "ok", "Version 1.0.0", 5.2), Pruefpunkt("auth tools", "fehler", "offen"),
                 Pruefpunkt("planer", "warnung", "läuft nicht"), Pruefpunkt("auth kalender", "uebersprungen", "kein Key")])
    assert not b.go and b.zusammenfassung == {"ok": 1, "warnung": 1, "fehler": 1, "uebersprungen": 1}
    tabelle = b.als_tabelle()
    assert "✗ auth tools" in tabelle and "NO-GO: 1 ok, 1 Warnungen, 1 Fehler, 1 übersprungen" in tabelle
    md = b.als_markdown("FlinkNet")
    assert md.startswith("# Abnahmeprotokoll Integration FlinkNet") and "| health | ok | 5 ms | Version 1.0.0 |" in md
    d = json.loads(json.dumps(b.als_dict()))
    assert d["go"] is False and d["punkte"][0] == {"name": "health", "ausgang": "ok", "detail": "Version 1.0.0", "ms": 5.2}


def test_cli_check_writes_the_protocol_and_exits_nonzero_on_no_go(app, client, konfig, uhr, tmp_path, monkeypatch, capsys):
    from integrations import __main__ as cli
    from integrations import pruefung

    class ImProzess(pruefung.Abnahme):
        def __init__(self, url, konfig_, **kw):
            super().__init__(url, konfig_, client=client, uhr=uhr, **kw)       # App im Prozess, Uhr des Servers

    monkeypatch.setattr(pruefung, "Abnahme", ImProzess)
    monkeypatch.setattr(cli.Konfig, "aus_umgebung", classmethod(lambda cls: konfig))
    protokoll = tmp_path / "abnahme.md"
    cli.main(["check", "--url", "http://testserver", "--protokoll", str(protokoll), "--kunde", "SonnenWerk", "--json"])
    aus = json.loads(capsys.readouterr().out.split("Abnahmeprotokoll geschrieben")[0])
    assert aus["go"] is True and protokoll.read_text(encoding="utf-8").startswith("# Abnahmeprotokoll Integration SonnenWerk")
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "--url", "http://testserver", "--prod"])                   # HTTP ist in Produktion ein Fehler
    assert e.value.code == 1 and "NO-GO" in capsys.readouterr().out


def test_rotated_secrets_use_the_newest_key_on_the_checker_side(app, client, uhr):
    rotiert = Konfig(db_pfad=":memory:", tool_secret=f"alt,{TOOL_SECRET}", telli_api_key=f"alt,{API_KEY}",
                     telli_webhook_secret=f"whsec_YWx0,{WEBHOOK_SECRET}")
    bericht = Abnahme("http://testserver", rotiert, client=client, uhr=uhr).ausfuehren()
    assert bericht.go and all(p.ausgang != "fehler" for p in bericht.punkte)
    assert {p.name: p.ausgang for p in bericht.punkte}["tool lookup_lead"] == "ok"
