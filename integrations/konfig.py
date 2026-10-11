"""Konfiguration aus Umgebungsvariablen oder .env (gleiche Konvention wie jev.py).

Geheimnisse stehen nie im Code. Ohne gesetzte Werte läuft alles lokal und unsigniert; sobald ein
Geheimnis gesetzt ist, wird die zugehörige Prüfung Pflicht (siehe server.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env_datei(pfad: str = ".env") -> dict[str, str]:
    werte: dict[str, str] = {}
    p = Path(pfad)
    if not p.exists():
        return werte
    for zeile in p.read_text(encoding="utf-8-sig").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        k, v = zeile.split("=", 1)
        v = v.split(" #", 1)[0] if not v.strip().startswith(("'", '"')) else v     # Kommentar am Zeilenende
        werte[k.strip()] = v.strip().strip('"').strip("'")
    return werte


def lesen(name: str, standard: str = "") -> str:
    wert = os.environ.get(name)
    if wert is not None:
        return wert.strip()
    return _env_datei().get(name, standard)


@dataclass
class Konfig:
    """Alle Einstellungen der Integrationsschicht an einer Stelle.

    telli_api_key         signiert Custom-Calendar- und Contact-Lookup-Anfragen (x-telli-signature)
                          und authentifiziert ausgehende API-Aufrufe (Create Contact, Schedule Call)
    telli_webhook_secret  Svix-Signaturgeheimnis des call_ended-Webhooks (Präfix whsec_)
    tool_secret           Bearer-Token, das telli als "Secret"-Header an Custom Tools mitschickt
    kunde_webhook_url     Ziel für ausgehende Ereignisse (Ticketsystem, Automationsplattform, Slack-Bridge)
    kunde_webhook_secret  Geheimnis, mit dem ausgehende Ereignisse signiert werden
    hubspot_token         Private-App-Token; gesetzt = Spiegelung nach HubSpot
    salesforce_*          Instance-URL plus Token oder Client-Credentials; gesetzt = Spiegelung nach Salesforce
    kalender              Kalenderdienst hinter den Slots: leer (Tabelle) oder "calcom" mit calcom_*
    fristen               Aufbewahrung in Tagen je Datenart (AUFBEWAHRUNG_<ART>_TAGE), siehe aufbewahrung.Fristen
    """
    db_pfad: str = "crm.duckdb"
    telli_api_key: str = ""
    telli_api_url: str = "https://api.telli.com"
    telli_agent_id: str = ""
    telli_webhook_secret: str = ""
    tool_secret: str = ""
    kunde_webhook_url: str = ""
    kunde_webhook_secret: str = ""
    hubspot_token: str = ""
    hubspot_url: str = "https://api.hubapi.com"
    salesforce_instance_url: str = ""
    salesforce_token: str = ""
    salesforce_login_url: str = "https://login.salesforce.com"
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    zeitzone: str = "Europe/Berlin"
    signatur_toleranz_s: int = 300
    tool_latenzbudget_ms: int = 800
    usecase: str = "pv_outbound"   # integrations/usecases: pv_outbound | telko_inbound
    verifizierung_pflicht: Optional[bool] = None   # None = Vorgabe des Use Case
    mindestvorlauf_h: Optional[int] = None         # None = Vorgabe des Use Case
    umgebung: str = "dev"          # "prod": Start nur mit vollständigen Geheimnissen
    takt_s: int = 15               # Takt des Hintergrundplaners: Outbox, Spiegelung, Lead-Push
    push_takt_s: int = 60          # Lead-Push-Intervall (Speed-to-Lead-Ziel unter 5 Minuten)
    abgleich_takt_s: int = 1800    # Abgleich mit List Calls gegen Webhook-Lücken
    kalender: str = ""             # "" = Slot-Tabelle, "calcom" = Cal.com API v2 (integrations/kalender.py)
    calcom_api_key: str = ""
    calcom_event_type_id: str = ""
    calcom_url: str = "https://api.cal.com"
    kalender_takt_s: int = 300     # Verfügbarkeit des Kalenderdienstes spiegeln
    kalender_tage: int = 14        # so weit voraus
    aufbewahrung_takt_s: int = 86400   # Fristen täglich anwenden (integrations/aufbewahrung.py)
    datenschutz_pepper: str = ""       # geheimer Schlüssel für das Pseudonym im Löschprotokoll
    fristen: dict = field(default_factory=dict)   # Abweichungen von den Vorgaben, z. B. {"transkripte": 14}
    extra: dict = field(default_factory=dict)

    @classmethod
    def aus_umgebung(cls) -> "Konfig":
        return cls(
            db_pfad=lesen("DB_PATH", "crm.duckdb"),
            telli_api_key=lesen("TELLI_API_KEY"),
            telli_api_url=lesen("TELLI_API_URL", "https://api.telli.com"),
            telli_agent_id=lesen("TELLI_AGENT_ID"),
            telli_webhook_secret=lesen("TELLI_WEBHOOK_SECRET"),
            tool_secret=lesen("TOOL_SECRET"),
            kunde_webhook_url=lesen("KUNDE_WEBHOOK_URL"),
            kunde_webhook_secret=lesen("KUNDE_WEBHOOK_SECRET"),
            hubspot_token=lesen("HUBSPOT_TOKEN"),
            hubspot_url=lesen("HUBSPOT_URL", "https://api.hubapi.com"),
            salesforce_instance_url=lesen("SALESFORCE_INSTANCE_URL"),
            salesforce_token=lesen("SALESFORCE_TOKEN"),
            salesforce_login_url=lesen("SALESFORCE_LOGIN_URL", "https://login.salesforce.com"),
            salesforce_client_id=lesen("SALESFORCE_CLIENT_ID"),
            salesforce_client_secret=lesen("SALESFORCE_CLIENT_SECRET"),
            zeitzone=lesen("ZEITZONE", "Europe/Berlin"),
            signatur_toleranz_s=int(lesen("SIGNATUR_TOLERANZ_S", "300")),
            tool_latenzbudget_ms=int(lesen("TOOL_LATENZBUDGET_MS", "800")),
            usecase=lesen("USECASE", "pv_outbound"),
            verifizierung_pflicht={"": None, "true": True, "1": True, "false": False, "0": False}.get(lesen("VERIFIZIERUNG_PFLICHT").lower()),
            mindestvorlauf_h=int(lesen("MINDESTVORLAUF_H")) if lesen("MINDESTVORLAUF_H") else None,
            umgebung=lesen("UMGEBUNG", "dev").lower(),
            takt_s=int(lesen("TAKT_S", "15")),
            push_takt_s=int(lesen("PUSH_TAKT_S", "60")),
            abgleich_takt_s=int(lesen("ABGLEICH_TAKT_S", "1800")),
            kalender=lesen("KALENDER").lower(),
            calcom_api_key=lesen("CALCOM_API_KEY"),
            calcom_event_type_id=lesen("CALCOM_EVENT_TYPE_ID"),
            calcom_url=lesen("CALCOM_URL", "https://api.cal.com"),
            kalender_takt_s=int(lesen("KALENDER_TAKT_S", "300")),
            kalender_tage=int(lesen("KALENDER_TAGE", "14")),
            aufbewahrung_takt_s=int(lesen("AUFBEWAHRUNG_TAKT_S", "86400")),
            datenschutz_pepper=lesen("DATENSCHUTZ_PEPPER"),
            fristen={art: int(lesen(f"AUFBEWAHRUNG_{art.upper()}_TAGE")) for art in
                     ("transkripte", "tickets", "verifizierungen", "zustellungen", "webhooks", "erinnerungen", "protokoll")
                     if lesen(f"AUFBEWAHRUNG_{art.upper()}_TAGE")},
        )

    @property
    def produktiv(self) -> bool:
        return self.umgebung in ("prod", "produktion", "production")

    @property
    def salesforce_konfiguriert(self) -> bool:
        return bool(self.salesforce_token and self.salesforce_instance_url) or bool(
            self.salesforce_client_id and self.salesforce_client_secret)

    def fehlende_geheimnisse(self) -> list[str]:
        """Was vor einem Go-live gesetzt sein muss. Lokal und im Test darf alles leer sein."""
        fehlt = []
        if not self.telli_api_key:
            fehlt.append("TELLI_API_KEY")
        if not self.telli_webhook_secret:
            fehlt.append("TELLI_WEBHOOK_SECRET")
        if not self.tool_secret:
            fehlt.append("TOOL_SECRET")
        if self.kalender == "calcom" and not self.calcom_api_key:
            fehlt.append("CALCOM_API_KEY")
        return fehlt
