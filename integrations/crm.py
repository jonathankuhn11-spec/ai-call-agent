"""CRM-Zugriff hinter einer schmalen Schnittstelle: lokal (DuckDB) oder HubSpot (hubspot.py).

Der operative Zustand eines Deployments (freie Slots, was wem angeboten wurde, Buchungen,
Gesprächsprotokolle, Sperrliste, Zuordnung Lead -> telli-Kontakt, Deduplizierung eingehender
Webhooks) liegt immer in der lokalen Integrationsdatenbank. Das Kunden-CRM wird darüber
synchron gehalten. Das hat zwei Gründe: Jeder Tool-Aufruf der Plattform ist ein eigener
HTTP-Request ohne Gedächtnis, also muss der Gesprächszustand in einer Datenbank liegen; und
Geschäftsregeln wie "gebucht wird nur, was angeboten wurde" brauchen diesen Zustand, egal
welches CRM dahinter steht.

LocalCRM baut auf den Tabellen von agent.init_db auf (leads, slots, appointments, calls) und
nutzt agent.Tools für alle Schreibzugriffe mit Geschäftsregeln. Es gibt keinen zweiten Weg,
einen Lead zu qualifizieren oder einen Termin zu buchen.

Zeit: Die TIMESTAMP-Spalten des CRM (Slots, Buchungen, Aufgaben, Protokoll) sind Wanduhrzeit in
der konfigurierten Zeitzone (Standard Europe/Berlin). Die Verbindung wird darauf gesetzt
(SET TimeZone), damit now() in DuckDB, die vom Agenten geschriebenen Zeiten und die hier
umgerechneten UTC-Zeitpunkte der Plattform denselben Bezug haben, unabhängig von der Zeitzone
des Hosts. Nur die Outbox rechnet intern in naiver UTC mit ihrer eigenen Uhr (outbox.py).
Der Server nutzt seine eigene Datenbankdatei (DB_PATH); lokale Läufe von agent.py schreiben
Host-Ortszeit und gehören in eine andere Datei.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol
from zoneinfo import ZoneInfo

import agent
from integrations.modelle import telefon_normalisieren

LEAD_SPALTEN = ["id", "name", "telefon", "plz", "quelle", "status", "eigentuemer", "gebaeudetyp",
                "dachflaeche_m2", "jahresverbrauch_kwh", "notiz", "crm_id"]
ANGEBOT_GUELTIG_H = 24      # so lange darf ein angebotener Slot noch gebucht werden
SLOT_DAUER_MIN = 60         # Beratungstermin


class CRM(Protocol):
    """Was Nachbereitung, Contact Lookup und Lead-Push vom Kunden-CRM brauchen."""

    def lead(self, lead_id: Any) -> Optional[dict]: ...
    def lead_per_telefon(self, telefon: str) -> Optional[dict]: ...
    def lead_aktualisieren(self, lead_id: Any, **felder) -> dict: ...
    def notiz(self, lead_id: Any, text: str) -> Any: ...
    def aufgabe(self, lead_id: Any, titel: str, faellig: datetime, text: str = "") -> Any: ...


class PersistenteTools(agent.Tools):
    """agent.Tools, dessen Angebotsgedächtnis die Requests überlebt, mit idempotenter Buchung.

    `offered` wird aus der Tabelle angebote geladen und nach check_slots dorthin geschrieben.
    Eine Wiederholung derselben Buchung (Plattform oder Modell versuchen es nach einem Timeout
    erneut) antwortet mit demselben Erfolg statt mit "bereits vergeben"; ein anderer Slot für
    denselben Lead ist eine Umbuchung, der alte Slot wird frei. Ein Lead hält höchstens einen
    Termin. Alle übrigen Geschäftsregeln bleiben unverändert in agent.Tools.
    """

    def __init__(self, crm: "LocalCRM", lead_id: int):
        super().__init__(crm.con, lead_id)
        self.crm = crm
        self.offered = crm.angebotene_slots(lead_id)

    def check_slots(self, **kwargs):
        vorher = set(self.offered)
        result = super().check_slots(**kwargs)
        if "freie_termine" in result:
            neu = [s["slot_id"] for s in result["freie_termine"]]
            self.crm.angebote_merken(self.lead_id, neu)
            self.offered = vorher | set(neu)
        return result

    def book_appointment(self, **kwargs):
        bestehend = self.crm.termin(self.lead_id, zukunft=True)        # vergangene Termine bleiben Historie
        slot_id = kwargs.get("slot_id")
        if bestehend and str(bestehend["slot_id"]) == str(slot_id):          # Wiederholung: idempotent
            self.booked = True
            return {"ok": True, "termin": f"{bestehend['start']:%d.%m.%Y %H:%M} Uhr", "art": bestehend["art"],
                    "hinweis": "Dieser Termin war bereits gebucht."}
        result = super().book_appointment(**kwargs)
        if result.get("ok") and bestehend:                                     # Umbuchung: alten Slot freigeben
            self.crm.termin_freigeben(bestehend["id"])
            result["umgebucht_von"] = f"{bestehend['start']:%d.%m.%Y %H:%M} Uhr"
        return result


class LocalCRM:
    def __init__(self, con, zeitzone: str = "Europe/Berlin"):
        self.con = con
        self.zeitzone = zeitzone
        self.tz = ZoneInfo(zeitzone)
        self._tiefe = 0                                               # verschachtelte transaktion()-Aufrufe
        con.execute(f"SET TimeZone = '{zeitzone}'")
        self._schema()

    # ------------------------------------------------------------ Schema, Zeit, Transaktion
    def _schema(self):
        c = self.con
        # ID des Leads im Kunden-CRM (z. B. HubSpot-Kontakt-ID); leer im reinen Mock-Betrieb
        c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS crm_id TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS angebote (
            lead_id INTEGER, slot_id INTEGER, angeboten_am TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS notizen (
            id INTEGER, lead_id INTEGER, text TEXT, erstellt_am TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS aufgaben (
            id INTEGER, lead_id INTEGER, titel TEXT, text TEXT, faellig_am TIMESTAMP, status TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS sperrliste (
            telefon TEXT PRIMARY KEY, grund TEXT, seit TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS telli_kontakte (
            lead_id INTEGER PRIMARY KEY, contact_id TEXT, loop_id TEXT, erstellt_am TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS webhook_eingang (
            message_id TEXT PRIMARY KEY, call_id TEXT, ereignis TEXT, empfangen_am TIMESTAMP)""")
        # Spiegelung ins Kunden-CRM: in der Transaktion vorgemerkt, außerhalb der Sperre ausgeführt
        c.execute("""CREATE TABLE IF NOT EXISTS spiegel_auftraege (
            id INTEGER PRIMARY KEY, lead_id INTEGER, ziel_id TEXT, auftrag TEXT, status TEXT, versuche INTEGER,
            naechster_versuch TIMESTAMP, letzte_antwort TEXT, erstellt_am TIMESTAMP)""")

    def wandzeit(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Beliebiger Zeitpunkt -> naive Wanduhrzeit der konfigurierten Zone (so speichert die DB)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(self.tz).replace(tzinfo=None)

    def start_utc(self, start_lokal: datetime) -> datetime:
        return start_lokal.replace(tzinfo=self.tz).astimezone(timezone.utc)

    def jetzt(self) -> datetime:
        return datetime.now(self.tz).replace(tzinfo=None)

    @contextmanager
    def transaktion(self):
        """Alles oder nichts: Ein Fehler mittendrin hinterlässt keine halbe Nachbereitung.

        Verschachtelte Aufrufe hängen sich an die äußere Transaktion an; nur die äußerste
        schreibt oder rollt zurück.
        """
        if self._tiefe > 0:
            self._tiefe += 1
            try:
                yield
            finally:
                self._tiefe -= 1
            return
        self.con.execute("BEGIN TRANSACTION")
        self._tiefe = 1
        try:
            yield
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")
        finally:
            self._tiefe = 0

    # ------------------------------------------------------------ Leads
    def lead(self, lead_id: Any) -> Optional[dict]:
        try:
            lid = int(lead_id)
        except (TypeError, ValueError):
            return None
        row = self.con.execute(f"SELECT {', '.join(LEAD_SPALTEN)} FROM leads WHERE id = ?", [lid]).fetchone()
        return dict(zip(LEAD_SPALTEN, row)) if row else None

    def lead_per_telefon(self, telefon: str) -> Optional[dict]:
        gesucht = telefon_normalisieren(telefon)
        if not gesucht:
            return None
        for lid, nummer in self.con.execute("SELECT id, telefon FROM leads").fetchall():
            if telefon_normalisieren(nummer) == gesucht:
                return self.lead(lid)
        return None

    def lead_aktualisieren(self, lead_id: Any, **felder) -> dict:
        """Über agent.Tools, damit Validierung und Qualifizierungsregel greifen.

        Ohne ausdrücklichen Status bleibt der aktuelle erhalten. Widerspricht ein neuer Fakt der
        Qualifizierung (ein qualifizierter Lead sagt, er sei Mieter), wird der Fakt gespeichert und
        die Qualifizierung zurückgesetzt, statt den Fakt zu verwerfen.
        """
        if "status" in felder:
            return self.tools(int(lead_id)).run("update_lead", felder)
        aktuell = self.lead(lead_id)
        if aktuell is None:
            return {"ok": False, "fehler": f"Lead {lead_id} nicht gefunden"}
        status = aktuell["status"] if aktuell["status"] != "offen" else "in_qualifizierung"
        tools = self.tools(int(lead_id))
        r = tools.run("update_lead", {**felder, "status": status})
        if not r.get("ok") and status == "qualifiziert" and "validierungsfehler" not in r:
            r = tools.run("update_lead", {**felder, "status": "in_qualifizierung"})
            if r.get("ok"):
                r["hinweis"] = "Qualifizierung zurückgesetzt"
        return r

    def tools(self, lead_id: int) -> PersistenteTools:
        return PersistenteTools(self, lead_id)

    # ------------------------------------------------------------ Angebote, Slots, Buchung
    def angebote_merken(self, lead_id: int, slot_ids: list[int]):
        self.con.executemany("INSERT INTO angebote VALUES (?, ?, now()::TIMESTAMP)", [(lead_id, s) for s in slot_ids])

    def angebotene_slots(self, lead_id: int) -> set[int]:
        rows = self.con.execute(
            f"SELECT slot_id FROM angebote WHERE lead_id = ? AND angeboten_am > now()::TIMESTAMP - INTERVAL {ANGEBOT_GUELTIG_H} HOUR",
            [lead_id]).fetchall()
        return {r[0] for r in rows}

    def freie_slots(self, anzahl: int = 4) -> list[dict]:
        rows = self.con.execute(
            "SELECT id, start FROM slots WHERE lead_id IS NULL AND start > now()::TIMESTAMP ORDER BY start LIMIT ?",
            [anzahl]).fetchall()
        return [{"slot_id": r[0], "start": r[1]} for r in rows]

    def slot_per_start(self, start_utc: datetime) -> Optional[dict]:
        """Slot zu einem UTC-Zeitpunkt; die Tabelle speichert Wanduhrzeit."""
        row = self.con.execute("SELECT id, start, lead_id FROM slots WHERE start = ?", [self.wandzeit(start_utc)]).fetchone()
        return {"slot_id": row[0], "start": row[1], "lead_id": row[2]} if row else None

    def termin(self, lead_id: int, seit: Optional[datetime] = None, zukunft: bool = False) -> Optional[dict]:
        """Jüngster Termin des Leads; `seit`: nur Buchungen ab diesem Zeitpunkt (Gesprächsbeginn),
        `zukunft`: nur Termine, die noch bevorstehen."""
        sql = ("SELECT a.id, a.slot_id, s.start, a.art, a.gebucht_am FROM appointments a JOIN slots s ON s.id = a.slot_id "
               "WHERE a.lead_id = ?")
        params: list = [lead_id]
        if seit is not None:
            sql += " AND a.gebucht_am >= ?"
            params.append(self.wandzeit(seit))
        if zukunft:
            sql += " AND s.start > now()::TIMESTAMP"
        row = self.con.execute(sql + " ORDER BY a.gebucht_am DESC, a.id DESC LIMIT 1", params).fetchone()
        if not row:
            return None
        return {"id": row[0], "slot_id": row[1], "start": row[2], "art": row[3], "gebucht_am": row[4]}

    def termin_freigeben(self, appointment_id: int):
        row = self.con.execute("SELECT slot_id FROM appointments WHERE id = ?", [appointment_id]).fetchone()
        if row:
            self.con.execute("UPDATE slots SET lead_id = NULL WHERE id = ?", [row[0]])
            self.con.execute("DELETE FROM appointments WHERE id = ?", [appointment_id])

    # ------------------------------------------------------------ Notizen, Aufgaben, Sperrliste
    def notiz(self, lead_id: Any, text: str) -> int:
        nid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM notizen").fetchone()[0]
        self.con.execute("INSERT INTO notizen VALUES (?, ?, ?, now()::TIMESTAMP)", [nid, int(lead_id), text[:4000]])
        return nid

    def notizen(self, lead_id: int) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT text FROM notizen WHERE lead_id = ? ORDER BY id", [lead_id]).fetchall()]

    def aufgabe(self, lead_id: Any, titel: str, faellig: datetime, text: str = "") -> int:
        aid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM aufgaben").fetchone()[0]
        self.con.execute("INSERT INTO aufgaben VALUES (?, ?, ?, ?, ?, 'offen')",
                         [aid, int(lead_id), titel[:200], text[:2000], self.wandzeit(faellig)])
        return aid

    def aufgaben(self, lead_id: int) -> list[dict]:
        rows = self.con.execute("SELECT id, titel, text, faellig_am, status FROM aufgaben WHERE lead_id = ? ORDER BY id",
                                [lead_id]).fetchall()
        return [dict(zip(["id", "titel", "text", "faellig_am", "status"], r)) for r in rows]

    def sperren(self, telefon: str, grund: str):
        nummer = telefon_normalisieren(telefon)
        if not nummer:
            return
        self.con.execute("INSERT OR REPLACE INTO sperrliste VALUES (?, ?, now()::TIMESTAMP)", [nummer, grund[:200]])

    def gesperrt(self, telefon: str) -> bool:
        nummer = telefon_normalisieren(telefon)
        return bool(nummer) and self.con.execute(
            "SELECT COUNT(*) FROM sperrliste WHERE telefon = ?", [nummer]).fetchone()[0] > 0

    # ------------------------------------------------------------ telli-Kontakte (Lead-Push)
    def telli_kontakt_speichern(self, lead_id: int, contact_id: Optional[str], loop_id: Optional[str] = None):
        self.con.execute("INSERT OR REPLACE INTO telli_kontakte VALUES (?, ?, ?, now()::TIMESTAMP)",
                         [lead_id, contact_id, loop_id])

    def telli_kontakt(self, lead_id: int) -> Optional[dict]:
        row = self.con.execute("SELECT contact_id, loop_id FROM telli_kontakte WHERE lead_id = ?", [lead_id]).fetchone()
        return {"contact_id": row[0], "loop_id": row[1]} if row else None

    def leads_fuer_push(self) -> list[dict]:
        """Offene Leads ohne telli-Kontakt und ohne Sperrvermerk: die Kandidaten für den Dialer."""
        rows = self.con.execute(
            "SELECT l.id FROM leads l LEFT JOIN telli_kontakte t ON t.lead_id = l.id "
            "WHERE l.status = 'offen' AND t.lead_id IS NULL ORDER BY l.id").fetchall()
        leads = [self.lead(r[0]) for r in rows]
        return [l for l in leads if l and not self.gesperrt(l["telefon"])]

    def leads_ohne_anrufplanung(self) -> list[dict]:
        """Kontakt bei telli angelegt, Anruf aber nie geplant (z. B. Dialer war aus): nachholen."""
        rows = self.con.execute(
            "SELECT l.id, t.contact_id FROM leads l JOIN telli_kontakte t ON t.lead_id = l.id "
            "WHERE l.status = 'offen' AND t.loop_id IS NULL AND t.contact_id IS NOT NULL ORDER BY l.id").fetchall()
        aus = []
        for lid, contact_id in rows:
            lead = self.lead(lid)
            if lead and not self.gesperrt(lead["telefon"]):
                aus.append({**lead, "contact_id": contact_id})
        return aus

    def zuordnung_offen(self) -> list[int]:
        """Leads, deren externalId bei telli schon existiert (409): Kontakt-ID manuell zuordnen."""
        return [r[0] for r in self.con.execute(
            "SELECT lead_id FROM telli_kontakte WHERE contact_id IS NULL ORDER BY lead_id").fetchall()]

    # ------------------------------------------------------------ Spiegel-Aufträge (Kunden-CRM)
    def spiegel_einreihen(self, lead_id: int, ziel_id: Any, auftrag: dict) -> int:
        sid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM spiegel_auftraege").fetchone()[0]
        self.con.execute("INSERT INTO spiegel_auftraege VALUES (?, ?, ?, ?, 'offen', 0, now()::TIMESTAMP, NULL, now()::TIMESTAMP)",
                         [sid, lead_id, str(ziel_id), json.dumps(auftrag, ensure_ascii=False, default=str)])
        return sid

    def spiegel_faellig(self, max_n: int = 50) -> list[dict]:
        rows = self.con.execute(
            "SELECT id, lead_id, ziel_id, auftrag, versuche FROM spiegel_auftraege "
            "WHERE status = 'offen' AND naechster_versuch <= now()::TIMESTAMP ORDER BY id LIMIT ?", [max_n]).fetchall()
        return [{"id": r[0], "lead_id": r[1], "ziel_id": r[2], "auftrag": json.loads(r[3]), "versuche": r[4]} for r in rows]

    def spiegel_abschliessen(self, auftrag_id: int, ok: bool, antwort: str, versuche: int, naechster: Optional[datetime],
                             tot: bool = False, auftrag: Optional[dict] = None):
        status = "erledigt" if ok else ("tot" if tot else "offen")
        self.con.execute("UPDATE spiegel_auftraege SET status = ?, versuche = ?, letzte_antwort = ?, naechster_versuch = ? WHERE id = ?",
                         [status, versuche, antwort[:300], self.wandzeit(naechster) if naechster else self.jetzt(), auftrag_id])
        if auftrag is not None:                                       # Fortschritt je Schritt, damit nichts doppelt angelegt wird
            self.con.execute("UPDATE spiegel_auftraege SET auftrag = ? WHERE id = ?",
                             [json.dumps(auftrag, ensure_ascii=False, default=str), auftrag_id])

    def spiegel_status(self) -> dict:
        return {r[0]: r[1] for r in self.con.execute("SELECT status, COUNT(*) FROM spiegel_auftraege GROUP BY 1").fetchall()}

    # ------------------------------------------------------------ Webhooks, Protokoll
    def webhook_bekannt(self, message_id: str, call_id: Optional[str] = None) -> bool:
        if self.con.execute("SELECT COUNT(*) FROM webhook_eingang WHERE message_id = ?", [message_id]).fetchone()[0]:
            return True
        if call_id and self.con.execute(
                "SELECT COUNT(*) FROM webhook_eingang WHERE call_id = ? AND ereignis = 'call_ended'", [call_id]).fetchone()[0]:
            return True
        return False

    def webhook_merken(self, message_id: str, call_id: Optional[str], ereignis: str):
        self.con.execute("INSERT OR IGNORE INTO webhook_eingang VALUES (?, ?, ?, now()::TIMESTAMP)",
                         [message_id, call_id, ereignis])

    def anruf_protokollieren(self, lead_id: int, start: Optional[datetime], ende: Optional[datetime], ergebnis: str,
                             turns: int, tool_fehler: int, flags: list[str], transkript: list[dict]) -> int:
        cid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM calls").fetchone()[0]
        self.con.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [cid, lead_id, self.wandzeit(start), self.wandzeit(ende), ergebnis, turns, tool_fehler,
                          json.dumps(flags), json.dumps(transkript, ensure_ascii=False)])
        return cid

    def anrufe(self, lead_id: int) -> list[dict]:
        rows = self.con.execute("SELECT id, ergebnis, turns, flags FROM calls WHERE lead_id = ? ORDER BY id", [lead_id]).fetchall()
        return [{"id": r[0], "ergebnis": r[1], "turns": r[2], "flags": json.loads(r[3] or "[]")} for r in rows]


def slot_ende(start: datetime) -> datetime:
    return start + timedelta(minutes=SLOT_DAUER_MIN)
