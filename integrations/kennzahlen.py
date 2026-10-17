"""Pilot-Kennzahlen aus der Integrationsdatenbank: Definitionen aus docs/playbook/05-kpis-und-reporting.md.

Reine Funktionen auf der Datenbank, ohne Streamlit, damit Dashboard, Wochenreport und Tests
dieselben Zahlen sehen. Jede Quote hat Zähler und Nenner im Ergebnis, damit niemand eine
Prozentzahl ohne Grundgesamtheit liest. Ground Truth sind CRM, Kalender und Protokoll, nie das,
was der Agent behauptet hat.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from typing import Any, Optional

KORREKTUR_FLAGS = ("ergebnis_korrigiert", "termin_behauptet_ohne_buchung", "disqualifiziert_ohne_grund",
                   "buchung_trotz_opt_out", "status_verworfen:", "feld_verworfen:", "termin_verschoben_behauptet_ohne_beleg",
                   "termin_abgesagt_behauptet_ohne_beleg", "ticket_erstellt_behauptet_ohne_beleg")
ZIELE = {                       # aus SCOPING.md, Abschnitt 2; "<" heißt Ziel unterschreiten, ">" überschreiten
    "speed_to_lead_min": ("<", 5), "erreichbarkeit": (">", 0.60), "qualifizierungsquote": (">", 0.80),
    "terminquote": (">", 0.35), "fehlerquote": ("<", 0.05), "opt_out_rate": ("<", 0.10), "zustellquote": (">=", 1.0),
    "outbox_tot": ("==", 0),
}


def _quote(zaehler: int, nenner: int) -> Optional[float]:
    return round(zaehler / nenner, 4) if nenner else None


def kennzahlen(crm, tage: int = 7, jetzt: Optional[datetime] = None) -> dict[str, Any]:
    """Alle KPIs für die letzten `tage` (Anrufe nach Endzeitpunkt). Zeiten sind Wanduhrzeit der DB."""
    con = crm.con
    jetzt = jetzt or crm.jetzt()
    von = jetzt - timedelta(days=tage)
    rows = con.execute("SELECT id, lead_id, start, ende, ergebnis, turns, flags FROM calls "
                       "WHERE COALESCE(ende, start) >= ? ORDER BY id", [von]).fetchall()
    anrufe = [{"id": r[0], "lead_id": r[1], "start": r[2], "ende": r[3], "ergebnis": r[4] or "-", "turns": r[5] or 0,
               "flags": json.loads(r[6] or "[]")} for r in rows]
    erreicht = [a for a in anrufe if a["ergebnis"] != "nicht_erreicht"]
    leads_angerufen = {a["lead_id"] for a in anrufe if a["lead_id"]}
    leads_erreicht = {a["lead_id"] for a in erreicht if a["lead_id"]}

    qualifiziert_bekannt = 0                       # Qualifizierungsfrage beantwortet: Mieter (nein) oder Eigentümer mit Gebäudetyp
    for lid in leads_erreicht:
        lead = crm.lead(lid) or {}
        if lead.get("eigentuemer") is False or (lead.get("eigentuemer") is True and lead.get("gebaeudetyp")):
            qualifiziert_bekannt += 1
    # Terminquote im Zeitfenster: Buchungen ab `von`, Nenner sind die erreichten Leads, die qualifiziert sind oder gebucht haben
    leads_qualifiziert = {r[0] for r in con.execute("SELECT id FROM leads WHERE status = 'qualifiziert'").fetchall()} & leads_erreicht
    mit_termin = {r[0] for r in con.execute("SELECT DISTINCT lead_id FROM appointments WHERE gebucht_am >= ?", [von]).fetchall()}
    nenner_termin = leads_qualifiziert | mit_termin

    fehler = [a for a in erreicht if any(f.startswith(KORREKTUR_FLAGS) for f in a["flags"])]
    opt_outs = [a for a in erreicht if a["ergebnis"] == "opt_out"]

    # Speed-to-Lead: Eingang des Leads -> erster Anrufversuch (Leads mit Eingangszeit, deren erster Versuch im Fenster liegt).
    # calls.start ist der Gesprächsbeginn; der Wählzeitpunkt (triggered_at) liegt Sekunden davor und wird nicht gespeichert.
    dauern = []
    for _lid, erstellt, erster in con.execute(
            "SELECT l.id, l.erstellt_am, MIN(c.start) FROM leads l JOIN calls c ON c.lead_id = l.id "
            "WHERE l.erstellt_am IS NOT NULL AND c.start IS NOT NULL GROUP BY 1, 2 HAVING MIN(c.start) >= ?", [von]).fetchall():
        if erster >= erstellt:
            dauern.append((erster - erstellt).total_seconds() / 60)

    ergebnisse: dict[str, int] = {}
    flags: dict[str, int] = {}
    je_tag: dict[str, int] = {}
    for a in anrufe:
        ergebnisse[a["ergebnis"]] = ergebnisse.get(a["ergebnis"], 0) + 1
        for f in a["flags"]:
            if not f.startswith(("plattform:", "attempt:", "call:", "status:")):
                flags[f] = flags.get(f, 0) + 1
        tag = (a["ende"] or a["start"] or jetzt).strftime("%Y-%m-%d")
        je_tag[tag] = je_tag.get(tag, 0) + 1

    outbox = {r[0]: r[1] for r in con.execute(
        "SELECT status, COUNT(*) FROM outbox GROUP BY 1").fetchall()} if _tabelle(con, "outbox") else {}
    zugestellt, tot = outbox.get("zugestellt", 0), outbox.get("tot", 0)
    erinnerungen = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM erinnerungen GROUP BY 1").fetchall()}
    stornierungen = con.execute("SELECT COUNT(*) FROM stornierungen WHERE storniert_am >= ?", [von]).fetchone()[0]
    aufgaben_offen = con.execute("SELECT COUNT(*) FROM aufgaben WHERE status = 'offen'").fetchone()[0]
    gesperrt = con.execute("SELECT COUNT(*) FROM sperrliste").fetchone()[0]

    return {
        "zeitraum": {"von": von.isoformat(timespec="minutes"), "bis": jetzt.isoformat(timespec="minutes"), "tage": tage},
        "anrufe": len(anrufe), "anrufe_erreicht": len(erreicht), "leads_angerufen": len(leads_angerufen),
        "leads_erreicht": len(leads_erreicht),
        "speed_to_lead_min": round(statistics.median(dauern), 1) if dauern else None, "speed_to_lead_n": len(dauern),
        "erreichbarkeit": _quote(len(leads_erreicht), len(leads_angerufen)),
        "qualifizierungsquote": _quote(qualifiziert_bekannt, len(leads_erreicht)),
        "terminquote": _quote(len(mit_termin & nenner_termin), len(nenner_termin)), "termine": len(mit_termin),
        "fehlerquote": _quote(len(fehler), len(erreicht)), "fehler_anrufe": len(fehler),
        "opt_out_rate": _quote(len(opt_outs), len(erreicht)), "opt_outs": len(opt_outs),
        "zustellquote": _quote(zugestellt, zugestellt + outbox.get("offen", 0) + tot) if outbox else None,
        "outbox": outbox, "outbox_tot": tot,
        "erinnerungen": erinnerungen, "stornierungen": stornierungen, "aufgaben_offen": aufgaben_offen,
        "gesperrt": gesperrt, "turns_mittel": round(statistics.mean(a["turns"] for a in erreicht), 1) if erreicht else None,
        "ergebnisse": dict(sorted(ergebnisse.items(), key=lambda kv: -kv[1])),
        "flags": dict(sorted(flags.items(), key=lambda kv: -kv[1])),
        "anrufe_je_tag": dict(sorted(je_tag.items())),
    }


def _tabelle(con, name: str) -> bool:
    return con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0] > 0


def status(k: dict[str, Any], name: str) -> str:
    """✓ Ziel erreicht, ✗ verfehlt, – keine Daten."""
    wert = k.get(name)
    if wert is None or name not in ZIELE:
        return "–"
    op, ziel = ZIELE[name]
    ok = {"<": wert < ziel, ">": wert > ziel, ">=": wert >= ziel, "==": wert == ziel}[op]
    return "✓" if ok else "✗"


def _fmt(wert, art: str) -> str:
    if wert is None:
        return "n/a"
    if art == "%":
        return f"{wert:.0%}"
    if art == "min":
        return f"{wert:.1f} min"
    return str(wert)


def wochenreport(k: dict[str, Any], kunde: str = "SonnenWerk Energie", anteil: str = "", gelernt: Optional[list[str]] = None,
                 aenderungen: Optional[list[str]] = None) -> str:
    """Markdown nach der Vorlage in docs/playbook/05-kpis-und-reporting.md, Zahlen aus kennzahlen()."""
    z = k["zeitraum"]
    zeilen = [f"# Wochenreport Voice-Agent, {kunde}", "",
              f"Zeitraum: {z['von']} bis {z['bis']} ({z['tage']} Tage)" + (f" · Anteil der Leads im Agenten: {anteil}" if anteil else ""),
              "", "## Zahlen", "", "| KPI | Wert | Ziel | Status | Grundgesamtheit |", "|---|---|---|---|---|"]
    zeilen += [
        f"| Speed-to-Lead (Median) | {_fmt(k['speed_to_lead_min'], 'min')} | < 5 min | {status(k, 'speed_to_lead_min')} | {k['speed_to_lead_n']} Leads mit Eingangszeit |",
        f"| Erreichbarkeit | {_fmt(k['erreichbarkeit'], '%')} | > 60 % | {status(k, 'erreichbarkeit')} | {k['leads_erreicht']} von {k['leads_angerufen']} Leads |",
        f"| Qualifizierungsquote | {_fmt(k['qualifizierungsquote'], '%')} | > 80 % | {status(k, 'qualifizierungsquote')} | erreichte Leads mit beantworteter Qualifizierung (Mieter oder Eigentümer mit Gebäudetyp) |",
        f"| Terminquote | {_fmt(k['terminquote'], '%')} | > 35 % | {status(k, 'terminquote')} | {k['termine']} Termine bei qualifizierten Leads |",
        f"| Fehlerquote | {_fmt(k['fehlerquote'], '%')} | < 5 % | {status(k, 'fehlerquote')} | {k['fehler_anrufe']} von {k['anrufe_erreicht']} erreichten Anrufen mit Korrektur-Flag |",
        f"| Opt-out-Rate | {_fmt(k['opt_out_rate'], '%')} | < 10 % | {status(k, 'opt_out_rate')} | {k['opt_outs']} von {k['anrufe_erreicht']} |",
        f"| Zustellquote Outbox | {_fmt(k['zustellquote'], '%')} | 100 % | {status(k, 'zustellquote')} | {k['outbox']} |",
        f"| Outbox tot | {k['outbox_tot']} | 0 | {status(k, 'outbox_tot')} | |",
        "| Übergabequalität (Stichprobe 20) | manuell | > 90 % | – | Review-Liste |",
        "", f"Anrufe gesamt: {k['anrufe']} · erreicht: {k['anrufe_erreicht']} · Ø Turns: {k['turns_mittel'] or 'n/a'} · "
            f"offene Rückruf-Aufgaben: {k['aufgaben_offen']} · gesperrte Nummern: {k['gesperrt']} · "
            f"Erinnerungen: {k['erinnerungen'] or {}} · Absagen: {k['stornierungen']}",
        "", "## Ergebnisverteilung", "", "| Ergebnis | Anzahl | Anteil |", "|---|---|---|"]
    for name, n in k["ergebnisse"].items():
        zeilen.append(f"| {name} | {n} | {n / k['anrufe']:.0%} |")
    if not k["ergebnisse"]:
        zeilen.append("| – | 0 | – |")
    zeilen += ["", "## Flags (Hinweise aus der Nachbereitung)", "", "| Flag | Anzahl |", "|---|---|"]
    zeilen += [f"| {f} | {n} |" for f, n in k["flags"].items()] or ["| keine | 0 |"]
    zeilen += ["", "## Anrufe je Tag", "", "| Tag | Anrufe |", "|---|---|"]
    zeilen += [f"| {t} | {n} |" for t, n in k["anrufe_je_tag"].items()] or ["| – | 0 |"]
    zeilen += ["", "## Was wir gelernt haben (aus den Transkripten)", ""]
    zeilen += [f"- {g}" for g in (gelernt or ["<Beobachtung → Maßnahme → Status>"])]
    zeilen += ["", "## Änderungen am Agenten diese Woche", "", "| Datum | Änderung | Grund | Wirkung sichtbar ab |", "|---|---|---|---|"]
    zeilen += [f"| {a} |" for a in (aenderungen or [])] or ["| – | – | – | – |"]
    zeilen += ["", "## Entscheidungen, die wir vom Kunden brauchen", "", "- <Frage, Optionen, Empfehlung, bis wann>", ""]
    return "\n".join(zeilen)
