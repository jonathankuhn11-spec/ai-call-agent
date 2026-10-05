"""
UAT mit simulierten Kunden: Ein zweites LLM spielt Kunden-Personas, der Agent führt das Gespräch.
Jede Persona hat ein erwartetes Ergebnis. Am Ende: Pass-Rate, Fehlerarten, KPIs.

Start:  python simulate.py                 -> alle Personas, geführter Modus, je 1 Durchlauf
        python simulate.py --mode free     -> freier Modus: das LLM führt mit Tool-Calling
        python simulate.py --templates     -> geführt, nur Vorlagensätze (schnellster Lauf)
        python simulate.py --runs 3        -> jede Persona 3x (Varianz messen!)
        python simulate.py --persona mieter -v   -> eine Persona mit komplettem Gesprächsverlauf
"""
import argparse
import json
import os
import time

import agent
import guided
import jev

SIM_DB = "eval.duckdb"
CUSTOMER_MODEL = agent.MODEL

PERSONAS = [
    {"id": "happy_path", "erwartet": "termin_gebucht",
     "rolle": "Du bist Eigentümer eines Einfamilienhauses, ca. 70 m² Dach, 4200 kWh Verbrauch. "
              "Du bist interessiert und nimmst den ersten angebotenen Termin."},
    {"id": "mieter", "erwartet": "disqualifiziert",
     "rolle": "Du wohnst zur Miete in einer Wohnung. Du findest Solar spannend und fragst, ob das trotzdem geht."},
    {"id": "mehrfamilienhaus", "erwartet": "disqualifiziert",
     "rolle": "Dir gehört ein Mehrfamilienhaus mit 6 Parteien. Du bist interessiert."},
    {"id": "keine_zeit", "erwartet": "rueckruf_vereinbart",
     "rolle": "Du bist gerade im Auto und hast keine Zeit. Du willst morgen Nachmittag zurückgerufen werden."},
    {"id": "hoefliches_nein", "erwartet": "opt_out",
     "rolle": "Du hast es dir anders überlegt und sagst höflich, dass du doch keine Solaranlage möchtest."},
    {"id": "preisfrager", "erwartet": "termin_gebucht", "kein_preis": True,
     "rolle": "Eigentümer Doppelhaushälfte, 45 m² Dach, 3500 kWh. Du fragst mindestens zweimal hartnäckig, "
              "was die Anlage kostet und wie hoch die Förderung ist. Wenn du keinen Preis bekommst, "
              "nimmst du trotzdem einen Termin."},
    {"id": "falsche_person", "erwartet": "falsche_person",
     "rolle": "Du bist die Ehefrau der angerufenen Person. Dein Mann ist nicht da, du weißt nichts von der Anfrage."},
    {"id": "vage_angaben", "erwartet": "termin_gebucht",
     "rolle": "Eigentümer Reihenhaus. Du weißt weder die Dachgröße noch den Stromverbrauch, nur 'normal halt'. "
              "Du bist aber interessiert und nimmst einen Termin."},
    {"id": "werbung_gesehen", "erwartet": "termin_gebucht",
     "rolle": "Eigentümer Einfamilienhaus, 90 m² Dach, 5000 kWh. Erwähne gleich am Anfang, dass du "
              "'die Werbung auf Instagram gesehen' hast. Du willst einen Termin."},
    {"id": "aggressiv", "erwartet": "opt_out",
     "rolle": "Du bist genervt von Werbeanrufen und beschimpfst den Anrufer grob."},
]


def make_customer(persona: dict):
    system = (f"Du spielst in einem Test einen Kunden am Telefon. {persona['rolle']} "
              "Antworte kurz und natürlich wie am Telefon, maximal 2 Sätze, kein Markdown. "
              "Erfinde keine Details, die deiner Rolle widersprechen. "
              "Wenn das Gespräch beendet ist, sag nur 'Tschüss'.")

    def customer_input(messages):
        # Rollen tauschen: aus Sicht des Kunden ist der Agent der 'user'
        conv = [{"role": "system", "content": system}]
        for m in messages:
            role = m["role"] if isinstance(m, dict) else m.role
            content = m["content"] if isinstance(m, dict) else (m.content or "")
            if role == "assistant" and content:
                conv.append({"role": "user", "content": content})
            elif role == "user" and content and not content.startswith(("(Anruf", "SYSTEM-KORREKTUR")):
                conv.append({"role": "assistant", "content": content})
        if not agent.VERBOSE:
            print(".", end="", flush=True)          # Lebenszeichen pro Turn
        resp = agent.safe_chat(model=CUSTOMER_MODEL, messages=conv, options={"temperature": 0.8})
        if resp is None:
            return "Hallo? Sind Sie noch da?"
        return (resp.message.content or "").strip() or "Hallo?"
    return customer_input


def evaluate(persona: dict, r: dict) -> list[str]:
    """Gibt eine Liste von Fehlern zurück. Leer = bestanden."""
    errors = []
    if r["ergebnis"] != persona["erwartet"]:
        errors.append(f"ergebnis={r['ergebnis']} (erwartet {persona['erwartet']})")
    if persona["erwartet"] == "termin_gebucht" and not r["termin_im_system"]:
        errors.append("kein Termin im System")
    if persona["erwartet"] != "termin_gebucht" and r["termin_im_system"]:
        errors.append("Termin gebucht, obwohl nicht erlaubt")
    if persona.get("kein_preis") and "preis_oder_zahl_genannt" in r["flags"]:
        errors.append("Preis genannt")
    if r["tool_fehler"] > 2:
        errors.append(f"{r['tool_fehler']} Tool-Fehler")
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--persona", help="eine oder mehrere, kommagetrennt: mieter,preisfrager")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--label", help="Name der Iteration, z. B. 'Baseline' oder 'Fix Preisfrage'")
    ap.add_argument("--model", help="Agent-Modell überschreiben, z. B. qwen2.5:14b (Kunde bleibt gleich)")
    ap.add_argument("--no-jev", action="store_true", help="Jev abschalten, nur Regeln (für Vergleich)")
    ap.add_argument("--customer-model", help="Kunden-Modell überschreiben. Gleiches Modell wie Agent = kein Modellwechsel im RAM")
    ap.add_argument("--mode", choices=["guided", "free"], default="guided",
                    help="guided: Leitfaden im Code (Standard); free: das LLM führt mit Tool-Calling")
    ap.add_argument("--templates", action="store_true", help="geführter Modus ohne LLM-Formulierung, nur Vorlagensätze")
    a = ap.parse_args()
    if a.templates:
        guided.FORMULATE = False
    runner = guided.run_guided_call if a.mode == "guided" else agent.run_call
    agent.VERBOSE = a.verbose
    global CUSTOMER_MODEL
    if a.model:
        agent.MODEL = a.model
    if a.customer_model:
        CUSTOMER_MODEL = a.customer_model
    if a.no_jev:
        agent.USE_JEV = False
    print(f"Jev: {'aktiv' if agent.USE_JEV and jev.available() else 'aus (nur Regeln)'}")
    print(f"Modus: {a.mode}{' (nur Vorlagen)' if a.templates else ''} | Agent-Modell: {agent.MODEL} | Kunden-Modell: {CUSTOMER_MODEL}")

    wanted = set(a.persona.split(",")) if a.persona else None
    personas = [p for p in PERSONAS if not wanted or p["id"] in wanted]
    results = []
    t_start = time.time()
    total, done = len(personas) * a.runs, 0
    print(f"Starte UAT: {total} Gespräche. Jeder Punkt = ein Kundenturn.", flush=True)
    print("Hinweis: Nicht ins Fenster klicken, sonst friert Windows die Ausgabe ein (dann Esc drücken).\n", flush=True)
    for p in personas:
        for run in range(a.runs):
            print(f"[{done + 1}/{total}] {p['id']:<18} ", end="", flush=True)
            if os.path.exists(SIM_DB):
                os.remove(SIM_DB)
            con = agent.init_db(reset=True, path=SIM_DB)
            try:
                r = runner(con, 1, get_input=make_customer(p))
            except Exception as e:           # technischer Fehler: protokollieren, weitermachen
                r = {"ergebnis": "technischer_fehler", "turns": 0, "tool_fehler": 0,
                     "flags": [f"exception: {type(e).__name__}: {str(e)[:100]}"],
                     "termin_im_system": False, "latenz_avg": 0.0, "jev_aktiv": False, "transkript": []}
            con.close()
            errs = evaluate(p, r)
            results.append({"persona": p["id"], "run": run + 1, "bestanden": not errs, "fehler": errs,
                            **{k: r[k] for k in ("ergebnis", "turns", "tool_fehler", "flags", "latenz_avg")},
                            "transkript": r["transkript"]})
            done += 1
            mark = "PASS" if not errs else "FAIL"
            elapsed = time.time() - t_start
            eta = elapsed / done * (total - done)
            print(f" {mark}  ergebnis={r['ergebnis']:<20} turns={r['turns']:<3} "
                  f"| noch ca. {eta / 60:.0f} min  {'; '.join(errs)}")

    n = len(results)
    passed = sum(r["bestanden"] for r in results)
    halluz = sum(r["flags"].count("halluzination_blockiert") for r in results)
    print("\n=== UAT-Report ===")
    print(f"Gespräche: {n} | Bestanden: {passed}/{n} ({passed / n:.0%}) | Dauer: {time.time() - t_start:.0f}s")
    if jev.STATS["calls"] or jev.STATS["fehler"]:
        lat = jev.STATS["latenzen"]
        print(f"Jev: {jev.STATS['calls']} Aufrufe | Ø {sum(lat) / max(len(lat), 1) * 1000:.0f} ms | "
              f"{jev.STATS['fehler']} Fehler mit Fallback")
    print(f"Ø Turns: {sum(r['turns'] for r in results) / n:.1f} | "
          f"Ø Latenz: {sum(r['latenz_avg'] for r in results) / n:.1f}s | "
          f"Tool-Fehler: {sum(r['tool_fehler'] for r in results)} | Halluzinationen blockiert: {halluz}")
    fails = [r for r in results if not r["bestanden"]]
    if fails:
        print("\nFehlgeschlagen (Transkripte in eval_results.json):")
        for r in fails:
            print(f"  - {r['persona']} run {r['run']}: {'; '.join(r['fehler'])}")
    history = []
    if os.path.exists("eval_history.json"):
        with open("eval_history.json", encoding="utf-8") as f:
            history = json.load(f)
    history.append({"zeit": time.strftime("%Y-%m-%d %H:%M"), "label": a.label or f"Lauf {len(history) + 1}",
                    "modus": a.mode, "formulierung": guided.FORMULATE if a.mode == "guided" else None,
                    "modell": agent.MODEL, "jev": agent.USE_JEV,
                    "gespraeche": n, "pass_rate": passed / n, "halluzinationen": halluz,
                    "tool_fehler": sum(r["tool_fehler"] for r in results),
                    "latenz": sum(r["latenz_avg"] for r in results) / n})
    with open("eval_history.json", "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    with open("eval_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
