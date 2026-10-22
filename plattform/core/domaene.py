"""Domänenlogik: Statusautomat, Event-Log, Outbox und Freigabe-Policies.

Invarianten:
- Zustandsübergänge eines Servicefalls laufen ausschließlich über `uebergang()`, das den
  Übergang prüft und als Ereignis protokolliert (Event Sourcing light).
- Kundenwirksame und finanzwirksame Aktionen laufen über `aktion_beantragen()`: Klasse A wird
  sofort ausgeführt, B nach Policy des Mandanten automatisch oder über die Freigabe-Inbox,
  C immer über die Inbox. Ausführung heißt: Fachänderung plus Outbox-Eintrag in einer Transaktion.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.modelle import Ereignis, Freigabe, Mandant, OutboxEintrag, Rechnung, Servicefall, Termin, jetzt

# Statusautomat des Servicefalls (Architektur: eingegangen → … → an Kanzlei übergeben)
UEBERGAENGE: dict[str, set[str]] = {
    "eingegangen": {"qualifiziert", "storniert"},
    "qualifiziert": {"eingeplant", "storniert"},
    "eingeplant": {"durchgefuehrt", "qualifiziert", "storniert"},  # zurück bei Terminabsage
    "durchgefuehrt": {"abgerechnet"},
    "abgerechnet": {"uebergeben"},
    "uebergeben": set(),
    "storniert": set(),
}

# Risikoklassen je Aktion (Architektur: Freigabe-Policies)
RISIKOKLASSEN: dict[str, str] = {
    "kunde_anlegen": "A",
    "notiz_anlegen": "A",
    "termin_vorschlagen": "A",
    "termin_bestaetigen": "B",
    "auftrag_an_fachsoftware": "B",
    "nachricht_an_endkunde": "B",
    "rechnung_erstellen": "C",
    "storno": "C",
    "belegversand_kanzlei": "C",
    "angebot_senden": "B",
    "zahlungserinnerung_senden": "B",
    "mahnung_senden": "C",
    "bewertung_anfragen": "B",
}

AKTEURE = {"mensch", "ki", "system"}


class Domaenenfehler(ValueError):
    """Fachlich unzulässige Operation (wird von der API als 409/422 gemeldet)."""


def als_utc(zeit: datetime) -> datetime:
    """SQLite liefert naive Zeitstempel zurück; alle Zeiten sind intern UTC."""
    return zeit.replace(tzinfo=timezone.utc) if zeit.tzinfo is None else zeit.astimezone(timezone.utc)


def protokollieren(s: Session, mandant_id: str, typ: str, entitaet: str, entitaet_id: str,
                   akteur: str, payload: dict[str, Any] | None = None, akteur_name: str | None = None) -> Ereignis:
    if akteur not in AKTEURE:
        raise Domaenenfehler(f"Unbekannter Akteur {akteur!r}")
    e = Ereignis(mandant_id=mandant_id, typ=typ, entitaet=entitaet, entitaet_id=entitaet_id,
                 payload=payload or {}, akteur=akteur, akteur_name=akteur_name)
    s.add(e)
    return e


def outbox_schreiben(s: Session, mandant_id: str, ziel: str, aktion: str, payload: dict[str, Any],
                     idempotenz_schluessel: str) -> OutboxEintrag:
    """Idempotent: derselbe Schlüssel erzeugt keinen zweiten Eintrag (keine doppelten Aufträge)."""
    vorhanden = s.scalar(select(OutboxEintrag).where(
        OutboxEintrag.mandant_id == mandant_id, OutboxEintrag.idempotenz_schluessel == idempotenz_schluessel))
    if vorhanden:
        return vorhanden
    eintrag = OutboxEintrag(mandant_id=mandant_id, ziel=ziel, aktion=aktion, payload=payload,
                            idempotenz_schluessel=idempotenz_schluessel)
    s.add(eintrag)
    return eintrag


def uebergang(s: Session, fall: Servicefall, ziel: str, akteur: str, grund: str | None = None,
              akteur_name: str | None = None) -> Servicefall:
    erlaubt = UEBERGAENGE.get(fall.status, set())
    if ziel not in erlaubt:
        raise Domaenenfehler(f"Übergang {fall.status} → {ziel} ist nicht zulässig (erlaubt: {sorted(erlaubt)})")
    vorher = fall.status
    fall.status = ziel
    protokollieren(s, fall.mandant_id, f"servicefall.{ziel}", "servicefall", fall.id, akteur,
                   {"von": vorher, "nach": ziel, "grund": grund}, akteur_name)
    return fall


def risikoklasse(aktion: str) -> str:
    if aktion not in RISIKOKLASSEN:
        raise Domaenenfehler(f"Unbekannte Aktion {aktion!r}")
    return RISIKOKLASSEN[aktion]


def automatisch_freigegeben(mandant: Mandant, aktion: str) -> bool:
    klasse = risikoklasse(aktion)
    if klasse == "A":
        return True
    if klasse == "C":
        return False  # finanz- oder rechtswirksam: stets manuell, Policy kann das nicht überstimmen
    return (mandant.policies or {}).get(aktion) == "auto"


def aktion_beantragen(s: Session, mandant: Mandant, aktion: str, payload: dict[str, Any], akteur: str,
                      servicefall_id: str | None = None, begruendung: str | None = None) -> Freigabe:
    """Legt einen Freigabe-Eintrag an und führt ihn aus, wenn die Policy es erlaubt."""
    f = Freigabe(mandant_id=mandant.id, aktion=aktion, risikoklasse=risikoklasse(aktion), payload=payload,
                 servicefall_id=servicefall_id, begruendung=begruendung)
    s.add(f)
    s.flush()
    protokollieren(s, mandant.id, "freigabe.beantragt", "freigabe", f.id, akteur,
                   {"aktion": aktion, "klasse": f.risikoklasse})
    if automatisch_freigegeben(mandant, aktion):
        entscheiden(s, f, freigeben=True, wer="policy", akteur="system")
    return f


def entscheiden(s: Session, f: Freigabe, freigeben: bool, wer: str, akteur: str = "mensch") -> Freigabe:
    if f.status != "offen":
        raise Domaenenfehler(f"Freigabe {f.id} ist bereits {f.status}")
    f.status = "freigegeben" if freigeben else "abgelehnt"
    f.entschieden_von, f.entschieden_am = wer, jetzt()
    protokollieren(s, f.mandant_id, f"freigabe.{f.status}", "freigabe", f.id, akteur,
                   {"aktion": f.aktion}, akteur_name=wer)
    if freigeben:
        _ausfuehren(s, f)
    return f


def _ausfuehren(s: Session, f: Freigabe) -> None:
    """Wirkung einer freigegebenen Aktion: Fachänderung und Outbox in derselben Transaktion."""
    p = f.payload
    if f.aktion == "termin_bestaetigen":
        termin = s.get(Termin, p["termin_id"])
        if termin is None or termin.mandant_id != f.mandant_id:
            raise Domaenenfehler("Termin nicht gefunden")
        termin.status = "bestaetigt"
        fall = termin.servicefall
        if fall.status == "qualifiziert":
            uebergang(s, fall, "eingeplant", "system", grund=f"Freigabe {f.id}")
        outbox_schreiben(s, f.mandant_id, "fachsoftware", "termin_upsert", {"termin_id": termin.id},
                         f"termin_upsert:{termin.id}")
        outbox_schreiben(s, f.mandant_id, "endkunde", "terminbestaetigung", {"termin_id": termin.id},
                         f"terminbestaetigung:{termin.id}")
    elif f.aktion == "auftrag_an_fachsoftware":
        outbox_schreiben(s, f.mandant_id, "fachsoftware", "auftrag_anlegen", p,
                         f"auftrag_anlegen:{p.get('servicefall_id', f.id)}")
    elif f.aktion == "nachricht_an_endkunde":
        outbox_schreiben(s, f.mandant_id, "endkunde", "nachricht", p, f"nachricht:{f.id}")
    elif f.aktion == "rechnung_erstellen":
        from core.rechnung import rechnung_erzeugen  # zyklischer Import vermieden

        mandant = s.get(Mandant, f.mandant_id)
        if f.servicefall_id and not p.get("servicefall_id"):
            p = {**p, "servicefall_id": f.servicefall_id}  # Rechnung hängt am Vorgang der Freigabe (Nachkalkulation)
        r = rechnung_erzeugen(s, mandant, p, jetzt().date())
        protokollieren(s, f.mandant_id, "rechnung.gestellt", "rechnung", r.id, "system",
                       {"nummer": r.nummer, "brutto": r.brutto, "freigabe_id": f.id})
        outbox_schreiben(s, f.mandant_id, "finanz", "rechnung_uebermitteln", {"rechnung_id": r.id}, f"rechnung:{r.id}")
        outbox_schreiben(s, f.mandant_id, "endkunde", "rechnung_zustellen", {"rechnung_id": r.id}, f"rechnung_zustellen:{r.id}")
        if p.get("angebot_id"):
            from core.modelle import Angebot
            a = s.get(Angebot, p["angebot_id"])
            if a is not None and a.mandant_id == f.mandant_id:
                a.rechnung_id = r.id
    elif f.aktion == "angebot_senden":
        from core import angebote
        from core.modelle import Angebot
        a = s.get(Angebot, p.get("angebot_id", ""))
        if a is None or a.mandant_id != f.mandant_id:
            raise Domaenenfehler("Angebot nicht gefunden")
        angebote.versenden(s, a, jetzt().date())
    elif f.aktion in ("zahlungserinnerung_senden", "mahnung_senden"):
        from core import forderungen
        mandant = s.get(Mandant, f.mandant_id)
        r = s.get(Rechnung, p.get("rechnung_id", ""))
        if r is None or r.mandant_id != f.mandant_id:
            raise Domaenenfehler("Rechnung nicht gefunden")
        if forderungen.offen(s, r) <= 0:
            raise Domaenenfehler(f"Rechnung {r.nummer} ist inzwischen bezahlt")
        forderungen.mahnung_erzeugen(s, mandant, r, int(p.get("stufe", 1)), jetzt().date(), str(p.get("gebuehr", "0.00")))
    elif f.aktion == "bewertung_anfragen":
        outbox_schreiben(s, f.mandant_id, "endkunde", "nachricht", {"kunde_id": p.get("kunde_id"), "servicefall_id": p.get("servicefall_id"),
                         "betreff": p.get("betreff", "Wie zufrieden waren Sie?"), "text": p.get("text", "")}, f"bewertung:{p.get('servicefall_id', f.id)}")
    elif f.aktion == "storno":
        from core.rechnung import storno_erzeugen

        mandant = s.get(Mandant, f.mandant_id)
        original = s.get(Rechnung, p.get("rechnung_id", ""))
        if original is None or original.mandant_id != f.mandant_id:
            raise Domaenenfehler("Rechnung für Storno nicht gefunden")
        st = storno_erzeugen(s, mandant, original, jetzt().date())
        protokollieren(s, f.mandant_id, "rechnung.storniert", "rechnung", original.id, "system",
                       {"storno": st.nummer, "grund": p.get("grund"), "freigabe_id": f.id})
        outbox_schreiben(s, f.mandant_id, "finanz", "rechnung_uebermitteln", {"rechnung_id": st.id}, f"rechnung:{st.id}")
        outbox_schreiben(s, f.mandant_id, "endkunde", "rechnung_zustellen", {"rechnung_id": st.id}, f"rechnung_zustellen:{st.id}")
    elif f.aktion == "belegversand_kanzlei":
        outbox_schreiben(s, f.mandant_id, "kanzlei", "belegpaket", p, f"belegpaket:{p.get('zeitraum', f.id)}")
    # Klasse A (kunde_anlegen, notiz_anlegen, termin_vorschlagen) wirkt bereits beim Anlegen
