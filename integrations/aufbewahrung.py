"""Aufbewahrung und Betroffenenrechte: Fristen anwenden, Auskunft geben, löschen.

Was die Integrationsschicht speichert, sind personenbezogene Daten: Transkripte, Notizen, Tickets,
Telefonnummern in Ereignissen. Die Checkliste (02, Abschnitt F) verlangt festgelegte Fristen; dieses
Modul setzt sie um, täglich im Planer, und beantwortet die zwei Anfragen, die im Betrieb sicher
kommen: "Was habt ihr über mich?" (Art. 15 DSGVO) und "Löscht mich" (Art. 17).

Grundsätze:
- Kennzahlen bleiben, Inhalte gehen. Ein Anruf bleibt als Zeile mit Ergebnis, Flags und Dauer in
  `calls` (Wochenreport, Hochfahr-Kriterien), das Transkript wird nach der Frist entfernt.
- Löschen heißt pseudonymisieren, nicht spurlos. Die Lead-Zeile bleibt mit Status `geloescht`
  und ohne Name, Nummer, Postleitzahl und Fakten, damit Fremdschlüssel (Anrufe, Termine) nicht
  ins Leere zeigen; ein Eintrag in `loeschungen` hält Zeitpunkt, Grund und einen Hash der Nummer
  fest (Rechenschaftspflicht, Art. 5 Abs. 2), nie die Nummer selbst.
- Die Sperrliste bleibt. Wer gelöscht werden will, will auch nicht noch einmal angerufen werden;
  die normalisierte Nummer bleibt als Werbesperre stehen (berechtigtes Interesse, § 7 UWG).
- Das Kunden-CRM ist das führende System. Was dort steht, löscht der Kunde dort; dieses Modul
  liefert dafür die Liste der Ziel-IDs (crm_id) mit.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from integrations.modelle import iso_utc, telefon_normalisieren
from integrations.uhr import Uhr


@dataclass
class Fristen:
    """Tage. 0 = nie anwenden. Vorgaben sind ein Vorschlag für den Pilot; der Kunde legt sie im Scoping fest."""
    transkripte: int = 30         # Transkript im Anrufprotokoll (Review-Fenster: täglich im Pilot, wöchentlich danach)
    tickets: int = 90             # Beschreibung der Störung in Kundenworten
    verifizierungen: int = 30     # Verifizierungsversuche je Anruf (enthalten keine PLZ, aber Call-IDs)
    zustellungen: int = 30        # erledigte Outbox-, Spiegel- und Kalenderaufträge (enthalten Ereignis-Bodys)
    webhooks: int = 90            # Dedup-Tabelle der eingegangenen Webhooks
    erinnerungen: int = 90        # gesendete oder entfallene Erinnerungen
    protokoll: int = 365          # Anrufzeilen ohne Transkript (Kennzahlen), danach weg

    @classmethod
    def aus_konfig(cls, konfig) -> "Fristen":
        return cls(**{k: int(v) for k, v in (konfig.fristen or {}).items() if k in cls.__dataclass_fields__})

    def als_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Bericht:
    zeit: str
    entfernt: dict[str, int] = field(default_factory=dict)


class Aufbewahrung:
    def __init__(self, crm, fristen: Optional[Fristen] = None, uhr: Optional[Uhr] = None, sperre=None, pepper: str = ""):
        self.crm, self.fristen = crm, fristen or Fristen()
        self.uhr = uhr or crm.uhr
        self.sperre = sperre or nullcontext()
        self.pepper = pepper                   # geheimer Schlüssel für das Pseudonym im Löschprotokoll (DATENSCHUTZ_PEPPER)
        self.letzter_lauf: Optional[datetime] = None
        crm.con.execute("""CREATE TABLE IF NOT EXISTS loeschungen (
            id INTEGER PRIMARY KEY, nummer_hash TEXT, lead_id INTEGER, grund TEXT, zeit TIMESTAMP, umfang TEXT)""")

    # ------------------------------------------------------------ Fristen
    def anwenden(self) -> dict[str, Any]:
        """Alle Fristen einmal anwenden. Idempotent: ein zweiter Lauf findet nichts mehr."""
        f, con = self.fristen, self.crm.con
        jetzt = self.crm.jetzt()
        bericht: dict[str, int] = {}

        def grenze(tage: int) -> datetime:
            return jetzt - timedelta(days=tage)

        def zaehlen(sql: str, params: list) -> int:
            return con.execute(sql, params).fetchone()[0]

        with self.sperre, self.crm.transaktion():
            if f.transkripte:
                n = zaehlen("SELECT COUNT(*) FROM calls WHERE transkript IS NOT NULL AND COALESCE(ende, start) < ?", [grenze(f.transkripte)])
                con.execute("UPDATE calls SET transkript = NULL WHERE transkript IS NOT NULL AND COALESCE(ende, start) < ?", [grenze(f.transkripte)])
                bericht["transkripte"] = n
            if f.tickets:
                n = zaehlen("SELECT COUNT(*) FROM tickets WHERE text IS NOT NULL AND text <> '' AND erstellt_am < ?", [grenze(f.tickets)])
                con.execute("UPDATE tickets SET text = '' WHERE erstellt_am < ?", [grenze(f.tickets)])
                bericht["tickets"] = n
            if f.verifizierungen:
                n = zaehlen("SELECT COUNT(*) FROM verifizierungen WHERE versuch_am < ?", [grenze(f.verifizierungen)])
                con.execute("DELETE FROM verifizierungen WHERE versuch_am < ?", [grenze(f.verifizierungen)])
                bericht["verifizierungen"] = n
            if f.zustellungen:
                g = grenze(f.zustellungen)
                n = 0
                if self._tabelle("outbox"):
                    # die Outbox rechnet in naiver UTC
                    g_utc = self.uhr().astimezone(timezone.utc).replace(tzinfo=None) - timedelta(days=f.zustellungen)
                    n += zaehlen("SELECT COUNT(*) FROM outbox WHERE status = 'zugestellt' AND zugestellt_am < ?", [g_utc])
                    con.execute("DELETE FROM outbox WHERE status = 'zugestellt' AND zugestellt_am < ?", [g_utc])
                n += zaehlen("SELECT COUNT(*) FROM spiegel_auftraege WHERE status = 'erledigt' AND erstellt_am < ?", [g])
                con.execute("DELETE FROM spiegel_auftraege WHERE status = 'erledigt' AND erstellt_am < ?", [g])
                n += zaehlen("SELECT COUNT(*) FROM kalender_auftraege WHERE status NOT IN ('offen', 'laeuft', 'verworfen_laeuft') AND erstellt_am < ?", [g])
                con.execute("DELETE FROM kalender_auftraege WHERE status NOT IN ('offen', 'laeuft', 'verworfen_laeuft') AND erstellt_am < ?", [g])
                bericht["zustellungen"] = n
            if f.webhooks:
                n = zaehlen("SELECT COUNT(*) FROM webhook_eingang WHERE empfangen_am < ?", [grenze(f.webhooks)])
                con.execute("DELETE FROM webhook_eingang WHERE empfangen_am < ?", [grenze(f.webhooks)])
                bericht["webhooks"] = n
            if f.erinnerungen:
                n = zaehlen("SELECT COUNT(*) FROM erinnerungen WHERE status <> 'offen' AND faellig_am < ?", [grenze(f.erinnerungen)])
                con.execute("DELETE FROM erinnerungen WHERE status <> 'offen' AND faellig_am < ?", [grenze(f.erinnerungen)])
                bericht["erinnerungen"] = n
            if f.protokoll:
                n = zaehlen("SELECT COUNT(*) FROM calls WHERE COALESCE(ende, start) < ?", [grenze(f.protokoll)])
                con.execute("DELETE FROM calls WHERE COALESCE(ende, start) < ?", [grenze(f.protokoll)])
                bericht["protokoll"] = n
        self.letzter_lauf = self.uhr()
        return {"zeit": iso_utc(self.letzter_lauf), "entfernt": bericht, "fristen_tage": self.fristen.als_dict()}

    def tick(self, erzwingen: bool = False, takt_s: int = 86400) -> Optional[dict[str, Any]]:
        """Für den Planer: höchstens einmal je Takt (Standard täglich)."""
        if erzwingen or self.letzter_lauf is None or (self.uhr() - self.letzter_lauf).total_seconds() >= takt_s:
            return self.anwenden()
        return None

    def _tabelle(self, name: str) -> bool:
        return self.crm.con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0] > 0

    # ------------------------------------------------------------ Betroffenenrechte
    def _lead(self, telefon: str) -> Optional[dict]:
        return self.crm.lead_per_telefon(telefon)

    def auskunft(self, telefon: str) -> dict[str, Any]:
        """Art. 15: alles, was zu dieser Nummer gespeichert ist, als ein JSON-Dokument."""
        nummer = telefon_normalisieren(telefon)
        con = self.crm.con
        with self.sperre:
            lead = self._lead(telefon)
            aus: dict[str, Any] = {"telefon": nummer, "stand": iso_utc(self.uhr()), "gesperrt": self.crm.gesperrt(telefon),
                                   "lead": lead}
            if lead is None:
                return aus
            lid = lead["id"]
            aus["anrufe"] = [dict(zip(["id", "start", "ende", "ergebnis", "turns", "flags", "transkript"], r)) for r in con.execute(
                "SELECT id, start, ende, ergebnis, turns, flags, transkript FROM calls WHERE lead_id = ? ORDER BY id", [lid]).fetchall()]
            for a in aus["anrufe"]:
                a["flags"] = json.loads(a["flags"] or "[]")
                a["transkript"] = json.loads(a["transkript"]) if a["transkript"] else None
            aus["notizen"] = self.crm.notizen(lid)
            aus["aufgaben"] = self.crm.aufgaben(lid)
            aus["termine"] = [dict(zip(["id", "start", "art", "gebucht_am", "extern_id"], r)) for r in con.execute(
                "SELECT a.id, s.start, a.art, a.gebucht_am, a.extern_id FROM appointments a JOIN slots s ON s.id = a.slot_id "
                "WHERE a.lead_id = ? ORDER BY a.id", [lid]).fetchall()]
            aus["absagen"] = self.crm.stornierungen(lid)
            aus["tickets"] = self.crm.tickets(lid)
            aus["erinnerungen"] = self.crm.erinnerungen(lid)
            aus["plattform_kontakt"] = self.crm.telli_kontakt(lid)
            aus["kunden_crm_id"] = lead.get("crm_id")
        return json.loads(json.dumps(aus, ensure_ascii=False, default=str))

    def loeschen(self, telefon: str, grund: str = "Löschersuchen der betroffenen Person") -> dict[str, Any]:
        """Art. 17: Inhalte dieser Person entfernen, Lead pseudonymisieren, Nummer auf der Sperrliste lassen.

        Rückgabe: was entfernt wurde und was der Kunde in seinen eigenen Systemen noch löschen muss
        (CRM-Kontakt, Kalenderbuchung, Plattform-Kontakt mit Aufzeichnungen).
        """
        nummer = telefon_normalisieren(telefon)
        con = self.crm.con
        with self.sperre, self.crm.transaktion():
            lead = self._lead(telefon)
            self.crm.sperren(telefon, "Löschersuchen: nicht mehr anrufen")
            if lead is None:
                self._protokollieren(nummer, None, grund, {"sperrliste": 1})
                return {"telefon": nummer, "lead": None, "sperrliste": True, "entfernt": {},
                        "beim_kunden_zu_loeschen": []}
            lid = lead["id"]
            umfang: dict[str, int] = {}

            def weg(tabelle: str, bedingung: str = "lead_id = ?", params: Optional[list] = None):
                p = params if params is not None else [lid]
                umfang[tabelle] = con.execute(f"SELECT COUNT(*) FROM {tabelle} WHERE {bedingung}", p).fetchone()[0]
                con.execute(f"DELETE FROM {tabelle} WHERE {bedingung}", p)

            offene_termine = [dict(zip(["id", "extern_id"], r)) for r in con.execute(
                "SELECT a.id, a.extern_id FROM appointments a JOIN slots s ON s.id = a.slot_id WHERE a.lead_id = ? AND s.start > ?",
                [lid, self.crm.jetzt()]).fetchall()]
            for t in offene_termine:                                              # Slot wieder frei, Kalender wird benachrichtigt
                self.crm.termin_stornieren(t["id"], "Löschersuchen", None)
            umfang["termine_abgesagt"] = len(offene_termine)
            umfang["transkripte"] = con.execute("SELECT COUNT(*) FROM calls WHERE lead_id = ? AND transkript IS NOT NULL", [lid]).fetchone()[0]
            con.execute("UPDATE calls SET transkript = NULL WHERE lead_id = ?", [lid])
            weg("notizen"); weg("aufgaben"); weg("tickets"); weg("verifizierungen"); weg("erinnerungen"); weg("angebote")
            weg("stornierungen"); weg("appointments")
            plattform = self.crm.telli_kontakt(lid)
            weg("telli_kontakte")
            weg("spiegel_auftraege")
            weg("kalender_auftraege", "lead_id = ? AND status NOT IN ('offen', 'laeuft')")
            if self._tabelle("outbox"):
                weg("outbox", "body LIKE ?", [f'%"lead_id":{lid},%'])
            if self._tabelle("nachfass"):                                      # Kanal-Fallback (nachfass.py)
                weg("nachfass")
                con.execute("UPDATE leads SET einwilligung_kanaele = NULL WHERE id = ?", [lid])
            con.execute("UPDATE leads SET name = 'gelöscht', telefon = NULL, plz = NULL, quelle = NULL, status = 'geloescht', "
                        "eigentuemer = NULL, gebaeudetyp = NULL, dachflaeche_m2 = NULL, jahresverbrauch_kwh = NULL, notiz = NULL "
                        "WHERE id = ?", [lid])
            umfang["lead_pseudonymisiert"] = 1
            self._protokollieren(nummer, lid, grund, umfang)
            beim_kunden = []
            if lead.get("crm_id"):
                beim_kunden.append({"system": "Kunden-CRM", "id": lead["crm_id"]})
            if plattform and plattform.get("contact_id"):
                beim_kunden.append({"system": "telli (Kontakt, Anrufe, Aufzeichnungen)", "id": plattform["contact_id"]})
            for t in offene_termine:
                if t["extern_id"]:
                    beim_kunden.append({"system": "Kalenderdienst (Buchung, Absage läuft über den Planer)", "id": t["extern_id"]})
        return {"telefon": nummer, "lead": lid, "sperrliste": True, "entfernt": umfang, "beim_kunden_zu_loeschen": beim_kunden}

    def _protokollieren(self, nummer: str, lead_id: Optional[int], grund: str, umfang: dict):
        con = self.crm.con
        lid = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM loeschungen").fetchone()[0]
        con.execute("INSERT INTO loeschungen VALUES (?, ?, ?, ?, ?, ?)",
                    [lid, nummer_hash(nummer, self.pepper), lead_id, grund[:200], self.crm.jetzt(), json.dumps(umfang)])

    def loeschungen(self) -> list[dict]:
        rows = self.crm.con.execute("SELECT id, nummer_hash, lead_id, grund, zeit, umfang FROM loeschungen ORDER BY id").fetchall()
        return [{"id": r[0], "nummer_hash": r[1], "lead_id": r[2], "grund": r[3], "zeit": r[4], "umfang": json.loads(r[5])} for r in rows]


def nummer_hash(nummer: str, pepper: str = "") -> str:
    """Pseudonym einer Telefonnummer für das Löschprotokoll: nachprüfbar, wer die Nummer kennt.

    Mit `pepper` (DATENSCHUTZ_PEPPER, ein geheimer Schlüssel) ist es ein HMAC und ohne den Schlüssel nicht
    durchprobierbar; ohne Pepper ist es ein einfacher Hash, den man über den Nummernraum durchrechnen kann."""
    nummer = telefon_normalisieren(nummer)
    if pepper:
        return hmac.new(pepper.encode(), nummer.encode(), hashlib.sha256).hexdigest()[:24]
    return hashlib.sha256(("ai-call-agent:" + nummer).encode()).hexdigest()[:24]
