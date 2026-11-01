"""Wartungsverträge: Laufzeit, automatische Verlängerung, Kündigungsfrist, periodische Abrechnung.

Die Abrechnung entsteht als Freigabe rechnung_erstellen (Klasse C) mit einer Pauschalposition; der Tageslauf legt sie
an, sobald naechste_abrechnung erreicht ist. Die Kündigungsfrist wird als Frist (Art vertrag) geführt, damit das Büro
rechtzeitig an die Verlängerung erinnert wird. Der Kundendatensatz trägt den Vertragsstatus für Twin und Löschkonzept.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import domaene
from core.modelle import Anlage, Frist, Kunde, Mandant, Vertrag


def monate_addieren(d: date, monate: int) -> date:
    monat = d.month - 1 + monate
    jahr, monat = d.year + monat // 12, monat % 12 + 1
    tag = min(d.day, [31, 29 if jahr % 4 == 0 and (jahr % 100 != 0 or jahr % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][monat - 1])
    return date(jahr, monat, tag)


def laufzeitende(v: Vertrag, heute: date) -> date:
    """Ende der laufenden Vertragsperiode (Erstlaufzeit, danach jeweils Verlängerungsperiode)."""
    ende = monate_addieren(v.beginn, v.laufzeit_monate) - timedelta(days=1)
    while ende < heute and v.verlaengerung_monate > 0:
        ende = monate_addieren(ende + timedelta(days=1), v.verlaengerung_monate) - timedelta(days=1)
    return ende


def kuendigungstermin(v: Vertrag, heute: date) -> date:
    """Letzter Tag, an dem zum nächsten Laufzeitende gekündigt werden kann."""
    return monate_addieren(laufzeitende(v, heute) + timedelta(days=1), -v.kuendigungsfrist_monate) - timedelta(days=1)


def vertrag_anlegen(s: Session, m: Mandant, daten: dict[str, Any], heute: date, akteur_name: str | None = None) -> Vertrag:
    kunde = s.get(Kunde, daten.get("kunde_id", ""))
    if kunde is None or kunde.mandant_id != m.id:
        raise ValueError("Vertrag ohne Kunde")
    for aid in daten.get("anlagen_ids") or []:
        a = s.get(Anlage, aid)
        if a is None or a.mandant_id != m.id:
            raise ValueError("Anlage nicht gefunden")
    beginn = daten.get("beginn") or heute
    beginn = date.fromisoformat(beginn) if isinstance(beginn, str) else beginn
    v = Vertrag(mandant_id=m.id, kunde_id=kunde.id, bezeichnung=daten.get("bezeichnung") or "Wartungsvertrag", anlagen_ids=daten.get("anlagen_ids") or [],
                leistungen=daten.get("leistungen"), beginn=beginn, laufzeit_monate=int(daten.get("laufzeit_monate", 12)),
                kuendigungsfrist_monate=int(daten.get("kuendigungsfrist_monate", 3)), verlaengerung_monate=int(daten.get("verlaengerung_monate", 12)),
                preis_jahr_netto=f"{Decimal(str(daten['preis_jahr_netto'])):.2f}", ust_satz=str(daten.get("ust_satz", "19")),
                abrechnung_monate=int(daten.get("abrechnung_monate", 12)), naechste_abrechnung=daten.get("naechste_abrechnung") or beginn, notiz=daten.get("notiz"))
    if isinstance(v.naechste_abrechnung, str):
        v.naechste_abrechnung = date.fromisoformat(v.naechste_abrechnung)
    s.add(v)
    s.flush()
    kunde.vertragsstatus = "wartungsvertrag"
    s.add(Frist(mandant_id=m.id, bezug_typ="vertrag", bezug_id=v.id, art="vertrag", bezeichnung=f"Kündigungsfrist {v.bezeichnung} {kunde.name}",
                faellig=kuendigungstermin(v, heute), intervall_monate=v.verlaengerung_monate or None,
                hinweis=f"Laufzeitende {laufzeitende(v, heute).isoformat()}; ohne Kündigung verlängert sich der Vertrag um {v.verlaengerung_monate} Monate"))
    domaene.protokollieren(s, m.id, "vertrag.angelegt", "vertrag", v.id, "mensch", {"kunde_id": kunde.id, "preis_jahr_netto": v.preis_jahr_netto}, akteur_name)
    return v


def kuendigen(s: Session, v: Vertrag, heute: date, zum: date | None = None, akteur_name: str | None = None) -> Vertrag:
    if v.status != "aktiv":
        raise ValueError(f"Vertrag ist {v.status}")
    ende = laufzeitende(v, heute)
    fruehestens = ende if heute <= kuendigungstermin(v, heute) else monate_addieren(ende + timedelta(days=1), v.verlaengerung_monate or 0) - timedelta(days=1)
    v.gekuendigt_zum = max(zum or fruehestens, fruehestens)
    v.status = "gekuendigt"
    for f in s.scalars(select(Frist).where(Frist.bezug_typ == "vertrag", Frist.bezug_id == v.id, Frist.erledigt_am.is_(None))):
        f.erledigt_am = heute
    domaene.protokollieren(s, v.mandant_id, "vertrag.gekuendigt", "vertrag", v.id, "mensch", {"zum": v.gekuendigt_zum.isoformat()}, akteur_name)
    return v


def abrechnungslauf(s: Session, m: Mandant, heute: date) -> list[dict[str, Any]]:
    """Fällige Vertragsabrechnungen als Freigaben anlegen und den nächsten Termin fortschreiben; beendete Verträge schließen."""
    ergebnis = []
    for v in s.scalars(select(Vertrag).where(Vertrag.mandant_id == m.id, Vertrag.status.in_(("aktiv", "gekuendigt")))):
        if v.status == "gekuendigt" and v.gekuendigt_zum and v.gekuendigt_zum < heute:
            v.status = "beendet"
            kunde = s.get(Kunde, v.kunde_id)
            if kunde and not s.scalar(select(Vertrag).where(Vertrag.kunde_id == kunde.id, Vertrag.status.in_(("aktiv", "gekuendigt")), Vertrag.id != v.id)):
                kunde.vertragsstatus = "ohne"
            domaene.protokollieren(s, m.id, "vertrag.beendet", "vertrag", v.id, "system", {})
            continue
        while v.naechste_abrechnung <= heute and (v.status == "aktiv" or (v.gekuendigt_zum and v.naechste_abrechnung < v.gekuendigt_zum)):
            periode_bis = monate_addieren(v.naechste_abrechnung, v.abrechnung_monate)
            anteil = Decimal(v.abrechnung_monate) / Decimal(12)
            betrag = (Decimal(v.preis_jahr_netto) * anteil).quantize(Decimal("0.01"))
            kunde = s.get(Kunde, v.kunde_id)
            f = domaene.aktion_beantragen(s, m, "rechnung_erstellen", {
                "kunde_id": v.kunde_id, "vertrag_id": v.id, "leistungsdatum": v.naechste_abrechnung.isoformat(),
                "positionen": [{"bezeichnung": f"{v.bezeichnung} {v.naechste_abrechnung.strftime('%d.%m.%Y')} bis {periode_bis.strftime('%d.%m.%Y')}",
                                "menge": "1", "einheit": "psch", "einzelpreis_netto": str(betrag), "ust_satz": v.ust_satz}]},
                "system", None, f"Vertragsabrechnung {v.bezeichnung} für {kunde.name if kunde else ''}: {str(betrag).replace('.', ',')} € netto")
            ergebnis.append({"vertrag_id": v.id, "freigabe_id": f.id, "periode_von": v.naechste_abrechnung.isoformat(), "betrag_netto": str(betrag)})
            v.naechste_abrechnung = periode_bis
    return ergebnis


def bestand(s: Session, m: Mandant) -> dict[str, Any]:
    aktive = s.scalars(select(Vertrag).where(Vertrag.mandant_id == m.id, Vertrag.status == "aktiv")).all()
    return {"aktive": len(aktive), "jahresvolumen_netto": str(sum((Decimal(v.preis_jahr_netto) for v in aktive), Decimal("0"))),
            "gekuendigt": s.scalar(select(func.count()).select_from(Vertrag).where(Vertrag.mandant_id == m.id, Vertrag.status == "gekuendigt"))}
