"""Konfigurationspaket für den No-Code-Builder der Plattform, erzeugt aus der Use-Case-Definition.

    python -m integrations konfigpaket --usecase pv_outbound --url https://backend.kunde.example --out telli-konfig/

Erzeugt je Use Case: prompt.txt (Agent-Prompt in telli-Struktur), outcomes.json (Call Analysis),
contact_properties.json (Form der Create-Contact-Property-API), custom_tools.json (Form, die
`definitions` liefert), calendar.json, webhook.json, contact_lookup.json, testszenarien.md und eine
README mit der Reihenfolge der Einträge. Die Beispiele unter docs/telli-konfig/ sind mit genau diesem
Befehl erzeugt; ein Test stellt sicher, dass sie nicht vom Code wegdriften.
"""
from __future__ import annotations

import json
from pathlib import Path

from integrations.usecases import UseCase

EREIGNISSE = ["call_ended", "call_rescheduled", "auto_dialer_status_changed"]


def _json(daten) -> str:
    return json.dumps(daten, ensure_ascii=False, indent=2) + "\n"


def outcomes_json(u: UseCase) -> list[dict]:
    aus = []
    for o in u.outcomes:
        eintrag = {"key": o.key, "dataType": o.typ, "instructions": o.anweisung}
        if o.optionen:
            eintrag["options"] = o.optionen
        aus.append(eintrag)
    return aus


def properties_json(u: UseCase) -> list[dict]:
    aus = []
    for e in u.eigenschaften:
        eintrag = {"key": e.key, "dataType": e.typ, "label": e.label}
        if e.beschreibung:
            eintrag["description"] = e.beschreibung
        if e.optionen:
            eintrag["options"] = [{"value": o, "label": o} for o in e.optionen]
        aus.append(eintrag)
    return aus


def erzeugen(u: UseCase, basis_url: str = "https://backend.kunde.example") -> dict[str, str]:
    from integrations.server import tool_definitionen
    basis_url = basis_url.rstrip("/")
    tools = tool_definitionen(basis_url, nur=u.tool_namen())["telli_custom_tools"]
    for t in tools:                                              # Tool-Prompt nach Cookbook aus der Use-Case-Definition
        anweisung = next((a for a in u.tools if a.name == t["name"]), None)
        if anweisung:
            t["description"] = (f"{t['description']} WANN: {anweisung.wann} NICHT: {anweisung.wann_nicht} "
                                f"SAGEN: {anweisung.sagen}" + (f" BEISPIEL: {anweisung.beispiel}" if anweisung.beispiel else ""))
    kalender = {"variante": u.kalender}
    if u.kalender == "custom":
        kalender.update({"available_url": f"{basis_url}/calendar/available", "book_url": f"{basis_url}/calendar/book",
                         "signatur": "x-telli-signature (HMAC-SHA256 hex über den rohen Body, Schlüssel = API-Key)"})
    elif u.kalender == "tools":
        kalender["hinweis"] = "Terminbuchung über die Custom Tools check_slots und book_appointment; keine native Kalenderanbindung aktivieren"
    dateien = {
        "prompt.txt": u.prompt(),
        "outcomes.json": _json(outcomes_json(u)),
        "contact_properties.json": _json(properties_json(u)),
        "custom_tools.json": _json(tools),
        "calendar.json": _json(kalender),
        "webhook.json": _json({"url": f"{basis_url}/webhooks/telli", "events": EREIGNISSE,
                               "signatur": "Svix (svix-id, svix-timestamp, svix-signature); Geheimnis in TELLI_WEBHOOK_SECRET",
                               "antwort": "2xx innerhalb von 15 s; Verarbeitung dedupliziert über svix-id und call_id"}),
        "contact_lookup.json": _json({"url": f"{basis_url}/webhooks/contact-lookup", "timeout_s": 2,
                                      "aktiv": True,
                                      "hinweis": "Antwort {\"contact\": null} für Unbekannte; properties werden zu {{...}}-Variablen"}),
        "testszenarien.md": "\n".join(["| Szenario | Erwartung |", "|---|---|"] +
                                      [f"| {t.szenario} | {t.erwartung} |" for t in u.testszenarien]) + "\n",
        "README.md": _readme(u, basis_url),
    }
    return dateien


def _readme(u: UseCase, basis_url: str) -> str:
    z = [f"# telli-Konfiguration: {u.titel}", "",
         f"Kunde: {u.kunde} · Agent: {u.agent_name} · Richtung: {u.richtung} · Backend: `{basis_url}`",
         "", "Erzeugt aus `integrations/usecases/" + u.id + ".py` mit `python -m integrations konfigpaket`. "
             "Nicht von Hand ändern: Änderungen gehören in die Use-Case-Definition, sonst driften Prompt und Backend.",
         "", "## Reihenfolge im Portal", "",
         "| Schritt | Wo | Datei | Prüfung |", "| --- | --- | --- | --- |",
         "| 1 | Settings → Contact properties | `contact_properties.json` | Schlüssel exakt übernehmen; `python -m integrations push --trocken` zeigt, was gesendet wird |",
         "| 2 | Agent anlegen, Stimme und Sprache | `prompt.txt` Abschnitt 1 | KI-Kennzeichnung in der Begrüßung |",
         "| 3 | Agent → Prompt | `prompt.txt` | Variablen `{{...}}` werden im Editor vorgeschlagen |",
         "| 4 | Agent → Tools (je Tool) | `custom_tools.json` | Header `Authorization` als Secret, Body-Felder wie angegeben, Timeout 3 s |",
         "| 5 | Agent → Analysis / Outcomes | `outcomes.json` | Schlüssel stimmen mit `OUTCOME_SCHLUESSEL` der Nachbereitung überein |",
         "| 6 | Integrations → Calendar | `calendar.json` | nur eine Variante aktiv |",
         "| 7 | Webhook configuration | `webhook.json` | Testzustellung aus dem Portal wird mit `verarbeitet` beantwortet |",
         "| 8 | Settings → Integrations → Contact Lookup | `contact_lookup.json` | Testanruf von bekannter Nummer wird mit Namen begrüßt |",
         "| 9 | Testanrufe | `testszenarien.md` | Ergebnis gegen CRM und Kalender (Playbook 03) |",
         "", "## Anrufverhalten", "", "| Einstellung | Wert |", "| --- | --- |"]
    z += [f"| {k} | {v} |" for k, v in u.anrufverhalten.items()]
    if u.verifizierung_pflicht:
        z += ["", "Backend-Regel: Vor `verify_customer` gibt kein Tool Daten heraus (`VERIFIZIERUNG_PFLICHT`)."]
    if u.mindestvorlauf_h:
        z += [f"Backend-Regel: Termine innerhalb von {u.mindestvorlauf_h} Stunden sind nicht änderbar (`MINDESTVORLAUF_H`)."]
    if u.scoping:
        z += ["", f"Scoping: `{u.scoping}`"]
    return "\n".join(z) + "\n"


def schreiben(u: UseCase, basis_url: str, ziel: Path) -> list[Path]:
    ziel.mkdir(parents=True, exist_ok=True)
    pfade = []
    for name, inhalt in erzeugen(u, basis_url).items():
        p = ziel / name
        p.write_text(inhalt, encoding="utf-8")
        pfade.append(p)
    return pfade
