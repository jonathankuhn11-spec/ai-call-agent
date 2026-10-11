"""Abgleich mit der Plattform: Anrufe nachholen, deren call_ended-Webhook nie angekommen ist.

Webhooks sind At-least-once, aber nicht garantiert: Ein Endpunkt war down, ein Deploy lief, eine
Signatur passte nach einer Rotation nicht, jemand hat den Endpunkt im Portal kurz deaktiviert.
telli wiederholt zwar nach Plan, doch nach dem letzten Versuch ist die Nachricht weg. Darum holt
der Abgleich im Takt des Planers die Anrufe der Kontakte über List Calls und gibt jeden beendeten
Anruf, den die Nachbereitung noch nicht kennt, nachträglich in dieselbe Verarbeitung. Die
Deduplizierung über die Call-ID stellt sicher, dass ein später doch eintreffender Webhook nichts
doppelt anlegt.

List Calls liefert die Call-Objekte ohne external_contact_id; die Zuordnung zum Lead kommt aus
der Tabelle telli_kontakte. HTTP läuft ohne Sperre, Datenbankarbeit mit.
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from integrations.modelle import CallEndedEreignis, TelliAnruf, TelliKontaktV1
from integrations.nachbereitung import Nachbereitung
from integrations.telli import TelliClient, TelliFehler


class Abgleich:
    def __init__(self, crm, client: TelliClient, nachbereitung: Nachbereitung, sperre=None,
                 mindestalter_min: int = 60, max_kontakte: int = 50):
        self.crm, self.client, self.nachbereitung = crm, client, nachbereitung
        self.sperre = sperre or nullcontext()
        self.mindestalter_min, self.max_kontakte = mindestalter_min, max_kontakte

    def ausfuehren(self) -> dict[str, Any]:
        bericht: dict[str, Any] = {"kontakte": 0, "nachgeholt": [], "bekannt": 0, "offen": 0, "fehler": []}
        with self.sperre:
            kandidaten = self.crm.abgleich_kandidaten(self.mindestalter_min, max_n=self.max_kontakte)
        for k in kandidaten:
            bericht["kontakte"] += 1
            try:
                anrufe = self.client.anrufe_auflisten(contact_id=k["contact_id"], limit=20)
            except TelliFehler as e:
                bericht["fehler"].append({"lead_id": k["lead_id"], "fehler": str(e)})
                continue
            for roh in anrufe:
                call = TelliAnruf.model_validate(roh)
                if call.state != "ended" or not call.call_id:
                    bericht["offen"] += 1
                    continue
                ereignis = CallEndedEreignis(event="call_ended", call=call,
                                             contact=TelliKontaktV1(contact_id=k["contact_id"], external_contact_id=str(k["lead_id"])))
                with self.sperre:
                    r = self.nachbereitung.verarbeiten(ereignis, message_id=f"abgleich_{call.call_id}")
                if r.get("status") == "duplikat":
                    bericht["bekannt"] += 1
                else:
                    bericht["nachgeholt"].append({"lead_id": k["lead_id"], "call_id": call.call_id, "ergebnis": r.get("ergebnis")})
            with self.sperre:
                self.crm.abgleich_merken(k["lead_id"])
        return bericht


def anruf_nachholen(crm, client: TelliClient, nachbereitung: Nachbereitung, call_id: str, sperre=None) -> dict[str, Any]:
    """Einen bestimmten Anruf über Get Call nachträglich verarbeiten (Runbook: Lücke nach Störung schließen)."""
    sperre = sperre or nullcontext()
    daten = client.anruf(call_id)
    call = TelliAnruf.model_validate(daten.get("call") or daten)
    kontakt = TelliKontaktV1.model_validate(daten.get("contact") or {})
    if not kontakt.external_contact_id and call.contact_id:
        with sperre:
            lead = crm.lead_per_telli_kontakt(call.contact_id)
        if lead:
            kontakt.external_contact_id = str(lead["id"])
    with sperre:
        return nachbereitung.verarbeiten(CallEndedEreignis(event="call_ended", call=call, contact=kontakt),
                                         message_id=f"abgleich_{call.call_id}")
