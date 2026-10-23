"""Verschlüsselung von Zugangsdaten der Anbindungen (API-Schlüssel, SMTP-Passwörter).

Symmetrisch mit Fernet (AES-128-CBC + HMAC) unter dem Schlüssel INTEGRATIONS_KEY. Werte in der
Datenbank tragen das Präfix „enc:“; ohne Schlüssel bleiben Geheimnisse im Klartext nur in der
Entwicklung erlaubt (Warnung im Log). Schlüssel erzeugen: python -m core schluessel
"""
from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

PRAEFIX = "enc:"
GEHEIME_FELDER = {"api_key", "api_token", "passwort", "token", "secret", "signatur_schluessel", "client_secret", "refresh_token"}
log = logging.getLogger(__name__)


def _fernet() -> Fernet | None:
    schluessel = os.environ.get("INTEGRATIONS_KEY")
    return Fernet(schluessel.encode()) if schluessel else None


def schluessel_erzeugen() -> str:
    return Fernet.generate_key().decode()


def verschluesseln(wert: str) -> str:
    if wert.startswith(PRAEFIX):
        return wert
    f = _fernet()
    if f is None:
        log.warning("INTEGRATIONS_KEY fehlt: Geheimnis wird im Klartext gespeichert (nur Entwicklung)")
        return wert
    return PRAEFIX + f.encrypt(wert.encode()).decode()


def entschluesseln(wert: str) -> str:
    if not wert.startswith(PRAEFIX):
        return wert
    f = _fernet()
    if f is None:
        raise RuntimeError("INTEGRATIONS_KEY fehlt, verschlüsseltes Geheimnis kann nicht gelesen werden")
    try:
        return f.decrypt(wert[len(PRAEFIX):].encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("INTEGRATIONS_KEY passt nicht zum gespeicherten Geheimnis") from e


def konfiguration_verschluesseln(konfig: dict) -> dict:
    return {k: verschluesseln(v) if k in GEHEIME_FELDER and isinstance(v, str) and v else v for k, v in konfig.items()}


def konfiguration_entschluesseln(konfig: dict) -> dict:
    return {k: entschluesseln(v) if k in GEHEIME_FELDER and isinstance(v, str) else v for k, v in konfig.items()}


def konfiguration_maskieren(konfig: dict) -> dict:
    """Für die Ausgabe an die Oberfläche: Geheimnisse nur als „gesetzt“ kennzeichnen."""
    return {k: ("••••" if v else "") if k in GEHEIME_FELDER else v for k, v in konfig.items()}
