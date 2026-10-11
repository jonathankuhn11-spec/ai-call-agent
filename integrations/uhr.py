"""Die eine Uhr der Integrationsschicht.

Jeder Zeitvergleich der Schicht läuft über eine Uhr, die beim Aufbau der App übergeben wird:
freie Slots, fällige Erinnerungen, der Mindestvorlauf vor einem Termin, das Gesprächsfenster der
Nachbereitung, die Wiederholungen von Outbox und Spiegelung, der Replay-Schutz der Signaturen.
Produktiv ist das die Systemzeit in UTC. In den Tests steht sie fest und wird von Hand vorgestellt:
Ein Test, der prüft, dass eine Erinnerung 24 Stunden vor dem Termin fällig wird, stellt die Uhr
vor, statt Zeitstempel in der Datenbank zu verbiegen. Die Testsuite läuft damit an jedem Wochentag
und zu jeder Uhrzeit identisch, auch am Sonntagabend, in der Nacht und in der Nacht der
Zeitumstellung.

Warum keine Datenbankzeit (now() in DuckDB)? Sie lässt sich nicht stellen, und die Tabellen
speichern Wanduhrzeit der konfigurierten Zone (crm.py). Die Uhr liefert einen zeitzonenbewussten
UTC-Zeitpunkt; was in die Datenbank geschrieben oder mit ihr verglichen wird, rechnet LocalCRM in
Wanduhrzeit um. Ein Wert, zwei Darstellungen, eine Quelle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Union

from integrations.modelle import iso_lesen


class Uhr:
    """Systemzeit in UTC, zeitzonenbewusst. Aufrufbar wie eine Funktion."""

    def __call__(self) -> datetime:
        return datetime.now(timezone.utc)

    def sekunden(self) -> float:
        """Unix-Zeit, für Signaturzeitstempel (Svix) und deren Toleranzprüfung."""
        return self().timestamp()


class Testuhr(Uhr):
    """Steht fest und läuft nur, wenn man sie stellt. Für Tests und reproduzierbare Demos."""

    __test__ = False                        # kein Testfall, trotz des Namens (pytest-Sammlung)

    def __init__(self, start: Union[datetime, str]):
        self.stellen(start)

    def __call__(self) -> datetime:
        return self._jetzt

    def stellen(self, zeitpunkt: Union[datetime, str]) -> datetime:
        if isinstance(zeitpunkt, str):
            zeitpunkt = iso_lesen(zeitpunkt)
        if zeitpunkt.tzinfo is None:
            raise ValueError("Die Testuhr braucht einen zeitzonenbewussten Zeitpunkt (z. B. '2026-10-13T08:00:00Z').")
        self._jetzt = zeitpunkt.astimezone(timezone.utc)
        return self._jetzt

    def vor(self, **delta) -> datetime:
        """Uhr vorstellen: uhr.vor(hours=24). Negative Werte stellen zurück."""
        self._jetzt += timedelta(**delta)
        return self._jetzt
