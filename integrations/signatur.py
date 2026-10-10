"""Signaturen für ein- und ausgehende Webhooks. Ohne externe Bibliothek, damit jede Zeile prüfbar ist.

Zwei Verfahren kommen bei telli vor, beide HMAC-SHA256:

1. Svix (Webhook-Ereignisse wie call_ended): Kopfzeilen svix-id, svix-timestamp, svix-signature.
   Signiert wird "{id}.{timestamp}.{body}" mit dem base64-dekodierten Geheimnis (Präfix whsec_),
   die Signatur steht base64-kodiert als "v1,<signatur>" im Header, bei Schlüsselrotation
   mehrere durch Leerzeichen getrennt. Der Zeitstempel schützt gegen Replay (Toleranz 5 Minuten).
2. x-telli-signature (Custom Calendar, Contact Lookup): Hex-Digest über den rohen Request-Body,
   Schlüssel ist der API-Key des Accounts.

Ausgehende Webhooks dieser Schicht (an Ticketsystem, Automationsplattform) nutzen das
Svix-Format. Dann braucht der Kunde für alle Webhooks nur ein Verifikationsmuster.

Grundregeln, die hier eingehalten werden:
- Immer über den rohen Body prüfen, nie über neu serialisiertes JSON (Feldreihenfolge und
  Leerzeichen würden die Signatur brechen).
- Vergleich in konstanter Zeit (hmac.compare_digest), kein ==.
- Unbekannte Signaturversionen überspringen, aber mindestens eine muss passen.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Mapping, Optional


class SignaturFehler(Exception):
    """Anfrage ablehnen (HTTP 401). Die Nachricht ist bewusst knapp, sie landet in Logs."""


def _kopf(headers: Mapping[str, str], *namen: str) -> Optional[str]:
    klein = {k.lower(): v for k, v in headers.items()}
    for n in namen:
        if n.lower() in klein:
            return klein[n.lower()]
    return None


def _gleich(a: str, b: str) -> bool:
    """Vergleich in konstanter Zeit über Bytes: Kopfzeilen mit Nicht-ASCII-Zeichen sind nur falsch, kein Absturz."""
    return hmac.compare_digest(a.encode("utf-8", "surrogateescape"), b.encode("utf-8", "surrogateescape"))


def _svix_schluessel(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_"):]
    try:
        return base64.b64decode(secret, validate=True)
    except Exception as e:  # noqa: BLE001
        raise SignaturFehler("Webhook-Geheimnis ist kein gültiges base64") from e


def svix_signieren(secret: str, msg_id: str, zeitstempel: int, body: bytes) -> str:
    """Signaturheader-Wert für eine ausgehende Nachricht: "v1,<base64>"."""
    inhalt = f"{msg_id}.{zeitstempel}.".encode() + body
    sig = hmac.new(_svix_schluessel(secret), inhalt, hashlib.sha256).digest()
    return "v1," + base64.b64encode(sig).decode()


def svix_kopfzeilen(secret: str, msg_id: str, body: bytes, zeitstempel: Optional[int] = None) -> dict[str, str]:
    """Die drei Kopfzeilen, mit denen eine ausgehende Nachricht signiert wird."""
    ts = int(zeitstempel if zeitstempel is not None else time.time())
    return {"svix-id": msg_id, "svix-timestamp": str(ts), "svix-signature": svix_signieren(secret, msg_id, ts, body)}


def svix_pruefen(secret: str, body: bytes, headers: Mapping[str, str],
                 jetzt: Optional[float] = None, toleranz_s: int = 300) -> str:
    """Prüft Signatur und Zeitstempel. Gibt die Nachrichten-ID zurück (für die Deduplizierung)."""
    msg_id = _kopf(headers, "svix-id", "webhook-id")
    ts_roh = _kopf(headers, "svix-timestamp", "webhook-timestamp")
    sig_roh = _kopf(headers, "svix-signature", "webhook-signature")
    if not msg_id or not ts_roh or not sig_roh:
        raise SignaturFehler("Svix-Kopfzeilen fehlen")
    try:
        ts = int(ts_roh)
    except ValueError as e:
        raise SignaturFehler("Zeitstempel unlesbar") from e
    jetzt = time.time() if jetzt is None else jetzt
    if abs(jetzt - ts) > toleranz_s:
        raise SignaturFehler("Zeitstempel außerhalb der Toleranz")
    erwartet = svix_signieren(secret, msg_id, ts, body).split(",", 1)[1]
    for eintrag in sig_roh.split():
        if "," not in eintrag:
            continue
        version, wert = eintrag.split(",", 1)
        if version == "v1" and _gleich(wert, erwartet):
            return msg_id
    raise SignaturFehler("Signatur passt nicht")


def telli_signatur(api_key: str, body: bytes) -> str:
    """Hex-Digest, wie telli ihn in x-telli-signature schickt (Custom Calendar, Contact Lookup)."""
    return hmac.new(api_key.encode(), body, hashlib.sha256).hexdigest()


def telli_pruefen(api_key: str, body: bytes, headers: Mapping[str, str]) -> None:
    kopf = _kopf(headers, "x-telli-signature")
    if not kopf:
        raise SignaturFehler("x-telli-signature fehlt")
    if not _gleich(kopf.strip().lower(), telli_signatur(api_key, body)):
        raise SignaturFehler("x-telli-signature passt nicht")


def bearer_pruefen(erwartet: str, headers: Mapping[str, str]) -> None:
    """Custom Tools: telli schickt ein als "Secret" hinterlegtes Token im Authorization-Header."""
    kopf = _kopf(headers, "authorization") or ""
    teile = kopf.split(None, 1)
    token = teile[1].strip() if len(teile) == 2 and teile[0].lower() == "bearer" else ""
    if not token or not _gleich(token, erwartet):
        raise SignaturFehler("Bearer-Token fehlt oder passt nicht")
