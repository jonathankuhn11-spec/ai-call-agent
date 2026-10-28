"""Löschkonzept als Code: Aufbewahrungsfristen je Datenart, regelmäßiger Löschlauf, Betroffenenrechte.

Rechtsgrundlagen (ohne Gewähr, siehe recht/LOESCHKONZEPT.md): Art. 5 Abs. 1 lit. e DSGVO
(Speicherbegrenzung), Art. 17 (Löschung), Art. 15 (Auskunft); Aufbewahrungspflichten § 147 AO und
§ 14b UStG (Rechnungen 10 Jahre, ab 2025 für Buchungsbelege 8 Jahre nach BEG IV; wir behalten 10),
§ 257 HGB. Gesprächsaufzeichnungen und Transkripte sind keine Buchungsbelege und werden früh gelöscht.

Fristen in Tagen, je Mandant überschreibbar über Mandant.policies["aufbewahrung"]:
  transkript_tage        90    Transkript wird entfernt, Zusammenfassung und Extraktion bleiben beim Vorgang
  kommunikation_lose_tage 30   Kommunikation ohne Kunde und ohne Vorgang (z. B. Falschwahl, Werbeanruf)
  outbox_tage            90    zugestellte und ohne_kanal; tote Einträge 365 Tage (Fehleranalyse)
  ereignis_tage          1095  Event-Log, außer rechnung.* und freigabe.* der Klasse C (10 Jahre, Nachweis)
  vorgang_tage           1095  abgeschlossene Vorgänge ohne Rechnung (Gewährleistung § 634a BGB: bis 5 Jahre bei Bauwerken,
                               deshalb Standard 3 Jahre nach Abschluss, für Bauwerke 1825 setzen)
  kunde_inaktiv_tage     1095  Kunden ohne Vorgang, Vertrag und Rechnung seit dieser Frist werden anonymisiert
  rechnung_tage          3650  Rechnungen bleiben; der Kunde kann anonymisiert werden, die Rechnung trägt den Empfänger als Momentaufnahme
  beleg_tage             3650  Eingangsbelege mit Datei (§ 147 AO); Rapporte, Fotos und Zeiten folgen dem Vorgang (vorgang_tage),
                               Zeiteinträge außerhalb von Vorgängen zwei Jahre (§ 16 Abs. 2 ArbZG)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from core.domaene import protokollieren
from core.modelle import (Abwesenheit, Anfrage, Angebot, Anhang, Anlage, Bauteil, Beleg, Ereignis, Kommunikation, Kunde, Mandant, Objekt,
                          OutboxEintrag, Rapport, Rechnung, Servicefall, Termin, Vertrag, Zaehlerstand, Zeiteintrag)

STANDARD_FRISTEN = {"transkript_tage": 90, "kommunikation_lose_tage": 30, "outbox_tage": 90, "outbox_tot_tage": 365,
                    "ereignis_tage": 1095, "vorgang_tage": 1095, "kunde_inaktiv_tage": 1095, "rechnung_tage": 3650,
                    "beleg_tage": 3650, "zeit_tage": 730, "angebot_tage": 1095, "vertrag_tage": 1095, "anfrage_tage": 365}
ABGESCHLOSSEN = ("uebergeben", "storniert", "abgerechnet")
DAUERHAFT = ("rechnung.", "freigabe.", "loeschung.", "kunde.geloescht")
ANONYM = "Gelöschte Person"


def fristen(m: Mandant) -> dict[str, int]:
    return {**STANDARD_FRISTEN, **{k: int(v) for k, v in (m.policies.get("aufbewahrung") or {}).items()}}


def _sperrfrist(s: Session, kunde: Kunde, jetzt: datetime, f: dict[str, int]) -> str | None:
    """Gründe, die einer vollständigen Löschung entgegenstehen (Art. 17 Abs. 3 lit. b DSGVO)."""
    offen = s.scalar(select(exists().where(Servicefall.kunde_id == kunde.id, Servicefall.status.notin_(ABGESCHLOSSEN))))
    if offen:
        return "offener Vorgang"
    if kunde.vertragsstatus == "wartungsvertrag" or s.scalar(select(exists().where(Vertrag.kunde_id == kunde.id, Vertrag.status == "aktiv"))):
        return "laufender Wartungsvertrag"
    letzte = s.scalar(select(Rechnung.datum).where(Rechnung.kunde_id == kunde.id).order_by(Rechnung.datum.desc()).limit(1))
    if letzte and (jetzt.date() - letzte).days < f["rechnung_tage"]:
        return f"Rechnung vom {letzte.isoformat()} in der Aufbewahrungsfrist (Anonymisierung statt Löschung)"
    return None


def anonymisieren(s: Session, kunde: Kunde, akteur: str, grund: str) -> dict[str, Any]:
    """Personenbezug entfernen, Fachdaten (Anlagen, Vorgänge, Rechnungen) bleiben für Nachweis und Statistik."""
    kunde.name, kunde.telefon, kunde.email, kunde.strasse, kunde.notiz = ANONYM, None, None, None, None
    kunde.einwilligungen = {}
    kunde.kartei = {}
    kunde.vertragsstatus = "ohne"
    for o in kunde.objekte:
        o.strasse, o.bezeichnung = None, "Objekt (anonymisiert)"
    for k in s.scalars(select(Kommunikation).where(Kommunikation.kunde_id == kunde.id)):
        k.transkript, k.zusammenfassung, k.extraktion = None, None, {}
    s.execute(delete(Ereignis).where(Ereignis.mandant_id == kunde.mandant_id, Ereignis.entitaet == "kunde",
                                     Ereignis.entitaet_id == kunde.id))
    protokollieren(s, kunde.mandant_id, "kunde.geloescht", "kunde", kunde.id, akteur, {"grund": grund, "art": "anonymisiert"})
    return {"kunde_id": kunde.id, "art": "anonymisiert", "grund": grund}


def _vorgangsanhang_loeschen(s: Session, fall_ids: list[str]) -> None:
    """Rapporte, Zeiten, Fotos und Unterschriften der Vorgänge entfernen; Eingangsbelege bleiben (§ 147 AO) und
    verlieren nur die Zuordnung."""
    if not fall_ids:
        return
    rapporte = list(s.scalars(select(Rapport.id).where(Rapport.servicefall_id.in_(fall_ids))))
    s.execute(delete(Anhang).where((Anhang.bezug_typ == "servicefall") & Anhang.bezug_id.in_(fall_ids)))
    if rapporte:
        s.execute(delete(Anhang).where((Anhang.bezug_typ == "rapport") & Anhang.bezug_id.in_(rapporte)))
        s.execute(delete(Rapport).where(Rapport.id.in_(rapporte)))
    s.execute(delete(Zeiteintrag).where(Zeiteintrag.servicefall_id.in_(fall_ids)))
    for b in s.scalars(select(Beleg).where(Beleg.servicefall_id.in_(fall_ids))):
        b.servicefall_id = None


def kunde_loeschen(s: Session, kunde: Kunde, jetzt: datetime, akteur: str = "mensch", erzwingen: bool = False) -> dict[str, Any]:
    """Löschantrag nach Art. 17: vollständig, wenn keine Sperrfrist; sonst Anonymisierung oder Ablehnung."""
    f = fristen(s.get(Mandant, kunde.mandant_id))
    sperre = _sperrfrist(s, kunde, jetzt, f)
    if sperre in ("offener Vorgang", "laufender Wartungsvertrag") and not erzwingen:
        raise ValueError(f"Löschung nicht möglich: {sperre}. Vorgang abschließen bzw. Vertrag beenden.")
    if sperre and sperre.startswith("Rechnung"):
        return anonymisieren(s, kunde, akteur, f"Löschantrag; {sperre}")
    ids = list(s.scalars(select(Servicefall.id).where(Servicefall.kunde_id == kunde.id)))
    _vorgangsanhang_loeschen(s, ids)
    s.execute(delete(Termin).where(Termin.servicefall_id.in_(ids)))
    s.execute(delete(Kommunikation).where((Kommunikation.kunde_id == kunde.id) | (Kommunikation.servicefall_id.in_(ids))))
    s.execute(delete(Anfrage).where(Anfrage.kunde_id == kunde.id))
    s.execute(delete(Angebot).where(Angebot.kunde_id == kunde.id))
    s.execute(delete(Vertrag).where(Vertrag.kunde_id == kunde.id))
    s.execute(delete(Servicefall).where(Servicefall.id.in_(ids)))
    objekte = list(s.scalars(select(Objekt.id).where(Objekt.kunde_id == kunde.id)))
    s.execute(delete(Anhang).where(((Anhang.bezug_typ == "kunde") & (Anhang.bezug_id == kunde.id))
                                   | ((Anhang.bezug_typ == "objekt") & Anhang.bezug_id.in_(objekte))))
    anlagen = list(s.scalars(select(Anlage.id).where(Anlage.objekt_id.in_(objekte))))
    s.execute(delete(Bauteil).where(Bauteil.anlage_id.in_(anlagen)))
    s.execute(delete(Zaehlerstand).where(Zaehlerstand.kunde_id == kunde.id))
    s.execute(delete(Anlage).where(Anlage.objekt_id.in_(objekte)))
    s.execute(delete(Objekt).where(Objekt.id.in_(objekte)))
    s.execute(delete(Ereignis).where(Ereignis.mandant_id == kunde.mandant_id, Ereignis.entitaet_id.in_([kunde.id, *ids, *objekte, *anlagen])))
    kunde_id, mandant_id = kunde.id, kunde.mandant_id
    s.delete(kunde)
    protokollieren(s, mandant_id, "kunde.geloescht", "kunde", kunde_id, akteur, {"grund": "Löschantrag", "art": "vollständig",
                                                                                 "vorgaenge": len(ids)})
    return {"kunde_id": kunde_id, "art": "vollständig", "vorgaenge": len(ids)}


def auskunft(s: Session, kunde: Kunde) -> dict[str, Any]:
    """Datenauskunft nach Art. 15 DSGVO als strukturierter Export (auch Art. 20)."""
    faelle = s.scalars(select(Servicefall).where(Servicefall.kunde_id == kunde.id)).all()
    return {
        "kunde": {"name": kunde.name, "telefon": kunde.telefon, "email": kunde.email, "strasse": kunde.strasse, "plz": kunde.plz,
                  "ort": kunde.ort, "vertragsstatus": kunde.vertragsstatus, "einwilligungen": kunde.einwilligungen, "kartei": kunde.kartei,
                  "erstellt": kunde.erstellt.isoformat()},
        "objekte": [{"bezeichnung": o.bezeichnung, "ort": o.ort, "anlagen": [{"typ": a.anlagentyp, "hersteller": a.hersteller,
                     "baujahr": a.baujahr, "letzte_wartung": a.letzte_wartung.isoformat() if a.letzte_wartung else None}
                                                                              for a in o.anlagen]} for o in kunde.objekte],
        "vorgaenge": [{"anliegen": f.anliegen, "status": f.status, "dringlichkeit": f.dringlichkeit, "erstellt": f.erstellt.isoformat(),
                       "termine": [{"beginn": t.beginn.isoformat(), "status": t.status} for t in f.termine],
                       "rapporte": [{"nummer": r.nummer, "taetigkeiten": r.taetigkeiten, "unterzeichnet_von": r.unterzeichnet_von}
                                    for r in s.scalars(select(Rapport).where(Rapport.servicefall_id == f.id))],
                       "fotos": [a.name for a in s.scalars(select(Anhang).where(Anhang.bezug_typ == "servicefall", Anhang.bezug_id == f.id))]}
                      for f in faelle],
        "kommunikation": [{"kanal": k.kanal, "zeit": k.zeit.isoformat(), "zusammenfassung": k.zusammenfassung,
                           "transkript_vorhanden": bool(k.transkript)}
                          for k in s.scalars(select(Kommunikation).where(Kommunikation.kunde_id == kunde.id))],
        "rechnungen": [{"nummer": r.nummer, "datum": r.datum.isoformat(), "brutto": r.brutto, "status": r.status}
                       for r in s.scalars(select(Rechnung).where(Rechnung.kunde_id == kunde.id))],
        "angebote": [{"nummer": a.nummer, "datum": a.datum.isoformat(), "titel": a.titel, "brutto": a.brutto, "status": a.status}
                     for a in s.scalars(select(Angebot).where(Angebot.kunde_id == kunde.id))],
        "vertraege": [{"bezeichnung": v.bezeichnung, "beginn": v.beginn.isoformat(), "preis_jahr_netto": v.preis_jahr_netto, "status": v.status}
                      for v in s.scalars(select(Vertrag).where(Vertrag.kunde_id == kunde.id))],
        "anfragen": [{"kanal": a.kanal, "eingegangen": a.eingegangen.isoformat(), "betreff": a.betreff, "status": a.status}
                     for a in s.scalars(select(Anfrage).where(Anfrage.kunde_id == kunde.id))],
        "zaehlerstaende": [{"art": z.art, "wert": z.wert, "datum": z.datum.isoformat(), "quelle": z.quelle}
                           for z in s.scalars(select(Zaehlerstand).where(Zaehlerstand.kunde_id == kunde.id))],
        "empfaenger_der_daten": ["Buchhaltungssoftware und Steuerkanzlei (Rechnungen)", "E-Mail-/SMS-Dienstleister (Benachrichtigungen)",
                                 "Sprach- und KI-Dienste während des Telefonats (keine dauerhafte Speicherung dort)"],
        "speicherdauer": STANDARD_FRISTEN,
    }


def loeschlauf(s: Session, m: Mandant, jetzt: datetime | None = None, trocken: bool = False) -> dict[str, int]:
    """Regelmäßiger Lauf (täglich, cron): setzt die Fristen des Mandanten um und protokolliert das Ergebnis."""
    jetzt = jetzt or datetime.now(timezone.utc)
    f = fristen(m)
    vor = lambda tage: jetzt - timedelta(days=tage)  # noqa: E731
    z: dict[str, int] = {}
    mid = m.id

    transkripte = s.scalars(select(Kommunikation).where(Kommunikation.mandant_id == mid, Kommunikation.transkript.isnot(None),
                                                        Kommunikation.zeit < vor(f["transkript_tage"]))).all()
    z["transkripte_entfernt"] = len(transkripte)
    for k in transkripte:
        k.transkript = None

    lose = select(Kommunikation.id).where(Kommunikation.mandant_id == mid, Kommunikation.kunde_id.is_(None),
                                          Kommunikation.servicefall_id.is_(None), Kommunikation.zeit < vor(f["kommunikation_lose_tage"]))
    z["kommunikation_geloescht"] = len(list(s.scalars(lose)))
    s.execute(delete(Kommunikation).where(Kommunikation.id.in_(lose)))

    alt = select(OutboxEintrag.id).where(OutboxEintrag.mandant_id == mid, (
        (OutboxEintrag.status.in_(("zugestellt", "ohne_kanal")) & (OutboxEintrag.naechster_versuch < vor(f["outbox_tage"])))
        | ((OutboxEintrag.status == "tot") & (OutboxEintrag.naechster_versuch < vor(f["outbox_tot_tage"])))))
    z["outbox_geloescht"] = len(list(s.scalars(alt)))
    s.execute(delete(OutboxEintrag).where(OutboxEintrag.id.in_(alt)))

    ereignisse = select(Ereignis.id).where(Ereignis.mandant_id == mid, Ereignis.zeit < vor(f["ereignis_tage"]),
                                           *[Ereignis.typ.notlike(f"{p}%") for p in DAUERHAFT])
    z["ereignisse_geloescht"] = len(list(s.scalars(ereignisse)))
    s.execute(delete(Ereignis).where(Ereignis.id.in_(ereignisse)))

    faelle = s.scalars(select(Servicefall).where(Servicefall.mandant_id == mid, Servicefall.status.in_(ABGESCHLOSSEN),
                                                 Servicefall.geaendert < vor(f["vorgang_tage"]),
                                                 ~exists().where(Rechnung.servicefall_id == Servicefall.id))).all()
    z["vorgaenge_geloescht"] = len(faelle)
    _vorgangsanhang_loeschen(s, [fall.id for fall in faelle])
    for fall in faelle:
        s.execute(delete(Termin).where(Termin.servicefall_id == fall.id))
        s.execute(delete(Kommunikation).where(Kommunikation.servicefall_id == fall.id))
        s.execute(delete(Ereignis).where(Ereignis.entitaet_id == fall.id))
        s.delete(fall)
    s.flush()

    alte_zeiten = select(Zeiteintrag.id).where(Zeiteintrag.mandant_id == mid, Zeiteintrag.servicefall_id.is_(None),
                                               Zeiteintrag.beginn < vor(f["zeit_tage"]))
    z["zeiten_geloescht"] = len(list(s.scalars(alte_zeiten)))
    s.execute(delete(Zeiteintrag).where(Zeiteintrag.id.in_(alte_zeiten)))

    alte_belege = s.scalars(select(Beleg).where(Beleg.mandant_id == mid, Beleg.datum < vor(f["beleg_tage"]).date())).all()
    z["belege_geloescht"] = len(alte_belege)
    for b in alte_belege:
        if b.anhang_id:
            s.execute(delete(Anhang).where(Anhang.id == b.anhang_id))
        s.delete(b)

    alte_angebote = select(Angebot.id).where(Angebot.mandant_id == mid, Angebot.status.in_(("entwurf", "abgelehnt", "abgelaufen")),
                                             Angebot.rechnung_id.is_(None), Angebot.datum < vor(f["angebot_tage"]).date())
    z["angebote_geloescht"] = len(list(s.scalars(alte_angebote)))
    s.execute(delete(Angebot).where(Angebot.id.in_(alte_angebote)))
    alte_vertraege = select(Vertrag.id).where(Vertrag.mandant_id == mid, Vertrag.status != "aktiv",
                                              Vertrag.gekuendigt_zum < vor(f["vertrag_tage"]).date())
    z["vertraege_geloescht"] = len(list(s.scalars(alte_vertraege)))
    s.execute(delete(Vertrag).where(Vertrag.id.in_(alte_vertraege)))
    alte_anfragen = select(Anfrage.id).where(Anfrage.mandant_id == mid, Anfrage.eingegangen < vor(f["anfrage_tage"]))
    z["anfragen_geloescht"] = len(list(s.scalars(alte_anfragen)))
    s.execute(delete(Anfrage).where(Anfrage.id.in_(alte_anfragen)))
    s.execute(delete(Abwesenheit).where(Abwesenheit.mandant_id == mid, Abwesenheit.bis < vor(f["zeit_tage"]).date()))

    z["kunden_anonymisiert"] = 0
    for kunde in s.scalars(select(Kunde).where(Kunde.mandant_id == mid, Kunde.name != ANONYM, Kunde.vertragsstatus != "wartungsvertrag",
                                               Kunde.erstellt < vor(f["kunde_inaktiv_tage"]))):
        letzte_aktivitaet = max(
            [kunde.erstellt] + [x.geaendert for x in s.scalars(select(Servicefall).where(Servicefall.kunde_id == kunde.id))]
            + [datetime.combine(r.datum, datetime.min.time(), timezone.utc) for r in s.scalars(select(Rechnung).where(Rechnung.kunde_id == kunde.id))]
            + [k.zeit for k in s.scalars(select(Kommunikation).where(Kommunikation.kunde_id == kunde.id))])
        letzte_aktivitaet = letzte_aktivitaet if letzte_aktivitaet.tzinfo else letzte_aktivitaet.replace(tzinfo=timezone.utc)
        sperre = _sperrfrist(s, kunde, jetzt, f)
        # Rechnungen in der Aufbewahrungsfrist verhindern nur die Löschung, nicht die Anonymisierung
        if letzte_aktivitaet < vor(f["kunde_inaktiv_tage"]) and (sperre is None or sperre.startswith("Rechnung")):
            anonymisieren(s, kunde, "system", f"inaktiv seit {letzte_aktivitaet.date().isoformat()}")
            z["kunden_anonymisiert"] += 1

    if trocken:
        s.rollback()
        return z
    protokollieren(s, mid, "loeschung.lauf", "mandant", mid, "system", {**z, "fristen": f})
    s.commit()
    return z
