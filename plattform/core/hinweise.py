"""Push-Hinweise an den Inhaber (Outbox-Ziel `inhaber`), nur wenn ein Kanal konfiguriert ist.

Auslöser: Notdienst eingegangen, Freigabe der Klasse C wartet, Zustellung endgültig gescheitert (Dead Letter),
Anfrage mit Sicherheitsfall. Ohne konfigurierten Kanal entsteht kein Outbox-Eintrag (kein CSV-Rauschen).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from core.domaene import outbox_schreiben
from core.modelle import Mandant

PRIORITAET = {"notdienst": 5, "klasse_c": 4, "dead_letter": 4, "sicherheit": 5, "info": 3}
TAGS = {"notdienst": ["rotating_light"], "klasse_c": ["moneybag"], "dead_letter": ["warning"], "sicherheit": ["fire"], "info": ["information_source"]}


def aktiv(m: Mandant) -> bool:
    return bool(((m.integrationen or {}).get("inhaber") or {}).get("kanaele"))


def senden(s: Session, m: Mandant, art: str, titel: str, text: str, schluessel: str, pfad: str | None = None) -> bool:
    if not aktiv(m):
        return False
    basis = (m.stammdaten or {}).get("basis_url")
    payload: dict[str, Any] = {"titel": titel, "text": text, "prioritaet": PRIORITAET.get(art, 3), "tags": TAGS.get(art, []), "art": art}
    if basis and pfad:
        payload["url"] = f"{basis.rstrip('/')}/app/#{pfad}"
    outbox_schreiben(s, m.id, "inhaber", "hinweis", payload, f"hinweis:{art}:{schluessel}")
    return True
