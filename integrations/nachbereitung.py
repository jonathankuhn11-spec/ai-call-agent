"""Nachbereitung eines Anrufs: call_ended-Ereignis -> CRM, Aufgaben, Sperrliste, Ereignisse.

Dieselbe Regel wie in agent.finish_call: Das Ergebnis eines Gesprächs wird aus dem Zustand des
Backends abgeleitet, nicht aus der Behauptung der Plattform. Meldet der Agent "Termin gebucht",
aber weder unsere Buchung noch der Kalender der Plattform kennen einen Termin aus diesem
Gespräch, wird das nicht übernommen, sondern geflaggt und einem Menschen vorgelegt. Meldet er
"disqualifiziert", ohne dass das CRM einen Disqualifizierungsgrund kennt, ebenso. Zwei Dinge
gelten immer: Ein Opt-out wird nie überstimmt, und gezählt werden nur Buchungen aus diesem
Gespräch, nicht ein Termin aus einem früheren Anruf.

Die Verarbeitung ist eine Transaktion: Entweder stehen Statusänderung, Notiz, Aufgabe,
Sperrvermerk, Protokoll, Ereignisse und der Dedup-Eintrag zusammen in der Datenbank, oder
nichts davon. Eine Wiederholung durch die Plattform nach einem Fehler beginnt dann sauber.

Outcomes und Collected Data sind bei telli frei konfigurierbar; welche Schlüssel der Agent
liefert, legt das Playbook fest (docs/playbook/02-integrations-checkliste.md, Abschnitt
"Outcomes des Agenten"). Die Standardschlüssel stehen in OUTCOME_SCHLUESSEL.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from integrations.crm import CRM, LocalCRM
from integrations.modelle import ERGEBNISSE, CallEndedEreignis, Ereignis, iso_lesen, iso_utc
from integrations.outbox import Outbox

OUTCOME_SCHLUESSEL = {
    "ergebnis": "ergebnis",                      # category: termin_gebucht | disqualifiziert | rueckruf_vereinbart | opt_out | ...
    "rueckruf_zeitpunkt": "rueckruf_zeitpunkt",  # string, ISO 8601 oder Freitext
    "eigentuemer": "eigentuemer",                # boolean
    "gebaeudetyp": "gebaeudetyp",                # category
    "dachflaeche_m2": "dachflaeche_m2",          # number
    "jahresverbrauch_kwh": "jahresverbrauch_kwh",  # number
}
NICHT_ERREICHT = {"not_connected", "voicemail", "failed"}
RUECKRUF_STANDARD_H = 24
GESPRAECHSFENSTER_H = 2      # ohne started_at: Buchungen der letzten zwei Stunden zählen zu diesem Gespräch
UHRENTOLERANZ_S = 60         # Server- und Plattformuhr dürfen so weit auseinanderliegen


def _zeit(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        return iso_lesen(text)
    except ValueError:
        return None


class Nachbereitung:
    def __init__(self, crm: LocalCRM, outbox: Optional[Outbox] = None, spiegel: Optional[CRM] = None,
                 schluessel: Optional[dict[str, str]] = None, jetzt=None):
        self.crm, self.outbox, self.spiegel = crm, outbox, spiegel
        self.k = {**OUTCOME_SCHLUESSEL, **(schluessel or {})}
        self.jetzt = jetzt or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------ Einstieg
    def verarbeiten(self, ereignis: CallEndedEreignis, message_id: str) -> dict[str, Any]:
        call = ereignis.call
        if self.crm.webhook_bekannt(message_id, call.call_id):
            return {"status": "duplikat", "call_id": call.call_id}
        lead = self._lead(ereignis)
        if lead is None:
            with self.crm.transaktion():
                self.crm.webhook_merken(message_id, call.call_id, "call_ended")
            return {"status": "lead_unbekannt", "call_id": call.call_id, "external_id": ereignis.external_id()}

        jetzt = self.jetzt()
        start = _zeit(call.started_at_iso)
        ende = _zeit(call.ended_at_iso) or jetzt
        flags: list[str] = ["plattform:telli", f"attempt:{call.attempt or 1}"]
        if call.started_at_iso and start is None:
            flags.append("zeitstempel_unlesbar")
        with self.crm.transaktion():
            if call.status in NICHT_ERREICHT or (call.status is None and call.state != "ended"):
                ergebnis = "nicht_erreicht"
                flags.append(f"status:{call.status or call.state}")
                if call.follow_up and call.follow_up.scheduled_at:
                    flags.append("follow_up_geplant")
                gebucht = None
            else:
                flags += self._fakten_uebernehmen(lead["id"], call)
                beginn = (start or (ende - timedelta(hours=GESPRAECHSFENSTER_H))) - timedelta(seconds=UHRENTOLERANZ_S)
                ergebnis, mehr, gebucht = self._ergebnis(lead, call, beginn)
                flags += mehr
            wirkung = self._wirkung(lead, call, ergebnis, flags, jetzt, gebucht)
            self.crm.anruf_protokollieren(lead["id"], start, ende, ergebnis, call.kundenturns(), 0, flags, call.transkript())
            self.crm.webhook_merken(message_id, call.call_id, "call_ended")
        return {"status": "verarbeitet", "call_id": call.call_id, "lead_id": lead["id"], "ergebnis": ergebnis,
                "flags": flags, **wirkung}

    # ------------------------------------------------------------ Schritte
    def _lead(self, ereignis: CallEndedEreignis) -> Optional[dict]:
        ext = ereignis.external_id()
        lead = self.crm.lead(ext) if ext else None
        if lead is None and ereignis.telefon():
            lead = self.crm.lead_per_telefon(ereignis.telefon())
        return lead

    def _fakten_uebernehmen(self, lead_id: int, call) -> list[str]:
        """Qualifizierungsfelder aus Outcomes und Collected Data, einzeln durch die Validierung."""
        flags = []
        felder = {}
        for intern in ("eigentuemer", "gebaeudetyp", "dachflaeche_m2", "jahresverbrauch_kwh"):
            wert = call.outcome(self.k[intern])
            if wert is None:
                wert = call.gesammelt(self.k[intern])
            if wert not in (None, ""):
                felder[intern] = wert
        for feld, wert in felder.items():
            r = self.crm.lead_aktualisieren(lead_id, **{feld: wert})
            if not r.get("ok"):
                flags.append(f"feld_verworfen:{feld}")
        return flags

    def _ergebnis(self, lead: dict, call, gespraechsbeginn: datetime) -> tuple[str, list[str], Optional[dict]]:
        """Ground Truth zuerst, Behauptung des Agenten danach, Widersprüche als Flags.
        Rückgabe: (ergebnis, flags, Buchung aus diesem Gespräch oder None)."""
        flags = []
        behauptet = call.outcome(self.k["ergebnis"])
        behauptet = behauptet if behauptet in ERGEBNISSE else None
        if behauptet is None:
            flags.append("ergebnis_fehlt")
        gebucht = self.crm.termin(lead["id"], seit=gespraechsbeginn)            # nur Buchungen aus diesem Gespräch
        if behauptet == "opt_out":                                               # wird nie überstimmt
            if gebucht or call.gebuchter_termin():
                flags.append("buchung_trotz_opt_out")
            return "opt_out", flags, gebucht
        if gebucht:
            if behauptet not in (None, "termin_gebucht"):
                flags.append("ergebnis_korrigiert")
            return "termin_gebucht", flags, gebucht
        if call.gebuchter_termin():                       # Termin über native Kalenderanbindung der Plattform
            flags.append("termin_extern")
            return "termin_gebucht", flags, None
        if behauptet == "termin_gebucht":                 # Behauptung ohne Buchung: nicht glauben
            flags.append("termin_behauptet_ohne_buchung")
            return "rueckruf_vereinbart", flags, None
        aktuell = self.crm.lead(lead["id"]) or lead
        if behauptet == "disqualifiziert":
            if aktuell.get("eigentuemer") is False or aktuell.get("gebaeudetyp") == "mehrfamilienhaus":
                return "disqualifiziert", flags, None
            flags.append("disqualifiziert_ohne_grund")
            return "rueckruf_vereinbart", flags, None
        if behauptet in ("rueckruf_vereinbart", "falsche_person", "kein_interesse"):
            return behauptet, flags, None
        if call.ended_reason == "contact-ended-call" and call.kundenturns() <= 1:
            return "sonstiges", flags + ["frueh_aufgelegt"], None
        return "sonstiges", flags, None

    def _wirkung(self, lead: dict, call, ergebnis: str, flags: list[str], jetzt: datetime,
                 gebucht: Optional[dict] = None) -> dict[str, Any]:
        """Statusänderung, Notiz, Aufgabe, Sperrliste, Spiegelung ins Kunden-CRM, Ereignisse."""
        lid = lead["id"]
        aus: dict[str, Any] = {}
        status_neu: Optional[str] = None
        aufgabe: Optional[tuple[str, datetime, str]] = None        # (titel, faellig, text)
        if ergebnis == "opt_out":
            status_neu = "opt_out"
            self.crm.sperren(lead["telefon"], f"Opt-out im Gespräch {call.call_id}")
            self._ereignis("lead.gesperrt", lid, {"telefon": lead["telefon"], "call_id": call.call_id}, jetzt)
            aus["gesperrt"] = True
            if "buchung_trotz_opt_out" in flags:
                aufgabe = ("Prüfen: buchung_trotz_opt_out", jetzt + timedelta(hours=RUECKRUF_STANDARD_H),
                           f"Anruf {call.call_id}: Termin gebucht, danach Opt-out. Termin bestätigen oder absagen.")
        elif ergebnis == "disqualifiziert":
            status_neu = "disqualifiziert"
        elif ergebnis == "kein_interesse":
            status_neu = "kein_interesse"
        elif ergebnis == "rueckruf_vereinbart":
            status_neu = "rueckruf"
            grund = ", ".join(f for f in flags if f.endswith(("_ohne_buchung", "_ohne_grund", "ergebnis_fehlt"))) or "Rückrufwunsch"
            titel = "Rückruf" if grund == "Rückrufwunsch" else f"Prüfen: {grund}"
            aufgabe = (titel, self._rueckruf_zeitpunkt(call, jetzt), f"Anruf {call.call_id}: {grund}")
        elif ergebnis == "termin_gebucht":
            start = iso_utc(self.crm.start_utc(gebucht["start"])) if gebucht else call.gebuchter_termin()
            aus["termin"] = start
            if (self.crm.lead(lid) or lead).get("status") != "qualifiziert":
                status_neu = "qualifiziert"      # extern gebucht, aber nie qualifiziert: Regel prüft, Flag bei Ablehnung
            self._ereignis("termin.gebucht", lid, {"start": start, "extern": "termin_extern" in flags, "call_id": call.call_id}, jetzt)
        elif ergebnis == "nicht_erreicht" and (call.attempt or 1) >= 3:
            flags.append("mehrfach_nicht_erreicht")

        if status_neu and status_neu != "opt_out" and (self.crm.lead(lid) or lead).get("status") == "opt_out":
            flags.append("status_bleibt_opt_out")            # ein früherer Opt-out wird von keinem Ergebnis überschrieben
            status_neu = None
        if status_neu:
            r = self.crm.lead_aktualisieren(lid, status=status_neu)
            if not r.get("ok"):
                flags.append(f"status_verworfen:{status_neu}")
        if aufgabe:
            titel, faellig, text = aufgabe
            aid = self.crm.aufgabe(lid, titel, faellig, text)
            aus["aufgabe_id"] = aid
            self._ereignis("aufgabe.erstellt", lid, {"aufgabe_id": aid, "titel": titel, "faellig": iso_utc(faellig)}, jetzt)

        notiz = self._notiz(call, ergebnis, flags)
        self.crm.notiz(lid, notiz)
        if self.spiegel is not None:
            aus["spiegel_auftrag"] = self._spiegel_vormerken(lead, ergebnis, status_neu, notiz, aufgabe)
        self._ereignis("anruf.beendet", lid, {"call_id": call.call_id, "ergebnis": ergebnis, "flags": flags,
                                                "status": call.status, "turns": call.kundenturns()}, jetzt)
        return aus

    def _rueckruf_zeitpunkt(self, call, jetzt: datetime) -> datetime:
        wunsch = call.outcome(self.k["rueckruf_zeitpunkt"])
        if isinstance(wunsch, str):
            dt = _zeit(wunsch)
            if dt and dt > jetzt:
                return dt
        if call.follow_up:
            dt = _zeit(call.follow_up.scheduled_at)
            if dt and dt > jetzt:
                return dt
        return jetzt + timedelta(hours=RUECKRUF_STANDARD_H)

    def _notiz(self, call, ergebnis: str, flags: list[str]) -> str:
        zeilen = [f"KI-Anruf {call.call_id or '?'} (Versuch {call.attempt or 1}): {ergebnis}"]
        if call.outcomes:
            zeilen.append("Outcomes: " + ", ".join(f"{o.key}={o.value}" for o in call.outcomes))
        auffaellig = [f for f in flags if not f.startswith(("plattform:", "attempt:"))]
        if auffaellig:
            zeilen.append("Hinweise: " + ", ".join(auffaellig))
        if call.recording_url:   # der Link läuft nach einer Stunde ab und wird nicht gespeichert (Checkliste F)
            zeilen.append("Aufzeichnung vorhanden, abrufbar über die Plattform (Call-ID).")
        return "\n".join(zeilen)

    # ------------------------------------------------------------ Spiegelung ins Kunden-CRM
    def _spiegel_vormerken(self, lead: dict, ergebnis: str, status_neu: Optional[str], notiz: str,
                           aufgabe: Optional[tuple[str, datetime, str]]) -> int:
        """Der Auftrag wird in der Transaktion gespeichert; HTTP zum CRM läuft später außerhalb der Sperre."""
        aktuell = self.crm.lead(lead["id"]) or lead
        felder = {k: aktuell.get(k) for k in ("eigentuemer", "gebaeudetyp", "dachflaeche_m2", "jahresverbrauch_kwh")}
        felder["ergebnis"] = ergebnis
        felder["status"] = status_neu or aktuell.get("status")
        auftrag = {"felder": felder, "notiz": notiz,
                   "aufgabe": {"titel": aufgabe[0], "faellig": iso_utc(aufgabe[1]), "text": aufgabe[2]} if aufgabe else None}
        return self.crm.spiegel_einreihen(lead["id"], lead.get("crm_id") or lead["id"], auftrag)

    def spiegeln(self, sperre=None, max_n: int = 50) -> dict[str, int]:
        """Offene Spiegel-Aufträge ausführen: Datenbank unter der Sperre, HTTP ohne. Rückgabe: Zähler."""
        from contextlib import nullcontext
        from integrations.outbox import RETRY_PLAN_S
        sperre = sperre or nullcontext()
        bericht = {"erledigt": 0, "wiederholen": 0, "tot": 0}
        if self.spiegel is None:
            return bericht
        with sperre:
            auftraege = self.crm.spiegel_faellig(max_n)
        for a in auftraege:
            auftrag, ok, antwort = a["auftrag"], True, "ok"
            erledigt = set(auftrag.get("erledigt", []))          # Schritte, die beim letzten Versuch schon durchkamen
            schritte = [("update", lambda: self.spiegel.lead_aktualisieren(a["ziel_id"], **auftrag["felder"])),
                        ("notiz", lambda: self.spiegel.notiz(a["ziel_id"], auftrag["notiz"]))]
            if auftrag.get("aufgabe"):
                t = auftrag["aufgabe"]
                schritte.append(("aufgabe", lambda: self.spiegel.aufgabe(a["ziel_id"], t["titel"], iso_lesen(t["faellig"]),
                                                                          t.get("text", ""))))
            for name, schritt in schritte:
                if name in erledigt:
                    continue
                try:
                    schritt()
                    erledigt.add(name)
                except Exception as e:  # noqa: BLE001 - jeder Fehler wird gezählt und wiederholt, nie verschluckt
                    ok, antwort = False, f"{name}: {type(e).__name__}: {str(e)[:200]}"
                    break
            versuche = a["versuche"] + 1
            tot = not ok and versuche >= len(RETRY_PLAN_S)
            naechster = None if ok or tot else self.jetzt() + timedelta(seconds=RETRY_PLAN_S[versuche])
            with sperre:
                self.crm.spiegel_abschliessen(a["id"], ok, antwort, versuche, naechster, tot,
                                              auftrag={**auftrag, "erledigt": sorted(erledigt)})
            bericht["erledigt" if ok else ("tot" if tot else "wiederholen")] += 1
        return bericht

    def _ereignis(self, typ: str, lead_id: int, daten: dict[str, Any], jetzt: datetime):
        if self.outbox is None:
            return
        self.outbox.einreihen(Ereignis(id=f"evt_{uuid.uuid4().hex}", typ=typ, zeit=iso_utc(jetzt),
                                       lead_id=lead_id, daten=daten))
