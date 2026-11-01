"""Globale Suche über Kunden, Vorgänge, Rechnungen, Angebote, Belege, Anfragen und Artikel (Strg+K in der Oberfläche).

Bewusst ohne Volltextindex: ILIKE über die sprechenden Spalten je Entität, begrenzt auf wenige Treffer je Typ. Für
größere Bestände kann PostgreSQL pg_trgm nachgerüstet werden (GIN-Index auf kunde.name, servicefall.anliegen), die
Schnittstelle bleibt gleich. Monteure (nur Recht einsatz) sehen ausschließlich Kunden und eigene Vorgänge.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core.modelle import Anfrage, Angebot, Artikel, Beleg, Kunde, Mandant, Rechnung, Servicefall, Termin


def _muster(q: str) -> str:
    return f"%{q.strip()}%"


def suchen(s: Session, m: Mandant, q: str, rechte: set[str], eigene_ressourcen: list[str] | None = None, je_typ: int = 6) -> list[dict[str, Any]]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    muster = _muster(q)
    treffer: list[dict[str, Any]] = []
    ziffern = "".join(ch for ch in q if ch.isdigit())
    kunden_bedingung = [Kunde.name.ilike(muster), Kunde.ort.ilike(muster), Kunde.email.ilike(muster), Kunde.strasse.ilike(muster)]
    if len(ziffern) >= 4:  # Rufnummernsuche über die Ziffernfolge (gespeichert ist E.164), führende 0 der Ortsvorwahl abschneiden
        kunden_bedingung.append(Kunde.telefon.like(f"%{ziffern.lstrip('0')[-9:]}%"))
    for k in s.scalars(select(Kunde).where(Kunde.mandant_id == m.id, or_(*kunden_bedingung)).order_by(Kunde.name).limit(je_typ)):
        treffer.append({"typ": "kunde", "id": k.id, "titel": k.name, "untertitel": ", ".join(t for t in (k.strasse, f"{k.plz or ''} {k.ort or ''}".strip(), k.telefon) if t),
                        "status": k.vertragsstatus})
    faelle = select(Servicefall).where(Servicefall.mandant_id == m.id, or_(Servicefall.anliegen.ilike(muster), Servicefall.symptom.ilike(muster),
                                                                           Servicefall.kunde.has(Kunde.name.ilike(muster))))
    if eigene_ressourcen is not None:
        faelle = faelle.where(Servicefall.termine.any(Termin.ressource_id.in_(eigene_ressourcen)))
    for f in s.scalars(faelle.order_by(Servicefall.erstellt.desc()).limit(je_typ)):
        treffer.append({"typ": "servicefall", "id": f.id, "titel": f.anliegen, "untertitel": f"{f.kunde.name} · {f.dringlichkeit}", "status": f.status})
    if "finanzen" in rechte:
        for r in s.scalars(select(Rechnung).where(Rechnung.mandant_id == m.id, or_(Rechnung.nummer.ilike(muster), Rechnung.kunde.has(Kunde.name.ilike(muster))))
                           .order_by(Rechnung.datum.desc()).limit(je_typ)):
            treffer.append({"typ": "rechnung", "id": r.id, "titel": f"{'Storno ' if r.art == 'storno' else 'Rechnung '}{r.nummer}",
                            "untertitel": f"{r.empfaenger.get('name', '')} · {r.brutto} € · fällig {r.faellig.isoformat()}", "status": r.status})
        for b in s.scalars(select(Beleg).where(Beleg.mandant_id == m.id, or_(Beleg.lieferant.ilike(muster), Beleg.nummer.ilike(muster)))
                           .order_by(Beleg.datum.desc()).limit(je_typ)):
            treffer.append({"typ": "beleg", "id": b.id, "titel": f"{b.lieferant} {b.nummer or ''}".strip(), "untertitel": f"{b.datum.isoformat()} · {b.brutto} €", "status": b.status})
    if "bearbeiten" in rechte:
        for a in s.scalars(select(Angebot).where(Angebot.mandant_id == m.id, or_(Angebot.nummer.ilike(muster), Angebot.titel.ilike(muster),
                                                                                  Angebot.kunde.has(Kunde.name.ilike(muster)))).order_by(Angebot.datum.desc()).limit(je_typ)):
            treffer.append({"typ": "angebot", "id": a.id, "titel": f"Angebot {a.nummer}: {a.titel}", "untertitel": f"{a.empfaenger.get('name', '')} · {a.brutto} €", "status": a.status})
        for an in s.scalars(select(Anfrage).where(Anfrage.mandant_id == m.id, or_(Anfrage.name.ilike(muster), Anfrage.betreff.ilike(muster), Anfrage.nachricht.ilike(muster),
                                                                                   Anfrage.email.ilike(muster))).order_by(Anfrage.eingegangen.desc()).limit(je_typ)):
            treffer.append({"typ": "anfrage", "id": an.id, "titel": an.betreff or (an.nachricht or "")[:80] or "Anfrage", "untertitel": f"{an.name or an.email or ''} · {an.kanal}", "status": an.status})
        for a in s.scalars(select(Artikel).where(Artikel.mandant_id == m.id, or_(Artikel.nummer.ilike(muster), Artikel.bezeichnung.ilike(muster)))
                           .order_by(Artikel.nummer).limit(je_typ)):
            treffer.append({"typ": "artikel", "id": a.id, "titel": f"{a.nummer} {a.bezeichnung}", "untertitel": f"{a.verkauf_netto} €/{a.einheit}{' · ' + a.lieferant if a.lieferant else ''}",
                            "status": "aktiv" if a.aktiv else "inaktiv"})
    return treffer
