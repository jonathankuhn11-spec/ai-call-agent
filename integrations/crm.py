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
der konfigurierten Zeitzone (Standard Europe/Berlin). Jeder Zeitvergleich und jeder Zeitstempel
kommt aus der einen Uhr der Schicht (integrations/uhr.py), umgerechnet in diese Wanduhrzeit;
die Datenbankfunktion now() wird nicht benutzt, weil sie sich nicht stellen lässt. Die Verbindung
wird trotzdem auf die Zone gesetzt (SET TimeZone), damit Zeiten, die der Prototyp agent.py über
dieselbe Verbindung schreibt, denselben Bezug haben. Nur die Outbox rechnet intern in naiver UTC
mit derselben Uhr (outbox.py). Der Server nutzt seine eigene Datenbankdatei (DB_PATH); lokale
Läufe von agent.py schreiben Host-Ortszeit und gehören in eine andere Datei.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol
from zoneinfo import ZoneInfo

import agent
from integrations.modelle import telefon_normalisieren
from integrations.uhr import Uhr

LEAD_SPALTEN = ["id", "name", "telefon", "plz", "quelle", "status", "eigentuemer", "gebaeudetyp",
                "dachflaeche_m2", "jahresverbrauch_kwh", "notiz", "crm_id"]
ANGEBOT_GUELTIG_H = 24      # so lange darf ein angebotener Slot noch gebucht werden
SLOT_DAUER_MIN = 60         # Beratungstermin
ERINNERUNG_VORLAUF_H = 24   # Erinnerung an den Termin: einen Tag vorher ...
ERINNERUNG_KURZFRIST_H = 2  # ... oder, bei kurzfristiger Buchung, zwei Stunden vorher; näher dran gar nicht mehr
KALENDER_KARENZ_S = 10      # so lange gehört ein neuer Buchungsauftrag dem Server (Sofortbestätigung), dann dem Planer
TICKET_KATEGORIEN = ("stoerung_internet", "stoerung_tv", "stoerung_telefon", "rechnung", "sonstiges")


class CRM(Protocol):
    """Was Nachbereitung, Contact Lookup und Lead-Push vom Kunden-CRM brauchen."""

    def lead(self, lead_id: Any) -> Optional[dict]: ...
    def lead_anlegen(self, name: str, telefon: str, quelle: str, plz: Optional[str] = None) -> dict: ...
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

    call_id: Optional[str] = None            # setzt der Server je Request; Verifizierung und Idempotenz hängen daran

    def jetzt(self) -> datetime:
        return self.crm.jetzt()                  # die Uhr der Schicht, nicht die der Datenbank

    def _slots_formatiert(self, rows) -> dict:
        wt = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        return {"freie_termine": [{"slot_id": r[0], "zeit": f"{wt[r[1].weekday()]} {r[1]:%d.%m. %H:%M} Uhr"} for r in rows]}

    def check_slots(self, **kwargs):
        vorher = set(self.offered)
        if self.crm.qualifizierung_noetig:
            result = super().check_slots(**kwargs)
        else:                                         # Inbound-Service: Termine gibt es für jeden bekannten Kunden
            rows = self.con.execute("SELECT id, start FROM slots WHERE lead_id IS NULL AND start > ? "
                                    "ORDER BY start LIMIT 2", [self.jetzt()]).fetchall()
            result = self._slots_formatiert(rows)
        if "freie_termine" in result:
            neu = [s["slot_id"] for s in result["freie_termine"]]
            self.crm.angebote_merken(self.lead_id, neu)
            self.offered = vorher | set(neu)
        return result

    def _slot_pruefen(self, slot_id) -> Optional[dict]:
        """Vor jeder Buchung: Der Slot muss noch existieren (der Kalender-Spiegel kann ihn entfernt haben) und in der
        Zukunft liegen (Angebote gelten 24 Stunden, ein Slot von heute Morgen kann inzwischen vorbei sein)."""
        try:
            sid = int(slot_id)
        except (TypeError, ValueError):
            return None
        row = self.con.execute("SELECT start FROM slots WHERE id = ?", [sid]).fetchone()
        if row is None and sid in self.offered:
            return {"ok": False, "fehler": "Dieser Termin ist inzwischen vergeben. check_slots erneut aufrufen und einen "
                                           "anderen Termin anbieten."}
        if row is not None and row[0] <= self.jetzt():
            return {"ok": False, "fehler": f"Der Termin am {row[0]:%d.%m.%Y %H:%M} Uhr liegt nicht mehr in der Zukunft. "
                                           "check_slots erneut aufrufen."}
        return None

    def _buchen_ohne_qualifizierung(self, **kwargs) -> dict:
        """Dieselben Regeln wie agent.Tools.book_appointment, nur ohne die PV-Qualifizierung."""
        b = agent.Buchung(**kwargs)
        if b.slot_id not in self.offered:
            return {"ok": False, "fehler": "Dieser Termin wurde dem Kunden nicht angeboten. Erst check_slots, dann die "
                                           "Termine vorlesen, dann nur den Termin buchen, den der Kunde gewählt hat."}
        slot = self.con.execute("SELECT start, lead_id FROM slots WHERE id = ?", [b.slot_id]).fetchone()
        if slot is None:
            return {"ok": False, "fehler": f"slot_id {b.slot_id} existiert nicht. check_slots aufrufen."}
        if slot[1] is not None:
            return {"ok": False, "fehler": "Termin ist bereits vergeben. Anderen Termin anbieten."}
        self.con.execute("UPDATE slots SET lead_id = ? WHERE id = ?", [self.lead_id, b.slot_id])
        aid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM appointments").fetchone()[0]
        self.con.execute("INSERT INTO appointments (id, lead_id, slot_id, art, gebucht_am) VALUES (?, ?, ?, ?, ?)",
                         [aid, self.lead_id, b.slot_id, b.art, self.jetzt()])
        self.booked = True
        return {"ok": True, "termin": f"{slot[0]:%d.%m.%Y %H:%M} Uhr", "art": b.art}

    def _kurzfristig(self, termin: dict) -> Optional[dict]:
        """Mindestvorlauf: Termine, die näher liegen, sind über den Agenten nicht änderbar."""
        if self.crm.mindestvorlauf_h and termin["start"] < self.crm.jetzt() + timedelta(hours=self.crm.mindestvorlauf_h):
            return {"ok": False, "fehler": f"Der Termin am {termin['start']:%d.%m.%Y %H:%M} Uhr liegt innerhalb von "
                                           f"{self.crm.mindestvorlauf_h} Stunden und ist bereits eingeplant. Nicht über den "
                                           "Agenten änderbar: an einen Mitarbeiter weiterleiten.", "weiterleiten": True}
        return None

    def book_appointment(self, **kwargs):
        """Buchen, idempotent, mit Umbuchung. Steht ein Kalenderdienst dahinter (crm.kalender_aktiv), ist die
        Buchung hier erst vorgemerkt: Der Server bestätigt sie sofort danach im Kalender (kalender.py) und erst
        dann wird ein bisheriger Termin abgesagt. Lehnt der Kalender ab, bleibt der alte Termin bestehen."""
        bestehend = self.crm.termin(self.lead_id, zukunft=True, bestaetigt=True)   # vergangene Termine bleiben Historie
        offen = self.crm.termin(self.lead_id, zukunft=True, unbestaetigt=True)
        slot_id = kwargs.get("slot_id")
        if bestehend and str(bestehend["slot_id"]) == str(slot_id):          # Wiederholung: idempotent
            self.booked = True
            if offen:                                                          # "doch der alte": die Vormerkung entfällt
                self.crm.termin_freigeben(offen["id"], verwerfen=True)
            return {"ok": True, "termin": f"{bestehend['start']:%d.%m.%Y %H:%M} Uhr", "art": bestehend["art"],
                    "wiederholt": True, "hinweis": "Dieser Termin war bereits gebucht."}
        if offen and str(offen["slot_id"]) == str(slot_id):                  # Wiederholung, Kalender hat noch nicht bestätigt
            self.booked = True
            return {"ok": True, "termin": f"{offen['start']:%d.%m.%Y %H:%M} Uhr", "art": offen["art"], "wiederholt": True,
                    "appointment_id": offen["id"], "hinweis": "Dieser Termin ist bereits vorgemerkt."}
        if bestehend:
            abgelehnt = self._kurzfristig(bestehend)                       # Umbuchung unterliegt demselben Mindestvorlauf
            if abgelehnt:
                return abgelehnt
        abgelehnt = self._slot_pruefen(slot_id)
        if abgelehnt:
            return abgelehnt
        result = super().book_appointment(**kwargs) if self.crm.qualifizierung_noetig else self._buchen_ohne_qualifizierung(**kwargs)
        if result.get("ok"):
            neu = self.crm.termin(self.lead_id, zukunft=True)                 # die eben angelegte Buchung (jüngste)
            if self.crm.kalender_aktiv and neu:
                result["appointment_id"] = neu["id"]
                self.crm.kalender_auftrag(neu["id"], "buchen", ersetzt=bestehend["id"] if bestehend else None,
                                          call_id=self.call_id)
                if offen and offen["id"] != neu["id"]:                       # zweite Umbuchung vor der Bestätigung der ersten
                    self.crm.termin_freigeben(offen["id"], verwerfen=True)
                if bestehend:
                    result["umgebucht_von"] = f"{bestehend['start']:%d.%m.%Y %H:%M} Uhr"
                    result["umbuchung_offen"] = True                         # Absage des alten Termins folgt mit der Bestätigung
            elif bestehend:                                                    # Umbuchung: alter Termin wird zur Absage
                self.crm.termin_stornieren(bestehend["id"], "Umbuchung", self.call_id)
                result["umgebucht_von"] = f"{bestehend['start']:%d.%m.%Y %H:%M} Uhr"
            if neu:
                self.crm.erinnerung_planen(neu)
        return result

    def cancel_appointment(self, **kwargs):
        """Termin absagen (z. B. Rückruf des Kunden): Slot wird frei, Absage bleibt als Historie.
        Innerhalb des Mindestvorlaufs (Techniker eingeplant) lehnt der Code ab; der Agent leitet weiter."""
        grund = str(kwargs.get("grund") or "Kunde hat abgesagt")[:200]
        bestehend = self.crm.termin(self.lead_id, zukunft=True)
        if not bestehend:
            schon = [st for st in self.crm.stornierungen(self.lead_id) if self.call_id and st.get("call_id") == self.call_id]
            if schon:                                                          # Wiederholung desselben Aufrufs: idempotent
                return {"ok": True, "abgesagt": f"{schon[-1]['start']:%d.%m.%Y %H:%M} Uhr", "grund": schon[-1]["grund"],
                        "wiederholt": True, "hinweis": "Der Termin war in diesem Gespräch bereits abgesagt."}
            return {"ok": False, "fehler": "Es gibt keinen bevorstehenden Termin, der abgesagt werden könnte."}
        abgelehnt = self._kurzfristig(bestehend)
        if abgelehnt:
            return abgelehnt
        # Umbuchungsfenster: der neue Termin ist vorgemerkt, der alte noch bestätigt; der Kunde meint beide
        weitere = self.crm.termin(self.lead_id, zukunft=True, bestaetigt=True) if bestehend["extern_status"] == "offen" else None
        if weitere and weitere["id"] != bestehend["id"]:
            abgelehnt = self._kurzfristig(weitere)
            if abgelehnt:
                return abgelehnt
        self.crm.termin_stornieren(bestehend["id"], grund, self.call_id)
        abgesagt = f"{bestehend['start']:%d.%m.%Y %H:%M} Uhr"
        if weitere and weitere["id"] != bestehend["id"]:
            self.crm.termin_stornieren(weitere["id"], grund, self.call_id)
            abgesagt += f" und {weitere['start']:%d.%m.%Y %H:%M} Uhr"
        return {"ok": True, "abgesagt": abgesagt, "grund": grund,
                "hinweis": "Bei Bedarf mit check_slots einen neuen Termin anbieten."}

    def verify_customer(self, **kwargs):
        """Identitätsprüfung über die Postleitzahl der Anschlussadresse; höchstens drei Versuche je Anruf."""
        call_id = self.call_id
        if not call_id:                                                       # ohne call.id gibt es keine Sitzung zum Verifizieren
            return {"ok": False, "verifiziert": False, "weiterleiten": True,
                    "fehler": "call_id fehlt: In telli die System Variable call.id im Tool-Body konfigurieren. "
                              "Ohne Sitzung keine Verifizierung, an einen Mitarbeiter weiterleiten."}
        versuche = self.crm.verifizierungsversuche(call_id, self.lead_id)
        if any(v["ok"] for v in versuche):
            return {"ok": True, "verifiziert": True, "hinweis": "Bereits verifiziert."}
        if len(versuche) >= 3:
            return {"ok": False, "verifiziert": False, "fehler": "Drei Versuche fehlgeschlagen. Keine weiteren Versuche: "
                                                                  "an einen Mitarbeiter weiterleiten.", "weiterleiten": True}
        genannt = "".join(ch for ch in str(kwargs.get("plz") or "") if ch.isdigit())
        lead = self.crm.lead(self.lead_id) or {}
        erwartet = "".join(ch for ch in str(lead.get("plz") or "") if ch.isdigit())
        if not erwartet:                                                      # kein Prüfmerkmal hinterlegt: nicht prüfbar
            return {"ok": False, "verifiziert": False, "weiterleiten": True,
                    "fehler": "Für diesen Kunden ist keine Postleitzahl hinterlegt, die Identität ist nicht prüfbar: "
                              "an einen Mitarbeiter weiterleiten."}
        ok = bool(genannt) and genannt == erwartet
        self.crm.verifizierung_merken(call_id, self.lead_id, ok)
        if ok:
            return {"ok": True, "verifiziert": True}
        rest = 3 - len(versuche) - 1
        return {"ok": False, "verifiziert": False, "fehler": "Die Postleitzahl passt nicht zu unseren Daten.",
                "verbleibende_versuche": rest, "weiterleiten": rest == 0}

    def create_ticket(self, **kwargs):
        """Störung oder Anliegen als Ticket aufnehmen; das Ticketsystem bekommt ein Ereignis über die Outbox."""
        kategorie = str(kwargs.get("kategorie") or "sonstiges").strip().lower()
        hinweis = "Ticketnummer dem Kunden vorlesen; die Technik meldet sich innerhalb eines Werktags."
        if kategorie not in TICKET_KATEGORIEN:
            hinweis = f"Kategorie '{kategorie}' unbekannt, als 'sonstiges' aufgenommen. " + hinweis
            kategorie = "sonstiges"
        text = str(kwargs.get("beschreibung") or "").strip()
        if not text:
            return {"ok": False, "fehler": "Eine Beschreibung fehlt. Was genau funktioniert nicht, seit wann?"}
        if self.call_id:                                                      # Wiederholung im selben Gespräch: dasselbe Ticket
            for t in self.crm.tickets(self.lead_id):
                if t["call_id"] == self.call_id and t["kategorie"] == kategorie and t["text"] == text[:2000]:
                    return {"ok": True, "ticket_nummer": f"T-{t['id']:05d}", "ticket_id": t["id"], "kategorie": kategorie,
                            "wiederholt": True, "hinweis": "Dieses Ticket wurde in diesem Gespräch bereits angelegt. " + hinweis}
        tid = self.crm.ticket_anlegen(self.lead_id, self.call_id, kategorie, text)
        return {"ok": True, "ticket_nummer": f"T-{tid:05d}", "ticket_id": tid, "kategorie": kategorie, "hinweis": hinweis}


class LocalCRM:
    def __init__(self, con, zeitzone: str = "Europe/Berlin", mindestvorlauf_h: int = 0, qualifizierung_noetig: bool = True,
                 uhr: Optional[Callable[[], datetime]] = None):
        self.con = con
        self.zeitzone = zeitzone
        self.tz = ZoneInfo(zeitzone)
        self.uhr = uhr or Uhr()                                       # zeitzonenbewusste UTC-Zeit; stellbar in Tests
        self.mindestvorlauf_h = mindestvorlauf_h                      # Termine innerhalb dieser Frist sind nicht änderbar
        self.qualifizierung_noetig = qualifizierung_noetig            # PV: Termine nur für qualifizierte Eigentümer
        self.kalender_aktiv = False                                   # True: ein Kalenderdienst bestätigt Buchungen (kalender.py)
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
        # Dialer-Zustand je telli-Kontakt (call_rescheduled, auto_dialer_status_changed) und Abgleich
        for spalte, typ in (("next_call_at", "TIMESTAMP"), ("dialer_status", "TEXT"), ("dialer_grund", "TEXT"),
                            ("dialer_am", "TIMESTAMP"), ("abgleich_am", "TIMESTAMP")):
            c.execute(f"ALTER TABLE telli_kontakte ADD COLUMN IF NOT EXISTS {spalte} {typ}")
        # Eingangszeit des Leads (für Speed-to-Lead); Seed-Leads von agent.init_db haben keine
        c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS erstellt_am TIMESTAMP")
        # liefert das CRM keine Eingangszeit, gilt die erste Sichtung durch die Integrationsschicht
        c.execute("UPDATE leads SET erstellt_am = ? WHERE erstellt_am IS NULL", [self.jetzt()])
        c.execute("""CREATE TABLE IF NOT EXISTS stornierungen (
            id INTEGER, lead_id INTEGER, slot_id INTEGER, start TIMESTAMP, grund TEXT, storniert_am TIMESTAMP)""")
        c.execute("ALTER TABLE stornierungen ADD COLUMN IF NOT EXISTS call_id TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY, lead_id INTEGER, call_id TEXT, kategorie TEXT, text TEXT, status TEXT, erstellt_am TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS verifizierungen (
            call_id TEXT, lead_id INTEGER, ok BOOLEAN, versuch_am TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS erinnerungen (
            id INTEGER PRIMARY KEY, appointment_id INTEGER, lead_id INTEGER, faellig_am TIMESTAMP, status TEXT,
            gesendet_am TIMESTAMP)""")
        # Spiegelung ins Kunden-CRM: in der Transaktion vorgemerkt, außerhalb der Sperre ausgeführt
        c.execute("""CREATE TABLE IF NOT EXISTS spiegel_auftraege (
            id INTEGER PRIMARY KEY, lead_id INTEGER, ziel_id TEXT, auftrag TEXT, status TEXT, versuche INTEGER,
            naechster_versuch TIMESTAMP, letzte_antwort TEXT, erstellt_am TIMESTAMP)""")
        # Kalenderdienst hinter den Slots (kalender.py): gespiegelte Zeiten, externe Buchungs-IDs, Aufträge
        c.execute("ALTER TABLE slots ADD COLUMN IF NOT EXISTS ende TIMESTAMP")
        c.execute("ALTER TABLE slots ADD COLUMN IF NOT EXISTS quelle TEXT")
        c.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS extern_id TEXT")
        c.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS extern_status TEXT")   # NULL: kein Kalender; offen; bestaetigt
        c.execute("ALTER TABLE stornierungen ADD COLUMN IF NOT EXISTS extern_id TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS kalender_auftraege (
            id INTEGER PRIMARY KEY, art TEXT, appointment_id INTEGER, lead_id INTEGER, extern_id TEXT, ersetzt INTEGER,
            call_id TEXT, grund TEXT, status TEXT, versuche INTEGER, naechster_versuch TIMESTAMP, letzte_antwort TEXT,
            erstellt_am TIMESTAMP)""")

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
        """Jetzt als Wanduhrzeit der Zone, so wie die Tabellen speichern. Einzige Zeitquelle dieser Klasse."""
        return self.wandzeit(self.uhr())

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

    def lead_anlegen(self, name: str, telefon: str, quelle: str, plz: Optional[str] = None) -> dict:
        """Unbekannter Anrufer (Inbound): als neuen Lead aufnehmen, damit Rückruf, Notiz und Protokoll einen Platz haben."""
        lid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM leads").fetchone()[0]
        self.con.execute("INSERT INTO leads (id, name, telefon, plz, quelle, status, erstellt_am) VALUES (?, ?, ?, ?, ?, 'offen', ?)",
                         [lid, name[:100], telefon_normalisieren(telefon), plz, quelle[:50], self.jetzt()])
        return self.lead(lid)

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
        jetzt = self.jetzt()
        self.con.executemany("INSERT INTO angebote VALUES (?, ?, ?)", [(lead_id, s, jetzt) for s in slot_ids])

    def angebotene_slots(self, lead_id: int) -> set[int]:
        rows = self.con.execute(
            "SELECT slot_id FROM angebote WHERE lead_id = ? AND angeboten_am > ?",
            [lead_id, self.jetzt() - timedelta(hours=ANGEBOT_GUELTIG_H)]).fetchall()
        return {r[0] for r in rows}

    def freie_slots(self, anzahl: int = 4) -> list[dict]:
        rows = self.con.execute(
            "SELECT id, start, ende FROM slots WHERE lead_id IS NULL AND start > ? ORDER BY start LIMIT ?",
            [self.jetzt(), anzahl]).fetchall()
        return [{"slot_id": r[0], "start": r[1], "ende": r[2]} for r in rows]

    def slot_per_start(self, start_utc: datetime) -> Optional[dict]:
        """Slot zu einem UTC-Zeitpunkt; die Tabelle speichert Wanduhrzeit."""
        row = self.con.execute("SELECT id, start, lead_id FROM slots WHERE start = ?", [self.wandzeit(start_utc)]).fetchone()
        return {"slot_id": row[0], "start": row[1], "lead_id": row[2]} if row else None

    def termin(self, lead_id: int, seit: Optional[datetime] = None, zukunft: bool = False, bestaetigt: bool = False,
               unbestaetigt: bool = False) -> Optional[dict]:
        """Jüngster Termin des Leads; `seit`: nur Buchungen ab diesem Zeitpunkt (Gesprächsbeginn),
        `zukunft`: nur Termine, die noch bevorstehen; `bestaetigt`: ohne Buchungen, die der Kalenderdienst noch
        bestätigen muss; `unbestaetigt`: nur diese. Ohne Kalenderdienst ist jede Buchung bestätigt."""
        sql = ("SELECT a.id, a.slot_id, s.start, a.art, a.gebucht_am, a.extern_id, a.extern_status FROM appointments a "
               "JOIN slots s ON s.id = a.slot_id WHERE a.lead_id = ?")
        params: list = [lead_id]
        if seit is not None:
            sql += " AND a.gebucht_am >= ?"
            params.append(self.wandzeit(seit))
        if zukunft:
            sql += " AND s.start > ?"
            params.append(self.jetzt())
        if bestaetigt:
            sql += " AND (a.extern_status IS NULL OR a.extern_status = 'bestaetigt')"
        if unbestaetigt:
            sql += " AND a.extern_status = 'offen'"
        row = self.con.execute(sql + " ORDER BY a.gebucht_am DESC, a.id DESC LIMIT 1", params).fetchone()
        if not row:
            return None
        return {"id": row[0], "slot_id": row[1], "start": row[2], "art": row[3], "gebucht_am": row[4], "extern_id": row[5],
                "extern_status": row[6]}

    def termin_freigeben(self, appointment_id: int, verwerfen: bool = False):
        """Buchung entfernen, Slot wieder frei, Erinnerung entfällt. `verwerfen`: offene Kalenderaufträge dazu
        verfallen, eine schon bestätigte Kalenderbuchung wird dort abgesagt."""
        row = self.con.execute("SELECT slot_id, extern_id, lead_id FROM appointments WHERE id = ?", [appointment_id]).fetchone()
        if row:
            self.con.execute("UPDATE slots SET lead_id = NULL WHERE id = ?", [row[0]])
            self.con.execute("DELETE FROM appointments WHERE id = ?", [appointment_id])
            self.con.execute("UPDATE erinnerungen SET status = 'entfallen' WHERE appointment_id = ? AND status = 'offen'",
                             [appointment_id])
            if verwerfen and self.kalender_aktiv:
                self.kalender_auftraege_verwerfen(appointment_id)
                if row[1]:
                    self.kalender_auftrag(appointment_id, "stornieren", extern_id=row[1], grund="Buchung verworfen", lead_id=row[2])

    def termin_stornieren(self, appointment_id: int, grund: str, call_id: Optional[str] = None):
        row = self.con.execute("SELECT a.lead_id, a.slot_id, s.start, a.extern_id FROM appointments a JOIN slots s ON s.id = a.slot_id "
                               "WHERE a.id = ?", [appointment_id]).fetchone()
        if not row:
            return
        sid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM stornierungen").fetchone()[0]
        self.con.execute("INSERT INTO stornierungen (id, lead_id, slot_id, start, grund, storniert_am, call_id, extern_id) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [sid, row[0], row[1], row[2], grund[:200], self.jetzt(), call_id, row[3]])
        if self.kalender_aktiv:
            self.kalender_auftraege_verwerfen(appointment_id)
            if row[3]:
                self.kalender_auftrag(appointment_id, "stornieren", extern_id=row[3], grund=grund, call_id=call_id, lead_id=row[0])
        self.termin_freigeben(appointment_id)

    def termin_bestaetigen(self, appointment_id: int, extern_id: str):
        self.con.execute("UPDATE appointments SET extern_id = ?, extern_status = 'bestaetigt' WHERE id = ?",
                         [extern_id, appointment_id])

    def stornierungen(self, lead_id: int, seit: Optional[datetime] = None) -> list[dict]:
        sql = "SELECT id, slot_id, start, grund, storniert_am, call_id, extern_id FROM stornierungen WHERE lead_id = ?"
        params: list = [lead_id]
        if seit is not None:
            sql += " AND storniert_am >= ?"
            params.append(self.wandzeit(seit))
        rows = self.con.execute(sql + " ORDER BY id", params).fetchall()
        return [dict(zip(["id", "slot_id", "start", "grund", "storniert_am", "call_id", "extern_id"], r)) for r in rows]

    # ------------------------------------------------------------ Kalenderdienst: gespiegelte Zeiten und Aufträge
    def slots_spiegeln(self, zeiten: list[tuple[datetime, datetime]]) -> dict:
        """Freie Zeiten des Kalenderdienstes (UTC, Start und Ende) in die Slot-Tabelle übernehmen.

        Gebuchte Slots bleiben unberührt. Freie Slots, die der Kalender nicht mehr anbietet, verschwinden,
        auch die Beispiel-Slots des Prototyps: Ist ein Kalender konfiguriert, ist er die einzige Quelle.
        """
        jetzt = self.jetzt()
        extern = {self.wandzeit(s): self.wandzeit(e) for s, e in zeiten if self.wandzeit(s) > jetzt}
        vorhanden = {r[1]: (r[0], r[2]) for r in self.con.execute(
            "SELECT id, start, lead_id FROM slots WHERE start > ?", [jetzt]).fetchall()}
        neu = entfernt = 0
        for start, ende in extern.items():
            if start in vorhanden:
                self.con.execute("UPDATE slots SET ende = ?, quelle = 'kalender' WHERE id = ?", [ende, vorhanden[start][0]])
            else:
                sid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM slots").fetchone()[0]
                self.con.execute("INSERT INTO slots (id, start, lead_id, ende, quelle) VALUES (?, ?, NULL, ?, 'kalender')",
                                 [sid, start, ende])
                neu += 1
        for start, (sid, lead_id) in vorhanden.items():
            if start not in extern and lead_id is None:                      # Angebotsvermerke bleiben: eine Buchung des
                self.con.execute("DELETE FROM slots WHERE id = ?", [sid])     # verschwundenen Slots heißt dann "inzwischen vergeben"
                entfernt += 1
        frei, gebucht = self.con.execute(
            "SELECT COUNT(*) FILTER (WHERE lead_id IS NULL), COUNT(*) FILTER (WHERE lead_id IS NOT NULL) FROM slots WHERE start > ?",
            [jetzt]).fetchone()
        return {"neu": neu, "entfernt": entfernt, "frei": frei, "gebucht": gebucht}

    def slot_entfernen(self, slot_id: int):
        """Der Kalender hat die Zeit inzwischen vergeben: Slot nicht mehr anbieten."""
        self.con.execute("DELETE FROM slots WHERE id = ? AND lead_id IS NULL", [slot_id])

    def kalender_auftrag(self, appointment_id: Optional[int], art: str, extern_id: Optional[str] = None,
                         ersetzt: Optional[int] = None, call_id: Optional[str] = None, grund: Optional[str] = None,
                         lead_id: Optional[int] = None) -> int:
        """Auftrag an den Kalenderdienst vormerken (buchen | stornieren); ausgeführt in kalender.py ohne Sperre."""
        if lead_id is None and appointment_id is not None:
            row = self.con.execute("SELECT lead_id FROM appointments WHERE id = ?", [appointment_id]).fetchone()
            lead_id = row[0] if row else None
        if art == "buchen":
            self.con.execute("UPDATE appointments SET extern_status = 'offen' WHERE id = ?", [appointment_id])
        kid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM kalender_auftraege").fetchone()[0]
        jetzt = self.jetzt()
        # Buchungen bestätigt der Server sofort selbst (kalender.py); der Planer übernimmt erst nach einer Karenz
        faellig = jetzt + timedelta(seconds=KALENDER_KARENZ_S) if art == "buchen" else jetzt
        self.con.execute("INSERT INTO kalender_auftraege VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'offen', 0, ?, NULL, ?)",
                         [kid, art, appointment_id, lead_id, extern_id, ersetzt, call_id, (grund or "")[:200], faellig, jetzt])
        return kid

    def kalender_auftraege_verwerfen(self, appointment_id: int):
        """Buchungsaufträge zu einem Termin, der nicht mehr gilt: offene verfallen; einer, der gerade läuft (HTTP ohne
        Sperre), wird als verworfen markiert und storniert seine Buchung selbst, sobald er zurück ist (kalender.py)."""
        self.con.execute("UPDATE kalender_auftraege SET status = CASE WHEN status = 'laeuft' THEN 'verworfen_laeuft' ELSE 'verworfen' END "
                         "WHERE appointment_id = ? AND art = 'buchen' AND status IN ('offen', 'laeuft')", [appointment_id])

    def kalender_auftrag_status(self, auftrag_id: int) -> Optional[str]:
        row = self.con.execute("SELECT status FROM kalender_auftraege WHERE id = ?", [auftrag_id]).fetchone()
        return row[0] if row else None

    def kalender_auftrag_fuer_termin(self, appointment_id: int) -> Optional[dict]:
        """Jüngster Buchungsauftrag zu einem Termin, egal in welchem Status (für die Sofortbestätigung im Server)."""
        row = self.con.execute(f"SELECT {', '.join(self.KALENDER_SPALTEN)} FROM kalender_auftraege WHERE appointment_id = ? "
                               "AND art = 'buchen' ORDER BY id DESC LIMIT 1", [appointment_id]).fetchone()
        return dict(zip(self.KALENDER_SPALTEN, row)) if row else None

    KALENDER_SPALTEN = ["id", "art", "appointment_id", "lead_id", "extern_id", "ersetzt", "call_id", "grund", "status",
                        "versuche", "naechster_versuch", "letzte_antwort"]

    def kalender_auftraege_faellig(self, max_n: int = 50, appointment_id: Optional[int] = None) -> list[dict]:
        sql = f"SELECT {', '.join(self.KALENDER_SPALTEN)} FROM kalender_auftraege WHERE status = 'offen'"
        params: list = []
        if appointment_id is not None:
            sql += " AND appointment_id = ? AND art = 'buchen'"
            params.append(appointment_id)
        else:
            sql += " AND naechster_versuch <= ?"
            params.append(self.jetzt())
        rows = self.con.execute(sql + " ORDER BY id LIMIT ?", [*params, max_n]).fetchall()
        return [dict(zip(self.KALENDER_SPALTEN, r)) for r in rows]

    def kalender_auftrag_beanspruchen(self, auftrag_id: int) -> bool:
        """Unter der Sperre: nur ein Ausführender je Auftrag (Server-Sofortbestätigung oder Planer)."""
        if self.con.execute("SELECT status FROM kalender_auftraege WHERE id = ?", [auftrag_id]).fetchone()[0] != "offen":
            return False
        self.con.execute("UPDATE kalender_auftraege SET status = 'laeuft' WHERE id = ?", [auftrag_id])
        return True

    def kalender_auftrag_abschliessen(self, auftrag_id: int, status: str, antwort: str, versuche: int,
                                      naechster: Optional[datetime] = None):
        self.con.execute("UPDATE kalender_auftraege SET status = ?, versuche = ?, letzte_antwort = ?, naechster_versuch = ? WHERE id = ?",
                         [status, versuche, antwort[:300], self.wandzeit(naechster) if naechster else self.jetzt(), auftrag_id])

    def kalender_status(self) -> dict:
        return {r[0]: r[1] for r in self.con.execute("SELECT status, COUNT(*) FROM kalender_auftraege GROUP BY 1").fetchall()}

    # ------------------------------------------------------------ Tickets, Verifizierung (Inbound-Service)
    def ticket_anlegen(self, lead_id: int, call_id: Optional[str], kategorie: str, text: str) -> int:
        tid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM tickets").fetchone()[0]
        self.con.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, 'offen', ?)",
                         [tid, lead_id, call_id, kategorie[:50], text[:2000], self.jetzt()])
        return tid

    def tickets(self, lead_id: int, seit: Optional[datetime] = None) -> list[dict]:
        sql = "SELECT id, call_id, kategorie, text, status, erstellt_am FROM tickets WHERE lead_id = ?"
        params: list = [lead_id]
        if seit is not None:
            sql += " AND erstellt_am >= ?"
            params.append(self.wandzeit(seit))
        rows = self.con.execute(sql + " ORDER BY id", params).fetchall()
        return [dict(zip(["id", "call_id", "kategorie", "text", "status", "erstellt_am"], r)) for r in rows]

    def verifizierung_merken(self, call_id: str, lead_id: int, ok: bool):
        self.con.execute("INSERT INTO verifizierungen VALUES (?, ?, ?, ?)", [call_id, lead_id, ok, self.jetzt()])

    def verifizierungsversuche(self, call_id: str, lead_id: int) -> list[dict]:
        rows = self.con.execute("SELECT ok, versuch_am FROM verifizierungen WHERE call_id = ? AND lead_id = ? ORDER BY versuch_am",
                                [call_id, lead_id]).fetchall()
        return [{"ok": r[0], "versuch_am": r[1]} for r in rows]

    def verifiziert(self, call_id: Optional[str], lead_id: int) -> bool:
        if not call_id:
            return False
        return self.con.execute("SELECT COUNT(*) FROM verifizierungen WHERE call_id = ? AND lead_id = ? AND ok",
                                [call_id, lead_id]).fetchone()[0] > 0

    # ------------------------------------------------------------ Erinnerungen (24 h vor dem Termin)
    def erinnerung_planen(self, termin: dict, vorlauf_h: int = ERINNERUNG_VORLAUF_H) -> Optional[datetime]:
        """Eine Erinnerung je Termin; bei Umbuchung entfällt die alte (termin_freigeben) und eine neue entsteht.

        Fällig ist sie `vorlauf_h` vor dem Termin. Liegt dieser Zeitpunkt bei der Buchung schon zurück
        (kurzfristiger Termin), rückt sie auf zwei Stunden vor den Termin; liegt auch das zurück, gibt es
        keine: Die Bestätigung im Gespräch war gerade erst. Rückgabe: Fälligkeit oder None.
        """
        if self.con.execute("SELECT COUNT(*) FROM erinnerungen WHERE appointment_id = ? AND status = 'offen'",
                            [termin["id"]]).fetchone()[0]:
            return None
        jetzt = self.jetzt()
        faellig = termin["start"] - timedelta(hours=vorlauf_h)
        if faellig <= jetzt:
            faellig = termin["start"] - timedelta(hours=ERINNERUNG_KURZFRIST_H)
        if faellig <= jetzt:
            return None
        lead_id = self.con.execute("SELECT lead_id FROM appointments WHERE id = ?", [termin["id"]]).fetchone()[0]
        eid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM erinnerungen").fetchone()[0]
        self.con.execute("INSERT INTO erinnerungen VALUES (?, ?, ?, ?, 'offen', NULL)", [eid, termin["id"], lead_id, faellig])
        return faellig

    def erinnerungen_faellig(self) -> list[dict]:
        jetzt = self.jetzt()
        rows = self.con.execute(
            "SELECT e.id, e.lead_id, a.id, s.start, a.art FROM erinnerungen e JOIN appointments a ON a.id = e.appointment_id "
            "JOIN slots s ON s.id = a.slot_id WHERE e.status = 'offen' AND e.faellig_am <= ? "
            "AND s.start > ? ORDER BY e.id", [jetzt, jetzt]).fetchall()
        return [dict(zip(["id", "lead_id", "appointment_id", "start", "art"], r)) for r in rows]

    def erinnerung_gesendet(self, erinnerung_id: int):
        self.con.execute("UPDATE erinnerungen SET status = 'gesendet', gesendet_am = ? WHERE id = ?",
                         [self.jetzt(), erinnerung_id])

    def erinnerungen(self, lead_id: int) -> list[dict]:
        rows = self.con.execute("SELECT id, appointment_id, faellig_am, status FROM erinnerungen WHERE lead_id = ? ORDER BY id",
                                [lead_id]).fetchall()
        return [dict(zip(["id", "appointment_id", "faellig_am", "status"], r)) for r in rows]

    # ------------------------------------------------------------ Notizen, Aufgaben, Sperrliste
    def notiz(self, lead_id: Any, text: str) -> int:
        nid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM notizen").fetchone()[0]
        self.con.execute("INSERT INTO notizen VALUES (?, ?, ?, ?)", [nid, int(lead_id), text[:4000], self.jetzt()])
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
        self.con.execute("INSERT OR REPLACE INTO sperrliste VALUES (?, ?, ?)", [nummer, grund[:200], self.jetzt()])

    def gesperrt(self, telefon: str) -> bool:
        nummer = telefon_normalisieren(telefon)
        return bool(nummer) and self.con.execute(
            "SELECT COUNT(*) FROM sperrliste WHERE telefon = ?", [nummer]).fetchone()[0] > 0

    # ------------------------------------------------------------ telli-Kontakte (Lead-Push)
    def telli_kontakt_speichern(self, lead_id: int, contact_id: Optional[str], loop_id: Optional[str] = None):
        if self.con.execute("SELECT COUNT(*) FROM telli_kontakte WHERE lead_id = ?", [lead_id]).fetchone()[0]:
            self.con.execute("UPDATE telli_kontakte SET contact_id = ?, loop_id = ? WHERE lead_id = ?",
                             [contact_id, loop_id, lead_id])
        else:
            self.con.execute("INSERT INTO telli_kontakte (lead_id, contact_id, loop_id, erstellt_am) VALUES (?, ?, ?, ?)",
                             [lead_id, contact_id, loop_id, self.jetzt()])

    def telli_kontakt(self, lead_id: int) -> Optional[dict]:
        row = self.con.execute("SELECT contact_id, loop_id, next_call_at, dialer_status, dialer_grund FROM telli_kontakte "
                               "WHERE lead_id = ?", [lead_id]).fetchone()
        return {"contact_id": row[0], "loop_id": row[1], "next_call_at": row[2], "dialer_status": row[3],
                "dialer_grund": row[4]} if row else None

    def lead_per_telli_kontakt(self, contact_id: Optional[str]) -> Optional[dict]:
        if not contact_id:
            return None
        row = self.con.execute("SELECT lead_id FROM telli_kontakte WHERE contact_id = ?", [str(contact_id)]).fetchone()
        return self.lead(row[0]) if row else None

    def _telli_zeile_sichern(self, lead_id: int, contact_id: Optional[str], loop_id: Optional[str]):
        """Zeile anlegen, falls der Kontakt nicht aus dem Lead-Push stammt; ohne loop_id gilt 'unbekannt',
        damit der Lead-Push keinen zweiten Anrufzyklus für einen Kontakt plant, den der Dialer schon bearbeitet."""
        if not self.con.execute("SELECT COUNT(*) FROM telli_kontakte WHERE lead_id = ?", [lead_id]).fetchone()[0]:
            self.con.execute("INSERT INTO telli_kontakte (lead_id, contact_id, loop_id, erstellt_am) VALUES (?, ?, ?, ?)",
                             [lead_id, contact_id, loop_id or "unbekannt", self.jetzt()])

    def anruf_geplant_setzen(self, lead_id: int, next_call_at: Optional[datetime], loop_id: Optional[str] = None,
                             contact_id: Optional[str] = None):
        self._telli_zeile_sichern(lead_id, contact_id, loop_id)
        self.con.execute("UPDATE telli_kontakte SET next_call_at = ?, loop_id = COALESCE(?, loop_id), "
                         "contact_id = COALESCE(contact_id, ?) WHERE lead_id = ?",
                         [self.wandzeit(next_call_at), loop_id, contact_id, lead_id])

    def dialer_status_setzen(self, lead_id: int, status: Optional[str], grund: Optional[str], contact_id: Optional[str] = None):
        self._telli_zeile_sichern(lead_id, contact_id, None)
        self.con.execute("UPDATE telli_kontakte SET dialer_status = ?, dialer_grund = ?, dialer_am = ?, "
                         "contact_id = COALESCE(contact_id, ?) WHERE lead_id = ?", [status, grund, self.jetzt(), contact_id, lead_id])

    def abgleich_kandidaten(self, mindestalter_min: int = 60, max_alter_tage: int = 14, max_n: int = 50) -> list[dict]:
        """Leads mit telli-Kontakt, deren Anrufe seit mindestens `mindestalter_min` nicht abgeglichen wurden."""
        jetzt = self.jetzt()
        rows = self.con.execute(
            "SELECT lead_id, contact_id FROM telli_kontakte WHERE contact_id IS NOT NULL AND erstellt_am >= ? "
            "AND (abgleich_am IS NULL OR abgleich_am <= ?) ORDER BY COALESCE(abgleich_am, erstellt_am) LIMIT ?",
            [jetzt - timedelta(days=max_alter_tage), jetzt - timedelta(minutes=mindestalter_min), max_n]).fetchall()
        return [{"lead_id": r[0], "contact_id": r[1]} for r in rows]

    def abgleich_merken(self, lead_id: int):
        self.con.execute("UPDATE telli_kontakte SET abgleich_am = ? WHERE lead_id = ?", [self.jetzt(), lead_id])

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
        jetzt = self.jetzt()
        self.con.execute("INSERT INTO spiegel_auftraege VALUES (?, ?, ?, ?, 'offen', 0, ?, NULL, ?)",
                         [sid, lead_id, str(ziel_id), json.dumps(auftrag, ensure_ascii=False, default=str), jetzt, jetzt])
        return sid

    def spiegel_faellig(self, max_n: int = 50) -> list[dict]:
        rows = self.con.execute(
            "SELECT id, lead_id, ziel_id, auftrag, versuche FROM spiegel_auftraege "
            "WHERE status = 'offen' AND naechster_versuch <= ? ORDER BY id LIMIT ?", [self.jetzt(), max_n]).fetchall()
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
        self.con.execute("INSERT OR IGNORE INTO webhook_eingang VALUES (?, ?, ?, ?)",
                         [message_id, call_id, ereignis, self.jetzt()])

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
