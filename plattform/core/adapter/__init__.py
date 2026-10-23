"""Adapter-Register: baut je Mandant und Ziel den passenden Adapter aus Mandant.integrationen.

Ziele der Outbox und ihre Standardbelegung:
  fachsoftware  webhook | csv (Standard)            Termine, Aufträge an openHandwerk, pds, n8n …
  endkunde      email | sms | csv (Standard)        Terminbestätigung, Nachricht, Rechnung an den Kunden
  finanz        lexware | sevdesk | csv (Standard)  Rechnung und Storno in die Buchhaltungssoftware
  inhaber       ntfy | webhook                       Push-Hinweise (Notdienst, Klasse C, Dead Letter) an den Inhaber
  kanzlei       email | belegpaket (Standard) | belegtransfer
                                                   Belegpaket an die Steuerkanzlei (DATEV-Format); Einzelbelege in den
                                                   DATEV-Belegtransfer-Ordner oder per Upload-Mail (email mit upload_email)
Eingangsbelege (beleg_uebermitteln) nehmen finanz: lexware, sevdesk; kanzlei: belegtransfer, email(upload_email).
Ein Ziel kann mehrere Kanäle haben (endkunde: ["email", "sms"]); der erste, der zustellt, zählt.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from core.adapter.basis import AdapterFehler, Konfigurationsfehler
from core.adapter.belegpaket import BelegpaketAdapter
from core.adapter.belegtransfer import BelegtransferAdapter
from core.adapter.email import EmailAdapter
from core.adapter.lexware import LexwareAdapter
from core.adapter.ntfy import NtfyAdapter
from core.adapter.sevdesk import SevdeskAdapter
from core.adapter.sms import SmsAdapter
from core.adapter.webhook import WebhookAdapter
from core.geheimnisse import konfiguration_entschluesseln
from core.modelle import Mandant, OutboxEintrag
from core.zustellung import Adapter, CsvAdapter

TYPEN: dict[str, Callable[[dict[str, Any]], Adapter]] = {
    "webhook": WebhookAdapter, "email": EmailAdapter, "sms": SmsAdapter, "lexware": LexwareAdapter,
    "sevdesk": SevdeskAdapter, "belegpaket": BelegpaketAdapter, "belegtransfer": BelegtransferAdapter, "ntfy": NtfyAdapter,
}
ZIELE = ("fachsoftware", "endkunde", "finanz", "kanzlei", "inhaber")
STANDARD = {"fachsoftware": ["csv"], "endkunde": ["csv"], "finanz": ["csv"], "kanzlei": ["belegpaket"], "inhaber": ["csv"]}
ERLAUBT = {"fachsoftware": {"webhook", "csv"}, "endkunde": {"email", "sms", "csv"}, "finanz": {"lexware", "sevdesk", "csv"},
           "kanzlei": {"email", "belegpaket", "belegtransfer"}, "inhaber": {"ntfy", "webhook", "csv"}}


class Mehrkanal:
    """Reiht Kanäle: der erste Adapter, der eine Zustellung meldet, beendet; None heißt „nicht zuständig“."""
    name = "mehrkanal"

    def __init__(self, kanaele: list[Adapter]):
        self.kanaele = kanaele
        self.name = "+".join(k.name for k in kanaele)

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        for k in self.kanaele:
            ergebnis = k.zustellen(e, s)
            if ergebnis is not None:
                return f"{k.name}:{ergebnis}"
        return None


class Register:
    def __init__(self, csv_verzeichnis: Path | None = None, zusatz: dict[str, Any] | None = None):
        self.csv_verzeichnis = Path(csv_verzeichnis or os.environ.get("CSV_EXPORT", "export"))
        self.zusatz = zusatz or {}  # z. B. {"transport": MockTransport} in Tests
        self.cache: dict[tuple[str, str, str], Adapter] = {}

    def bauen(self, typ: str, konfig: dict[str, Any]) -> Adapter:
        if typ == "csv":
            return CsvAdapter("csv", self.csv_verzeichnis)
        if typ not in TYPEN:
            raise Konfigurationsfehler(f"Unbekannter Adaptertyp {typ!r}")
        konfig = konfiguration_entschluesseln(konfig)
        try:
            return TYPEN[typ](konfig, **self.zusatz) if typ in ("webhook", "sms", "lexware", "sevdesk", "ntfy") else TYPEN[typ](konfig)
        except TypeError:
            return TYPEN[typ](konfig)

    def fuer(self, s: Session, mandant_id: str, ziel: str) -> Adapter:
        m = s.get(Mandant, mandant_id)
        einstellung = (m.integrationen if m else {}).get(ziel) or {}
        kanaele = einstellung.get("kanaele") or ([einstellung["typ"]] if einstellung.get("typ") else STANDARD[ziel])
        schluessel = (mandant_id, ziel, repr(sorted(einstellung.items())))
        if schluessel not in self.cache:
            adapter = [self.bauen(typ, einstellung.get(typ, einstellung if typ == einstellung.get("typ") else {})) for typ in kanaele]
            self.cache[schluessel] = adapter[0] if len(adapter) == 1 else Mehrkanal(adapter)
        return self.cache[schluessel]

    def __call__(self, s: Session, e: OutboxEintrag) -> Adapter:
        return self.fuer(s, e.mandant_id, e.ziel)


def konfiguration_pruefen(ziel: str, einstellung: dict[str, Any]) -> None:
    """Validierung beim Speichern über die API, damit Fehler nicht erst im Worker auffallen."""
    if ziel not in ZIELE:
        raise ValueError(f"Unbekanntes Ziel {ziel!r}")
    kanaele = einstellung.get("kanaele") or ([einstellung["typ"]] if einstellung.get("typ") else [])
    for typ in kanaele:
        if typ not in ERLAUBT[ziel]:
            raise ValueError(f"{typ!r} ist für {ziel} nicht vorgesehen (erlaubt: {', '.join(sorted(ERLAUBT[ziel]))})")
        konfig = einstellung.get(typ, einstellung if typ == einstellung.get("typ") else {})
        try:
            Register().bauen(typ, konfig)
        except Konfigurationsfehler as e:
            raise ValueError(str(e)) from e
        except AdapterFehler:
            pass
