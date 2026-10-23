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
from core.modelle import Angebot, Kommunikation, Mandant, OutboxEintrag, Rechnung, Servicefall, Termin


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


def daten(s: Session, m: Mandant, f: Servicefall) -> dict[str, Any]:
    st = m.stammdaten or {}
    angebote = s.scalars(select(Angebot).where(Angebot.servicefall_id == f.id, Angebot.status.in_(("versendet", "angenommen")))).all()
    rechnungen = s.scalars(select(Rechnung).where(Rechnung.servicefall_id == f.id, Rechnung.art == "rechnung")).all()
    return {"betrieb": {"name": m.name, "telefon": st.get("telefon"), "email": st.get("email"), "strasse": st.get("strasse"),
                        "plz": st.get("plz"), "ort": st.get("ort"), "impressum_url": st.get("impressum_url"), "datenschutz_url": st.get("datenschutz_url")},
            "kunde": {"name": f.kunde.name},
            "vorgang": {"id": f.id, "anliegen": f.anliegen, "status": f.status, "dringlichkeit": f.dringlichkeit},
            "termine": [_termin(t) for t in sorted(f.termine, key=lambda t: t.beginn) if t.status != "abgesagt"],
            "angebote": [{"id": a.id, "nummer": a.nummer, "titel": a.titel, "brutto": a.brutto, "status": a.status, "gueltig_bis": a.gueltig_bis.isoformat()} for a in angebote],
            "rechnungen": [{"id": r.id, "nummer": r.nummer, "brutto": r.brutto, "status": r.status, "faellig": r.faellig.isoformat()} for r in rechnungen],
            "haus": _haus(s, m, f)}


def _haus(s: Session, m: Mandant, f: Servicefall) -> dict[str, Any]:
    """„Ihr Haus“: Anlagen des Kunden mit anstehenden Erneuerungen/Prüfungen und meldbaren Zählerarten."""
    from core import lebenszyklus, zaehler   # später Import (Zyklus über portal)
    from core.modelle import Anlage, Objekt
    from core.ontologie import ANLAGENTYPEN
    heute = datetime.now(ZEITZONE).date()
    kal = lebenszyklus.kalender(s, m, f.kunde, heute, jahre=3)
    anlagen = s.scalars(select(Anlage).join(Objekt, Objekt.id == Anlage.objekt_id).where(Objekt.kunde_id == f.kunde_id)).all()
    return {"anlagen": [{"id": a.id, "typ": getattr(ANLAGENTYPEN.get(a.anlagentyp), "bezeichnung", a.anlagentyp), "hersteller": a.hersteller, "modell": a.modell, "baujahr": a.baujahr} for a in anlagen],
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
