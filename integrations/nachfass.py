"""Kanal-Fallback: Wer am Telefon nicht erreicht wird, bekommt eine Nachricht mit Buchungslink.

Der Auto-Dialer der Plattform gibt nach mehreren Versuchen auf (auto_dialer_status_changed mit
reason scheduler_gave_up_*). Bisher entstand daraus nur eine Aufgabe für den Vertrieb. Ein Teil
dieser Leads ist nie erreichbar, wenn angerufen wird, bucht aber per SMS oder WhatsApp. Diese
Schicht macht aus dem Anruf deshalb eine Kundeninteraktion über mehrere Kanäle, ohne dass das
Kundensystem dafür etwas Neues bauen muss: Sie meldet ein Ereignis `nachricht.nachfassen` über
dieselbe signierte Outbox wie alle anderen Ereignisse; das SMS-Gateway oder die
Automationsplattform des Kunden stellt zu.

Was der Code erzwingt, statt es dem Kundensystem zu überlassen:

- **Einwilligung.** Ohne ausdrückliche Einwilligung in den Kanal (Formularhäkchen, im CRM
  vermerkt) keine Nachricht. Eine Telefon-Einwilligung gilt nicht für SMS (UWG § 7).
- **Sperrliste schlägt alles.** Ein Opt-out am Telefon sperrt auch die Nachricht.
- **Höchstens eine Nachricht je Lead im Sperrfenster** (Standard 14 Tage), auch wenn die
  Plattform das Ereignis wiederholt oder der Lead erneut in den Dialer kommt.
- **Zustellfenster.** Außerhalb der erlaubten Zeiten (Standard Mo bis Sa, 9 bis 20 Uhr) wird
  die Nachricht für den nächsten erlaubten Zeitpunkt geplant, nicht nachts verschickt.
- **Nur offene Leads.** Wer inzwischen gebucht hat oder disqualifiziert ist, bekommt nichts.

Die Entscheidung steht mit Grund im CRM (Notiz) und in der Tabelle `nachfass`, damit das
Reporting die Rückgewinnungsquote je Kanal messen kann.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

KANAELE = ("sms", "whatsapp")
STANDARD_VORLAGE = ("Hallo {vorname}, wir haben Sie leider telefonisch nicht erreicht. "
                    "Ihren Beratungstermin bei {kunde} können Sie hier selbst wählen: {link} "
                    "Antworten Sie STOP, wenn Sie keine Nachrichten mehr möchten.")


@dataclass
class Nachfassregel:
    kanal: str = "sms"
    kunde: str = "SonnenWerk Energie"
    link: str = ""                                   # Self-Service-Buchung, z. B. Cal.com-Seite
    vorlage: str = STANDARD_VORLAGE
    sperrfenster_tage: int = 14
    wochentage: tuple[int, ...] = (0, 1, 2, 3, 4, 5)  # Montag = 0
    von: time = time(9, 0)
    bis: time = time(20, 0)
    zeitzone: str = "Europe/Berlin"
    status_erlaubt: tuple[str, ...] = field(default=("offen", "in_qualifizierung"))

    def __post_init__(self):
        if self.kanal not in KANAELE:
            raise ValueError(f"Unbekannter Kanal '{self.kanal}'. Erlaubt: {', '.join(KANAELE)}")
        if not self.link:
            raise ValueError("Nachfassen ohne Buchungslink ergibt keinen Sinn: NACHFASS_LINK setzen")
        if self.von >= self.bis:
            raise ValueError("Zustellfenster: von muss vor bis liegen")

    def naechster_zeitpunkt(self, jetzt_utc: datetime) -> datetime:
        """Frühester erlaubter Zustellzeitpunkt ab jetzt, zeitzonenbewusst zurück in UTC."""
        tz = ZoneInfo(self.zeitzone)
        lokal = jetzt_utc.astimezone(tz)
        for tag in range(8):
            kandidat_tag = (lokal + timedelta(days=tag)).date()
            if kandidat_tag.weekday() not in self.wochentage:
                continue
            beginn = datetime.combine(kandidat_tag, self.von, tz)
            ende = datetime.combine(kandidat_tag, self.bis, tz)
            if tag == 0 and lokal >= ende:
                continue
            return max(beginn, lokal).astimezone(jetzt_utc.tzinfo)
        raise ValueError("Zustellfenster ohne erlaubten Wochentag")

    def text(self, lead: dict) -> str:
        vorname = (lead.get("name") or "").split(" ")[0] or "Hallo"
        return self.vorlage.format(vorname=vorname, kunde=self.kunde, link=self.link)


class Nachfass:
    """Entscheidet und plant den Kanal-Fallback. Läuft innerhalb der Transaktion der Nachbereitung."""

    def __init__(self, crm, regel: Nachfassregel):
        self.crm, self.regel = crm, regel
        crm.con.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS einwilligung_kanaele TEXT")
        crm.con.execute("""CREATE TABLE IF NOT EXISTS nachfass (
            id INTEGER PRIMARY KEY, lead_id INTEGER, kanal TEXT, grund TEXT, geplant_am TIMESTAMP, erstellt_am TIMESTAMP)""")

    # ------------------------------------------------------------ Einwilligung
    def einwilligung_setzen(self, lead_id: int, kanaele: list[str]):
        unbekannt = [k for k in kanaele if k not in KANAELE]
        if unbekannt:
            raise ValueError(f"Unbekannte Kanäle: {unbekannt}")
        self.crm.con.execute("UPDATE leads SET einwilligung_kanaele = ? WHERE id = ?", [",".join(sorted(set(kanaele))), lead_id])

    def einwilligungen(self, lead_id: int) -> set[str]:
        row = self.crm.con.execute("SELECT einwilligung_kanaele FROM leads WHERE id = ?", [lead_id]).fetchone()
        return set(filter(None, (row[0] or "").split(","))) if row else set()

    def verlauf(self, lead_id: int) -> list[dict]:
        rows = self.crm.con.execute("SELECT id, kanal, grund, geplant_am FROM nachfass WHERE lead_id = ? ORDER BY id",
                                    [lead_id]).fetchall()
        return [dict(zip(["id", "kanal", "grund", "geplant_am"], r)) for r in rows]

    # ------------------------------------------------------------ Entscheidung
    def pruefen(self, lead: dict, jetzt_utc: datetime) -> Optional[str]:
        """None, wenn nachgefasst werden darf; sonst der Grund, warum nicht."""
        r = self.regel
        aktuell = self.crm.lead(lead["id"]) or lead
        if aktuell.get("status") not in r.status_erlaubt:
            return f"status_{aktuell.get('status')}"
        if self.crm.gesperrt(aktuell.get("telefon") or ""):
            return "gesperrt"
        if r.kanal not in self.einwilligungen(aktuell["id"]):
            return "keine_einwilligung"
        grenze = self.crm.wandzeit(jetzt_utc - timedelta(days=r.sperrfenster_tage))
        if self.crm.con.execute("SELECT COUNT(*) FROM nachfass WHERE lead_id = ? AND erstellt_am >= ?",
                                [aktuell["id"], grenze]).fetchone()[0]:
            return "bereits_nachgefasst"
        return None

    def planen(self, lead: dict, grund: str, jetzt_utc: datetime) -> dict[str, Any]:
        """Prüft und merkt die Nachricht vor. Rückgabe: Entscheidung plus Ereignisdaten für die Outbox."""
        abgelehnt = self.pruefen(lead, jetzt_utc)
        if abgelehnt:
            return {"nachfass": "uebersprungen", "grund": abgelehnt}
        r = self.regel
        senden_ab = r.naechster_zeitpunkt(jetzt_utc)
        nid = self.crm.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM nachfass").fetchone()[0]
        self.crm.con.execute("INSERT INTO nachfass VALUES (?, ?, ?, ?, ?, ?)",
                             [nid, lead["id"], r.kanal, grund, self.crm.wandzeit(senden_ab), self.crm.wandzeit(jetzt_utc)])
        self.crm.notiz(lead["id"], f"Kanal-Fallback: {r.kanal.upper()} mit Buchungslink geplant ({grund}).")
        return {"nachfass": "geplant", "nachfass_id": nid, "kanal": r.kanal, "senden_ab": senden_ab,
                "telefon": lead.get("telefon"), "text": r.text(lead), "link": r.link}

    def kennzahlen(self) -> dict[str, Any]:
        """Wie viele nie erreichte Leads über den Fallback doch noch gebucht haben."""
        geplant, gebucht = self.crm.con.execute("""
            SELECT COUNT(DISTINCT n.lead_id),
                   COUNT(DISTINCT CASE WHEN a.lead_id IS NOT NULL THEN n.lead_id END)
            FROM nachfass n LEFT JOIN appointments a ON a.lead_id = n.lead_id AND a.gebucht_am >= n.erstellt_am""").fetchone()
        return {"nachgefasst": geplant, "danach_gebucht": gebucht,
                "rueckgewinnungsquote": round(gebucht / geplant, 3) if geplant else None}
