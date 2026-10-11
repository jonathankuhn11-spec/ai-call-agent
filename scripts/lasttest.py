"""Lasttest der Integrationsschicht: gleichzeitige Tool-Aufrufe wie bei parallelen Gesprächen, p50/p95/max.

    python scripts/lasttest.py                       # App im Prozess (uvicorn-Thread), 20 parallele Gespräche à 6 Aufrufe
    python scripts/lasttest.py --url https://... --secret TOOL_SECRET   # gegen einen laufenden Server (Staging; braucht Leads 6..N
                                                                        # und freie Slots, bucht wirklich: nur gegen Testdaten)
    python scripts/lasttest.py --gespraeche 50 --schwelle-ms 300        # Schwelle für p95; Exit-Code 1 bei Verstoß

Jedes simulierte Gespräch macht, was die Plattform macht: lookup_lead, update_lead (dreimal),
check_slots, book_appointment. Dazu läuft im Hintergrund die Outbox-Zustellung an ein langsames
Kundensystem (im Prozess), damit die Messung den Fall "Zustellung blockiert nichts" einschließt.
Die Schwelle begründet sich aus dem telli-Timeout je Tool (1 bis 10 s): Bei p95 unter 300 ms bleibt
auch bei Netzlatenz zur Plattform reichlich Luft.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _im_prozess(port: int, tool_secret: str):
    """Startet die App mit uvicorn in einem Thread; Outbox gegen ein langsames Attrappen-Kundensystem."""
    import uvicorn

    import agent
    from integrations.konfig import Konfig
    from integrations.outbox import Outbox
    from integrations.server import erstelle_app

    def langsamer_kunde(request: httpx.Request) -> httpx.Response:
        time.sleep(0.5)
        return httpx.Response(200)

    agent.VERBOSE = False
    con = agent.init_db(reset=True, path=":memory:")
    # Viele Leads und Slots, damit gleichzeitige Gespräche nicht um dieselben Datensätze konkurrieren
    con.execute("INSERT INTO leads (id, name, telefon, plz, quelle) SELECT i, 'Lead ' || i, '+49 251 1' || lpad(i::VARCHAR, 5, '0'), "
                "'48149', 'Lasttest' FROM range(6, 1006) t(i)")
    con.execute("INSERT INTO slots (id, start, lead_id) SELECT 1000 + i, now()::TIMESTAMP + INTERVAL 1 DAY + INTERVAL (i) MINUTE, NULL FROM range(0, 3000) t(i)")
    konfig = Konfig(tool_secret=tool_secret, takt_s=1, push_takt_s=10 ** 6)
    outbox = Outbox(con, "https://kunde.example/webhooks", "", client=httpx.Client(transport=httpx.MockTransport(langsamer_kunde)))
    app = erstelle_app(konfig, con=con, outbox=outbox)
    # Outbox mit Arbeit füllen, bevor der Planer läuft: Der Planer stellt dann während der Messung an das 500-ms-Kundensystem zu
    from datetime import datetime, timezone

    from integrations.modelle import Ereignis, iso_utc
    for i in range(40):
        outbox.einreihen(Ereignis(id=f"evt_last_{i}", typ="anruf.beendet", zeit=iso_utc(datetime.now(timezone.utc)), lead_id=1, daten={"i": i}))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    return server, app


def gespraech(client: httpx.Client, basis: str, kopf: dict, lead_id: int, zeiten: list[tuple[str, float]], fehler: list[str]):
    call_id = f"last_{lead_id}"

    def tool(name, **args):
        t0 = time.perf_counter()
        r = client.post(f"{basis}/tools/{name}", json={"call_id": call_id, "external_id": str(lead_id), **args}, headers=kopf, timeout=10)
        zeiten.append((name, (time.perf_counter() - t0) * 1000))
        if r.status_code != 200:
            fehler.append(f"{name}: HTTP {r.status_code}")
            return {}
        return r.json()

    tool("lookup_lead")
    tool("update_lead", status="in_qualifizierung", eigentuemer=True)
    tool("update_lead", status="in_qualifizierung", gebaeudetyp="einfamilienhaus", dachflaeche_m2=60)
    tool("update_lead", status="qualifiziert")
    slots = tool("check_slots").get("freie_termine") or []
    if slots:
        r = tool("book_appointment", slot_id=slots[0]["slot_id"])
        if not r.get("ok"):
            # Unter Last kann ein Slot gerade vergeben worden sein; das ist das erwartete, korrekte Verhalten
            if "vergeben" not in r.get("fehler", ""):
                fehler.append(f"book_appointment: {r}")
    else:
        fehler.append("check_slots: keine Slots")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="laufender Server; ohne: App im Prozess")
    ap.add_argument("--secret", default="lasttest-secret")
    ap.add_argument("--gespraeche", type=int, default=20)
    ap.add_argument("--parallel", type=int, default=10)
    ap.add_argument("--schwelle-ms", type=float, default=300)
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--json", action="store_true", help="nur das Ergebnis als JSON ausgeben")
    a = ap.parse_args()

    server = None
    basis = a.url.rstrip("/") if a.url else f"http://127.0.0.1:{a.port}"
    if not a.url:
        server, app = _im_prozess(a.port, a.secret)
    kopf = {"Authorization": f"Bearer {a.secret}"}
    zeiten: list[tuple[str, float]] = []
    fehler: list[str] = []
    t_start = time.perf_counter()
    with httpx.Client() as client:
        semaphore = threading.Semaphore(a.parallel)

        def arbeit(lead_id):
            with semaphore:
                gespraech(client, basis, kopf, lead_id, zeiten, fehler)

        threads = [threading.Thread(target=arbeit, args=(6 + i,)) for i in range(a.gespraeche)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    dauer = time.perf_counter() - t_start
    if server is not None:
        server.should_exit = True

    ms = sorted(z for _, z in zeiten)
    if not ms:
        print(json.dumps({"bestanden": False, "fehler": fehler[:10] or ["keine Messwerte"]}, ensure_ascii=False))
        return 1
    je_tool: dict[str, list[float]] = {}
    for name, z in zeiten:
        je_tool.setdefault(name, []).append(z)
    ergebnis = {
        "gespraeche": a.gespraeche, "parallel": a.parallel, "aufrufe": len(ms), "dauer_s": round(dauer, 2),
        "aufrufe_pro_s": round(len(ms) / dauer, 1) if dauer else None,
        "p50_ms": round(statistics.median(ms), 1), "p95_ms": round(ms[max(0, int(len(ms) * 0.95) - 1)], 1), "max_ms": round(ms[-1], 1),
        "je_tool_p95_ms": {n: round(sorted(v)[max(0, int(len(v) * 0.95) - 1)], 1) for n, v in je_tool.items()},
        "fehler": fehler[:10], "fehler_anzahl": len(fehler), "schwelle_ms": a.schwelle_ms,
    }
    ergebnis["bestanden"] = ergebnis["p95_ms"] <= a.schwelle_ms and not fehler
    if a.json:
        print(json.dumps(ergebnis, ensure_ascii=False))
    else:
        print(f"Lasttest: {a.gespraeche} Gespräche, {a.parallel} parallel, {len(ms)} Tool-Aufrufe in {dauer:.1f} s "
              f"({ergebnis['aufrufe_pro_s']} Aufrufe/s)")
        print(f"  p50 {ergebnis['p50_ms']} ms · p95 {ergebnis['p95_ms']} ms · max {ergebnis['max_ms']} ms · Schwelle p95 {a.schwelle_ms} ms")
        for n, p in ergebnis["je_tool_p95_ms"].items():
            print(f"  {n:<18} p95 {p} ms")
        print(f"  Fehler: {len(fehler)}" + (f" ({'; '.join(fehler[:3])})" if fehler else ""))
        print("  BESTANDEN" if ergebnis["bestanden"] else "  NICHT BESTANDEN")
    return 0 if ergebnis["bestanden"] else 1


if __name__ == "__main__":
    sys.exit(main())
