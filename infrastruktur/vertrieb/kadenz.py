"""Nachfass-Agent: leitet aus der Ereignishistorie je Lead den Kadenzstatus und die nächste fällige Aufgabe ab.

Kadenz (Fristen in katalog.toml, [kadenz]):
    Erstbrief ──14 T.──▶ Nachfassbrief ──35 T.──▶ Kadenzende, Wiedervorlage nach 120 T.
    jede positive Rückmeldung ──▶ Rückruf binnen 24 h (Einwilligung liegt mit der Rückmeldung vor)
    Termin ──▶ Vorführung vorbereiten (Kundenpaket erzeugen) ──▶ Angebot ──7 T.──▶ Angebot nachfassen
Rechtlicher Rahmen: Erst- und Nachfasskontakt nur per Brief (§ 7 Abs. 1 UWG, zulässig ohne erkennbaren
Widerspruch). Telefon, E-Mail und WhatsApp erst nach dokumentierter Rückmeldung bzw. Einwilligung (§ 7 Abs. 2
Nr. 1 und 2 UWG). Jede ablehnende Rückmeldung oder ein Widerspruch sperrt den Lead dauerhaft.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class Stand:
    status: str
    aufgabe: str = ""
    faellig: dt.date | None = None
    kanal: str = ""
    letzte: str = ""


def _d(text: str) -> dt.date:
    return dt.date.fromisoformat(text[:10])


def stand(ereignisse, gesperrt: bool, kadenz: dict, heute: dt.date, sperrgrund: str = "") -> Stand:
    letzte = {}
    for e in ereignisse:
        letzte[e["typ"]] = _d(e["datum"])
    zuletzt = max(letzte.values()) if letzte else None
    zl = zuletzt.isoformat() if zuletzt else ""
    if gesperrt or "widerspruch" in letzte or "antwort_negativ" in letzte:
        return Stand("gesperrt", letzte=sperrgrund or zl)
    if "abschluss" in letzte:
        return Stand("Kunde", "Onboarding starten (Unterlagen: build.py --unterlagen --kunde …)", letzte["abschluss"], "Termin", zl)
    if "pilot" in letzte:
        return Stand("Pilot", "Pilot betreuen, Abnahme vorbereiten", letzte["pilot"] + dt.timedelta(weeks=8), "Termin", zl)
    if "absage" in letzte:
        return Stand("Absage", "Wiedervorlage (nur mit Einwilligung)", letzte["absage"] + dt.timedelta(days=kadenz["wiedervorlage_nach_tagen"]), "", zl)
    if "angebot" in letzte:
        return Stand("Angebot offen", "Angebot nachfassen", letzte["angebot"] + dt.timedelta(days=kadenz["angebot_nachfassen_nach_tagen"]),
                     "Telefon (Einwilligung)", zl)
    if "demo" in letzte:
        return Stand("Vorführung erfolgt", "Angebot erstellen und senden", letzte["demo"] + dt.timedelta(days=2), "E-Mail (Einwilligung)", zl)
    if "termin" in letzte:
        return Stand("Termin vereinbart", "Vorführung vorbereiten: Kundenpaket erzeugen (paketagent.py --firma …)",
                     letzte["termin"] - dt.timedelta(days=1), "Termin", zl)
    if "antwort_positiv" in letzte or "einwilligung" in letzte:
        d = max(letzte.get("antwort_positiv", dt.date.min), letzte.get("einwilligung", dt.date.min))
        return Stand("Rückmeldung positiv", f"Rückruf binnen {kadenz['rueckruf_binnen_stunden']} h, Termin anbieten",
                     d + dt.timedelta(days=1), "Telefon (Einwilligung)", zl)
    if "unzustellbar" in letzte:
        return Stand("unzustellbar", "Anschrift prüfen und korrigieren", letzte["unzustellbar"], "", zl)
    if "brief_2" in letzte:
        ende = letzte["brief_2"] + dt.timedelta(days=kadenz["kadenzende_nach_tagen"])
        if heute < ende:
            return Stand("wartet auf Reaktion (Brief 2)", "", ende, "", zl)
        return Stand("Kadenz beendet", "Wiedervorlage", ende + dt.timedelta(days=kadenz["wiedervorlage_nach_tagen"]), "Brief", zl)
    if "brief_1" in letzte:
        nf = letzte["brief_1"] + dt.timedelta(days=kadenz["nachfassbrief_nach_tagen"])
        if heute < nf:
            return Stand("wartet auf Reaktion (Brief 1)", "Nachfassbrief", nf, "Brief", zl)
        return Stand("Nachfassbrief fällig", "Nachfassbrief versenden (in die nächste Charge)", nf, "Brief", zl)
    return Stand("neu")


def aufgaben(staende: dict[str, Stand], heute: dt.date, horizont_tage: int = 7) -> list[tuple[str, Stand]]:
    """Aufgaben, die bis heute + Horizont fällig sind, überfällige zuerst."""
    grenze = heute + dt.timedelta(days=horizont_tage)
    aus = [(lid, s) for lid, s in staende.items() if s.aufgabe and s.faellig and s.faellig <= grenze
           and s.status not in ("wartet auf Reaktion (Brief 1)",)]
    return sorted(aus, key=lambda x: x[1].faellig)
