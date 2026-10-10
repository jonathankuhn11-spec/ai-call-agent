"""Kommandozeile der Integrationsschicht.

    python -m integrations serve              HTTP-Server (uvicorn) für die Plattform
    python -m integrations demo               Pilotgespräche im Prozess, ohne Account, ohne Netz
    python -m integrations definitions        Tool-Definitionen für die telli-Konfiguration (JSON)
    python -m integrations outbox             Zustand der ausgehenden Webhooks, --zustellen, --erneut ID
    python -m integrations push --trocken     Lead-Push ins Dialer: zeigt die Requests, ohne zu senden
    python -m integrations review             Transkript-Review der letzten 24 h als Markdown (--quelle telli für Live-Anrufe)
    python -m integrations abgleich           Anrufe ohne call_ended über List Calls nachholen (--call ID für einen Anruf)
    python -m integrations report             Wochenreport (KPIs aus der Datenbank) als Markdown, --tage 7
    python -m integrations konfigpaket        Prompt, Outcomes, Properties, Tools, Kalender, Webhook je Use Case (--usecase, --alle)
    python -m integrations demo --db pilot.duckdb   Demo-Daten in eine Datei schreiben (für Pilot-Cockpit und Report)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import httpx

import agent
from integrations.konfig import Konfig
from integrations.modelle import iso_utc


def cmd_serve(a, konfig: Konfig):
    import uvicorn

    from integrations.server import erstelle_app
    fehlt = konfig.fehlende_geheimnisse()
    if fehlt and konfig.produktiv:
        sys.exit(f"UMGEBUNG=prod: Start verweigert, es fehlen {', '.join(fehlt)}.")
    if fehlt:
        print(f"Hinweis: ohne {', '.join(fehlt)} laufen die betroffenen Prüfungen nicht (nur für lokale Tests ok).")
    print("Hintergrundplaner im Prozess: Outbox, Spiegelung, Lead-Push. DuckDB erlaubt nur einen schreibenden "
          "Prozess, deshalb bei laufendem Server die Endpunkte /push und /outbox nutzen, nicht die Kommandozeile.")
    uvicorn.run(erstelle_app(konfig), host=a.host, port=a.port, workers=1)


def cmd_definitions(a, konfig: Konfig):
    from integrations.server import tool_definitionen
    from integrations.usecases import alle
    usecase = alle().get(a.usecase or konfig.usecase)
    nur = usecase.tool_namen() if usecase else None
    print(json.dumps(tool_definitionen(a.url, nur=nur), ensure_ascii=False, indent=2))


def _db(konfig: Konfig):
    try:
        return agent.init_db(path=konfig.db_pfad)
    except Exception as e:  # noqa: BLE001 - DuckDB: nur ein schreibender Prozess
        if "lock" in str(e).lower():
            sys.exit("Datenbank ist vom laufenden Server gesperrt. Bei laufendem Server die Endpunkte "
                     "POST /push, GET /outbox, POST /outbox/zustellen, POST /outbox/{id}/erneut nutzen.")
        raise


def cmd_outbox(a, konfig: Konfig):
    from integrations.outbox import Outbox
    con = _db(konfig)
    outbox = Outbox(con, konfig.kunde_webhook_url, konfig.kunde_webhook_secret)
    if a.erneut:
        outbox.erneut(a.erneut)
        print(f"Eintrag {a.erneut} wieder offen.")
    if a.zustellen:
        print("Zustellung:", outbox.zustellen())
    print("Status:", outbox.status() or "leer")
    for e in outbox.eintraege("tot"):
        print(f"  tot: #{e['id']} {e['typ']} nach {e['versuche']} Versuchen: {e['letzte_antwort']}")


def cmd_push(a, konfig: Konfig):
    from integrations.crm import LocalCRM
    from integrations.telli import LeadPush, TelliClient
    con = _db(konfig)
    crm = LocalCRM(con, konfig.zeitzone)
    gesendet = []
    if a.trocken:
        def handler(request: httpx.Request) -> httpx.Response:
            gesendet.append((request.method, request.url.path, json.loads(request.content)))
            if request.url.path == "/v2/contacts":
                return httpx.Response(201, json={"id": f"ct_trocken_{len(gesendet)}", "type": "Contact"})
            return httpx.Response(200, json={"status": "success", "contact_id": "ct", "loop_id": f"loop_{len(gesendet)}"})
        client = TelliClient("trocken", client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telli.com"))
        con.execute("BEGIN")
    else:
        if not konfig.telli_api_key or not konfig.telli_agent_id:
            sys.exit("TELLI_API_KEY und TELLI_AGENT_ID fehlen (.env).")
        from integrations.server import telli_client_aus_konfig
        client = telli_client_aus_konfig(konfig)
    bericht = LeadPush(crm, client, konfig.telli_agent_id or "agent_demo").synchronisieren()
    if a.trocken:
        con.execute("ROLLBACK")
        print("Trockenlauf, nichts gesendet, nichts gespeichert. Requests, die gesendet würden:\n")
        for methode, pfad, body in gesendet:
            print(f"{methode} https://api.telli.com{pfad}\n{json.dumps(body, ensure_ascii=False, indent=2)}\n")
    print("Bericht:", json.dumps(bericht, ensure_ascii=False))


def cmd_review(a, konfig: Konfig):
    from integrations import review
    from integrations.crm import LocalCRM
    con = _db(konfig)
    crm = LocalCRM(con, konfig.zeitzone)
    if a.quelle == "telli":
        from integrations.server import telli_client_aus_konfig
        if not konfig.telli_api_key:
            sys.exit("TELLI_API_KEY fehlt (.env).")
        reviews = review.aus_telli(telli_client_aus_konfig(konfig), crm, agent_id=konfig.telli_agent_id or None, limit=a.limit)
    else:
        reviews = review.aus_db(crm, tage=a.tage)
    text = review.reviewliste(reviews)
    pfad = a.ausgabe or f"review-{datetime.now():%Y-%m-%d}.md"
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"Geschrieben: {pfad}")


def cmd_konfigpaket(a, konfig: Konfig):
    from pathlib import Path

    from integrations import konfigpaket
    from integrations.usecases import alle
    faelle = alle()
    ids = list(faelle) if a.alle else [a.usecase or konfig.usecase]
    for uid in ids:
        if uid not in faelle:
            sys.exit(f"Unbekannter Use Case '{uid}'. Bekannt: {', '.join(faelle)}")
        pfade = konfigpaket.schreiben(faelle[uid], a.url, Path(a.out) / uid)
        print(f"{uid}: {len(pfade)} Dateien nach {Path(a.out) / uid}")


def cmd_report(a, konfig: Konfig):
    from integrations import kennzahlen
    from integrations.crm import LocalCRM
    con = _db(konfig)
    crm = LocalCRM(con, konfig.zeitzone)
    k = kennzahlen.kennzahlen(crm, tage=a.tage)
    text = kennzahlen.wochenreport(k, kunde=a.kunde, anteil=a.anteil)
    pfad = a.ausgabe or f"wochenreport-{datetime.now():%Y-%m-%d}.md"
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"Geschrieben: {pfad}")


def cmd_abgleich(a, konfig: Konfig):
    from integrations.abgleich import Abgleich, anruf_nachholen
    from integrations.crm import LocalCRM
    from integrations.nachbereitung import Nachbereitung
    from integrations.server import outbox_aus_konfig, spiegel_aus_konfig, telli_client_aus_konfig
    if not konfig.telli_api_key:
        sys.exit("TELLI_API_KEY fehlt (.env).")
    con = _db(konfig)
    crm = LocalCRM(con, konfig.zeitzone)
    client = telli_client_aus_konfig(konfig)
    outbox = outbox_aus_konfig(konfig, con)
    nb = Nachbereitung(crm, outbox, spiegel_aus_konfig(konfig))          # wie im Server: Ereignisse und Spiegelung
    if a.call:
        print(json.dumps(anruf_nachholen(crm, client, nb, a.call), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(Abgleich(crm, client, nb, mindestalter_min=0).ausfuehren(), ensure_ascii=False, indent=2))
    if outbox is not None:
        print("Outbox:", outbox.zustellen())
    if nb.spiegel is not None:
        print("Spiegel:", nb.spiegeln())


def cmd_demo(a, konfig: Konfig):
    """Alle Personas gegen die App im Prozess; ein Attrappen-Ticketsystem empfängt die Ereignisse."""
    from fastapi.testclient import TestClient

    from integrations.outbox import Outbox
    from integrations.server import erstelle_app
    from integrations.signatur import svix_pruefen
    from integrations.simulator import PlattformSimulator

    konfig.telli_api_key = konfig.telli_api_key or "demo-api-key"
    konfig.telli_webhook_secret = konfig.telli_webhook_secret or "whsec_ZGVtby13ZWJob29rLXNlY3JldA=="
    konfig.tool_secret = konfig.tool_secret or "demo-tool-secret"
    kunden_secret = "whsec_a3VuZGVuLXN5c3RlbS1zZWNyZXQ="
    empfangen: list[dict] = []

    def ticketsystem(request: httpx.Request) -> httpx.Response:
        svix_pruefen(kunden_secret, request.content, request.headers)       # wirft bei falscher Signatur
        empfangen.append({"typ": request.headers["x-ereignis-typ"], "id": request.headers["idempotency-key"],
                          "daten": json.loads(request.content)["daten"]})
        return httpx.Response(200)

    agent.VERBOSE = False
    if a.db and os.path.exists(a.db):
        print(f"Hinweis: {a.db} wird für die Demo neu aufgesetzt (vorheriger Inhalt entfällt).")
        os.remove(a.db)
    con = agent.init_db(reset=True, path=a.db or ":memory:")
    # Eingangszeit der Leads für Speed-to-Lead: fiktiv sieben Minuten vor der Demo
    con.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS erstellt_am TIMESTAMP")
    con.execute("UPDATE leads SET erstellt_am = now()::TIMESTAMP - INTERVAL 7 MINUTE")
    outbox = Outbox(con, "https://ticketsystem.kunde.example/webhooks", kunden_secret,
                    client=httpx.Client(transport=httpx.MockTransport(ticketsystem)))
    app = erstelle_app(konfig, con=con, outbox=outbox, planer_starten=False)
    personas = ["happy_path", "mieter", "keine_zeit", "opt_out", "behauptet_termin"]
    with TestClient(app) as client:
        sim = PlattformSimulator(client, konfig.telli_api_key, konfig.telli_webhook_secret, konfig.tool_secret)
        print("=== Demo: Pilotgespräche der Plattform gegen die Integrationsschicht ===\n")
        for lead_id, persona in enumerate(personas, start=1):
            lead = app.state.ctx.crm.lead(lead_id)
            b = sim.gespraech(lead, persona)
            ce = b["call_ended"]
            zusatz = ""
            if "termin" in ce:
                zusatz = f" Termin {ce['termin']}"
            if "aufgabe_id" in ce:
                zusatz = f" Aufgabe #{ce['aufgabe_id']}"
            flags = [f for f in ce.get("flags", []) if not f.startswith(("plattform:", "attempt:", "call:"))]
            print(f"Lead {lead_id} {lead['name']:<16} {persona:<17} -> {ce['ergebnis']:<20}{zusatz}"
                  f"{'  Flags: ' + ', '.join(flags) if flags else ''}")
        lookup = sim.contact_lookup(app.state.ctx.crm.lead(1)["telefon"])
        print(f"\nContact Lookup (Rückruf von Lead 1): {lookup['contact']['first_name']} {lookup['contact']['last_name']}, "
              f"Status {lookup['contact']['properties']['status']}")
        metrik = client.get("/metrics", headers={"Authorization": f"Bearer {konfig.tool_secret}"}).json()["endpunkte"]
    print("\nLatenz je Endpunkt (im Prozess, ohne Netz):")
    for ep, m in metrik.items():
        print(f"  {ep:<26} {m['anzahl']:>3} Aufrufe  p50 {m['p50_ms']:>5} ms  p95 {m['p95_ms']:>5} ms  Fehler {m['fehler']}")
    print(f"\nOutbox: {outbox.status()} | beim Ticketsystem angekommen und signaturgeprüft: {len(empfangen)}")
    for e in empfangen:
        print(f"  {e['typ']:<16} {json.dumps(e['daten'], ensure_ascii=False)[:110]}")
    crm = app.state.ctx.crm
    print("\nCRM danach:")
    for lid in range(1, len(personas) + 1):
        l = crm.lead(lid)
        termin = crm.termin(lid)
        print(f"  {l['name']:<16} status={l['status']:<16} eigentuemer={l['eigentuemer']!s:<5} "
              f"termin={iso_utc(crm.start_utc(termin['start'])) if termin else '-':<25} "
              f"aufgaben={len(crm.aufgaben(lid))} gesperrt={crm.gesperrt(l['telefon'])}")
    _demo_inbound(konfig, kunden_secret, ticketsystem, empfangen)
    if a.db:
        con.close()
        print(f"\nDatenbank geschrieben: {a.db}  (Pilot-Cockpit: DB_PATH={a.db} python -m streamlit run pilot_dashboard.py; "
              f"Report: DB_PATH={a.db} python -m integrations report)")
    print(f"\nStand: {iso_utc(datetime.now(timezone.utc))}")


def _demo_inbound(konfig: Konfig, kunden_secret: str, ticketsystem, empfangen: list):
    """Use Case 2 im selben Lauf: Inbound-Service (telko_inbound) gegen eine zweite App mit eigener Datenbank."""
    from fastapi.testclient import TestClient

    from integrations.outbox import Outbox
    from integrations.server import erstelle_app
    from integrations.simulator import INBOUND_PERSONAS, PlattformSimulator
    from integrations.usecases import alle

    print("\n=== Use Case 2: Inbound-Service FlinkNet (Verifizierung, Termin-Lebenszyklus, Ticket) ===\n")
    konfig2 = Konfig(**{**konfig.__dict__, "usecase": "telko_inbound"})
    con = agent.init_db(reset=True, path=":memory:")
    vorher = len(empfangen)
    outbox = Outbox(con, "https://ticketsystem.kunde.example/webhooks", kunden_secret,
                    client=httpx.Client(transport=httpx.MockTransport(ticketsystem)))
    app = erstelle_app(konfig2, con=con, outbox=outbox, planer_starten=False)
    crm = app.state.ctx.crm
    # Bestehende Techniker-Termine wie aus dem Auftragssystem: Lead 1 in drei Tagen, Lead 2 in zwölf Stunden, Lead 3 in vier Tagen
    for lead_id, in_stunden in ((1, 72), (2, 12), (3, 96)):
        tools = crm.tools(lead_id)
        slot = crm.freie_slots(1)[0]
        con.execute(f"UPDATE slots SET start = now()::TIMESTAMP + INTERVAL {in_stunden} HOUR WHERE id = ?", [slot["slot_id"]])
        tools.offered = {slot["slot_id"]}
        tools.book_appointment(slot_id=slot["slot_id"], art="vor_ort")
    con.execute("UPDATE appointments SET gebucht_am = gebucht_am - INTERVAL 3 DAY")
    with TestClient(app) as client:
        sim = PlattformSimulator(client, konfig2.telli_api_key, konfig2.telli_webhook_secret, konfig2.tool_secret)
        for lead_id, persona in zip((1, 2, 3, 4, 5), INBOUND_PERSONAS):
            lead = crm.lead(lead_id)
            b = sim.gespraech(lead, persona)
            ce = b["call_ended"]
            flags = [f for f in ce.get("flags", []) if not f.startswith(("plattform:", "attempt:", "call:"))]
            detail = ""
            if "ticket" in b:
                detail = f" Ticket {b['ticket']['ticket_nummer']}"
            elif b.get("cancel", {}).get("weiterleiten"):
                detail = " cancel_appointment abgelehnt (Mindestvorlauf 24 h)"
            elif "termin" in ce:
                detail = f" neuer Termin {ce['termin']}"
            elif persona == "nicht_verifiziert":
                detail = f" {len(b['versuche'])} Versuche, keine Auskunft (lookup_lead vorher: ok={b['vor_verifizierung']['ok']})"
            print(f"Lead {lead_id} {lead['name']:<16} {persona:<19} -> {ce['ergebnis']:<18}{detail}"
                  f"{'  Flags: ' + ', '.join(flags) if flags else ''}")
        app.state.ctx.planer.tick()
    neu = empfangen[vorher:]
    print(f"\nEreignisse an das Ticketsystem (signaturgeprüft): {', '.join(e['typ'] for e in neu)}")
    print(f"Konfigurationspaket für den Builder: docs/telli-konfig/telko_inbound/ ({len(alle()['telko_inbound'].tools)} Tools, "
          f"{len(alle()['telko_inbound'].outcomes)} Outcomes)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m integrations", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="0.0.0.0"); s.add_argument("--port", type=int, default=8000)
    d = sub.add_parser("definitions"); d.add_argument("--url", default="https://backend.kunde.example"); d.add_argument("--usecase")
    o = sub.add_parser("outbox"); o.add_argument("--zustellen", action="store_true"); o.add_argument("--erneut", type=int)
    p = sub.add_parser("push"); p.add_argument("--trocken", action="store_true")
    r = sub.add_parser("review"); r.add_argument("--quelle", choices=["db", "telli"], default="db")
    r.add_argument("--tage", type=int, default=1); r.add_argument("--limit", type=int, default=100); r.add_argument("--ausgabe")
    g = sub.add_parser("abgleich"); g.add_argument("--call", help="nur diesen Anruf per Get Call nachholen")
    k = sub.add_parser("konfigpaket"); k.add_argument("--usecase"); k.add_argument("--alle", action="store_true")
    k.add_argument("--url", default="https://backend.kunde.example"); k.add_argument("--out", default="telli-konfig")
    w = sub.add_parser("report"); w.add_argument("--tage", type=int, default=7); w.add_argument("--kunde", default="SonnenWerk Energie")
    w.add_argument("--anteil", default=""); w.add_argument("--ausgabe")
    m = sub.add_parser("demo"); m.add_argument("--db", help="Demo-Daten in diese DuckDB-Datei schreiben statt in den Speicher")
    a = ap.parse_args(argv)
    konfig = Konfig.aus_umgebung()
    {"serve": cmd_serve, "definitions": cmd_definitions, "outbox": cmd_outbox, "push": cmd_push, "demo": cmd_demo,
     "review": cmd_review, "abgleich": cmd_abgleich, "report": cmd_report, "konfigpaket": cmd_konfigpaket}[a.cmd](a, konfig)


if __name__ == "__main__":
    main()
