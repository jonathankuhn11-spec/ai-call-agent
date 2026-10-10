"""Kommandozeile der Integrationsschicht.

    python -m integrations serve              HTTP-Server (uvicorn) für die Plattform
    python -m integrations demo               Pilotgespräche im Prozess, ohne Account, ohne Netz
    python -m integrations definitions        Tool-Definitionen für die telli-Konfiguration (JSON)
    python -m integrations outbox             Zustand der ausgehenden Webhooks, --zustellen, --erneut ID
    python -m integrations push --trocken     Lead-Push ins Dialer: zeigt die Requests, ohne zu senden
"""
from __future__ import annotations

import argparse
import json
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
    print(json.dumps(tool_definitionen(a.url), ensure_ascii=False, indent=2))


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
        client = TelliClient(konfig.telli_api_key, konfig.telli_api_url)
    bericht = LeadPush(crm, client, konfig.telli_agent_id or "agent_demo").synchronisieren()
    if a.trocken:
        con.execute("ROLLBACK")
        print("Trockenlauf, nichts gesendet, nichts gespeichert. Requests, die gesendet würden:\n")
        for methode, pfad, body in gesendet:
            print(f"{methode} https://api.telli.com{pfad}\n{json.dumps(body, ensure_ascii=False, indent=2)}\n")
    print("Bericht:", json.dumps(bericht, ensure_ascii=False))


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
    con = agent.init_db(reset=True, path=":memory:")
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
            flags = [f for f in ce.get("flags", []) if not f.startswith(("plattform:", "attempt:"))]
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
    print(f"\nStand: {iso_utc(datetime.now(timezone.utc))}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m integrations", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="0.0.0.0"); s.add_argument("--port", type=int, default=8000)
    d = sub.add_parser("definitions"); d.add_argument("--url", default="https://backend.kunde.example")
    o = sub.add_parser("outbox"); o.add_argument("--zustellen", action="store_true"); o.add_argument("--erneut", type=int)
    p = sub.add_parser("push"); p.add_argument("--trocken", action="store_true")
    sub.add_parser("demo")
    a = ap.parse_args(argv)
    konfig = Konfig.aus_umgebung()
    {"serve": cmd_serve, "definitions": cmd_definitions, "outbox": cmd_outbox, "push": cmd_push, "demo": cmd_demo}[a.cmd](a, konfig)


if __name__ == "__main__":
    main()
