"""Push-Hinweise an das Telefon des Inhabers über ntfy (Apache-2.0, selbst gehostet im Profil `push` oder ntfy.sh).

Ziel `inhaber`, Aktion `hinweis`: Payload titel, text, prioritaet (1–5), tags, url. Keine personenbezogenen Details
im Text, wenn ein fremder ntfy-Server genutzt wird; der Hinweis verweist per Link auf die Oberfläche.
API: POST <basis_url>/<topic>, Header Title, Priority, Tags, Click, optional Authorization: Bearer <token>.
"""
from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.orm import Session

from core.adapter.basis import Konfigurationsfehler, antwort_pruefen, client
from core.modelle import OutboxEintrag


class NtfyAdapter:
    name = "ntfy"

    def __init__(self, konfig: dict[str, Any], transport: httpx.BaseTransport | None = None):
        if not konfig.get("topic"):
            raise Konfigurationsfehler("ntfy: topic fehlt")
        self.topic = str(konfig["topic"]).strip("/")
        kopf = {"Authorization": f"Bearer {konfig['token']}"} if konfig.get("token") else {}
        self.http = client(konfig.get("basis_url") or "https://ntfy.sh", kopf, transport)

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        if e.aktion != "hinweis":
            return None
        p = e.payload
        kopf = {"Title": str(p.get("titel", "SHK-Backoffice")).encode("ascii", "replace").decode(),
                "Priority": str(min(5, max(1, int(p.get("prioritaet", 3))))), "Tags": ",".join(p.get("tags") or [])}
        if p.get("url"):
            kopf["Click"] = str(p["url"])
        r = self.http.post(f"/{self.topic}", content=str(p.get("text", "")).encode("utf-8"),
                           headers={**kopf, "Content-Type": "text/plain; charset=utf-8"})
        antwort = antwort_pruefen(r, "ntfy")
        return str(antwort.get("id") or e.idempotenz_schluessel)
