"""Ausgehende Webhooks an Kundensysteme (Ticketsystem, Automationsplattform, Data Warehouse).

Transaktionales Outbox-Muster: Ein Ereignis wird in derselben Datenbank gespeichert wie die
Zustandsänderung, die es auslöst, und danach zugestellt. Geht die Zustellung schief, bleibt es
liegen und wird nach einem festen Plan wiederholt. Das gibt At-least-once-Zustellung; der
Empfänger dedupliziert über den Header Idempotency-Key (gleich der Ereignis-ID).

Der Wiederholungsplan entspricht dem von telli dokumentierten: sofort, 5 s, 5 min, 30 min, 2 h,
5 h, 10 h, 10 h, dann tot. Jede Nachricht ist Svix-kompatibel signiert (siehe signatur.py),
damit der Kunde für telli-Webhooks und für diese nur ein Verifikationsmuster braucht.
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx

from integrations.modelle import Ereignis
from integrations.signatur import SignaturFehler, svix_kopfzeilen

RETRY_PLAN_S = [0, 5, 300, 1800, 7200, 18000, 36000, 36000]
SPALTEN = ["id", "ereignis_id", "typ", "url", "body", "versuche", "naechster_versuch", "status", "letzte_antwort",
           "erstellt_am", "zugestellt_am"]


class Outbox:
    """Zeitstempel in der Tabelle sind naive UTC; die Outbox vergleicht nur mit ihrer eigenen Uhr."""

    def __init__(self, con, url: str, secret: str, client: Optional[httpx.Client] = None,
                 jetzt: Optional[Callable[[], datetime]] = None, timeout: float = 10.0):
        self.con, self.url, self.secret = con, url, secret
        self.client = client or httpx.Client(timeout=timeout)
        self.jetzt = jetzt or (lambda: datetime.now(timezone.utc))
        if secret:
            try:
                svix_kopfzeilen(secret, "pruefung", b"", 0)        # ungültiges Geheimnis fällt beim Start auf, nicht im Betrieb
            except SignaturFehler as e:
                raise ValueError(f"KUNDE_WEBHOOK_SECRET unbrauchbar: {e}") from e
        self.con.execute("""CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, ereignis_id TEXT, typ TEXT, url TEXT, body TEXT, versuche INTEGER,
            naechster_versuch TIMESTAMP, status TEXT, letzte_antwort TEXT, erstellt_am TIMESTAMP, zugestellt_am TIMESTAMP)""")

    def _naiv(self) -> datetime:
        return self.jetzt().astimezone(timezone.utc).replace(tzinfo=None)

    def einreihen(self, ereignis: Ereignis, url: Optional[str] = None) -> int:
        oid = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM outbox").fetchone()[0]
        jetzt = self._naiv()
        self.con.execute("INSERT INTO outbox VALUES (?, ?, ?, ?, ?, 0, ?, 'offen', NULL, ?, NULL)",
                         [oid, ereignis.id, ereignis.typ, url or self.url, ereignis.model_dump_json(), jetzt, jetzt])
        return oid

    def faellig(self, max_n: int = 50) -> list[dict]:
        rows = self.con.execute(
            f"SELECT {', '.join(SPALTEN)} FROM outbox WHERE status = 'offen' AND naechster_versuch <= ? ORDER BY id LIMIT ?",
            [self._naiv(), max_n]).fetchall()
        return [dict(zip(SPALTEN, r)) for r in rows]

    def zustellen(self, max_n: int = 50, sperre=None) -> dict:
        """Alle fälligen Einträge einmal versuchen. Rückgabe: Zähler je Ausgang.

        `sperre` schützt die geteilte Datenbankverbindung; die HTTP-Zustellung selbst läuft ohne
        Sperre, damit Tool-Aufrufe der Plattform nicht hinter einem langsamen Empfänger warten.
        """
        sperre = sperre or nullcontext()
        bericht = {"zugestellt": 0, "wiederholen": 0, "tot": 0}
        with sperre:
            faellig = self.faellig(max_n)
        for eintrag in faellig:
            body = eintrag["body"].encode()
            ts = int(self.jetzt().timestamp())
            kopf = {"Content-Type": "application/json", "Idempotency-Key": eintrag["ereignis_id"],
                    "X-Ereignis-Typ": eintrag["typ"]}
            if self.secret:
                kopf.update(svix_kopfzeilen(self.secret, eintrag["ereignis_id"], body, ts))
            try:
                r = self.client.post(eintrag["url"], content=body, headers=kopf)
                antwort, ok = f"HTTP {r.status_code}", 200 <= r.status_code < 300
            except Exception as e:  # noqa: BLE001 - auch ein Transportfehler ist ein Fehlversuch mit Backoff, keine Endlosschleife
                antwort, ok = f"{type(e).__name__}: {str(e)[:150]}", False
            versuche = eintrag["versuche"] + 1
            with sperre:
                if ok:
                    self.con.execute("UPDATE outbox SET status = 'zugestellt', versuche = ?, letzte_antwort = ?, zugestellt_am = ? WHERE id = ?",
                                     [versuche, antwort, self._naiv(), eintrag["id"]])
                    bericht["zugestellt"] += 1
                elif versuche >= len(RETRY_PLAN_S):
                    self.con.execute("UPDATE outbox SET status = 'tot', versuche = ?, letzte_antwort = ? WHERE id = ?",
                                     [versuche, antwort, eintrag["id"]])
                    bericht["tot"] += 1
                else:
                    naechster = self._naiv() + timedelta(seconds=RETRY_PLAN_S[versuche])
                    self.con.execute("UPDATE outbox SET versuche = ?, letzte_antwort = ?, naechster_versuch = ? WHERE id = ?",
                                     [versuche, antwort, naechster, eintrag["id"]])
                    bericht["wiederholen"] += 1
        return bericht

    def eintraege(self, status: Optional[str] = None) -> list[dict]:
        sql = f"SELECT {', '.join(SPALTEN)} FROM outbox"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        return [dict(zip(SPALTEN, r)) for r in self.con.execute(sql + " ORDER BY id", params).fetchall()]

    def erneut(self, outbox_id: int):
        """Toten Eintrag wieder in die Zustellung nehmen (nach Behebung beim Empfänger)."""
        self.con.execute("UPDATE outbox SET status = 'offen', versuche = 0, naechster_versuch = ? WHERE id = ?",
                         [self._naiv(), outbox_id])

    def status(self) -> dict:
        rows = self.con.execute("SELECT status, COUNT(*) FROM outbox GROUP BY 1").fetchall()
        return {r[0]: r[1] for r in rows}


def ereignis_json(eintrag: dict) -> dict:
    return json.loads(eintrag["body"])
