"""Tageslauf je Mandant: Angebotsablauf, Mahnvorschläge, Vertragsabrechnungen, Terminerinnerungen (Portal-Link).

Läuft per Cron (`python -m core tagesaufgaben`, 06:30) oder aus der Oberfläche (`POST /tagesaufgaben`). Alles, was
finanz- oder kundenwirksam ist, entsteht nur als Freigabe; der Lauf selbst verschickt nichts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from core import (
    angebote,
    branchen,
    domaene,
    forderungen,
    hinweise,
    lebenszyklus,
    portal,
    qualifikationen,
    upsell,
    vertraege,
    warteliste,
    zaehler,
)
from core.modelle import Mandant


def _qualifikationen_melden(s: Session, m: Mandant, heute) -> int:
    """Ablaufende/abgelaufene Qualifikationen montags an den Inhaber melden (Push/E-Mail laut Hinweisziel)."""
    ablaufend = qualifikationen.ablaufende(s, m, heute)
    if ablaufend and heute.weekday() == 0:
        text = "; ".join(f"{w['name']}: {w['bezeichnung']} ({'abgelaufen' if w['status'] == 'abgelaufen' else 'läuft ab'} {w['ablauf']})" for w in ablaufend[:6])
        hinweise.senden(s, m, "qualifikation", f"{len(ablaufend)} Qualifikation(en) laufen ab", text, f"qualifikationen:{heute.isoformat()}", "qualifikationen")
    return len(ablaufend)


def tagesaufgaben(s: Session, m: Mandant, jetzt: datetime) -> dict[str, Any]:
    heute = jetzt.date()
    ergebnis = {"angebote_abgelaufen": angebote.ablauf_pruefen(s, m, heute),
                "mahnvorschlaege": forderungen.mahnlauf(s, m, heute),
                "vertragsabrechnungen": vertraege.abrechnungslauf(s, m, heute),
                "terminerinnerungen": portal.erinnerungen(s, m, jetzt),
                "wiederkehr": branchen.wiederkehr(s, m, heute),
                "warteliste_verfallen": len(warteliste.ablauf_pruefen(s, m, jetzt)),
                "erneuerungen": lebenszyklus.erinnerungen(s, m, heute),
                "qualifikationen_ablaufend": _qualifikationen_melden(s, m, heute),
                "verbrauch_auffaellig": len(zaehler.auffaellige(s, m)),
                "upsell_kampagne": upsell.kampagne(s, m, jetzt)}
    domaene.protokollieren(s, m.id, "tageslauf", "mandant", m.id, "system",
                           {k: (len(v) if isinstance(v, list) else v) for k, v in ergebnis.items()})
    return ergebnis
