"""Deterministische Fachregeln vor jedem LLM: Rufnummern, Dringlichkeit, Sicherheitsfälle.

Regeln laufen vor der Modellentscheidung, damit sicherheitsrelevante Fälle (Gasgeruch,
Wasserrohrbruch) nie von einer Modellantwort abhängen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from core.ontologie import ANLAGENTYPEN

HEIZPERIODE = {10, 11, 12, 1, 2, 3, 4}


@dataclass(frozen=True)
class Bewertung:
    dringlichkeit: str
    sicherheitshinweis: str | None = None
    grund: str = ""


SICHERHEIT = [
    (re.compile(r"gas\s?geruch|riech\w*.{0,20}nach gas|nach gas riech", re.I),
     "Bitte verlassen Sie sofort das Gebäude, betätigen Sie keine elektrischen Schalter und rufen Sie "
     "von draußen den Entstörungsdienst Ihres Gasnetzbetreibers oder die 112 an."),
    (re.compile(r"\bco\b|kohlenmonoxid|abgas.*(warn|melder)", re.I),
     "Bitte verlassen Sie sofort die Räume, lüften Sie nur, wenn es gefahrlos möglich ist, und rufen Sie die 112 an."),
]
NOTDIENST = re.compile(r"rohrbruch|wasser (läuft|tritt) aus|überschwemm|steht unter wasser", re.I)
AUSFALL = re.compile(r"(heizung|therme|kessel|wärmepumpe).{0,30}(aus|kalt|geht nicht|defekt|störung)|kein warm(es )?wasser|kalt in der wohnung", re.I)


def telefon_normalisieren(nummer: str | None) -> str | None:
    """Deutsche Rufnummern auf E.164 (+49…); None bei unbrauchbarer Eingabe."""
    if not nummer:
        return None
    ziffern = re.sub(r"[^\d+]", "", nummer)
    if ziffern.startswith("00"):
        ziffern = "+" + ziffern[2:]
    elif ziffern.startswith("0"):
        ziffern = "+49" + ziffern[1:]
    elif not ziffern.startswith("+"):
        return None
    return ziffern if 8 <= len(ziffern) <= 16 else None


def dringlichkeit_bewerten(text: str, monat: int, angegeben: str | None = None) -> Bewertung:
    for muster, hinweis in SICHERHEIT:
        if muster.search(text):
            return Bewertung("notdienst", hinweis, "Sicherheitsfall")
    if NOTDIENST.search(text):
        return Bewertung("notdienst", None, "Wasserschaden")
    if AUSFALL.search(text):
        return Bewertung("hoch" if monat in HEIZPERIODE else "normal", None,
                         "Ausfall in der Heizperiode" if monat in HEIZPERIODE else "Ausfall außerhalb der Heizperiode")
    return Bewertung(angegeben if angegeben in {"hoch", "normal", "niedrig"} else "normal", None, "keine Regel")


def qualifikation_fuer(anlagentyp: str | None) -> str | None:
    return ANLAGENTYPEN[anlagentyp].qualifikation if anlagentyp in ANLAGENTYPEN else None
