"""Transkript-Review: Jeden Anruf eines Tages automatisch auf die Fehler prüfen, die im Pilot zählen.

Das Runbook verlangt in der ersten Woche täglich 30 Minuten Transkript-Review. Dieses Werkzeug
sortiert vor: Es liest die Anrufe (lokale calls-Tabelle oder telli List Calls / Get Call), prüft
jedes Transkript gegen feste Regeln und schreibt eine Reviewliste als Markdown, schwerste Befunde
zuerst. Die Regeln sind dieselben Guardrails wie im Agenten (agent.py, guided.py) plus die
Betriebsregeln aus Playbook und telli-Cookbook (Tool-Namen nie aussprechen, KI-Kennzeichnung,
Opt-out sofort respektieren).

Schwere: S1 = sofort pausieren (Eskalationsmatrix), S2 = Betrieb gestört, S3 = Qualität.
Ein Befund ist ein Hinweis zum Lesen, kein Urteil: Die Regeln sind bewusst empfindlich.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import agent
import guided
from integrations.modelle import TelliAnruf

TOOL_NAMEN = ("lookup_lead", "update_lead", "check_slots", "book_appointment", "cancel_appointment", "verify_customer",
              "create_ticket", "end_call")
ZEIT_BEKANNT_NACH = ("check_slots", "lookup_lead", "cancel_appointment")     # danach darf der Agent Uhrzeiten nennen
# Meta-Sprache im Review: enger als guided.META, damit "Ticketsystem" oder "im System eingetragen" nicht anschlagen
META_REVIEW = re.compile(r"\bsatz\b|vorlage|\btext\b|\bprompt\b|extraktion|formulier|\banweisung\b|kunde sagte|"
                         r"systemanweisung|\bsystem-?text\b|als (ki|sprachmodell|modell) (darf|kann|soll) ich", re.IGNORECASE)
KI_KENNZEICHNUNG = re.compile(r"\bKI\b|künstliche intelligenz|digitale[rn]? (assistent|sprachassistent)|sprachassistent|"
                              r"automatisierte[rn]? (anruf|assistent)|virtuelle[rn]? assistent|\bAI\b", re.IGNORECASE)
DUZEN = re.compile(r"\b(du|dich|dir|dein|deine|deinen|deinem|deiner)\b", re.IGNORECASE)
TOOL_IM_TEXT = re.compile(r"\b(" + "|".join(TOOL_NAMEN) + r")\b", re.IGNORECASE)
MONOLOG_WOERTER = 60
SCHWERE = {"S1": 0, "S2": 1, "S3": 2}


@dataclass
class Befund:
    regel: str
    schwere: str
    hinweis: str
    turn: Optional[int] = None
    auszug: str = ""


@dataclass
class Anrufreview:
    quelle: str                       # "db" oder "telli"
    kennung: str                      # calls.id oder call_id
    lead_id: Optional[int]
    ergebnis: Optional[str]
    zeit: Optional[str]
    turns: int
    befunde: list[Befund] = field(default_factory=list)

    @property
    def schwerste(self) -> Optional[str]:
        return min((b.schwere for b in self.befunde), key=SCHWERE.get, default=None)


def _rolle(e: dict) -> str:
    return "assistant" if e.get("role") in ("assistant", "agent") else ("user" if e.get("role") == "user" else "tool")


class Transkriptpruefung:
    def __init__(self, agent_name: str = agent.AGENT_NAME):
        self.agent_name = agent_name

    def pruefen(self, transkript: list[dict], ergebnis: Optional[str] = None, termin_im_system: Optional[bool] = None,
                flags: Optional[list[str]] = None) -> list[Befund]:
        befunde: list[Befund] = []
        agent_turns = [(i, e.get("content") or "") for i, e in enumerate(transkript) if _rolle(e) == "assistant"]
        user_turns = [(i, e.get("content") or "") for i, e in enumerate(transkript) if _rolle(e) == "user"]
        tool_turns = [(i, e.get("content") or "") for i, e in enumerate(transkript) if _rolle(e) == "tool"]
        if not agent_turns:
            return befunde

        # KI-Kennzeichnung in der Begrüßung (EU AI Act, Art. 50)
        erster_i, erster = agent_turns[0]
        if not KI_KENNZEICHNUNG.search(erster):
            befunde.append(Befund("ki_kennzeichnung_fehlt", "S1", "Begrüßung nennt nicht, dass eine KI anruft", erster_i, erster[:160]))

        # Opt-out: Nach einem Opt-out des Kunden darf nur noch verabschiedet werden
        for ui, text in user_turns:
            if agent.is_opt_out(text):
                danach = [(ai, a) for ai, a in agent_turns if ai > ui]
                weiter = [a for _, a in danach if a.strip().endswith("?")] or danach[1:]
                if weiter or (ergebnis and ergebnis not in ("opt_out", "kein_interesse", "falsche_person")):
                    befunde.append(Befund("opt_out_ignoriert", "S1", "Kunde lehnt ab, Agent führt das Gespräch weiter oder Ergebnis ist kein Opt-out",
                                          ui, text[:160]))
                break

        zeit_bekannt_ab = next((ti for ti, t in tool_turns if any(n in t for n in ZEIT_BEKANNT_NACH)), None)
        gesehene_fragen: set[str] = set()
        for ai, text in agent_turns:
            if agent.PRICE_PATTERN.search(text):
                befunde.append(Befund("preis_genannt", "S1", "Preis-, Euro- oder Prozentangabe im Gespräch", ai, text[:160]))
            if agent.BOOKING_CLAIM.search(text) and termin_im_system is False:
                befunde.append(Befund("buchung_behauptet_ohne_buchung", "S1", "Agent spricht von einer Buchung, im System steht keine", ai, text[:160]))
            if agent.TIME_CLAIM.search(text) and tool_turns and (zeit_bekannt_ab is None or ai < zeit_bekannt_ab):
                befunde.append(Befund("termin_genannt_ohne_angebot", "S2", "Uhrzeit genannt, bevor ein Termin-Tool lief", ai, text[:160]))
            if TOOL_IM_TEXT.search(text):
                befunde.append(Befund("tool_name_ausgesprochen", "S2", "Tool-Name im gesprochenen Text (telli-Cookbook: nie aussprechen)", ai, text[:160]))
            if META_REVIEW.search(text):
                befunde.append(Befund("meta_sprache", "S2", "Anweisung oder Systemtext scheint durch", ai, text[:160]))
            if DUZEN.search(text):
                befunde.append(Befund("geduzt", "S3", "Kunde wird geduzt, Leitfaden siezt", ai, text[:160]))
            if len(text.split()) > MONOLOG_WOERTER:
                befunde.append(Befund("monolog", "S3", f"Agentensatz länger als {MONOLOG_WOERTER} Wörter", ai, text[:160]))
            frage = text.strip().lower()
            if frage.endswith("?"):
                if frage in gesehene_fragen:
                    befunde.append(Befund("frage_wiederholt", "S3", "Dieselbe Frage wörtlich zweimal", ai, text[:160]))
                gesehene_fragen.add(frage)

        if ergebnis not in (None, "nicht_erreicht") and not guided.FAREWELL.search(agent_turns[-1][1]):
            befunde.append(Befund("keine_verabschiedung", "S3", "Letzter Agentensatz ohne Verabschiedung", agent_turns[-1][0],
                                  agent_turns[-1][1][:160]))
        for f in flags or []:
            if f in ("termin_behauptet_ohne_buchung", "disqualifiziert_ohne_grund", "buchung_trotz_opt_out"):
                befunde.append(Befund(f, "S1" if f == "buchung_trotz_opt_out" else "S2", "Flag aus der Nachbereitung"))
            elif f in ("ergebnis_fehlt", "ergebnis_korrigiert", "termin_extern", "status_verworfen:qualifiziert"):
                befunde.append(Befund(f, "S2", "Flag aus der Nachbereitung"))
            elif f.startswith("feld_verworfen:"):
                befunde.append(Befund(f, "S3", "Wert aus dem Gespräch hat die Validierung nicht bestanden"))
        befunde.sort(key=lambda b: (SCHWERE[b.schwere], b.turn if b.turn is not None else 999))
        return befunde


def _termin_laut_anruf(ergebnis: Optional[str], flags: list[str]) -> Optional[bool]:
    """Ob in diesem Anruf eine Buchung stattfand: aus Ergebnis und Flags des Anrufs selbst, nicht aus dem
    heutigen Zustand (ein später abgesagter Termin macht die damalige Buchungsbestätigung nicht falsch)."""
    if "termin_behauptet_ohne_buchung" in flags:
        return False
    if ergebnis in ("termin_gebucht", "termin_verschoben"):
        return True
    if ergebnis in ("disqualifiziert", "opt_out", "kein_interesse", "falsche_person", "nicht_erreicht", "rueckruf_vereinbart",
                    "termin_abgesagt", "nicht_verifiziert"):
        return False
    return None


# ---------------------------------------------------------------- Quellen
def aus_db(crm, tage: int = 1, pruefung: Optional[Transkriptpruefung] = None) -> list[Anrufreview]:
    """Anrufe der letzten `tage` aus der calls-Tabelle (lokaler UAT und Nachbereitung schreiben dieselbe Form)."""
    pruefung = pruefung or Transkriptpruefung()
    rows = crm.con.execute(
        "SELECT id, lead_id, start, ergebnis, turns, flags, transkript FROM calls "
        "WHERE ende >= ? OR ende IS NULL ORDER BY id", [crm.jetzt() - timedelta(days=tage)]).fetchall()
    aus = []
    for cid, lead_id, start, ergebnis, turns, flags, transkript in rows:
        flags = json.loads(flags or "[]")
        transkript = json.loads(transkript or "[]")
        befunde = pruefung.pruefen(transkript, ergebnis, _termin_laut_anruf(ergebnis, flags), flags)
        aus.append(Anrufreview("db", str(cid), lead_id, ergebnis, start.isoformat(timespec="minutes") if start else None,
                               turns or 0, befunde))
    return aus


def aus_telli(client, crm, agent_id: Optional[str] = None, limit: int = 100,
              pruefung: Optional[Transkriptpruefung] = None) -> list[Anrufreview]:
    """Anrufe von telli (List Calls), Ergebnis und Flags aus der lokalen Nachbereitung dazu (Flag call:<id>)."""
    pruefung = pruefung or Transkriptpruefung()
    lokal: dict[str, tuple] = {}
    for cid, lead_id, ergebnis, flags in crm.con.execute("SELECT id, lead_id, ergebnis, flags FROM calls").fetchall():
        for f in json.loads(flags or "[]"):
            if f.startswith("call:"):
                lokal[f[5:]] = (lead_id, ergebnis, json.loads(flags or "[]"))
    aus = []
    for roh in client.anrufe_auflisten(agent_id=agent_id, limit=limit):
        call = TelliAnruf.model_validate(roh)
        lead_id, ergebnis, flags = lokal.get(call.call_id or "", (None, None, []))
        if ergebnis is None:
            ergebnis = call.outcome("ergebnis")
            flags = flags + ["nicht_nachbereitet"]
        if call.status in ("not_connected", "voicemail", "failed"):
            ergebnis = "nicht_erreicht"
        termin = _termin_laut_anruf(ergebnis, flags)
        if termin is None and call.gebuchter_termin():
            termin = True
        befunde = pruefung.pruefen(call.transkript(), ergebnis, termin, flags)
        if "nicht_nachbereitet" in flags:
            befunde.append(Befund("nicht_nachbereitet", "S2", "Anruf bei telli, aber kein call_ended in der Nachbereitung"))
        aus.append(Anrufreview("telli", call.call_id or "?", lead_id, ergebnis, call.ended_at_iso, call.kundenturns(), befunde))
    return aus


# ---------------------------------------------------------------- Ausgabe
def reviewliste(reviews: list[Anrufreview], datum: Optional[datetime] = None) -> str:
    datum = datum or datetime.now(timezone.utc)
    zaehler = {"S1": 0, "S2": 0, "S3": 0}
    for r in reviews:
        for b in r.befunde:
            zaehler[b.schwere] += 1
    auffaellig = sorted((r for r in reviews if r.befunde), key=lambda r: (SCHWERE[r.schwerste], r.kennung))
    zeilen = [f"# Transkript-Review {datum:%Y-%m-%d}", "",
              f"Anrufe: {len(reviews)} · auffällig: {len(auffaellig)} · S1: {zaehler['S1']} · S2: {zaehler['S2']} · S3: {zaehler['S3']}", ""]
    if zaehler["S1"]:
        zeilen += ["**S1-Befunde zuerst lesen und nach Eskalationsmatrix handeln.**", ""]
    zeilen += ["| Anruf | Lead | Ergebnis | Turns | Schwerste | Befunde |", "|---|---|---|---|---|---|"]
    for r in auffaellig:
        regeln = ", ".join(dict.fromkeys(b.regel for b in r.befunde))
        zeilen.append(f"| {r.quelle}:{r.kennung} | {r.lead_id or '-'} | {r.ergebnis or '-'} | {r.turns} | {r.schwerste} | {regeln} |")
    if not auffaellig:
        zeilen.append("| – | – | – | – | – | keine Befunde |")
    for r in auffaellig:
        ernst = [b for b in r.befunde if b.schwere in ("S1", "S2") and b.auszug]
        if ernst:
            zeilen += ["", f"## {r.quelle}:{r.kennung} (Lead {r.lead_id or '-'}, {r.ergebnis or '-'})"]
            for b in ernst:
                zeilen.append(f"- **{b.schwere} {b.regel}** (Turn {b.turn}): {b.hinweis}  ")
                zeilen.append(f"  > {b.auszug}")
    ergebnisse: dict[str, int] = {}
    for r in reviews:
        ergebnisse[r.ergebnis or "-"] = ergebnisse.get(r.ergebnis or "-", 0) + 1
    zeilen += ["", "## Ergebnisverteilung", "", "| Ergebnis | Anzahl |", "|---|---|"]
    zeilen += [f"| {k} | {v} |" for k, v in sorted(ergebnisse.items(), key=lambda kv: -kv[1])]
    zeilen += ["", "Review durchgeführt von: ______  · Maßnahmen ins Änderungsprotokoll (Scoping, Abschnitt 10)."]
    return "\n".join(zeilen) + "\n"


def zeitraum_text(tage: int) -> str:
    return f"letzte {tage} Tage" if tage != 1 else "letzte 24 Stunden"
