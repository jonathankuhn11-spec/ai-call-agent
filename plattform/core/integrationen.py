"""Integrations-Hub: Katalog aller Anbindungen, Zustand je Betrieb (konfiguriert, zugestellt, Fehler), Verbindungstests,
Auflösung eingehender Webhooks (WhatsApp, Stripe) auf den richtigen Mandanten.

Zustand entsteht aus Mandant.integrationen (Kanäle je Ziel) und der Outbox (zugestellt/ausstehend/tot je Ziel). Ein
Verbindungstest baut den Adapter mit den gespeicherten (entschlüsselten) Zugangsdaten und ruft, wo vorhanden,
``pruefen()`` auf; Adapter ohne Prüfaufruf gelten als „Konfiguration gültig“.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.adapter import ERLAUBT, Register
from core.adapter.basis import AdapterFehler, Konfigurationsfehler
from core.geheimnisse import konfiguration_entschluesseln
from core.modelle import ExternalRef, Mandant, OutboxEintrag

# ziel, typ → Anzeige; kategorie: kunde | finanzen | betrieb | dienste
KATALOG: list[dict[str, Any]] = [
    {"ziel": "endkunde", "typ": "email", "bezeichnung": "E-Mail an Kunden", "kategorie": "kunde", "text": "Terminbestätigung, Angebote, Rechnungen, Portal-Link per SMTP des Betriebs.", "doku": "README.md#anbindungen"},
    {"ziel": "endkunde", "typ": "sms", "bezeichnung": "SMS (seven.io)", "kategorie": "kunde", "text": "Terminbestätigung und Erinnerung; nur mit Einwilligung des Kunden.", "doku": "https://www.seven.io"},
    {"ziel": "endkunde", "typ": "whatsapp", "bezeichnung": "WhatsApp Business", "kategorie": "kunde", "text": "Terminbestätigung und Nachrichten über die Meta Cloud API; Antworten der Kunden landen als Anfrage bzw. im Vorgang. Nur mit Einwilligung.", "doku": "https://developers.facebook.com/docs/whatsapp/cloud-api"},
    {"ziel": "zahlung", "typ": "stripe", "bezeichnung": "Online-Zahlung (Stripe Checkout)", "kategorie": "kunde", "text": "Kunden bezahlen Rechnungen im Portal per Karte, Apple/Google Pay oder SEPA; der Zahlungseingang bucht sich selbst.", "doku": "https://docs.stripe.com/payments/checkout"},
    {"ziel": "finanz", "typ": "lexware", "bezeichnung": "Lexware Office", "kategorie": "finanzen", "text": "Rechnungen, Stornos und Eingangsbelege in die Buchhaltung.", "doku": "https://developers.lexware.io"},
    {"ziel": "finanz", "typ": "sevdesk", "bezeichnung": "sevDesk", "kategorie": "finanzen", "text": "Rechnungen, Stornos und Eingangsbelege in die Buchhaltung.", "doku": "https://api.sevdesk.de"},
    {"ziel": "finanz", "typ": "datev", "bezeichnung": "DATEV Unternehmen online", "kategorie": "finanzen", "text": "Rechnungsausgang und Belege über die DATEV-Datenservices (OAuth-Anmeldung).", "doku": "https://developer.datev.de"},
    {"ziel": "kanzlei", "typ": "belegpaket", "bezeichnung": "Belegpaket an die Kanzlei", "kategorie": "finanzen", "text": "Monatliches Paket (DATEV-Format) im Ablageverzeichnis.", "doku": "README.md#anbindungen"},
    {"ziel": "kanzlei", "typ": "belegtransfer", "bezeichnung": "DATEV Belegtransfer", "kategorie": "finanzen", "text": "Einzelbelege in den Belegtransfer-Ordner der Kanzlei.", "doku": "README.md#anbindungen"},
    {"ziel": "kanzlei", "typ": "email", "bezeichnung": "E-Mail an die Kanzlei", "kategorie": "finanzen", "text": "Belegpaket oder Einzelbelege (Upload-Adresse DATEV Unternehmen online) per E-Mail.", "doku": "README.md#anbindungen"},
    {"ziel": "fachsoftware", "typ": "webhook", "bezeichnung": "Fachsoftware / Automatisierung (Webhook)", "kategorie": "betrieb", "text": "Termine und Aufträge signiert an openHandwerk, pds, n8n oder Make.", "doku": "README.md#anbindungen"},
    {"ziel": "fachsoftware", "typ": "csv", "bezeichnung": "CSV-Export", "kategorie": "betrieb", "text": "Rückfall ohne Schnittstelle: Dateien im Exportverzeichnis.", "doku": "README.md#anbindungen"},
    {"ziel": "inhaber", "typ": "ntfy", "bezeichnung": "Push an den Inhaber (ntfy)", "kategorie": "betrieb", "text": "Notdienst, Freigaben der Klasse C, Zustellfehler, Portal-Ereignisse auf das Telefon.", "doku": "https://ntfy.sh"},
    {"ziel": "inhaber", "typ": "webhook", "bezeichnung": "Hinweise per Webhook", "kategorie": "betrieb", "text": "Dieselben Hinweise an Slack, Teams oder n8n.", "doku": "README.md#anbindungen"},
]

DIENSTE: list[dict[str, Any]] = [   # systemweite Dienste ohne Mandantenkonfiguration; Zustand kommt aus app.state / Umgebung
    {"schluessel": "voice", "bezeichnung": "Telefonassistent (LiveKit + SIP-Trunk)", "text": "Anrufannahme, Servicefall-Anlage, Terminvorschlag am Telefon.", "doku": "TELEFONIE.md"},
    {"schluessel": "routing", "bezeichnung": "Routenplanung (OSRM)", "text": "Fahrzeiten und Touren; ohne Dienst Luftlinie.", "doku": "KATALOG.md"},
    {"schluessel": "geocoder", "bezeichnung": "Geokodierung (Nominatim)", "text": "Kundenkarte und Touren brauchen Koordinaten.", "doku": "KATALOG.md"},
    {"schluessel": "oidc", "bezeichnung": "Anmeldung (Keycloak/OIDC)", "text": "Rollen Inhaber, Büro, Monteur, Steuerberater; ohne OIDC API-Schlüssel.", "doku": "HOSTING.md"},
    {"schluessel": "scanner", "bezeichnung": "Scanner-Eingang (IMAP/Ordner)", "text": "Belege per Scan-to-Mail oder Eingangsordner.", "doku": "README.md#scanner-eingang"},
    {"schluessel": "grosshandel", "bezeichnung": "Großhandel (IDS-Connect/DATANORM)", "text": "Preise und Bestellungen beim Großhändler.", "doku": "README.md"},
]


def _outbox(s: Session, m: Mandant, seit: datetime) -> dict[str, dict[str, Any]]:
    aus: dict[str, dict[str, Any]] = {}
    for ziel, status, anzahl in s.execute(select(OutboxEintrag.ziel, OutboxEintrag.status, func.count()).where(OutboxEintrag.mandant_id == m.id).group_by(OutboxEintrag.ziel, OutboxEintrag.status)):
        aus.setdefault(ziel, {"zugestellt": 0, "ausstehend": 0, "tot": 0, "letzter_fehler": None, "offen_seit": None})[status] = anzahl
    for e in s.scalars(select(OutboxEintrag).where(OutboxEintrag.mandant_id == m.id, OutboxEintrag.letzter_fehler.isnot(None)).order_by(OutboxEintrag.id.desc()).limit(50)):
        z = aus.setdefault(e.ziel, {"zugestellt": 0, "ausstehend": 0, "tot": 0, "letzter_fehler": None, "offen_seit": None})
        if z["letzter_fehler"] is None:
            z["letzter_fehler"] = e.letzter_fehler[:300]
    aeltester = s.execute(select(OutboxEintrag.ziel, func.min(OutboxEintrag.naechster_versuch)).where(OutboxEintrag.mandant_id == m.id, OutboxEintrag.status == "ausstehend").group_by(OutboxEintrag.ziel))
    for ziel, zeit in aeltester:
        if ziel in aus and zeit is not None:
            aus[ziel]["offen_seit"] = zeit.isoformat()
    return aus


def status(s: Session, m: Mandant, jetzt: datetime, dienste: dict[str, bool | None] | None = None) -> dict[str, Any]:
    outbox = _outbox(s, m, jetzt - timedelta(days=7))
    integ = m.integrationen or {}
    eintraege = []
    for k in KATALOG:
        einstellung = integ.get(k["ziel"]) or {}
        aktiv = k["typ"] in (einstellung.get("kanaele") or [])
        konfig = einstellung.get(k["typ"]) or {}
        eintraege.append({**k, "aktiv": aktiv, "konfiguriert": bool(konfig) or k["typ"] == "csv", "reihenfolge": (einstellung.get("kanaele") or []).index(k["typ"]) + 1 if aktiv else None,
                          "outbox": outbox.get(k["ziel"]) if k["ziel"] in outbox and aktiv else None,
                          "zustand": "aktiv" if aktiv and not (outbox.get(k["ziel"]) or {}).get("tot") else ("gestoert" if aktiv else ("vorbereitet" if konfig else "aus"))})
    dienste = dienste or {}
    dl = [{**d, "zustand": {True: "aktiv", False: "aus", None: "unbekannt"}[dienste.get(d["schluessel"])]} for d in DIENSTE]
    aktiv_n = sum(1 for e in eintraege if e["aktiv"])
    return {"integrationen": eintraege, "dienste": dl, "aktiv": aktiv_n, "gestoert": sum(1 for e in eintraege if e["zustand"] == "gestoert"),
            "kunde_kanaele": [e["typ"] for e in eintraege if e["ziel"] == "endkunde" and e["aktiv"]],
            "online_zahlung": any(e["typ"] == "stripe" and e["aktiv"] for e in eintraege),
            "webhooks": {"whatsapp": "/integrationen/whatsapp/webhook", "stripe": "/integrationen/stripe/webhook"}}


def verbindung_pruefen(m: Mandant, ziel: str, typ: str, zusatz: dict[str, Any] | None = None) -> dict[str, Any]:
    if ziel not in ERLAUBT or typ not in ERLAUBT[ziel]:
        raise ValueError(f"{typ!r} ist für {ziel} nicht vorgesehen")
    einstellung = (m.integrationen or {}).get(ziel) or {}
    konfig = einstellung.get(typ) or {}
    if not konfig and typ != "csv":
        return {"ok": False, "meldung": "Noch keine Zugangsdaten gespeichert."}
    try:
        adapter = Register(zusatz=zusatz).bauen(typ, konfig)
        if hasattr(adapter, "pruefen"):
            return {"ok": True, "meldung": adapter.pruefen()}
        return {"ok": True, "meldung": "Konfiguration gültig; dieser Kanal hat keinen Prüfaufruf (Zustellung zeigt sich in der Outbox)."}
    except Konfigurationsfehler as e:
        return {"ok": False, "meldung": str(e)}
    except AdapterFehler as e:
        return {"ok": False, "meldung": f"Nicht erreichbar: {e}"}
    except Exception as e:   # Netzfehler, Zeitüberschreitung
        return {"ok": False, "meldung": f"Fehler: {type(e).__name__}: {str(e)[:200]}"}


def adapter_fuer(m: Mandant, ziel: str, typ: str, zusatz: dict[str, Any] | None = None):
    konfig = ((m.integrationen or {}).get(ziel) or {}).get(typ)
    if not konfig or typ not in (((m.integrationen or {}).get(ziel) or {}).get("kanaele") or []):
        return None
    return Register(zusatz=zusatz).bauen(typ, konfig)


def konfig_lesen(m: Mandant, ziel: str, typ: str) -> dict[str, Any]:
    return konfiguration_entschluesseln(((m.integrationen or {}).get(ziel) or {}).get(typ) or {})


def mandant_nach_feld(s: Session, ziel: str, typ: str, feld: str, wert: str) -> Mandant | None:
    """Für eingehende Webhooks ohne Mandantenkopf: der Betrieb, dessen Konfiguration den Wert trägt (z. B. phone_number_id)."""
    if not wert:
        return None
    for m in s.scalars(select(Mandant)):
        if str(konfig_lesen(m, ziel, typ).get(feld) or "") == str(wert):
            return m
    return None


def mandant_nach_referenz(s: Session, zielsystem: str, externe_id: str) -> tuple[Mandant, ExternalRef] | None:
    ref = s.scalar(select(ExternalRef).where(ExternalRef.zielsystem == zielsystem, ExternalRef.externe_id == externe_id))
    if ref is None:
        return None
    m = s.get(Mandant, ref.mandant_id)
    return (m, ref) if m else None
