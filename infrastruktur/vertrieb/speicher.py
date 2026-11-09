"""Pipeline-Speicher: SQLite-Datei mit Leads, Kontaktereignissen und Briefchargen.

Leads werden bei jedem Lauf aus den Listen neu aufgebaut (Upsert über die stabile Lead-ID); Ereignisse und Chargen
sind die einzige Stelle, an der Vertriebshistorie entsteht, und werden nie überschrieben. Die Datei liegt außerhalb
des Repositorys (personenbezogene Daten). Auf dem Netzlaufwerk /mnt/project-files wird sie nicht direkt beschrieben:
der Agent arbeitet auf einer lokalen Kopie und spiegelt sie am Ende zurück (siehe vertriebsagent.py).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY, firma TEXT NOT NULL, werte TEXT NOT NULL, probleme TEXT NOT NULL, score REAL, akut INTEGER,
    gesperrt INTEGER, sperrgrund TEXT, ausgeschlossen TEXT, herkunft TEXT, erstmals TEXT, aktualisiert TEXT
);
CREATE TABLE IF NOT EXISTS ereignisse (
    nr INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT NOT NULL, typ TEXT NOT NULL, datum TEXT NOT NULL,
    notiz TEXT DEFAULT '', quelle TEXT DEFAULT '', erfasst TEXT NOT NULL,
    UNIQUE (lead_id, typ, datum)
);
CREATE TABLE IF NOT EXISTS chargen (
    id TEXT PRIMARY KEY, erstellt TEXT NOT NULL, status TEXT NOT NULL, art TEXT NOT NULL, parameter TEXT
);
CREATE TABLE IF NOT EXISTS chargen_leads (
    charge_id TEXT NOT NULL, lead_id TEXT NOT NULL, rang INTEGER, prioritaet REAL, auswahl TEXT, freigabe TEXT DEFAULT '',
    PRIMARY KEY (charge_id, lead_id)
);
CREATE TABLE IF NOT EXISTS sperrliste (
    schluessel TEXT PRIMARY KEY, lead_id TEXT, grund TEXT, datum TEXT
);
CREATE INDEX IF NOT EXISTS ix_ereignisse_lead ON ereignisse (lead_id, datum);
"""

# Ereignistypen und ihre Stufe im Trichter. "positiv" zählt als Erfolg im Response-Modell.
EREIGNISSE = {
    "brief_1": "Erstbrief versandt",
    "brief_2": "Nachfassbrief versandt",
    "unzustellbar": "Brief unzustellbar zurück",
    "antwort_positiv": "Rückmeldung mit Interesse (Rückruf/Termin erbeten)",
    "antwort_negativ": "Rückmeldung ohne Interesse",
    "widerspruch": "Werbewiderspruch (Sperre, Art. 21 DSGVO)",
    "einwilligung": "Einwilligung Telefon/E-Mail dokumentiert",
    "termin": "Termin/Vorführung vereinbart",
    "demo": "Vorführung durchgeführt",
    "angebot": "Angebot versandt",
    "pilot": "Pilot vereinbart",
    "abschluss": "Vertrag geschlossen",
    "absage": "Absage nach Gespräch/Angebot",
}
POSITIV = {"antwort_positiv", "einwilligung", "termin", "demo", "angebot", "pilot", "abschluss"}
NEGATIV = {"antwort_negativ", "widerspruch", "absage"}
KONTAKT = {"brief_1", "brief_2"}


def oeffnen(pfad: Path) -> sqlite3.Connection:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(pfad)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def leads_schreiben(db: sqlite3.Connection, leads, jetzt: str) -> None:
    gesperrt = {r["lead_id"] for r in db.execute("SELECT lead_id FROM sperrliste")}
    for le in leads:
        g = le.gesperrt or le.id in gesperrt
        db.execute("""INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(id) DO UPDATE SET firma=excluded.firma, werte=excluded.werte, probleme=excluded.probleme,
                      score=excluded.score, akut=excluded.akut, gesperrt=MAX(leads.gesperrt, excluded.gesperrt),
                      sperrgrund=COALESCE(NULLIF(leads.sperrgrund,''), excluded.sperrgrund), ausgeschlossen=excluded.ausgeschlossen,
                      herkunft=excluded.herkunft, aktualisiert=excluded.aktualisiert""",
                   (le.id, le.firma, json.dumps(le.werte, ensure_ascii=False), ",".join(le.probleme), le.score, int(le.akut), int(g),
                    le.sperrgrund, le.ausgeschlossen, json.dumps(le.herkunft, ensure_ascii=False), le.erstmals, jetzt))
    db.commit()


def ereignis(db: sqlite3.Connection, lead_id: str, typ: str, datum: str, notiz: str = "", quelle: str = "") -> bool:
    if typ not in EREIGNISSE:
        raise ValueError(f"unbekannter Ereignistyp {typ!r}; erlaubt: {', '.join(EREIGNISSE)}")
    if not db.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
        raise KeyError(f"Lead {lead_id} unbekannt")
    cur = db.execute("INSERT OR IGNORE INTO ereignisse (lead_id, typ, datum, notiz, quelle, erfasst) VALUES (?,?,?,?,?,?)",
                     (lead_id, typ, datum, notiz, quelle, dt.datetime.now().isoformat(timespec="seconds")))
    if typ in ("widerspruch", "antwort_negativ"):   # erkennbar entgegenstehender Wille, § 7 Abs. 1 S. 2 UWG
        sperren(db, lead_id, f"{EREIGNISSE[typ].split(' (')[0]} {datum}" + (f": {notiz}" if notiz else ""), datum)
    return cur.rowcount > 0


def sperren(db: sqlite3.Connection, lead_id: str, grund: str, datum: str) -> None:
    """Dauerhafte Sperre: überlebt jeden Neuaufbau der Leads (Schlüssel = Lead-ID und Rufnummer)."""
    db.execute("INSERT OR REPLACE INTO sperrliste VALUES (?,?,?,?)", (lead_id, lead_id, grund, datum))
    row = db.execute("SELECT werte FROM leads WHERE id=?", (lead_id,)).fetchone()
    if row:
        from vertrieb.erfassung import telefon_norm
        tel = telefon_norm(json.loads(row["werte"]).get("telefon", ""))
        if tel:
            db.execute("INSERT OR REPLACE INTO sperrliste VALUES (?,?,?,?)", ("t:" + tel, lead_id, grund, datum))
    db.execute("UPDATE leads SET gesperrt=1, sperrgrund=? WHERE id=?", (grund, lead_id))


def sperre_auf_rufnummer(db: sqlite3.Connection) -> None:
    """Überträgt Sperren auf neu entstandene Lead-IDs mit derselben Rufnummer (z. B. nach Korrektur der Firmierung)."""
    from vertrieb.erfassung import telefon_norm
    sperren_t = {r["schluessel"][2:]: r["grund"] for r in db.execute("SELECT * FROM sperrliste WHERE schluessel LIKE 't:%'")}
    if not sperren_t:
        return
    for r in db.execute("SELECT id, werte FROM leads WHERE gesperrt=0").fetchall():
        t = telefon_norm(json.loads(r["werte"]).get("telefon", ""))
        if t in sperren_t:
            db.execute("UPDATE leads SET gesperrt=1, sperrgrund=? WHERE id=?", (sperren_t[t], r["id"]))
    db.commit()


def historie(db: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    h: dict[str, list[sqlite3.Row]] = {}
    for r in db.execute("SELECT * FROM ereignisse ORDER BY datum, nr"):
        h.setdefault(r["lead_id"], []).append(r)
    return h
