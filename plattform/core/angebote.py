"""Angebote und Artikelstamm.

Angebot → (Annahme) Auftrag als Servicefall → Rechnung über Freigabe der Klasse C mit den Angebotspositionen.
Nummernkreis AN-JJJJ-NNNNN je Mandant (Zeilensperre wie bei Rechnungen). Versand an den Kunden läuft über
die Outbox (Aktion angebot_zustellen, Freigabe Klasse B). Abgelaufene Angebote markiert der Tageslauf.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene
from core.modelle import Angebot, Artikel, Kunde, Mandant, Servicefall
from core.rechnung import Position, dokument_pdf, positionen_aus_payload, summen

STATUS_UEBERGAENGE = {"entwurf": {"versendet", "angenommen", "abgelehnt"}, "versendet": {"angenommen", "abgelehnt", "abgelaufen"},
                      "angenommen": set(), "abgelehnt": set(), "abgelaufen": {"angenommen"}}


def naechste_angebotsnummer(s: Session, m: Mandant, datum: date) -> str:
    gesperrt = s.execute(select(Mandant).where(Mandant.id == m.id).with_for_update()).scalar_one()
    gesperrt.angebotsnummer_zaehler += 1
    s.flush()
    return f"{m.stammdaten.get('angebotsnummer_praefix', 'AN')}-{datum.year}-{gesperrt.angebotsnummer_zaehler:05d}"


def positionen_aus_artikeln(s: Session, m: Mandant, zeilen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ergänzt Positionen, die nur artikel_id und menge tragen, um Bezeichnung, Einheit, Preis und Steuersatz."""
    ergebnis = []
    for z in zeilen:
        if z.get("artikel_id"):
            a = s.get(Artikel, z["artikel_id"])
            if a is None or a.mandant_id != m.id:
                raise ValueError("Artikel nicht gefunden")
            ergebnis.append({"bezeichnung": z.get("bezeichnung") or a.bezeichnung, "menge": z.get("menge", 1), "einheit": a.einheit,
                             "einzelpreis_netto": z.get("einzelpreis_netto") or a.verkauf_netto, "ust_satz": a.ust_satz, "artikel_id": a.id})
        else:
            ergebnis.append(z)
    return ergebnis


def angebot_erzeugen(s: Session, m: Mandant, payload: dict[str, Any], datum: date, akteur_name: str | None = None) -> Angebot:
    kunde = s.get(Kunde, payload.get("kunde_id", ""))
    if kunde is None or kunde.mandant_id != m.id:
        raise ValueError("Angebot ohne Kunde")
    fall = s.get(Servicefall, payload["servicefall_id"]) if payload.get("servicefall_id") else None
    if fall is not None and fall.mandant_id != m.id:
        raise ValueError("Servicefall gehört nicht zum Mandanten")
    zeilen = positionen_aus_artikeln(s, m, payload.get("positionen") or [])
    positionen = positionen_aus_payload({"positionen": zeilen}, bool(m.stammdaten.get("kleinunternehmer"))) if zeilen else []
    netto, ust, brutto, _ = summen(positionen)
    a = Angebot(mandant_id=m.id, nummer=naechste_angebotsnummer(s, m, datum), kunde_id=kunde.id, servicefall_id=fall.id if fall else None,
                titel=payload.get("titel") or (fall.anliegen if fall else "Angebot"), einleitung=payload.get("einleitung"), datum=datum,
                gueltig_bis=date.fromisoformat(payload["gueltig_bis"]) if payload.get("gueltig_bis") else datum + timedelta(days=int(m.stammdaten.get("angebot_gueltig_tage", 30))),
                positionen=[{**p.als_dict(), **({"artikel_id": z["artikel_id"]} if z.get("artikel_id") else {})} for p, z in zip(positionen, zeilen)],
                netto=str(netto), umsatzsteuer=str(ust), brutto=str(brutto),
                empfaenger={"name": kunde.name, "strasse": kunde.strasse, "plz": kunde.plz, "ort": kunde.ort, "email": kunde.email})
    s.add(a)
    s.flush()
    domaene.protokollieren(s, m.id, "angebot.erstellt", "angebot", a.id, "mensch", {"nummer": a.nummer, "brutto": a.brutto}, akteur_name)
    return a


def _wechsel(s: Session, a: Angebot, ziel: str, akteur: str, akteur_name: str | None, heute: date) -> None:
    if ziel not in STATUS_UEBERGAENGE[a.status]:
        raise domaene.Domaenenfehler(f"Angebot {a.nummer} ist {a.status}; Übergang nach {ziel} nicht zulässig")
    vorher, a.status = a.status, ziel
    if ziel in ("angenommen", "abgelehnt"):
        a.entschieden_am = heute
    domaene.protokollieren(s, a.mandant_id, f"angebot.{ziel}", "angebot", a.id, akteur, {"nummer": a.nummer, "von": vorher}, akteur_name)


def versand_beantragen(s: Session, m: Mandant, a: Angebot, akteur_name: str | None = None):
    """Freigabe Klasse B: der Versand ist kundenwirksam (Preisbindung bis gueltig_bis)."""
    if a.status not in ("entwurf", "versendet"):
        raise domaene.Domaenenfehler(f"Angebot {a.nummer} ist {a.status}")
    if not a.positionen:
        raise ValueError(f"Angebot {a.nummer} hat keine Positionen")
    return domaene.aktion_beantragen(s, m, "angebot_senden", {"angebot_id": a.id, "kunde_id": a.kunde_id}, "mensch", a.servicefall_id,
                                     f"Angebot {a.nummer} über {a.brutto.replace('.', ',')} € an {a.empfaenger.get('name', '')}")


def versenden(s: Session, a: Angebot, heute: date) -> None:
    """Wirkung der Freigabe: Status und Outbox (E-Mail mit PDF; ohne E-Mail bleibt es eine Büro-Aufgabe)."""
    if a.status == "entwurf":
        _wechsel(s, a, "versendet", "system", None, heute)
    domaene.outbox_schreiben(s, a.mandant_id, "endkunde", "angebot_zustellen", {"angebot_id": a.id, "kunde_id": a.kunde_id},
                             f"angebot_zustellen:{a.id}:{a.status}")


def annehmen(s: Session, m: Mandant, a: Angebot, heute: date, akteur_name: str | None = None) -> Servicefall:
    """Annahme: Auftrag entsteht als Servicefall (Status qualifiziert), Kunde erhält Auftragsbestätigung."""
    _wechsel(s, a, "angenommen", "mensch", akteur_name, heute)
    fall = s.get(Servicefall, a.servicefall_id) if a.servicefall_id else None
    if fall is None:
        fall = Servicefall(mandant_id=m.id, kunde_id=a.kunde_id, anliegen=f"Auftrag {a.nummer}: {a.titel}", quelle="angebot",
                           dringlichkeit="niedrig", dauer_minuten=240)
        s.add(fall)
        s.flush()
        domaene.protokollieren(s, m.id, "servicefall.eingegangen", "servicefall", fall.id, "mensch", {"quelle": "angebot", "angebot": a.nummer}, akteur_name)
        a.servicefall_id = fall.id
    if fall.status == "eingegangen":
        domaene.uebergang(s, fall, "qualifiziert", "mensch", f"Angebot {a.nummer} angenommen", akteur_name)
    domaene.aktion_beantragen(s, m, "nachricht_an_endkunde", {"kunde_id": a.kunde_id, "betreff": f"Auftragsbestätigung zu Angebot {a.nummer}",
                              "text": f"Guten Tag {a.empfaenger.get('name', '')},\n\nvielen Dank für Ihren Auftrag zu unserem Angebot {a.nummer} "
                                      f"({a.titel}, {a.brutto.replace('.', ',')} € brutto). Wir melden uns zur Terminabstimmung."},
                              "system", fall.id, f"Auftragsbestätigung {a.nummer}")
    return fall


def ablehnen(s: Session, a: Angebot, heute: date, grund: str | None = None, akteur_name: str | None = None) -> None:
    _wechsel(s, a, "abgelehnt", "mensch", akteur_name, heute)
    if grund:
        domaene.protokollieren(s, a.mandant_id, "angebot.grund", "angebot", a.id, "mensch", {"grund": grund}, akteur_name)


def rechnung_beantragen(s: Session, m: Mandant, a: Angebot, akteur_name: str | None = None):
    """Rechnung mit den Angebotspositionen; entsteht erst mit der Freigabe der Inhaberrolle (Klasse C)."""
    if a.status != "angenommen":
        raise domaene.Domaenenfehler("Nur angenommene Angebote werden abgerechnet")
    if a.rechnung_id:
        raise domaene.Domaenenfehler(f"Angebot {a.nummer} ist bereits abgerechnet")
    payload = {"kunde_id": a.kunde_id, "servicefall_id": a.servicefall_id, "angebot_id": a.id,
               "positionen": [{k: v for k, v in p.items() if k != "netto"} for p in a.positionen]}
    return domaene.aktion_beantragen(s, m, "rechnung_erstellen", payload, "mensch", a.servicefall_id,
                                     f"Rechnung zu Angebot {a.nummer} ({a.brutto.replace('.', ',')} € brutto)")


def ablauf_pruefen(s: Session, m: Mandant, heute: date) -> int:
    """Tageslauf: versendete Angebote nach gueltig_bis als abgelaufen markieren."""
    n = 0
    for a in s.scalars(select(Angebot).where(Angebot.mandant_id == m.id, Angebot.status == "versendet", Angebot.gueltig_bis < heute)):
        _wechsel(s, a, "abgelaufen", "system", None, heute)
        n += 1
    return n


def _positionen(a: Angebot) -> list[Position]:
    return [Position(p["bezeichnung"], Decimal(p["menge"]), p["einheit"], Decimal(p["einzelpreis_netto"]), Decimal(p["ust_satz"])) for p in a.positionen]


def pdf(a: Angebot, m: Mandant) -> bytes:
    vortext = [a.titel, ""] + [z for z in (a.einleitung or "Gerne unterbreiten wir Ihnen folgendes Angebot:").splitlines()]
    schluss = [f"Dieses Angebot ist gültig bis {a.gueltig_bis.strftime('%d.%m.%Y')}. Es gelten unsere Allgemeinen Geschäftsbedingungen.",
               f"Zahlungsziel nach Rechnungsstellung: {m.stammdaten.get('zahlungsziel_tage', 14)} Tage."]
    return dokument_pdf(m, "Angebot", a.nummer, a.datum, a.empfaenger, _positionen(a), Decimal(a.netto), Decimal(a.brutto),
                        [f"Gültig bis: {a.gueltig_bis.strftime('%d.%m.%Y')}"], vortext, schluss)


def quote(s: Session, m: Mandant, ab: date) -> dict[str, Any]:
    liste = s.scalars(select(Angebot).where(Angebot.mandant_id == m.id, Angebot.datum >= ab)).all()
    entschieden = [a for a in liste if a.status in ("angenommen", "abgelehnt", "abgelaufen")]
    angenommen = [a for a in liste if a.status == "angenommen"]
    return {"angebote": len(liste), "angenommen": len(angenommen), "entschieden": len(entschieden),
            "quote": round(len(angenommen) / len(entschieden), 2) if entschieden else None,
            "volumen_offen_netto": str(sum((Decimal(a.netto) for a in liste if a.status in ("entwurf", "versendet")), Decimal("0"))),
            "volumen_angenommen_netto": str(sum((Decimal(a.netto) for a in angenommen), Decimal("0")))}
