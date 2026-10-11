"""KPIs als Code: dieselben Definitionen wie im Playbook (05), berechnet aus CRM, Kalender und Protokoll."""
from datetime import timedelta

from integrations import kennzahlen


def test_kpis_from_a_demo_pilot(sim, app):
    crm = app.state.ctx.crm
    crm.con.execute("UPDATE leads SET erstellt_am = ?", [crm.jetzt() - timedelta(minutes=7)])
    for lead_id, persona in enumerate(["happy_path", "mieter", "keine_zeit", "opt_out", "behauptet_termin"], start=1):
        sim.gespraech(crm.lead(lead_id), persona)
    sim.gespraech(crm.lead(5), "nicht_erreicht")                      # zweiter Versuch, Mailbox
    app.state.ctx.planer.tick()
    k = kennzahlen.kennzahlen(crm, tage=7)
    assert k["anrufe"] == 6 and k["anrufe_erreicht"] == 5 and k["leads_angerufen"] == 5 and k["leads_erreicht"] == 5
    assert k["erreichbarkeit"] == 1.0
    assert k["qualifizierungsquote"] == 0.4                            # Eigentümer EFH und Mieter haben geantwortet
    assert k["terminquote"] == 1.0 and k["termine"] == 1               # ein qualifizierter Lead, ein Termin
    assert k["fehlerquote"] == 0.2 and k["fehler_anrufe"] == 1         # behauptete Buchung ohne Buchung
    assert k["opt_out_rate"] == 0.2 and k["gesperrt"] == 1
    assert 3 <= k["speed_to_lead_min"] <= 5 and k["speed_to_lead_n"] == 5
    assert k["zustellquote"] == 1.0 and k["outbox_tot"] == 0
    assert k["ergebnisse"]["rueckruf_vereinbart"] == 2 and k["ergebnisse"]["nicht_erreicht"] == 1
    assert k["flags"]["termin_behauptet_ohne_buchung"] == 1 and "plattform:telli" not in k["flags"]
    assert k["aufgaben_offen"] == 2 and k["erinnerungen"] == {"offen": 1}
    assert kennzahlen.status(k, "erreichbarkeit") == "✓" and kennzahlen.status(k, "fehlerquote") == "✗"
    assert kennzahlen.status(k, "speed_to_lead_min") == "✓"


def test_empty_database_gives_no_division_errors(app):
    k = kennzahlen.kennzahlen(app.state.ctx.crm, tage=1)
    assert k["anrufe"] == 0 and k["erreichbarkeit"] is None and k["terminquote"] is None and k["speed_to_lead_min"] is None
    assert kennzahlen.status(k, "erreichbarkeit") == "–"
    report = kennzahlen.wochenreport(k, kunde="Testkunde", anteil="10 %")
    assert "# Wochenreport Voice-Agent, Testkunde" in report and "Anteil der Leads im Agenten: 10 %" in report
    assert "| – | 0 | – |" in report and "| keine | 0 |" in report


def test_report_follows_the_playbook_template(sim, app):
    crm = app.state.ctx.crm
    sim.gespraech(crm.lead(1), "happy_path")
    k = kennzahlen.kennzahlen(crm, tage=7)
    report = kennzahlen.wochenreport(k, gelernt=["Preisfrage kommt früh → Preis-Satz nach vorn"],
                                     aenderungen=["2026-10-10 | Preis-Satz | Review | 11.10."])
    for abschnitt in ("## Zahlen", "## Ergebnisverteilung", "## Flags", "## Anrufe je Tag", "## Was wir gelernt haben",
                      "## Änderungen am Agenten diese Woche", "## Entscheidungen, die wir vom Kunden brauchen"):
        assert abschnitt in report
    assert "| termin_gebucht | 1 | 100% |" in report and "Preis-Satz nach vorn" in report
    assert "| 2026-10-10 | Preis-Satz | Review | 11.10. |" in report


def test_time_window_filters_calls(app):
    crm = app.state.ctx.crm
    alt = crm.jetzt() - timedelta(days=30)
    crm.anruf_protokollieren(1, alt, alt, "kein_interesse", 2, 0, [], [])
    crm.anruf_protokollieren(2, None, None, "opt_out", 1, 0, [], [])      # ohne Zeit: zählt nicht zum Fenster
    assert kennzahlen.kennzahlen(crm, tage=7)["anrufe"] == 0
    assert kennzahlen.kennzahlen(crm, tage=60)["anrufe"] == 1
