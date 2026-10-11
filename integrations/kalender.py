"""Kalenderdienst hinter /calendar/available, /calendar/book und check_slots/book_appointment.

Ohne Kalenderdienst sind die freien Zeiten eine Tabelle (slots), gut für Pilot und Demo. Beim
Kunden liegen sie in einem Kalender: Cal.com (offen, selbst betreibbar, API v2), Google Calendar,
Outlook. Dieses Modul hängt einen solchen Dienst an, ohne dass sich an den Geschäftsregeln etwas
ändert. Das Muster heißt Kalender-Spiegel:

1. Verfügbarkeit wird gespiegelt, nicht live abgefragt. Der Planer holt im Takt die freien
   Zeiten der nächsten Tage und gleicht die Slot-Tabelle ab (LocalCRM.slots_spiegeln). Ein
   Tool-Aufruf der Plattform antwortet damit aus der Datenbank in Millisekunden, und der
   Kalender darf kurz ausfallen, ohne dass ein Gespräch hängt.
2. Buchungen werden bestätigt, bevor der Agent sie zusagt. book_appointment merkt die Buchung
   lokal vor (Status offen), der Server bucht sofort danach im Kalender, ohne Datenbanksperre, und
   trägt die Bestätigung ein. Lehnt der Kalender ab (Zeit inzwischen vergeben), wird die lokale
   Vormerkung verworfen und der Agent bietet einen anderen Termin an; bei einer Umbuchung bleibt
   dann der bisherige Termin bestehen. Ist der Kalender nicht erreichbar, bleibt die Vormerkung,
   der Planer versucht es nach demselben Plan wie die Outbox erneut, und nach dem letzten
   Versuch bekommt ein Mensch eine Aufgabe.
3. Absagen laufen hinterher. Eine Absage gilt sofort lokal; die Absage im Kalender folgt als
   Auftrag mit Wiederholung.

Cal.com API v2 (cal.com/docs/api-reference/v2):
  GET  /v2/slots?start&end&eventTypeId&timeZone&format=range     cal-api-version: 2024-09-04
  POST /v2/bookings  {start, eventTypeId, attendee{name,timeZone,phoneNumber,language}, metadata}
                                                                  cal-api-version: 2026-02-25, Antwort 201 mit data.uid
  POST /v2/bookings/{uid}/cancel  {cancellationReason}            cal-api-version: 2026-02-25
Auth: Authorization: Bearer cal_... . Der Event-Typ muss die E-Mail des Teilnehmers optional machen,
weil Leads aus dem Dialer nur mit Telefonnummer kommen (Checkliste, Abschnitt D).
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

import httpx

from integrations import logbuch
from integrations.modelle import iso_lesen, iso_utc
from integrations.outbox import RETRY_PLAN_S
from integrations.uhr import Uhr

KALENDER_TAGE = 14            # so weit voraus werden freie Zeiten gespiegelt
KALENDER_SPIEGEL_TAKT_S = 300  # Verfügbarkeit alle fünf Minuten; dazwischen entscheidet die Buchungsbestätigung


class KalenderFehler(Exception):
    """`endgueltig`: der Kalender hat die Anfrage abgelehnt (Zeit vergeben, ungültig); sonst vorübergehend (Netz, 5xx)."""

    def __init__(self, nachricht: str, status: int = 0, endgueltig: bool = False):
        super().__init__(nachricht)
        self.status, self.endgueltig = status, endgueltig


class Kalender(Protocol):
    """Was das Backend von einem Kalenderdienst braucht. Zeiten in UTC, zeitzonenbewusst.
    `timeout_s` begrenzt eine einzelne Anfrage (der Server bestätigt eine Buchung im Tool-Aufruf der Plattform)."""

    def freie_zeiten(self, von: datetime, bis: datetime) -> list[tuple[datetime, datetime]]: ...
    def buchen(self, start: datetime, lead: dict, art: str, call_id: Optional[str] = None,
               timeout_s: Optional[float] = None) -> str: ...
    def stornieren(self, extern_id: str, grund: str, timeout_s: Optional[float] = None) -> None: ...


def _json(r: httpx.Response) -> dict:
    try:
        daten = r.json()
    except ValueError:
        return {}
    return daten if isinstance(daten, dict) else {}


def _fehlertext(daten: dict, r: httpx.Response) -> str:
    fehler = daten.get("error")
    if isinstance(fehler, dict):
        return str(fehler.get("message") or fehler.get("code") or f"HTTP {r.status_code}")
    return str(daten.get("message") or fehler or f"HTTP {r.status_code}")


class CalComKalender:
    """Cal.com API v2. Ein Event-Typ (z. B. "Beratungstermin, 60 Minuten") ist der Kalender des Deployments."""

    SLOTS_VERSION = "2024-09-04"
    BOOKINGS_VERSION = "2026-02-25"
    ABGELEHNT = (400, 409, 422)        # die Anfrage selbst wird nicht angenommen (Zeit vergeben, ungültig): endgültig
    # 401/403 (Schlüssel), 404 (Event-Typ), 408/425/429, 5xx und Netzfehler sind Betriebsstörungen: wiederholen, dann Mensch

    def __init__(self, api_key: str, event_type_id: int, basis_url: str = "https://api.cal.com",
                 zeitzone: str = "Europe/Berlin", client: Optional[httpx.Client] = None, timeout: float = 8.0):
        self.event_type_id, self.zeitzone = int(event_type_id), zeitzone
        self.client = client or httpx.Client(base_url=basis_url, timeout=timeout)
        self.client.headers["Authorization"] = f"Bearer {api_key}"

    def _anfrage(self, methode: str, pfad: str, version: str, timeout_s: Optional[float] = None, **kw) -> dict:
        if timeout_s is not None:
            kw["timeout"] = timeout_s
        try:
            r = self.client.request(methode, pfad, headers={"cal-api-version": version}, **kw)
        except httpx.HTTPError as e:
            raise KalenderFehler(f"{methode} {pfad}: {e}") from e
        daten = _json(r)
        if r.status_code >= 400:
            raise KalenderFehler(_fehlertext(daten, r), r.status_code, r.status_code in self.ABGELEHNT)
        if daten.get("status") == "error":
            raise KalenderFehler(_fehlertext(daten, r), r.status_code, True)
        return daten

    def freie_zeiten(self, von: datetime, bis: datetime) -> list[tuple[datetime, datetime]]:
        daten = self._anfrage("GET", "/v2/slots", self.SLOTS_VERSION, params={
            "start": iso_utc(von), "end": iso_utc(bis), "eventTypeId": self.event_type_id, "timeZone": "UTC", "format": "range"})
        zeiten = []
        for tag in (daten.get("data") or {}).values():
            for slot in tag or []:
                if isinstance(slot, str):                                  # Format "time": nur der Beginn
                    start, ende = iso_lesen(slot), None
                else:
                    start = iso_lesen(slot["start"])
                    ende = iso_lesen(slot["end"]) if slot.get("end") else None
                zeiten.append((start, ende or start + timedelta(hours=1)))
        return sorted(zeiten)

    def buchen(self, start: datetime, lead: dict, art: str, call_id: Optional[str] = None,
               timeout_s: Optional[float] = None) -> str:
        body: dict[str, Any] = {
            "start": iso_utc(start), "eventTypeId": self.event_type_id,
            "attendee": {"name": (lead.get("name") or "Kunde")[:100], "timeZone": self.zeitzone, "language": "de",
                         **({"phoneNumber": lead["telefon"]} if lead.get("telefon") else {})},
            "metadata": {"lead_id": str(lead.get("id", "")), "art": art, "quelle": "ai-call-agent",
                         **({"call_id": call_id} if call_id else {})}}
        daten = self._anfrage("POST", "/v2/bookings", self.BOOKINGS_VERSION, timeout_s, json=body)
        data = daten.get("data")
        uid = data.get("uid") if isinstance(data, dict) else None
        if not uid:
            raise KalenderFehler("Buchung ohne uid in der Antwort", 200, False)
        return str(uid)

    def stornieren(self, extern_id: str, grund: str, timeout_s: Optional[float] = None) -> None:
        try:
            self._anfrage("POST", f"/v2/bookings/{extern_id}/cancel", self.BOOKINGS_VERSION, timeout_s,
                          json={"cancellationReason": grund[:200]})
        except KalenderFehler as e:
            if e.status == 404:                                           # schon weg: Ziel erreicht
                return
            raise


class Kalenderspiegel:
    """Verbindet Kalenderdienst und Integrationsdatenbank: Verfügbarkeit spiegeln, Aufträge ausführen.

    Aufträge (`kalender_auftraege`) werden vor der Ausführung beansprucht (Status `laeuft`), damit die
    Sofortbestätigung im Request und der Planer nie denselben Auftrag doppelt ausführen. Ein Auftrag, der
    bei einem Neustart noch `laeuft` war, wird nicht wiederholt (die Buchung könnte im Kalender schon
    angekommen sein), sondern tot gestellt und einem Menschen vorgelegt.
    """

    SOFORT_TIMEOUT_S = 2.5        # Sofortbestätigung im Tool-Aufruf: unter dem Timeout der Plattform (3 s)
    SOFORT_WARTEN_S = 2.0         # läuft der Auftrag gerade woanders (Planer), so lange auf das Ergebnis warten

    def __init__(self, crm, kalender: Kalender, uhr: Optional[Uhr] = None, nachbereitung=None, tage: int = KALENDER_TAGE,
                 sperre=None):
        self.crm, self.kalender, self.nachbereitung = crm, kalender, nachbereitung
        self.uhr = uhr or crm.uhr
        self.tage = tage
        self.sperre = sperre or nullcontext()
        self.letzter_spiegel: Optional[datetime] = None
        self.letzter_fehler: Optional[str] = None
        crm.kalender_aktiv = True
        with self.sperre, crm.transaktion():
            self._unterbrochene_auftraege_vorlegen()

    def _unterbrochene_auftraege_vorlegen(self):
        rows = self.crm.con.execute("SELECT id, art, lead_id, appointment_id FROM kalender_auftraege "
                                    "WHERE status IN ('laeuft', 'verworfen_laeuft')").fetchall()
        for kid, art, lead_id, appointment_id in rows:
            self.crm.kalender_auftrag_abschliessen(kid, "tot", "bei einem Neustart unterbrochen; Kalender von Hand prüfen", 0)
            if lead_id:
                self.crm.aufgabe(lead_id, f"Prüfen: Kalenderauftrag '{art}' bei Neustart unterbrochen", self.uhr() + timedelta(hours=4),
                                 f"Auftrag {kid} zu Termin {appointment_id}: Kalender und Buchung von Hand abgleichen.")
            logbuch.warnung("kalender.unterbrochen", auftrag=kid, art=art, lead_id=lead_id, appointment_id=appointment_id)

    # ------------------------------------------------------------ Verfügbarkeit
    def spiegeln(self) -> dict[str, Any]:
        """Freie Zeiten der nächsten Tage holen (ohne Sperre) und die Slot-Tabelle abgleichen (mit Sperre).
        Ein Fehler zählt als Lauf: Der nächste Versuch folgt erst nach dem Takt, nicht bei jedem Planer-Durchlauf."""
        jetzt = self.uhr()
        self.letzter_spiegel = jetzt
        try:
            zeiten = self.kalender.freie_zeiten(jetzt, jetzt + timedelta(days=self.tage))
        except KalenderFehler as e:
            self.letzter_fehler = f"{iso_utc(jetzt)}: {str(e)[:200]}"
            logbuch.warnung("kalender.spiegel", fehler=str(e), status=e.status)
            return {"fehler": str(e)[:200]}
        with self.sperre, self.crm.transaktion():
            bericht = self.crm.slots_spiegeln(zeiten)
        self.letzter_fehler = None
        return {"stand": iso_utc(jetzt), **bericht}

    # ------------------------------------------------------------ Aufträge: buchen, stornieren
    def bestaetigen(self, appointment_id: int) -> dict[str, Any]:
        """Die Buchung eines Tool-Aufrufs sofort im Kalender bestätigen (vom Server nach der Sperre aufgerufen).
        Hat der Planer den Auftrag schon übernommen, wird kurz auf sein Ergebnis gewartet und das zurückgegeben."""
        with self.sperre:
            auftraege = self.crm.kalender_auftraege_faellig(1, appointment_id=appointment_id)
        if auftraege:
            return self._ausfuehren(auftraege[0], synchron=True)
        frist = time.monotonic() + self.SOFORT_WARTEN_S
        while True:
            with self.sperre:
                a = self.crm.kalender_auftrag_fuer_termin(appointment_id)
                termin = self._termin(appointment_id) if a else None
                extern = self.crm.con.execute("SELECT extern_id FROM appointments WHERE id = ?", [appointment_id]).fetchone()
            if a is None:
                return {"ausgang": "uebersprungen", "bestaetigt": False, "hinweis": "kein Auftrag"}
            if a["status"] == "erledigt" and termin:
                return {"ausgang": "bestaetigt", "bestaetigt": True, "extern_id": extern[0] if extern else None, "alt_abgesagt": None}
            if a["status"] in ("abgelehnt", "verworfen", "tot") or termin is None:
                return {"ausgang": "abgelehnt" if a["status"] == "abgelehnt" else "uebersprungen", "bestaetigt": False,
                        "fehler": a["letzte_antwort"] or a["status"]}
            if a["status"] == "offen" or time.monotonic() >= frist:          # Wiederholung geplant oder noch unterwegs
                return {"ausgang": "wiederholen", "bestaetigt": False, "fehler": a["letzte_antwort"] or "läuft noch"}
            time.sleep(0.05)

    def auftraege_ausfuehren(self, max_n: int = 50) -> dict[str, int]:
        """Fällige Aufträge ausführen; Folgeaufträge aus demselben Durchlauf (die Absage des alten Termins nach einer
        bestätigten Umbuchung) kommen gleich mit dran, statt auf den nächsten Takt zu warten."""
        bericht = {"bestaetigt": 0, "storniert": 0, "abgelehnt": 0, "wiederholen": 0, "tot": 0, "uebersprungen": 0}
        for _ in range(3):
            with self.sperre:
                auftraege = self.crm.kalender_auftraege_faellig(max_n)
            if not auftraege:
                break
            for a in auftraege:
                bericht[self._ausfuehren(a, synchron=False)["ausgang"]] += 1
        return bericht

    def _ausfuehren(self, a: dict, synchron: bool) -> dict[str, Any]:
        """Ein Auftrag: beanspruchen, HTTP ohne Sperre, Ergebnis mit Sperre. Jede Ausnahme endet in einem
        definierten Status; ein Auftrag bleibt nie 'laeuft' hängen."""
        versuche = a["versuche"] + 1
        timeout_s = self.SOFORT_TIMEOUT_S if synchron else None
        with self.sperre:
            if not self.crm.kalender_auftrag_beanspruchen(a["id"]):
                return {"ausgang": "uebersprungen", "bestaetigt": False}
            termin = self._termin(a["appointment_id"]) if a["art"] == "buchen" else None
            lead = self.crm.lead(a["lead_id"]) if a["lead_id"] else None
        try:
            if a["art"] == "buchen":
                return self._buchen(a, termin, lead or {}, versuche, synchron, timeout_s)
            self.kalender.stornieren(a["extern_id"], a["grund"] or "Absage", timeout_s=timeout_s)
            with self.sperre:
                self.crm.kalender_auftrag_abschliessen(a["id"], "erledigt", "storniert", versuche)
            return {"ausgang": "storniert", "bestaetigt": False}
        except KalenderFehler as e:
            return self._fehlgeschlagen(a, e, versuche, synchron)
        except Exception as e:  # noqa: BLE001 - unerwartete Antwortform oder Datenbankfehler: wie eine Störung behandeln
            logbuch.fehler("kalender.ausnahme", auftrag=a["id"], art=a["art"], fehler=f"{type(e).__name__}: {str(e)[:200]}")
            return self._fehlgeschlagen(a, KalenderFehler(f"{type(e).__name__}: {str(e)[:150]}"), versuche, synchron)

    def _buchen(self, a: dict, termin: Optional[dict], lead: dict, versuche: int, synchron: bool,
                timeout_s: Optional[float]) -> dict[str, Any]:
        if termin is None:                                                    # inzwischen verworfen oder abgesagt
            with self.sperre:
                self.crm.kalender_auftrag_abschliessen(a["id"], "verworfen", "Buchung existiert nicht mehr", versuche)
            return {"ausgang": "abgelehnt", "bestaetigt": False, "fehler": "Buchung existiert nicht mehr"}
        extern_id = self.kalender.buchen(self.crm.start_utc(termin["start"]), lead, termin["art"], a["call_id"], timeout_s=timeout_s)
        with self.sperre, self.crm.transaktion():
            status = self.crm.kalender_auftrag_status(a["id"])
            if status != "laeuft" or self._termin(a["appointment_id"]) is None:
                # Während der Anfrage wurde der Termin abgesagt oder verworfen: die Kalenderbuchung ist verwaist, sofort absagen
                self.crm.kalender_auftrag_abschliessen(a["id"], "verworfen", f"uid {extern_id} nach Absage storniert", versuche)
                self.crm.kalender_auftrag(None, "stornieren", extern_id=extern_id, grund="Buchung verworfen", lead_id=a["lead_id"])
                logbuch.warnung("kalender.verwaist", auftrag=a["id"], extern_id=extern_id, lead_id=a["lead_id"])
                return {"ausgang": "abgelehnt", "bestaetigt": False, "fehler": "Buchung existiert nicht mehr"}
            self.crm.termin_bestaetigen(a["appointment_id"], extern_id)
            self.crm.kalender_auftrag_abschliessen(a["id"], "erledigt", f"uid {extern_id}", versuche)
            alt = self._termin(a["ersetzt"]) if a["ersetzt"] else None
            if alt:                                                        # Umbuchung: jetzt darf der alte Termin weg
                self.crm.termin_stornieren(a["ersetzt"], "Umbuchung", a["call_id"])
                self._ereignis("termin.abgesagt", termin["lead_id"], {"abgesagt": f"{alt['start']:%d.%m.%Y %H:%M} Uhr",
                                                                      "grund": "Umbuchung", "umbuchung": True, "call_id": a["call_id"]})
        logbuch.eintrag("kalender.bestaetigt", call_id=a["call_id"], lead_id=a["lead_id"], appointment_id=a["appointment_id"],
                        extern_id=extern_id, umbuchung=bool(alt), versuche=versuche, synchron=synchron)
        return {"ausgang": "bestaetigt", "bestaetigt": True, "extern_id": extern_id,
                "alt_abgesagt": f"{alt['start']:%d.%m.%Y %H:%M} Uhr" if alt else None}

    def _termin(self, appointment_id: Optional[int]) -> Optional[dict]:
        if appointment_id is None:
            return None
        row = self.crm.con.execute(
            "SELECT a.id, a.slot_id, s.start, a.art, a.lead_id FROM appointments a JOIN slots s ON s.id = a.slot_id WHERE a.id = ?",
            [appointment_id]).fetchone()
        return {"id": row[0], "slot_id": row[1], "start": row[2], "art": row[3], "lead_id": row[4]} if row else None

    def _ereignis(self, typ: str, lead_id: Optional[int], daten: dict[str, Any]):
        if self.nachbereitung is not None and lead_id:
            self.nachbereitung._ereignis(typ, lead_id, daten, self.uhr())

    def _aufgabe(self, a: dict, titel: str, text: str) -> Optional[int]:
        if not a["lead_id"]:
            return None
        faellig = self.uhr() + timedelta(hours=4)
        aid = self.crm.aufgabe(a["lead_id"], titel, faellig, text)
        self._ereignis("aufgabe.erstellt", a["lead_id"], {"aufgabe_id": aid, "titel": titel, "faellig": iso_utc(faellig)})
        return aid

    def _fehlgeschlagen(self, a: dict, e: KalenderFehler, versuche: int, synchron: bool) -> dict[str, Any]:
        tot = versuche >= len(RETRY_PLAN_S)
        logbuch.warnung("kalender.fehler", call_id=a["call_id"], lead_id=a["lead_id"], art=a["art"], appointment_id=a["appointment_id"],
                        endgueltig=e.endgueltig, status=e.status, versuche=versuche, tot=tot, synchron=synchron, fehler=str(e))
        with self.sperre, self.crm.transaktion():
            if a["art"] == "buchen" and e.endgueltig:                        # Zeit vergeben: Vormerkung weg, alter Termin bleibt
                termin = self._termin(a["appointment_id"])
                self.crm.kalender_auftrag_abschliessen(a["id"], "abgelehnt", str(e), versuche)
                if termin:
                    self.crm.termin_freigeben(a["appointment_id"])
                    self.crm.slot_entfernen(termin["slot_id"])
                    if not synchron:                                       # der Agent hatte schon zugesagt: Mensch und Kundensystem erfahren es
                        self._aufgabe(a, "Prüfen: Kalender hat vorgemerkte Buchung abgelehnt",
                                      f"Termin {termin['start']:%d.%m.%Y %H:%M} Uhr war dem Kunden zugesagt, der Kalender lehnt ab: "
                                      f"{str(e)[:200]}. Kunden anrufen und neuen Termin vereinbaren.")
                        self._ereignis("termin.abgesagt", a["lead_id"], {"abgesagt": f"{termin['start']:%d.%m.%Y %H:%M} Uhr",
                                                                         "grund": "Kalender hat die Buchung abgelehnt",
                                                                         "umbuchung": False, "call_id": a["call_id"]})
                return {"ausgang": "abgelehnt", "bestaetigt": False, "fehler": str(e),
                        "termin": f"{termin['start']:%d.%m.%Y %H:%M} Uhr" if termin else None}
            if tot or (a["art"] == "stornieren" and e.endgueltig):
                self.crm.kalender_auftrag_abschliessen(a["id"], "tot", str(e), versuche)
                titel = ("Prüfen: Kalenderbuchung nicht bestätigt" if a["art"] == "buchen"
                         else "Prüfen: Absage im Kalender fehlgeschlagen")
                self._aufgabe(a, titel, f"Kalenderdienst: {str(e)[:200]} (Auftrag {a['id']}, {a['art']})")
                return {"ausgang": "tot", "bestaetigt": False, "fehler": str(e)}
            naechster = self.uhr() + timedelta(seconds=RETRY_PLAN_S[min(versuche, len(RETRY_PLAN_S) - 1)])
            self.crm.kalender_auftrag_abschliessen(a["id"], "offen", str(e), versuche, naechster)
        return {"ausgang": "wiederholen", "bestaetigt": False, "fehler": str(e), "naechster_versuch": iso_utc(naechster)}

    # ------------------------------------------------------------ Planer
    def tick(self, spiegeln: Optional[bool] = None, takt_s: int = KALENDER_SPIEGEL_TAKT_S) -> dict[str, Any]:
        """Ein Durchlauf: offene Aufträge immer, Verfügbarkeit nur, wenn der Takt abgelaufen ist (oder `spiegeln`)."""
        bericht: dict[str, Any] = {"auftraege": self.auftraege_ausfuehren()}
        faellig = spiegeln if spiegeln is not None else (
            self.letzter_spiegel is None or (self.uhr() - self.letzter_spiegel).total_seconds() >= takt_s)
        if faellig:
            bericht["spiegel"] = self.spiegeln()
        return bericht

    def status(self) -> dict[str, Any]:
        return {"auftraege": self.crm.kalender_status(), "letzter_spiegel": iso_utc(self.letzter_spiegel) if self.letzter_spiegel else None,
                "letzter_fehler": self.letzter_fehler}


def kalender_aus_konfig(konfig) -> Optional[Kalender]:
    """Genau ein Kalenderdienst laut Konfiguration; leer = Slot-Tabelle."""
    art = (konfig.kalender or "").lower()
    if not art:
        return None
    if art == "calcom":
        if not (konfig.calcom_api_key and konfig.calcom_event_type_id):
            raise RuntimeError("KALENDER=calcom braucht CALCOM_API_KEY und CALCOM_EVENT_TYPE_ID")
        return CalComKalender(konfig.calcom_api_key, int(konfig.calcom_event_type_id), konfig.calcom_url, konfig.zeitzone)
    raise RuntimeError(f"Unbekannter KALENDER '{konfig.kalender}'. Bekannt: calcom")


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
