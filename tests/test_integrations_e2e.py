"""Ende-zu-Ende: Der Simulator fährt als Plattform komplette Pilotgespräche gegen die App im Prozess,
das Attrappen-Kundensystem empfängt die signierten Ereignisse. Zum Schluss der Paritätsbeweis:
Der Leitfaden aus guided.py läuft unverändert, wenn seine Tools über HTTP gehen.
"""
import pytest

import agent
import guided
from conftest import TOOL_SECRET, customer


def test_pilot_conversations_end_in_the_right_crm_state(sim, app, kundensystem):
    crm = app.state.ctx.crm
    erwartung = {"happy_path": "termin_gebucht", "mieter": "disqualifiziert", "keine_zeit": "rueckruf_vereinbart",
                 "opt_out": "opt_out", "behauptet_termin": "rueckruf_vereinbart"}
    for lead_id, (persona, erwartet) in enumerate(erwartung.items(), start=1):
        bericht = sim.gespraech(crm.lead(lead_id), persona)
        assert bericht["call_ended"]["status"] == "verarbeitet" and bericht["call_ended"]["ergebnis"] == erwartet, persona
    assert crm.termin(1)["slot_id"] is not None and crm.lead(1)["status"] == "qualifiziert"
    assert crm.lead(2)["status"] == "disqualifiziert" and crm.lead(2)["eigentuemer"] is False
    assert crm.lead(3)["status"] == "rueckruf" and crm.aufgaben(3)[0]["titel"] == "Rückruf"
    assert crm.lead(4)["status"] == "opt_out" and crm.gesperrt(crm.lead(4)["telefon"])
    assert crm.aufgaben(5)[0]["titel"].startswith("Prüfen") and crm.termin(5) is None
    assert all(e["status"] == 200 for e in sim.protokoll)
    assert [a["ergebnis"] for lid in range(1, 6) for a in crm.anrufe(lid)] == list(erwartung.values())

    typen = [e["typ"] for e in kundensystem["empfangen"]]
    assert typen.count("anruf.beendet") == 5 and "termin.gebucht" in typen and "lead.gesperrt" in typen
    assert typen.count("aufgabe.erstellt") == 2
    assert app.state.ctx.outbox.status() == {"zugestellt": len(typen)}
    assert len({e["id"] for e in kundensystem["empfangen"]}) == len(typen)      # jede Ereignis-ID genau einmal

    # Rückruf des Kunden später: Contact Lookup kennt ihn samt Status
    lookup = sim.contact_lookup(crm.lead(4)["telefon"])
    assert lookup["contact"]["properties"]["gesperrt"] is True and lookup["contact"]["properties"]["status"] == "opt_out"


def test_platform_retries_of_the_webhook_do_not_double_anything(sim, app, kundensystem):
    crm = app.state.ctx.crm
    bericht = sim.gespraech(crm.lead(1), "keine_zeit")
    call_id = bericht["call_id"]
    wiederholung = sim.call_ended(call_id, "1", crm.lead(1)["telefon"], {"ergebnis": "rueckruf_vereinbart"},
                                  message_id="msg_retry_neu")
    assert wiederholung.json()["status"] == "duplikat"
    assert len(crm.aufgaben(1)) == 1 and len(crm.anrufe(1)) == 1
    assert len([e for e in kundensystem["empfangen"] if e["typ"] == "aufgabe.erstellt"]) == 1


def test_voicemail_attempts_do_not_change_the_lead(sim, app):
    crm = app.state.ctx.crm
    bericht = sim.gespraech(crm.lead(2), "nicht_erreicht")
    assert bericht["call_ended"]["ergebnis"] == "nicht_erreicht"
    assert crm.lead(2)["status"] == "offen" and crm.aufgaben(2) == [] and crm.anrufe(2)[0]["turns"] == 0


# ---------------------------------------------------------------- Parität: Leitfaden über HTTP-Tools
class HTTPTools:
    """Gegenstück zu agent.Tools auf der Plattformseite: gleiche run()-Schnittstelle, jeder Aufruf ein Request.

    Das Gesprächsende ist Sache der Plattform, deshalb bleibt end_call lokal; alles andere geht über
    /tools/{name} wie bei telli. Angebots- und Buchungszustand lebt serverseitig in der Datenbank.
    """

    def __init__(self, client, lead_id: int, call_id: str = "call_paritaet"):
        self.client, self.lead_id, self.call_id = client, lead_id, call_id
        self.ended, self.errors, self.booked, self.offered = None, 0, False, set()
        self.aufrufe = []

    def run(self, name: str, args: dict):
        if name == "end_call":
            self.ended = args.get("grund")
            return {"ok": True}
        r = self.client.post(f"/tools/{name}", json={"call_id": self.call_id, "external_id": str(self.lead_id), **(args or {})},
                             headers={"Authorization": f"Bearer {TOOL_SECRET}"})
        ergebnis = r.json()
        self.aufrufe.append((name, r.status_code))
        if "validierungsfehler" in ergebnis:
            self.errors += 1
        if name == "check_slots" and "freie_termine" in ergebnis:
            self.offered |= {s["slot_id"] for s in ergebnis["freie_termine"]}
        if name == "book_appointment" and ergebnis.get("ok"):
            self.booked = True
        return ergebnis


class LeitfadenHTTP(guided.Leitfaden):
    def known(self) -> dict:
        r = self.tools.run("lookup_lead", {})
        return {f: r.get(f) for f in guided.FIELDS}


@pytest.fixture
def leitfaden_ueber_http(client, monkeypatch):
    werkzeuge = []

    def tools_factory(con, lead_id):
        t = HTTPTools(client, lead_id)
        werkzeuge.append(t)
        return t

    monkeypatch.setattr(guided, "Tools", tools_factory)
    monkeypatch.setattr(guided, "Leitfaden", LeitfadenHTTP)
    monkeypatch.setattr(guided, "FORMULATE", False)
    return werkzeuge


def test_guided_flow_runs_unchanged_over_http_tools(con, app, no_jev, quiet, llm, leitfaden_ueber_http):
    llm([], extractions=[
        {"eigentuemer": True, "gebaeudetyp": "einfamilienhaus", "dachflaeche_m2": 70, "jahresverbrauch_kwh": 4200},
        {"termin_wahl": "zweiter"},
    ])
    r = guided.run_guided_call(con, 1, get_input=customer("Ja, Eigentümer, Einfamilienhaus, 70 m², 4200 kWh.",
                                                          "Der zweite passt."))
    assert r["ergebnis"] == "termin_gebucht" and r["termin_im_system"] is True and r["tool_fehler"] == 0
    tools = leitfaden_ueber_http[0]
    namen = [n for n, _ in tools.aufrufe]
    assert namen.count("check_slots") == 1 and namen[-1] == "book_appointment" and all(s == 200 for _, s in tools.aufrufe)
    assert namen.count("lookup_lead") >= 2                                   # Leitfaden liest den Stand über HTTP
    crm = app.state.ctx.crm
    zweiter = con.execute("SELECT id FROM slots WHERE start > now() ORDER BY start LIMIT 2").fetchall()[1][0]
    assert crm.termin(1)["slot_id"] == zweiter


def test_guided_disqualification_over_http(con, app, no_jev, quiet, llm, leitfaden_ueber_http):
    llm([], extractions=[{"eigentuemer": False}])
    r = guided.run_guided_call(con, 1, get_input=customer("Ich wohne zur Miete."))
    assert r["ergebnis"] == "disqualifiziert" and r["status"] == "disqualifiziert"
    assert app.state.ctx.crm.lead(1)["eigentuemer"] is False
    assert agent.Tools(con, 1).check_slots()["ok"] is False
