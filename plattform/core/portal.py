"""Kundenportal: Termin bestätigen oder absagen, Angebote und Rechnungen einsehen, ohne Konto.

Der Zugang ist ein Link je Servicefall mit HMAC-Token (wie beim Kalender-Abo, Schlüssel KALENDER_SCHLUESSEL); er steht
in Terminbestätigung und Terminerinnerung. Das Portal zeigt nur Daten dieses Vorgangs und des Betriebs, keine
anderen Vorgänge desselben Kunden (Link-Weitergabe bleibt damit eng begrenzt). Eine Absage setzt den Termin auf
„abgesagt“, den Vorgang zurück auf „qualifiziert“ und erzeugt einen Hinweis an den Inhaber; eine Bestätigung eines
vorgeschlagenen Termins läuft als Freigabe „termin_bestaetigen“ (Klasse B, per Policy automatisierbar).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene, hinweise, kalender
from core.disposition import ZEITZONE
from core.domaene import als_utc
from core.modelle import (
    Angebot,
    Ereignis,
    Kommunikation,
    Mandant,
    OutboxEintrag,
    Rechnung,
    Servicefall,
    Termin,
    Zeiteintrag,
)


def token(fall_id: str) -> str:
    return kalender.token(f"portal:{fall_id}")


def gueltig(fall_id: str, wert: str | None) -> bool:
    return kalender.token_gueltig(f"portal:{fall_id}", wert)


def link(m: Mandant, fall_id: str) -> str | None:
    basis = (m.stammdaten or {}).get("basis_url")
    return f"{basis.rstrip('/')}/portal/#{fall_id}/{token(fall_id)}" if basis else None


WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def _termin(t: Termin) -> dict[str, Any]:
    beginn, ende = als_utc(t.beginn).astimezone(ZEITZONE), als_utc(t.ende).astimezone(ZEITZONE)
    return {"id": t.id, "beginn": beginn.isoformat(), "ende": ende.isoformat(), "status": t.status, "monteur": t.ressource.name.split(" (")[0],
            "text": f"{WOCHENTAGE[beginn.weekday()]}, {beginn.strftime('%d.%m.%Y')} {beginn.strftime('%H:%M')}–{ende.strftime('%H:%M')} Uhr"}


def daten(s: Session, m: Mandant, f: Servicefall, jetzt: datetime | None = None) -> dict[str, Any]:
    st = m.stammdaten or {}
    angebote = s.scalars(select(Angebot).where(Angebot.servicefall_id == f.id, Angebot.status.in_(("versendet", "angenommen")))).all()
    rechnungen = s.scalars(select(Rechnung).where(Rechnung.servicefall_id == f.id, Rechnung.art == "rechnung")).all()
    return {"betrieb": {"name": m.name, "telefon": st.get("telefon"), "email": st.get("email"), "strasse": st.get("strasse"),
                        "plz": st.get("plz"), "ort": st.get("ort"), "impressum_url": st.get("impressum_url"), "datenschutz_url": st.get("datenschutz_url")},
            "kunde": {"name": f.kunde.name},
            "vorgang": {"id": f.id, "anliegen": f.anliegen, "status": f.status, "dringlichkeit": f.dringlichkeit},
            "termine": [_termin(t) for t in sorted(f.termine, key=lambda t: t.beginn) if t.status != "abgesagt"],
            "angebote": [{"id": a.id, "nummer": a.nummer, "titel": a.titel, "brutto": a.brutto, "status": a.status, "gueltig_bis": a.gueltig_bis.isoformat()} for a in angebote],
            "rechnungen": [{"id": r.id, "nummer": r.nummer, "brutto": r.brutto, "status": r.status, "faellig": r.faellig.isoformat(),
                            "girocode": bool(st.get("iban")) and r.status != "bezahlt"} for r in rechnungen],
            "zahlung": {"iban": st.get("iban"), "bic": st.get("bic"), "kontoinhaber": st.get("kontoinhaber") or m.name} if st.get("iban") else None,
            "verlauf": verlauf(s, m, f, jetzt or datetime.now(ZEITZONE)),
            "alternativen_moeglich": f.status in ("eingegangen", "qualifiziert", "eingeplant") and not any(t.status == "bestaetigt" for t in f.termine),
            "empfehlungen": _empfehlungen(s, m, f, jetzt),
            "bewertung": bewertung_lesen(s, f),
            "bewertung_moeglich": f.status in ("durchgefuehrt", "abgerechnet", "uebergeben"),
            "haus": _haus(s, m, f)}


def _empfehlungen(s: Session, m: Mandant, f: Servicefall, jetzt: datetime | None) -> list[dict[str, Any]]:
    """„Für Sie empfohlen“: höchstens drei Empfehlungen mit Preis, die der Kunde per Klick als Angebot anfordern kann."""
    from core import upsell  # späte Einbindung (upsell nutzt portal._kommunikation)
    heute = als_utc(jetzt or datetime.now(ZEITZONE)).date()
    aus = []
    for e in upsell.empfehlungen(s, m, f.kunde, heute, f):
        if e["art"] in ("risiko",):   # Ausfallrisiko bespricht der Betrieb persönlich
            continue
        aus.append({"schluessel": e["schluessel"], "titel": e["titel"], "text": e["begruendung"], "preis_brutto": e["preis_brutto"], "art": e["art"],
                    "aktion": "angebot" if e["positionen"] else "beratung"})
        if len(aus) == 3:
            break
    return aus


SCHRITTE = (("eingegangen", "Anfrage eingegangen"), ("geplant", "Termin geplant"), ("unterwegs", "Wir sind unterwegs"),
            ("erledigt", "Arbeit erledigt"), ("rechnung", "Rechnung"))


def verlauf(s: Session, m: Mandant, f: Servicefall, jetzt: datetime) -> list[dict[str, Any]]:
    """Statuszeitachse für Kunden: fünf verständliche Schritte statt interner Zustände. „Unterwegs“ entsteht aus einem laufenden
    Zeiteintrag der Art fahrt des eingeteilten Monteurs am Termintag (mobile Zeiterfassung), mit Ankunft = Terminbeginn."""
    termine = [t for t in f.termine if t.status != "abgesagt"]
    bestaetigt = [t for t in termine if t.status == "bestaetigt"]
    erledigt = f.status in ("durchgefuehrt", "abgerechnet", "uebergeben")
    rechnungen = s.scalars(select(Rechnung).where(Rechnung.servicefall_id == f.id, Rechnung.art == "rechnung")).all()
    unterwegs = None
    if bestaetigt and not erledigt:
        heute = als_utc(jetzt).astimezone(ZEITZONE).date()
        for t in bestaetigt:
            if als_utc(t.beginn).astimezone(ZEITZONE).date() != heute:
                continue
            fahrt = s.scalar(select(Zeiteintrag).where(Zeiteintrag.ressource_id == t.ressource_id, Zeiteintrag.art == "fahrt", Zeiteintrag.ende.is_(None),
                                                       Zeiteintrag.beginn >= als_utc(jetzt) - timedelta(hours=4)).order_by(Zeiteintrag.beginn.desc()))
            if fahrt is not None and (fahrt.servicefall_id in (None, f.id)):
                unterwegs = {"seit": als_utc(fahrt.beginn).astimezone(ZEITZONE).isoformat(), "ankunft": als_utc(t.beginn).astimezone(ZEITZONE).strftime("%H:%M"),
                             "monteur": t.ressource.name.split(" (")[0]}
    zustand = {"eingegangen": "erledigt",
               "geplant": "erledigt" if bestaetigt or erledigt else ("aktiv" if termine else "offen"),
               "unterwegs": "erledigt" if erledigt else ("aktiv" if unterwegs else "offen"),
               "erledigt": "erledigt" if erledigt else "offen",
               "rechnung": "offen" if not rechnungen else ("erledigt" if all(r.status == "bezahlt" for r in rechnungen) else "aktiv")}
    if f.status == "storniert":
        zustand = {k: "offen" for k, _ in SCHRITTE} | {"eingegangen": "erledigt"}
    hinweise_ = {"geplant": (f"{_termin(bestaetigt[0])['text']}" if bestaetigt else ("Terminvorschlag wartet auf Ihre Bestätigung." if termine else "Wir melden uns mit einem Terminvorschlag.")),
                 "unterwegs": (f"{unterwegs['monteur']} ist seit {unterwegs['seit'][11:16]} Uhr unterwegs, Ankunft gegen {unterwegs['ankunft']} Uhr." if unterwegs else None),
                 "erledigt": "Vielen Dank für Ihren Auftrag." if erledigt else None,
                 "rechnung": ("Alles bezahlt, vielen Dank." if rechnungen and zustand["rechnung"] == "erledigt" else ("Rechnung liegt unten zum Herunterladen bereit." if rechnungen else None))}
    if f.status == "storniert":
        hinweise_["geplant"] = "Der Vorgang wurde storniert."
    return [{"schluessel": k, "titel": titel, "zustand": zustand[k], "hinweis": hinweise_.get(k)} for k, titel in SCHRITTE]


def alternativen(s: Session, m: Mandant, f: Servicefall, jetzt: datetime, anzahl: int = 6) -> list[dict[str, Any]]:
    """Freie Fenster, die der Kunde statt des vorgeschlagenen Termins wählen kann (gleiche Dauer, Qualifikation, Sachmittel)."""
    from core.disposition import freie_zeitfenster
    fenster = freie_zeitfenster(s, m, als_utc(jetzt) + timedelta(hours=2), 10, f.dauer_minuten, f.benoetigte_qualifikation, anzahl, je_ressource=3, sachmittel=f.sachmittel)
    aus = []
    for z in fenster:
        b, e = als_utc(z.beginn).astimezone(ZEITZONE), als_utc(z.ende).astimezone(ZEITZONE)
        aus.append({"ressource_id": z.ressource_id, "beginn": b.isoformat(), "ende": e.isoformat(), "sachmittel_id": z.sachmittel_id,
                    "text": f"{WOCHENTAGE[b.weekday()]}, {b.strftime('%d.%m.%Y')} {b.strftime('%H:%M')}–{e.strftime('%H:%M')} Uhr"})
    return aus


def alternative_gewaehlt(s: Session, m: Mandant, f: Servicefall, neu: Termin, jetzt: datetime) -> dict[str, Any]:
    """Nach dem Anlegen des Wunschtermins: bisherige Vorschläge zurückziehen, Büro informieren."""
    for t in f.termine:
        if t.id != neu.id and t.status == "vorgeschlagen":
            t.status = "abgesagt"
            domaene.protokollieren(s, m.id, "termin.abgesagt", "termin", t.id, "mensch", {"servicefall_id": f.id, "grund": "Kunde hat im Portal einen anderen Termin gewählt", "quelle": "portal"}, f.kunde.name)
    _kommunikation(s, f, f"Kunde hat im Portal den Termin {_termin(neu)['text']} gewählt", jetzt)
    hinweise.senden(s, m, "info", "Terminwunsch im Portal", f"{f.kunde.name}: {f.anliegen} – Wunsch {_termin(neu)['text']} (Freigabe offen).", f"wunsch:{neu.id}", "freigaben")
    return {"termin": _termin(neu), "hinweis": "Vielen Dank, wir haben Ihren Wunschtermin vorgemerkt und bestätigen ihn in Kürze."}


def bewertung_lesen(s: Session, f: Servicefall) -> dict[str, Any] | None:
    e = s.scalar(select(Ereignis).where(Ereignis.typ == "bewertung.abgegeben", Ereignis.entitaet_id == f.id).order_by(Ereignis.zeit.desc()))
    return {"sterne": e.payload.get("sterne"), "text": e.payload.get("text"), "zeit": als_utc(e.zeit).isoformat()} if e else None


def bewerten(s: Session, m: Mandant, f: Servicefall, sterne: int, text: str | None, jetzt: datetime) -> dict[str, Any]:
    if f.status not in ("durchgefuehrt", "abgerechnet", "uebergeben"):
        raise domaene.Domaenenfehler("Eine Bewertung ist erst nach erledigter Arbeit möglich")
    if not 1 <= sterne <= 5:
        raise domaene.Domaenenfehler("Bewertung mit 1 bis 5 Sternen")
    domaene.protokollieren(s, m.id, "bewertung.abgegeben", "servicefall", f.id, "mensch", {"sterne": sterne, "text": (text or "")[:1000] or None, "kunde_id": f.kunde_id}, f.kunde.name)
    _kommunikation(s, f, f"Bewertung im Portal: {sterne}/5" + (f" – {text[:300]}" if text else ""), jetzt)
    if sterne <= 2:
        hinweise.senden(s, m, "warnung", "Kritische Bewertung", f"{f.kunde.name}: {sterne}/5 – {(text or '')[:200]}", f"bewertung:{f.id}", "vorgaenge")
    return {"bewertung": bewertung_lesen(s, f), "hinweis": "Vielen Dank für Ihre Bewertung!" if sterne >= 4 else "Vielen Dank für Ihre Rückmeldung, wir melden uns bei Ihnen."}


def bewertungen(s: Session, m: Mandant, limit: int = 50) -> dict[str, Any]:
    eintraege = s.scalars(select(Ereignis).where(Ereignis.mandant_id == m.id, Ereignis.typ == "bewertung.abgegeben").order_by(Ereignis.zeit.desc()).limit(limit)).all()
    sterne = [e.payload.get("sterne") for e in eintraege if e.payload.get("sterne")]
    return {"anzahl": len(sterne), "durchschnitt": round(sum(sterne) / len(sterne), 2) if sterne else None,
            "verteilung": {str(n): sterne.count(n) for n in range(1, 6)},
            "eintraege": [{"servicefall_id": e.entitaet_id, "kunde": e.akteur_name, "sterne": e.payload.get("sterne"), "text": e.payload.get("text"), "zeit": als_utc(e.zeit).isoformat()} for e in eintraege]}


def _haus(s: Session, m: Mandant, f: Servicefall) -> dict[str, Any]:
    """„Ihr Haus“: Anlagen des Kunden mit anstehenden Erneuerungen/Prüfungen und meldbaren Zählerarten."""
    from core import lebenszyklus, zaehler  # später Import (Zyklus über portal)
    from core.modelle import Anlage, Objekt
    from core.ontologie import ANLAGENTYPEN
    heute = datetime.now(ZEITZONE).date()
    kal = lebenszyklus.kalender(s, m, f.kunde, heute, jahre=3)
    anlagen = s.scalars(select(Anlage).join(Objekt, Objekt.id == Anlage.objekt_id).where(Objekt.kunde_id == f.kunde_id)).all()
    return {"anlagen": [{"id": a.id, "typ": getattr(ANLAGENTYPEN.get(a.anlagentyp), "bezeichnung", a.anlagentyp), "hersteller": a.hersteller, "modell": a.modell, "baujahr": a.baujahr, "merkmale": {k: v for k, v in (a.merkmale or {}).items() if k in ("kennzeichen", "hu_bis", "kilometerstand")}} for a in anlagen],
            "anstehend": [{"jahr": j["jahr"], "eintraege": [{"bezeichnung": e["bezeichnung"], "pflicht": e["pflicht"], "faellig": e["faellig"], "kosten": e["kosten_schaetzung"]} for e in j["eintraege"]]}
                          for j in kal["jahre"] if j["eintraege"]][:3],
            "ueberfaellig": [{"bezeichnung": e["bezeichnung"], "faellig": e["faellig"]} for e in kal["ueberfaellig"]],
            "zaehlerarten": {k: {"bezeichnung": b, "einheit": e} for k, (b, e, _) in zaehler.ARTEN.items()},
            "letzte_staende": zaehler.liste(s, f.kunde)[-5:]}


def _kommunikation(s: Session, f: Servicefall, text: str, jetzt: datetime) -> None:
    s.add(Kommunikation(mandant_id=f.mandant_id, kunde_id=f.kunde_id, servicefall_id=f.id, kanal="portal", richtung="eingehend",
                        zusammenfassung=text, zeit=jetzt))


def termin_bestaetigen(s: Session, m: Mandant, f: Servicefall, t: Termin, jetzt: datetime) -> dict[str, Any]:
    if t.servicefall_id != f.id or t.status == "abgesagt":
        raise domaene.Domaenenfehler("Termin gehört nicht zu diesem Vorgang oder ist abgesagt")
    _kommunikation(s, f, f"Kunde hat den Termin {_termin(t)['text']} im Portal bestätigt", jetzt)
    domaene.protokollieren(s, m.id, "termin.kunde_bestaetigt", "termin", t.id, "mensch", {"servicefall_id": f.id}, f.kunde.name)
    if t.status == "vorgeschlagen":
        domaene.aktion_beantragen(s, m, "termin_bestaetigen", {"termin_id": t.id}, "mensch", f.id, "Kunde hat im Portal bestätigt")
    from core import warteliste  # späte Einbindung (warteliste nutzt portal.link)
    warteliste.angebot_angenommen(s, m, t, jetzt)
    return {"termin": _termin(t), "hinweis": "Vielen Dank, Ihr Termin ist vorgemerkt." if t.status == "vorgeschlagen" else "Vielen Dank, Ihr Termin ist bestätigt."}


def termin_absagen(s: Session, m: Mandant, f: Servicefall, t: Termin, jetzt: datetime, grund: str | None = None) -> dict[str, Any]:
    if t.servicefall_id != f.id:
        raise domaene.Domaenenfehler("Termin gehört nicht zu diesem Vorgang")
    if t.status == "abgesagt":
        return {"termin": _termin(t), "hinweis": "Der Termin war bereits abgesagt."}
    if als_utc(t.beginn) - als_utc(jetzt) < timedelta(hours=(m.stammdaten or {}).get("absage_frist_stunden", 24)):
        raise domaene.Domaenenfehler("Kurzfristige Absagen bitte telefonisch, damit wir den Monteur noch erreichen.")
    t.status = "abgesagt"
    text = f"Kunde hat den Termin {_termin(t)['text']} im Portal abgesagt" + (f": {grund[:300]}" if grund else "")
    _kommunikation(s, f, text, jetzt)
    domaene.protokollieren(s, m.id, "termin.abgesagt", "termin", t.id, "mensch", {"servicefall_id": f.id, "grund": grund, "quelle": "portal"}, f.kunde.name)
    if f.status == "eingeplant" and not any(x.status in ("vorgeschlagen", "bestaetigt") for x in f.termine):
        domaene.uebergang(s, f, "qualifiziert", "system", "Terminabsage durch den Kunden im Portal")
    domaene.outbox_schreiben(s, m.id, "fachsoftware", "termin_upsert", {"termin_id": t.id}, f"termin_upsert:{t.id}:abgesagt")
    hinweise.senden(s, m, "info", "Terminabsage im Portal", f"{f.kunde.name}: {f.anliegen} – Termin {_termin(t)['text']} abgesagt.", f"absage:{t.id}", "plantafel")
    from core import warteliste  # späte Einbindung
    warteliste.nachbesetzen(s, m, t.ressource_id, t.beginn, t.ende, jetzt)
    return {"termin": _termin(t), "hinweis": "Der Termin ist abgesagt. Wir melden uns für einen neuen Termin."}


def erinnerungen(s: Session, m: Mandant, jetzt: datetime, vorlauf_tage: int = 1) -> int:
    """Terminerinnerung für bestätigte Termine am Folgetag (Tageslauf); Outbox-Schlüssel verhindert Doppelversand."""
    tag = (als_utc(jetzt).astimezone(ZEITZONE) + timedelta(days=vorlauf_tage)).date()
    von = als_utc(datetime.combine(tag, datetime.min.time(), ZEITZONE))
    bis = von + timedelta(days=1)
    n = 0
    for t in s.scalars(select(Termin).where(Termin.mandant_id == m.id, Termin.status == "bestaetigt", Termin.beginn >= von, Termin.beginn < bis)):
        f = t.servicefall
        if not (f.kunde.email or f.kunde.telefon):
            continue
        portal = link(m, f.id)
        text = (f"Guten Tag {f.kunde.name},\n\nwir erinnern an Ihren Termin {_termin(t)['text']} (Monteur: {t.ressource.name}).\nAnliegen: {f.anliegen}\n"
                + (f"\nTermin online bestätigen oder absagen: {portal}\n" if portal else "\nSollte der Termin nicht passen, rufen Sie uns bitte an.\n"))
        schluessel = f"erinnerung:{t.id}"
        if s.scalar(select(OutboxEintrag).where(OutboxEintrag.mandant_id == m.id, OutboxEintrag.idempotenz_schluessel == schluessel)) is not None:
            continue
        domaene.outbox_schreiben(s, m.id, "endkunde", "nachricht", {"kunde_id": f.kunde_id, "servicefall_id": f.id, "betreff": f"Terminerinnerung {tag.strftime('%d.%m.%Y')}",
                                                                 "text": text, "kurz": f"Erinnerung: Termin {_termin(t)['text']} bei {m.name}."}, schluessel)
        n += 1
    return n
