"""Tageslauf je Mandant: Angebotsablauf, Mahnvorschläge, Vertragsabrechnungen, Bewertungsanfragen.

Läuft per Cron (`python -m core tagesaufgaben`, 06:30) oder aus der Oberfläche (`POST /tagesaufgaben`). Alles, was
finanz- oder kundenwirksam ist, entsteht nur als Freigabe; der Lauf selbst verschickt nichts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from core import angebote, domaene, forderungen, vertraege
from core.modelle import Mandant


def tagesaufgaben(s: Session, m: Mandant, jetzt: datetime) -> dict[str, Any]:
    heute = jetzt.date()
    ergebnis = {"angebote_abgelaufen": angebote.ablauf_pruefen(s, m, heute),
                "mahnvorschlaege": forderungen.mahnlauf(s, m, heute),
                "vertragsabrechnungen": vertraege.abrechnungslauf(s, m, heute)}
    domaene.protokollieren(s, m.id, "tageslauf", "mandant", m.id, "system",
                           {k: (len(v) if isinstance(v, list) else v) for k, v in ergebnis.items()})
    return ergebnis
