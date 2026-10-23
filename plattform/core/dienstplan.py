"""Dienst- und Stundenplanung: Schichten je Mitarbeiter und Tag, Prüfung gegen das Arbeitszeitgesetz, automatischer
Wochenvorschlag aus dem prognostizierten Bedarf, Zeitkonto (Soll aus Diensten, Ist aus der Zeiterfassung) und
Urlaubskonto.

Ein `Dienst` übersteuert für den Tag die Regelarbeitszeit der Ressource (arbeitstage/arbeitsbeginn/arbeitsende):
`arbeitszeit()` ist die eine Stelle, an der Disposition, Warteliste, Optimierer, Prognose und Twin die verfügbare
Zeit eines Monteurs ermitteln – ohne Dienst gilt die Regelarbeitszeit, mit Dienst „arbeit“ dessen Zeiten, mit Dienst
„frei“ keine, bei reiner „bereitschaft“ (Rufbereitschaft) ebenfalls keine planbare Zeit.

Arbeitszeitgesetz (Hinweise, keine Rechtsberatung): § 3 höchstens 8 h werktäglich, bis 10 h bei Ausgleich auf 8 h im
Schnitt von 6 Monaten (hier: Warnung ab 10 h netto am Tag und ab 48 h in der Woche); § 4 Ruhepausen 30 min ab 6 h,
45 min ab 9 h; § 5 Ruhezeit 11 h zwischen Dienstende und nächstem Dienstbeginn; § 9/§ 11 Sonntagsarbeit nur in
Ausnahmen (Notdienst) mit Ersatzruhetag binnen zwei Wochen. Rufbereitschaft zählt nicht als Arbeitszeit, der
tatsächliche Einsatz schon – der wird über die Zeiterfassung sichtbar, nicht über den Dienstplan.

Vorschlag. Bedarf je Tag = gebuchte Terminstunden + anteilige Prognose der Woche (prognose.auslastung) · 1,15
Reserve; daraus die Zahl benötigter Monteure (Tagesnettozeit 8 h). Besetzt werden die verfügbaren Mitarbeiter mit
den wenigsten Stunden der letzten vier Wochen zuerst (Fairness), Schichtfolge frueh/spaet alternierend, wenn der
Betrieb im Schichtmodell arbeitet (stammdaten.schichtbetrieb), sonst Tagschicht. Abwesenheiten, Feiertage und freie
Tage der Regelarbeitszeit werden ausgelassen; bestehende Dienste bleiben unangetastet.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene
from core.domaene import Domaenenfehler
from core.feiertage import feiertag
from core.modelle import Abwesenheit, Dienst, Mandant, Ressource, Termin, Zeiteintrag

SCHICHTEN: dict[str, tuple[time, time, int]] = {        # beginn, ende, pause_minuten
    "frueh": (time(6, 0), time(14, 30), 30),
    "tag": (time(7, 30), time(16, 30), 45),
    "spaet": (time(10, 0), time(18, 30), 30),
    "bereitschaft": (time(16, 30), time(7, 30), 0),
}
TAGESNETTO_H = 8.0
MAX_TAG_H, MAX_WOCHE_H, RUHEZEIT_H = 10.0, 48.0, 11.0


# --- Verfügbarkeit (von allen Planungsmodulen genutzt) ---------------------------------------
def dienstkarte(s: Session, mandant_id: str, von: date, bis: date, ressource_ids: list[str] | None = None) -> dict[tuple[str, date], list[Dienst]]:
    abfrage = select(Dienst).where(Dienst.mandant_id == mandant_id, Dienst.datum >= von, Dienst.datum <= bis)
    if ressource_ids is not None:
        abfrage = abfrage.where(Dienst.ressource_id.in_(ressource_ids))
    karte: dict[tuple[str, date], list[Dienst]] = defaultdict(list)
    for d in s.scalars(abfrage.order_by(Dienst.beginn)):
        karte[(d.ressource_id, d.datum)].append(d)
    return karte


def arbeitszeit(r: Ressource, tag: date, karte: dict[tuple[str, date], list[Dienst]] | None, stammdaten: dict | None) -> tuple[time, time] | None:
    """Planbare Arbeitszeit der Ressource am Tag oder None (frei, Feiertag, nur Rufbereitschaft)."""
    dienste = (karte or {}).get((r.id, tag), [])
    if dienste:
        if any(d.art == "frei" for d in dienste):
            return None
        arbeit = [d for d in dienste if d.art == "arbeit"]
        if not arbeit:
            return None
        return min(d.beginn for d in arbeit), max(d.ende for d in arbeit)
    if tag.weekday() not in (r.arbeitstage or []) or feiertag(tag, stammdaten):
        return None
    return r.arbeitsbeginn, r.arbeitsende


def stunden(d: Dienst) -> float:
    """Nettoarbeitszeit eines Dienstes in Stunden (Rufbereitschaft und frei: 0)."""
    if d.art != "arbeit":
        return 0.0
    brutto = (datetime.combine(d.datum, d.ende) - datetime.combine(d.datum, d.beginn)).total_seconds() / 3600
    if brutto < 0:
        brutto += 24
    return max(0.0, brutto - (d.pause_minuten or 0) / 60)


# --- Pflege ----------------------------------------------------------------------------------
def eintragen(s: Session, m: Mandant, r: Ressource, datum: date, art: str = "arbeit", schicht: str | None = None,
              beginn: time | None = None, ende: time | None = None, pause_minuten: int | None = None,
              notiz: str | None = None, akteur_name: str | None = None) -> Dienst:
    if art not in ("arbeit", "bereitschaft", "frei"):
        raise ValueError("Art muss arbeit, bereitschaft oder frei sein")
    if schicht:
        if schicht not in SCHICHTEN:
            raise ValueError(f"Unbekannte Schicht {schicht}")
        b, e, p = SCHICHTEN[schicht]
        beginn, ende = beginn or b, ende or e
        pause_minuten = p if pause_minuten is None else pause_minuten
        if schicht == "bereitschaft":
            art = "bereitschaft"
    if art == "frei":
        beginn, ende, pause_minuten = time(0, 0), time(0, 0), 0
        for alt in s.scalars(select(Dienst).where(Dienst.ressource_id == r.id, Dienst.datum == datum)):
            s.delete(alt)
    elif beginn is None or ende is None:
        raise ValueError("Beginn und Ende oder eine Schicht angeben")
    elif art == "arbeit" and ende <= beginn:
        raise ValueError("Ende muss nach Beginn liegen")
    if pause_minuten is None:
        pause_minuten = 30
    vorhanden = s.scalar(select(Dienst).where(Dienst.ressource_id == r.id, Dienst.datum == datum, Dienst.beginn == beginn))
    if vorhanden and vorhanden.art != "frei":
        d = vorhanden
        d.ende, d.art, d.pause_minuten, d.schicht, d.notiz = ende, art, pause_minuten, schicht, notiz
    else:
        if vorhanden:
            s.delete(vorhanden)
            s.flush()
        d = Dienst(mandant_id=m.id, ressource_id=r.id, datum=datum, beginn=beginn, ende=ende, art=art,
                   pause_minuten=pause_minuten, schicht=schicht, notiz=notiz)
        s.add(d)
    s.flush()
    domaene.protokollieren(s, m.id, "dienst.geplant", "ressource", r.id, "mensch",
                           {"datum": datum.isoformat(), "art": art, "schicht": schicht, "beginn": beginn.isoformat(), "ende": ende.isoformat()}, akteur_name)
    return d


def _eintrag(d: Dienst) -> dict[str, Any]:
    return {"id": d.id, "ressource_id": d.ressource_id, "datum": d.datum.isoformat(), "art": d.art, "schicht": d.schicht,
            "beginn": d.beginn.strftime("%H:%M"), "ende": d.ende.strftime("%H:%M"), "pause_minuten": d.pause_minuten,
            "stunden": round(stunden(d), 2), "notiz": d.notiz}


def montag(d: date) -> date:
    return d - timedelta(days=d.weekday())


def woche(s: Session, m: Mandant, start: date) -> dict[str, Any]:
    start = montag(start)
    ende = start + timedelta(days=6)
    ressourcen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv).order_by(Ressource.name)).all()
    karte = dienstkarte(s, m.id, start, ende)
    abwesend = s.scalars(select(Abwesenheit).where(Abwesenheit.mandant_id == m.id, Abwesenheit.bis >= start, Abwesenheit.von <= ende)).all()
    von_dt = datetime.combine(start, time.min)
    termine = s.scalars(select(Termin).where(Termin.mandant_id == m.id, Termin.status.in_(("vorgeschlagen", "bestaetigt")),
                                             Termin.beginn >= von_dt, Termin.beginn < von_dt + timedelta(days=7))).all()
    gebucht: dict[tuple[str, date], float] = defaultdict(float)
    for t in termine:
        gebucht[(t.ressource_id, t.beginn.date())] += (t.ende - t.beginn).total_seconds() / 3600
    zeilen = []
    for r in ressourcen:
        tage, summe = [], 0.0
        for i in range(7):
            tag = start + timedelta(days=i)
            dienste = karte.get((r.id, tag), [])
            az = arbeitszeit(r, tag, karte, m.stammdaten)
            std = sum(stunden(d) for d in dienste) if dienste else (_regelsoll(r) if az else 0.0)
            abw = next((a for a in abwesend if a.ressource_id == r.id and a.von <= tag <= a.bis), None)
            if abw:
                std = 0.0
            summe += std
            tage.append({"datum": tag.isoformat(), "dienste": [_eintrag(d) for d in dienste], "regel": az is not None and not dienste,
                         "beginn": az[0].strftime("%H:%M") if az else None, "ende": az[1].strftime("%H:%M") if az else None,
                         "stunden": round(std, 2), "abwesenheit": abw.art if abw else None, "feiertag": feiertag(tag, m.stammdaten),
                         "gebucht_h": round(gebucht.get((r.id, tag), 0.0), 1)})
        zeilen.append({"ressource_id": r.id, "name": r.name, "wochenstunden": r.wochenstunden, "tage": tage, "summe_h": round(summe, 1),
                       "abweichung_h": round(summe - (r.wochenstunden or 0), 1)})
    return {"montag": start.isoformat(), "sonntag": ende.isoformat(), "kw": start.isocalendar()[1], "zeilen": zeilen,
            "warnungen": pruefen(s, m, start - timedelta(days=1), ende + timedelta(days=14))}


# --- Arbeitszeitgesetz -----------------------------------------------------------------------
def pruefen(s: Session, m: Mandant, von: date, bis: date) -> list[dict[str, Any]]:
    """Verstöße und Hinweise im Zeitraum; geprüft werden nur explizit geplante Dienste (die Regelarbeitszeit ist
    beim Anlegen der Ressource gesetzt und wird dort plausibilisiert)."""
    ressourcen = {r.id: r for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m.id))}
    karte = dienstkarte(s, m.id, von, bis)
    warnungen: list[dict[str, Any]] = []
    je_ressource: dict[str, list[Dienst]] = defaultdict(list)
    for (rid, _), dienste in karte.items():
        je_ressource[rid].extend(dienste)

    def warnen(r: Ressource, tag: date, art: str, text: str, schwere: str = "verstoss") -> None:
        warnungen.append({"ressource_id": r.id, "name": r.name, "datum": tag.isoformat(), "art": art, "schwere": schwere, "text": text})

    for rid, dienste in je_ressource.items():
        r = ressourcen.get(rid)
        if r is None:
            continue
        arbeit = sorted((d for d in dienste if d.art == "arbeit"), key=lambda d: (d.datum, d.beginn))
        je_tag: dict[date, list[Dienst]] = defaultdict(list)
        for d in arbeit:
            je_tag[d.datum].append(d)
        for tag, ds in je_tag.items():
            netto = sum(stunden(d) for d in ds)
            brutto = netto + sum((d.pause_minuten or 0) for d in ds) / 60
            pause = sum((d.pause_minuten or 0) for d in ds)
            if netto > MAX_TAG_H:
                warnen(r, tag, "tageshoechstarbeitszeit", f"{netto:.1f} h Arbeitszeit, erlaubt sind höchstens 10 h (§ 3 ArbZG).")
            if brutto > 9 and pause < 45:
                warnen(r, tag, "ruhepause", f"{pause} min Pause bei über 9 h, vorgeschrieben sind 45 min (§ 4 ArbZG).")
            elif brutto > 6 and pause < 30:
                warnen(r, tag, "ruhepause", f"{pause} min Pause bei über 6 h, vorgeschrieben sind 30 min (§ 4 ArbZG).")
            if tag.weekday() == 6:
                ersatz = any(arbeitszeit(r, tag + timedelta(days=i), karte, m.stammdaten) is None and not feiertag(tag + timedelta(days=i), m.stammdaten)
                             and (tag + timedelta(days=i)).weekday() != 6 for i in range(1, 15))
                if not ersatz:
                    warnen(r, tag, "sonntagsarbeit", "Sonntagsarbeit ohne erkennbaren Ersatzruhetag binnen zwei Wochen (§ 11 Abs. 3 ArbZG).")
                else:
                    warnen(r, tag, "sonntagsarbeit", "Sonntagsarbeit – nur in den Ausnahmen des § 10 ArbZG (z. B. Notdienst) zulässig.", "hinweis")
        # Ruhezeit zwischen aufeinanderfolgenden Diensten
        for a, b in zip(arbeit, arbeit[1:]):
            ende_a = datetime.combine(a.datum, a.ende)
            beginn_b = datetime.combine(b.datum, b.beginn)
            if a.datum != b.datum and 0 < (beginn_b - ende_a).total_seconds() / 3600 < RUHEZEIT_H:
                warnen(r, b.datum, "ruhezeit", f"Nur {(beginn_b - ende_a).total_seconds() / 3600:.1f} h Ruhezeit nach dem Dienst vom {a.datum.strftime('%d.%m.')}, vorgeschrieben sind 11 h (§ 5 ArbZG).")
        # Wochenstunden und Tage in Folge (Regelarbeitszeit eingeschlossen)
        wochen: dict[date, float] = defaultdict(float)
        for d in arbeit:
            wochen[montag(d.datum)] += stunden(d)
        for mo, h in wochen.items():
            if h > MAX_WOCHE_H:
                warnen(r, mo, "wochenarbeitszeit", f"KW {mo.isocalendar()[1]}: {h:.1f} h geplant, mehr als 48 h erfordern Ausgleich (§ 3 ArbZG).")
        folge, tag = 0, von
        while tag <= bis:
            if arbeitszeit(r, tag, karte, m.stammdaten) is not None:
                folge += 1
                if folge == 7:
                    warnen(r, tag, "tage_in_folge", "Siebter Arbeitstag in Folge – Ruhetag einplanen.", "hinweis")
            else:
                folge = 0
            tag += timedelta(days=1)
    warnungen.sort(key=lambda w: (w["datum"], w["name"]))
    return warnungen


# --- Vorschlag -------------------------------------------------------------------------------
def _stunden_letzte_wochen(s: Session, m: Mandant, start: date, wochen: int = 4) -> dict[str, float]:
    karte = dienstkarte(s, m.id, start - timedelta(weeks=wochen), start - timedelta(days=1))
    last: dict[str, float] = defaultdict(float)
    for (rid, _), ds in karte.items():
        last[rid] += sum(stunden(d) for d in ds)
    return last


def vorschlag(s: Session, m: Mandant, start: date) -> dict[str, Any]:
    from core import prognose   # später Import: prognose nutzt dienstplan.arbeitszeit
    start = montag(start)
    ressourcen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv).order_by(Ressource.name)).all()
    karte = dienstkarte(s, m.id, start, start + timedelta(days=6))
    abwesend = s.scalars(select(Abwesenheit).where(Abwesenheit.mandant_id == m.id, Abwesenheit.bis >= start, Abwesenheit.von <= start + timedelta(days=6))).all()
    last = _stunden_letzte_wochen(s, m, start)
    schichtbetrieb = bool((m.stammdaten or {}).get("schichtbetrieb"))
    # Bedarf je Tag
    von_dt = datetime.combine(start, time.min)
    gebucht: dict[date, float] = defaultdict(float)
    for t in s.scalars(select(Termin).where(Termin.mandant_id == m.id, Termin.status.in_(("vorgeschlagen", "bestaetigt")),
                                            Termin.beginn >= von_dt, Termin.beginn < von_dt + timedelta(days=7))):
        gebucht[t.beginn.date()] += (t.ende - t.beginn).total_seconds() / 3600
    try:
        ausl = prognose.auslastung(s, m, start, horizont=1)
        w = next((x for x in ausl["wochen"] if x["woche"] == start.isoformat()), ausl["wochen"][0] if ausl["wochen"] else None)
        bedarf_woche = float(w["bedarf_h"]) if w else 0.0
    except Exception:   # keine Historie: nur Buchungen
        bedarf_woche = 0.0
    arbeitstage = [start + timedelta(days=i) for i in range(6) if not feiertag(start + timedelta(days=i), m.stammdaten)]
    werktage = [t for t in arbeitstage if t.weekday() < 5] or arbeitstage
    tage, zaehler = [], defaultdict(float, last)
    folge = 0
    for tag in arbeitstage:
        bedarf = gebucht.get(tag, 0.0) + (bedarf_woche / len(werktage) if tag in werktage else 0.0)
        bedarf *= 1.15
        benoetigt = max(1 if tag.weekday() < 5 else 0, -(-int(bedarf * 10) // int(TAGESNETTO_H * 10)))
        verfuegbar = [r for r in ressourcen if tag.weekday() in (r.arbeitstage or []) or tag.weekday() == 5]
        verfuegbar = [r for r in verfuegbar if not any(a.ressource_id == r.id and a.von <= tag <= a.bis for a in abwesend)]
        fest = [r for r in verfuegbar if karte.get((r.id, tag))]   # schon geplant
        frei = [r for r in verfuegbar if r not in fest and tag.weekday() in (r.arbeitstage or [])]
        frei.sort(key=lambda r: (zaehler[r.id], r.name))
        bereits = sum(stunden(d) for r in fest for d in karte.get((r.id, tag), []))
        offen = max(0, benoetigt - len([r for r in fest if any(d.art == "arbeit" for d in karte.get((r.id, tag), []))]))
        gewaehlt = frei[:offen]
        dienste = []
        for n, r in enumerate(gewaehlt):
            schicht = ("frueh" if (n + folge) % 2 == 0 else "spaet") if schichtbetrieb else "tag"
            b, e, p = SCHICHTEN[schicht]
            dienste.append({"ressource_id": r.id, "name": r.name, "datum": tag.isoformat(), "art": "arbeit", "schicht": schicht,
                            "beginn": b.strftime("%H:%M"), "ende": e.strftime("%H:%M"), "pause_minuten": p})
            zaehler[r.id] += TAGESNETTO_H
        folge += 1
        unterdeckung = max(0.0, bedarf - bereits - len(gewaehlt) * TAGESNETTO_H)
        tage.append({"datum": tag.isoformat(), "bedarf_h": round(bedarf, 1), "gebucht_h": round(gebucht.get(tag, 0.0), 1), "benoetigt": benoetigt,
                     "geplant": [r.name for r in fest], "vorschlag": dienste, "unterdeckung_h": round(unterdeckung, 1)})
    return {"montag": start.isoformat(), "kw": start.isocalendar()[1], "bedarf_woche_h": round(bedarf_woche, 1), "schichtbetrieb": schichtbetrieb,
            "tage": tage, "dienste": [d for t in tage for d in t["vorschlag"]],
            "fairness": {r.name: round(zaehler[r.id], 1) for r in ressourcen}}


def uebernehmen(s: Session, m: Mandant, dienste: list[dict[str, Any]], akteur_name: str | None = None) -> list[Dienst]:
    ressourcen = {r.id: r for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m.id))}
    angelegt = []
    for d in dienste:
        r = ressourcen.get(d["ressource_id"])
        if r is None:
            raise Domaenenfehler(f"Ressource {d['ressource_id']} unbekannt")
        angelegt.append(eintragen(s, m, r, date.fromisoformat(d["datum"]), d.get("art", "arbeit"), d.get("schicht"),
                                  time.fromisoformat(d["beginn"]) if d.get("beginn") else None,
                                  time.fromisoformat(d["ende"]) if d.get("ende") else None, d.get("pause_minuten"), d.get("notiz"), akteur_name))
    return angelegt


# --- Zeitkonto und Urlaub --------------------------------------------------------------------
def _regelsoll(r: Ressource) -> float:
    return (r.wochenstunden or 40.0) / max(1, len(r.arbeitstage or [0, 1, 2, 3, 4]))


def zeitkonto(s: Session, m: Mandant, r: Ressource, von: date, bis: date) -> dict[str, Any]:
    karte = dienstkarte(s, m.id, von, bis, [r.id])
    abwesend = s.scalars(select(Abwesenheit).where(Abwesenheit.ressource_id == r.id, Abwesenheit.bis >= von, Abwesenheit.von <= bis)).all()
    zeiten = s.scalars(select(Zeiteintrag).where(Zeiteintrag.ressource_id == r.id, Zeiteintrag.art.in_(("arbeit", "fahrt")), Zeiteintrag.ende.is_not(None),
                                                  Zeiteintrag.beginn >= datetime.combine(von, time.min), Zeiteintrag.beginn < datetime.combine(bis + timedelta(days=1), time.min))).all()
    ist_je_tag: dict[date, float] = defaultdict(float)
    for z in zeiten:
        ist_je_tag[z.beginn.date()] += (z.ende - z.beginn).total_seconds() / 3600
    tage, soll, ist, gutschrift = [], 0.0, 0.0, 0.0
    tag = von
    while tag <= bis:
        dienste = karte.get((r.id, tag), [])
        if dienste:
            tagessoll = sum(stunden(d) for d in dienste)
        else:
            tagessoll = _regelsoll(r) if arbeitszeit(r, tag, karte, m.stammdaten) else 0.0
        abw = next((a for a in abwesend if a.von <= tag <= a.bis), None)
        gut = tagessoll if (abw and abw.art in ("urlaub", "krank", "schulung")) else 0.0
        soll += tagessoll
        ist += ist_je_tag.get(tag, 0.0)
        gutschrift += gut
        if tagessoll or ist_je_tag.get(tag):
            tage.append({"datum": tag.isoformat(), "soll_h": round(tagessoll, 2), "ist_h": round(ist_je_tag.get(tag, 0.0), 2),
                         "gutschrift_h": round(gut, 2), "abwesenheit": abw.art if abw else None})
        tag += timedelta(days=1)
    saldo = ist + gutschrift - soll
    return {"ressource_id": r.id, "name": r.name, "von": von.isoformat(), "bis": bis.isoformat(), "soll_h": round(soll, 1), "ist_h": round(ist, 1),
            "gutschrift_h": round(gutschrift, 1), "saldo_h": round(saldo, 1), "tage": tage}


def urlaub(s: Session, m: Mandant, r: Ressource, jahr: int, heute: date) -> dict[str, Any]:
    von, bis = date(jahr, 1, 1), date(jahr, 12, 31)
    abw = s.scalars(select(Abwesenheit).where(Abwesenheit.ressource_id == r.id, Abwesenheit.art == "urlaub", Abwesenheit.bis >= von, Abwesenheit.von <= bis)).all()
    genommen, geplant, zeitraeume = 0, 0, []
    for a in abw:
        tage = 0
        tag = max(a.von, von)
        while tag <= min(a.bis, bis):
            if tag.weekday() in (r.arbeitstage or []) and not feiertag(tag, m.stammdaten):
                tage += 1
                if tag <= heute:
                    genommen += 1
                else:
                    geplant += 1
            tag += timedelta(days=1)
        zeitraeume.append({"von": a.von.isoformat(), "bis": a.bis.isoformat(), "arbeitstage": tage, "notiz": a.notiz})
    anspruch = r.urlaubsanspruch_tage or 0
    return {"ressource_id": r.id, "name": r.name, "jahr": jahr, "anspruch": anspruch, "genommen": genommen, "geplant": geplant,
            "rest": anspruch - genommen - geplant, "zeitraeume": zeitraeume}


def urlaubsuebersicht(s: Session, m: Mandant, jahr: int, heute: date) -> list[dict[str, Any]]:
    return [urlaub(s, m, r, jahr, heute) for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv).order_by(Ressource.name))]
