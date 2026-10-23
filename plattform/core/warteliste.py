"""Warteliste mit automatischer Nachbesetzung: Absagen reißen Lücken in die Tagespläne; dieses Modul füllt sie
mit dem bestpassenden wartenden Kunden – als zeitlich befristetes Angebot, nicht als stille Umbuchung.

Zuordnung (Matching). Für eine Lücke (Monteur, Beginn, Ende) kommen offene Einträge infrage, deren Dauer plus Puffer
hineinpasst, deren Qualifikation der Monteur hat und deren Wunschzeitraum, Wochentage und Tageszeiten (vormittag/
nachmittag) passen. Bewertung: Priorität aus Dringlichkeit (Notdienst 100 … niedrig 10) + Wartezeit in Tagen × 2
(Fairness: niemand wartet ewig) + Passgenauigkeit (wenig Leerlauf in der Lücke, bis 20) − Fahrweg zu den Nachbar-
terminen (Luftlinie, 2 Punkte je km, wenn Koordinaten vorliegen) − 15 je bereits verstrichenem Angebot (wer zweimal
nicht reagiert hat, rückt nach hinten).

Angebot. Der beste Eintrag erhält einen vorgeschlagenen Termin in der Lücke (Beginn am Raster, Tageszeitwunsch
beachtet) und eine Nachricht mit Portal-Link; die Reservierung verfällt nach `warteliste_frist_stunden` (Standard 4 h,
spätestens 2 h vor Terminbeginn). Bestätigt der Kunde im Portal, wird der Termin regulär per Freigabe
„termin_bestaetigen“ verbindlich und der Eintrag „gebucht“. Verfällt die Frist, wird der Termin abgesagt, der Eintrag
geht zurück auf „offen“, und die Lücke wird dem nächsten Kandidaten angeboten (Kaskade). Fristprüfung läuft im
Tageslauf und bei jedem Zugriff auf die Warteliste.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene, portal
from core.disposition import ZEITZONE, _belegungen
from core.domaene import als_utc
from core.feiertage import feiertag
from core.modelle import Kunde, Mandant, Ressource, Servicefall, Termin, Wartelisteneintrag
from core.routing import haversine_km

PRIORITAET = {"notdienst": 100, "hoch": 60, "normal": 30, "niedrig": 10}
AKTIV = ("offen", "angeboten")


def eintragen(s: Session, m: Mandant, f: Servicefall, wunsch_von: date, wunsch_bis: date, tageszeiten: list[str] | None = None,
              wochentage: list[int] | None = None, notiz: str | None = None, akteur_name: str | None = None) -> Wartelisteneintrag:
    if wunsch_bis < wunsch_von:
        raise ValueError("Wunschzeitraum ist leer")
    if not (f.kunde.email or f.kunde.telefon):
        raise domaene.Domaenenfehler("Für ein Terminangebot braucht der Kunde E-Mail oder Rufnummer")
    offen = s.scalar(select(Wartelisteneintrag).where(Wartelisteneintrag.servicefall_id == f.id, Wartelisteneintrag.status.in_(AKTIV)))
    if offen is not None:
        raise domaene.Domaenenfehler("Der Vorgang steht bereits auf der Warteliste")
    e = Wartelisteneintrag(mandant_id=m.id, servicefall_id=f.id, kunde_id=f.kunde_id, wunsch_von=wunsch_von, wunsch_bis=wunsch_bis,
                           tageszeiten=[t for t in (tageszeiten or []) if t in ("vormittag", "nachmittag")], wochentage=[w for w in (wochentage or []) if 0 <= w <= 6],
                           dauer_minuten=f.dauer_minuten or 90, qualifikation=f.benoetigte_qualifikation, prioritaet=PRIORITAET.get(f.dringlichkeit, 30), notiz=notiz)
    s.add(e)
    s.flush()
    domaene.protokollieren(s, m.id, "warteliste.eingetragen", "servicefall", f.id, "mensch", {"eintrag_id": e.id, "von": wunsch_von.isoformat(), "bis": wunsch_bis.isoformat()}, akteur_name)
    return e


def luecken(s: Session, m: Mandant, ab: datetime, tage: int = 7, mindest_minuten: int = 30) -> list[dict[str, Any]]:
    """Freie Fenster je Monteur und Tag innerhalb der Arbeitszeit, abzüglich Termine (mit Puffer) und Abwesenheiten."""
    ab = als_utc(ab)
    ressourcen = s.scalars(select(Ressource).where(Ressource.mandant_id == m.id, Ressource.aktiv).order_by(Ressource.name)).all()
    if not ressourcen:
        return []
    puffer = timedelta(minutes=m.puffer_minuten)
    bis = ab + timedelta(days=tage)
    belegt = _belegungen(s, m.id, [r.id for r in ressourcen], ab, bis, puffer)
    ergebnis = []
    for r in ressourcen:
        tag = ab.astimezone(ZEITZONE).date()
        for _ in range(tage):
            if tag.weekday() in (r.arbeitstage or []) and not feiertag(tag, m.stammdaten):
                von = max(als_utc(datetime.combine(tag, r.arbeitsbeginn, ZEITZONE)), ab)
                ende_tag = als_utc(datetime.combine(tag, r.arbeitsende, ZEITZONE))
                blocks = sorted((max(b, von), min(e, ende_tag)) for b, e in belegt[r.id] if e > von and b < ende_tag)
                zeiger = von
                for b, e in blocks + [(ende_tag, ende_tag)]:
                    if (b - zeiger) >= timedelta(minutes=mindest_minuten):
                        ergebnis.append({"ressource_id": r.id, "name": r.name, "beginn": zeiger, "ende": b, "minuten": int((b - zeiger).total_seconds() // 60)})
                    zeiger = max(zeiger, e)
            tag += timedelta(days=1)
    return ergebnis


def _nachbarn(s: Session, ressource_id: str, beginn: datetime, ende: datetime) -> list[Kunde]:
    tag = als_utc(beginn).astimezone(ZEITZONE).date()
    von = als_utc(datetime.combine(tag, datetime.min.time(), ZEITZONE))
    termine = s.scalars(select(Termin).where(Termin.ressource_id == ressource_id, Termin.status.in_(("vorgeschlagen", "bestaetigt")),
                                             Termin.beginn >= von, Termin.beginn < von + timedelta(days=1)).order_by(Termin.beginn)).all()
    vorher = [t for t in termine if als_utc(t.ende) <= als_utc(beginn)]
    nachher = [t for t in termine if als_utc(t.beginn) >= als_utc(ende)]
    return [t.servicefall.kunde for t in ([vorher[-1]] if vorher else []) + ([nachher[0]] if nachher else [])]


def bewerten(e: Wartelisteneintrag, r: Ressource, beginn: datetime, ende: datetime, puffer: int, heute: date, nachbarn: list[Kunde]) -> tuple[float, list[str]] | None:
    luecke = int((als_utc(ende) - als_utc(beginn)).total_seconds() // 60)
    if e.dauer_minuten + puffer > luecke + puffer:   # Dauer muss in die Lücke passen (Puffer ist bereits in der Belegung berücksichtigt)
        return None
    if e.qualifikation and e.qualifikation not in (r.qualifikationen or []):
        return None
    lokal = als_utc(beginn).astimezone(ZEITZONE)
    if not (e.wunsch_von <= lokal.date() <= e.wunsch_bis):
        return None
    if e.wochentage and lokal.weekday() not in e.wochentage:
        return None
    if e.tageszeiten:
        passt_vm = "vormittag" in e.tageszeiten and lokal.hour < 12
        passt_nm = "nachmittag" in e.tageszeiten and (als_utc(ende).astimezone(ZEITZONE).hour >= 12 or als_utc(ende).astimezone(ZEITZONE).date() > lokal.date())
        if not (passt_vm or passt_nm):
            return None
    gruende = []
    score = float(e.prioritaet)
    gruende.append(f"Priorität {e.prioritaet}")
    wartetage = (heute - als_utc(e.erstellt).date()).days
    score += 2 * wartetage
    if wartetage:
        gruende.append(f"wartet seit {wartetage} Tagen")
    leerlauf = max(0, luecke - e.dauer_minuten)
    passung = 20 * (1 - leerlauf / luecke) if luecke else 0
    score += passung
    gruende.append(f"Passung {passung:.0f}/20")
    k = e.kunde
    if k.lat is not None and k.lon is not None and nachbarn:
        km = sum(haversine_km((k.lat, k.lon), (n.lat, n.lon)) for n in nachbarn if n.lat is not None and n.lon is not None)
        score -= 2 * km
        gruende.append(f"Fahrweg zu Nachbarterminen {km:.1f} km")
    if e.angebote_anzahl:
        score -= 15 * e.angebote_anzahl
        gruende.append(f"{e.angebote_anzahl} Angebot(e) verfallen")
    return score, gruende


def kandidaten(s: Session, m: Mandant, ressource: Ressource, beginn: datetime, ende: datetime, heute: date,
               ausschluss: set[str] | None = None) -> list[tuple[float, Wartelisteneintrag, list[str]]]:
    eintraege = s.scalars(select(Wartelisteneintrag).where(Wartelisteneintrag.mandant_id == m.id, Wartelisteneintrag.status == "offen")).all()
    nachbarn = _nachbarn(s, ressource.id, beginn, ende)
    bewertet = []
    for e in eintraege:
        if ausschluss and e.id in ausschluss:
            continue
        b = bewerten(e, ressource, beginn, ende, m.puffer_minuten, heute, nachbarn)
        if b is not None:
            bewertet.append((b[0], e, b[1]))
    bewertet.sort(key=lambda x: (-x[0], x[1].erstellt))
    return bewertet


def _terminbeginn(e: Wartelisteneintrag, beginn: datetime, ende: datetime, raster: int) -> datetime:
    lokal = als_utc(beginn).astimezone(ZEITZONE)
    start = lokal
    if e.tageszeiten == ["nachmittag"] and lokal.hour < 12:
        start = max(lokal, lokal.replace(hour=12, minute=0, second=0, microsecond=0))
    rest = (start.minute % raster)
    if rest:
        start += timedelta(minutes=raster - rest)
    if start + timedelta(minutes=e.dauer_minuten) > als_utc(ende).astimezone(ZEITZONE):
        start = lokal
    return als_utc(start)


def nachbesetzen(s: Session, m: Mandant, ressource_id: str, beginn: datetime, ende: datetime, jetzt: datetime,
                 ausschluss: set[str] | None = None) -> dict[str, Any] | None:
    """Bietet die Lücke dem besten Kandidaten an; None, wenn niemand passt."""
    r = s.get(Ressource, ressource_id)
    if r is None or r.mandant_id != m.id or als_utc(ende) <= als_utc(jetzt):
        return None
    liste = kandidaten(s, m, r, beginn, ende, als_utc(jetzt).date(), ausschluss)
    if not liste:
        return None
    score, e, gruende = liste[0]
    f = e.servicefall
    start = _terminbeginn(e, beginn, ende, m.raster_minuten)
    t = Termin(mandant_id=m.id, servicefall_id=f.id, ressource_id=r.id, beginn=start, ende=start + timedelta(minutes=e.dauer_minuten), status="vorgeschlagen")
    s.add(t)
    s.flush()
    frist = min(als_utc(jetzt) + timedelta(hours=float((m.stammdaten or {}).get("warteliste_frist_stunden", 4) or 4)), als_utc(start) - timedelta(hours=2))
    if frist <= als_utc(jetzt):
        frist = als_utc(jetzt) + timedelta(minutes=30)
    e.status, e.angeboten_am = "angeboten", als_utc(jetzt)
    e.angebot = {"termin_id": t.id, "beginn": als_utc(start).isoformat(), "ende": als_utc(t.ende).isoformat(), "ressource_id": r.id, "frist": frist.isoformat(),
                 "score": round(score, 1), "gruende": gruende}
    lokal = als_utc(start).astimezone(ZEITZONE)
    link = portal.link(m, f.id)
    text = (f"Guten Tag {f.kunde.name},\n\nes ist ein früherer Termin frei geworden: {lokal.strftime('%d.%m.%Y um %H:%M')} Uhr für „{f.anliegen}“.\n"
            + (f"Bitte bestätigen Sie bis {frist.astimezone(ZEITZONE).strftime('%d.%m. %H:%M')} Uhr: {link}\n" if link else f"Bitte rufen Sie uns bis {frist.astimezone(ZEITZONE).strftime('%H:%M')} Uhr an: {(m.stammdaten or {}).get('telefon', '')}\n")
            + "Danach geben wir den Termin an den nächsten Kunden auf der Warteliste weiter; Ihr bisheriger Termin bleibt bestehen.")
    domaene.outbox_schreiben(s, m.id, "endkunde", "nachricht", {"kunde_id": f.kunde_id, "servicefall_id": f.id, "betreff": "Früherer Termin frei geworden", "text": text,
                                                              "kurz": f"{m.name}: früherer Termin {lokal.strftime('%d.%m. %H:%M')} frei. Bestätigen: {link or ''}"},
                             f"warteliste:{e.id}:{t.id}")
    domaene.protokollieren(s, m.id, "warteliste.angeboten", "servicefall", f.id, "system",
                           {"eintrag_id": e.id, "termin_id": t.id, "beginn": e.angebot["beginn"], "frist": e.angebot["frist"], "score": round(score, 1), "gruende": gruende})
    return {"eintrag_id": e.id, "termin_id": t.id, "kunde": f.kunde.name, "beginn": e.angebot["beginn"], "frist": e.angebot["frist"], "score": round(score, 1), "gruende": gruende}


def angebot_angenommen(s: Session, m: Mandant, t: Termin, jetzt: datetime) -> Wartelisteneintrag | None:
    e = s.scalar(select(Wartelisteneintrag).where(Wartelisteneintrag.mandant_id == m.id, Wartelisteneintrag.status == "angeboten",
                                                  Wartelisteneintrag.angebot["termin_id"].as_string() == t.id))
    if e is None:
        return None
    e.status = "gebucht"
    domaene.protokollieren(s, m.id, "warteliste.gebucht", "servicefall", e.servicefall_id, "mensch", {"eintrag_id": e.id, "termin_id": t.id})
    # bisherigen (späteren) Termin desselben Vorgangs freigeben, damit die Lücke weiterwandern kann
    for alt in e.servicefall.termine:
        if alt.id != t.id and alt.status in ("vorgeschlagen", "bestaetigt") and als_utc(alt.beginn) > als_utc(t.beginn):
            alt.status = "abgesagt"
            domaene.protokollieren(s, m.id, "termin.abgesagt", "termin", alt.id, "system", {"servicefall_id": e.servicefall_id, "grund": "früherer Termin über Warteliste"})
            domaene.outbox_schreiben(s, m.id, "fachsoftware", "termin_upsert", {"termin_id": alt.id}, f"termin_upsert:{alt.id}:abgesagt")
            nachbesetzen(s, m, alt.ressource_id, alt.beginn, alt.ende, jetzt, {e.id})
    return e


def ablauf_pruefen(s: Session, m: Mandant, jetzt: datetime) -> list[dict[str, Any]]:
    """Verfallene Angebote zurücknehmen und die Lücke dem nächsten Kandidaten anbieten."""
    ergebnis = []
    for e in s.scalars(select(Wartelisteneintrag).where(Wartelisteneintrag.mandant_id == m.id, Wartelisteneintrag.status == "angeboten")).all():
        frist = datetime.fromisoformat(e.angebot["frist"])
        if als_utc(frist) > als_utc(jetzt):
            continue
        t = s.get(Termin, e.angebot["termin_id"])
        if t is not None and t.status == "vorgeschlagen":
            t.status = "abgesagt"
            domaene.outbox_schreiben(s, m.id, "fachsoftware", "termin_upsert", {"termin_id": t.id}, f"termin_upsert:{t.id}:abgesagt")
        e.status, e.angebote_anzahl, altes = "offen", e.angebote_anzahl + 1, e.angebot
        e.angebot = None
        domaene.protokollieren(s, m.id, "warteliste.verfallen", "servicefall", e.servicefall_id, "system", {"eintrag_id": e.id, "termin_id": altes["termin_id"]})
        naechster = None
        if t is not None:
            naechster = nachbesetzen(s, m, t.ressource_id, t.beginn, t.ende, jetzt, {e.id})
        ergebnis.append({"eintrag_id": e.id, "verfallen": altes["termin_id"], "weitergegeben_an": naechster["eintrag_id"] if naechster else None})
    return ergebnis


def zuruecknehmen(s: Session, m: Mandant, e: Wartelisteneintrag, akteur_name: str | None = None) -> Wartelisteneintrag:
    if e.status == "angeboten" and e.angebot:
        t = s.get(Termin, e.angebot["termin_id"])
        if t is not None and t.status == "vorgeschlagen":
            t.status = "abgesagt"
    e.status = "zurueckgezogen"
    domaene.protokollieren(s, m.id, "warteliste.zurueckgezogen", "servicefall", e.servicefall_id, "mensch", {"eintrag_id": e.id}, akteur_name)
    return e


def uebersicht(s: Session, m: Mandant, jetzt: datetime, tage: int = 7) -> dict[str, Any]:
    ablauf_pruefen(s, m, jetzt)
    eintraege = s.scalars(select(Wartelisteneintrag).where(Wartelisteneintrag.mandant_id == m.id).order_by(Wartelisteneintrag.erstellt)).all()
    heute = als_utc(jetzt).date()
    offene = [e for e in eintraege if e.status in AKTIV]
    freie = luecken(s, m, jetzt, tage)
    ressourcen = {r.id: r for r in s.scalars(select(Ressource).where(Ressource.mandant_id == m.id))}
    luecken_aus = []
    for l_ in freie:
        kand = kandidaten(s, m, ressourcen[l_["ressource_id"]], l_["beginn"], l_["ende"], heute)
        luecken_aus.append({**l_, "beginn": l_["beginn"].isoformat(), "ende": l_["ende"].isoformat(), "kandidaten": len(kand),
                            "bester": {"eintrag_id": kand[0][1].id, "kunde": kand[0][1].kunde.name, "score": round(kand[0][0], 1), "gruende": kand[0][2]} if kand else None})
    return {"eintraege": [{"id": e.id, "servicefall_id": e.servicefall_id, "kunde_id": e.kunde_id, "kunde": e.kunde.name, "anliegen": e.servicefall.anliegen,
                           "wunsch_von": e.wunsch_von.isoformat(), "wunsch_bis": e.wunsch_bis.isoformat(), "tageszeiten": e.tageszeiten, "wochentage": e.wochentage,
                           "dauer_minuten": e.dauer_minuten, "qualifikation": e.qualifikation, "prioritaet": e.prioritaet, "status": e.status, "angebot": e.angebot,
                           "angebote_anzahl": e.angebote_anzahl, "wartetage": (heute - als_utc(e.erstellt).date()).days, "notiz": e.notiz} for e in eintraege if e.status in AKTIV or e.status == "gebucht"],
            "offen": len(offene), "luecken": luecken_aus}
