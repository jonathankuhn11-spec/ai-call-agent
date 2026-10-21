"""Frameworkunabhängiger Client der Voice-Tool-API des Core-Service.

Der Voice-Agent (agent.py) ruft ausschließlich diese Methoden auf. Dadurch bleibt der Agent
austauschbar (LiveKit Agents, Pipecat oder eine SaaS-Plattform wie telli, Retell, Vapi per
Webhook-Tools): Die Geschäftslogik liegt im Core, der Agent führt nur das Gespräch.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class KernClient:
    def __init__(self, basis_url: str | None = None, mandant: str | None = None, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 4.0):
        kopf = {"X-Mandant": mandant or os.environ["CORE_MANDANT"]}
        if key := api_key or os.environ.get("CORE_API_KEY"):
            kopf["X-API-Key"] = key
        # Kurzes Timeout: Der Anrufer wartet live; lieber Rückruf zusagen als Stille
        self._http = httpx.AsyncClient(base_url=basis_url or os.environ.get("CORE_URL", "http://core:8100"),
                                       headers=kopf, timeout=timeout, transport=transport)

    async def _post(self, pfad: str, daten: dict[str, Any]) -> dict[str, Any]:
        r = await self._http.post(pfad, json={k: v for k, v in daten.items() if v is not None})
        r.raise_for_status()
        return r.json()

    async def kunde_suchen(self, telefon: str) -> dict[str, Any]:
        return await self._post("/voice/kunde-suchen", {"telefon": telefon})

    async def zeitfenster(self, anliegen: str, anlagentyp: str | None = None, dringlichkeit: str | None = None,
                          anzahl: int = 3) -> dict[str, Any]:
        return await self._post("/voice/zeitfenster", {"anliegen": anliegen, "anlagentyp": anlagentyp,
                                                       "dringlichkeit": dringlichkeit, "anzahl": anzahl})

    async def servicefall_anlegen(self, **daten: Any) -> dict[str, Any]:
        return await self._post("/voice/servicefall", daten)

    async def gespraech_beendet(self, transkript: str, telefon: str | None = None, servicefall_id: str | None = None,
                                zusammenfassung: str | None = None, extraktion: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._post("/voice/gespraech-beendet", {"transkript": transkript, "telefon": telefon,
                                                             "servicefall_id": servicefall_id,
                                                             "zusammenfassung": zusammenfassung,
                                                             "extraktion": extraktion or {}})

    async def notdienst(self) -> dict[str, Any]:
        """Aktuelle Bereitschaft (Rufnummer und Name) für Notfälle außerhalb der Bürozeit."""
        return await self._post("/voice/notdienst", {})

    async def schliessen(self) -> None:
        await self._http.aclose()
