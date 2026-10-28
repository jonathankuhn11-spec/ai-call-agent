"""Online-Zahlung per Stripe Checkout (Kartenzahlung, Apple/Google Pay, SEPA-Lastschrift, Klarna je nach Konto).

Keine Stripe-Bibliothek: zwei HTTP-Aufrufe (Checkout-Session anlegen, Konto prüfen) und die Webhook-Signatur
(HMAC-SHA256 über "t.payload", Schema v1, Toleranz 5 Minuten). Konfiguration: secret_key (sk_live_/sk_test_),
webhook_secret (whsec_), optional zahlungsarten (Komma-Liste, Standard automatisch). Der Betrieb braucht ein
Stripe-Konto; Gebühren fallen bei Stripe an. Zahlungsdaten erreichen die Plattform nie (Hosted Checkout).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from core.adapter.basis import (
    AdapterFehler,
    Konfigurationsfehler,
    antwort_pruefen,
    client,
)
from core.modelle import Mandant, OutboxEintrag, Rechnung

BASIS = "https://api.stripe.com"
TOLERANZ_SEKUNDEN = 300


class StripeAdapter:
    name = "stripe"

    def __init__(self, konfig: dict[str, Any], transport: httpx.BaseTransport | None = None):
        if not str(konfig.get("secret_key") or "").startswith("sk_"):
            raise Konfigurationsfehler("Stripe: secret_key (sk_live_… oder sk_test_…) fehlt")
        self.k = konfig
        self.http = client(konfig.get("basis_url") or BASIS, {"Authorization": f"Bearer {konfig['secret_key']}"}, transport)

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        return None  # kein Outbox-Ziel: Sessions entstehen auf Wunsch des Kunden im Portal

    def checkout(self, r: Rechnung, m: Mandant, erfolg_url: str, abbruch_url: str, email: str | None = None) -> dict[str, str]:
        cent = int((Decimal(r.brutto) * 100).quantize(Decimal("1")))
        if cent <= 0:
            raise AdapterFehler("Stripe: Betrag muss größer 0 sein")
        felder = {"mode": "payment", "success_url": erfolg_url, "cancel_url": abbruch_url, "client_reference_id": r.id,
                  "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": "eur", "line_items[0][price_data][unit_amount]": str(cent),
                  "line_items[0][price_data][product_data][name]": f"Rechnung {r.nummer} – {m.name}"[:250],
                  "metadata[rechnung_id]": r.id, "metadata[mandant_id]": m.id, "metadata[nummer]": r.nummer, "locale": "de"}
        if email:
            felder["customer_email"] = email
        for i, art in enumerate(a.strip() for a in str(self.k.get("zahlungsarten") or "").split(",") if a.strip()):
            felder[f"payment_method_types[{i}]"] = art
        antwort = antwort_pruefen(self.http.post("/v1/checkout/sessions", data=felder, headers={"Idempotency-Key": f"rechnung-{r.id}-{cent}"}), "Stripe")
        return {"id": antwort["id"], "url": antwort["url"]}

    def pruefen(self) -> str:
        a = antwort_pruefen(self.http.get("/v1/balance"), "Stripe")
        modus = "Testmodus" if str(self.k.get("secret_key", "")).startswith("sk_test_") else "Livemodus"
        verfuegbar = ", ".join(f"{x.get('amount', 0) / 100:.2f} {x.get('currency', '').upper()}" for x in a.get("available") or [])
        return f"Verbunden ({modus}); verfügbar: {verfuegbar or '0,00 EUR'}"


def signatur_pruefen(body: bytes, kopf: str | None, geheimnis: str, jetzt: float | None = None) -> bool:
    if not kopf or not geheimnis:
        return False
    teile = dict(p.split("=", 1) for p in kopf.split(",") if "=" in p)
    t, v1 = teile.get("t"), teile.get("v1")
    if not t or not v1 or not t.isdigit():
        return False
    if abs((jetzt if jetzt is not None else time.time()) - int(t)) > TOLERANZ_SEKUNDEN:
        return False
    erwartet = hmac.new(geheimnis.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(erwartet, v1)


def ereignis_lesen(body: bytes) -> dict[str, Any] | None:
    """Liefert {session_id, rechnung_id, mandant_id, betrag, status} für checkout.session.completed, sonst None."""
    try:
        ev = json.loads(body)
    except ValueError:
        return None
    if ev.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return None
    o = (ev.get("data") or {}).get("object") or {}
    if o.get("payment_status") != "paid":
        return None
    meta = o.get("metadata") or {}
    return {"session_id": o.get("id"), "rechnung_id": meta.get("rechnung_id") or o.get("client_reference_id"), "mandant_id": meta.get("mandant_id"),
            "betrag": f"{(o.get('amount_total') or 0) / 100:.2f}", "status": o.get("payment_status")}
