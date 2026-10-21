"""Authentifizierung und Autorisierung des Core-Service.

Zwei Arten von Prinzipalen:
- Maschine: `X-API-Key` (CORE_API_KEY), z. B. Voice-Agent, n8n, Worker-Skripte. Alle Rechte,
  der Mandant kommt aus `X-Mandant`.
- Person: `Authorization: Bearer <Access Token>` eines OIDC-Providers (Keycloak, Realm in
  deploy/keycloak/). Mandant und Rollen stammen ausschließlich aus dem signierten Token; ein
  abweichender `X-Mandant` wird abgewiesen.

Ohne CORE_API_KEY und ohne OIDC_ISSUER läuft der Core im Entwicklungsmodus ohne Anmeldung.

Rollenmodell (Realm-Rollen in Keycloak):
  inhaber        alles, einschließlich Freigaben der Klasse C (finanzwirksam) und Stammdaten Monteure
  buero          CRM, Vorgänge, Disposition, Freigaben der Klassen A und B
  monteur        lesen, Statusmeldung am eigenen Einsatz (durchgeführt)
  steuerberater  lesen
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Literal

import jwt

ROLLEN = ("inhaber", "buero", "monteur", "steuerberater")
RECHTE: dict[str, frozenset[str]] = {
    "lesen": frozenset(ROLLEN),
    "status": frozenset({"inhaber", "buero", "monteur"}),
    "bearbeiten": frozenset({"inhaber", "buero"}),
    "freigeben": frozenset({"inhaber", "buero"}),
    "freigeben_c": frozenset({"inhaber"}),
    "verwalten": frozenset({"inhaber"}),
    "plattform": frozenset(),  # nur Maschinen: Mandanten anlegen
}


class Abgewiesen(Exception):
    def __init__(self, status: int, grund: str):
        super().__init__(grund)
        self.status, self.grund = status, grund


@dataclass(frozen=True)
class Prinzipal:
    art: Literal["maschine", "person", "entwicklung"]
    name: str
    rollen: frozenset[str] = field(default_factory=frozenset)
    mandant: str | None = None

    def darf(self, recht: str) -> bool:
        return self.art != "person" or bool(self.rollen & RECHTE[recht])

    @property
    def rechte(self) -> list[str]:
        return sorted(r for r in RECHTE if self.darf(r))


class OidcPruefer:
    """Prüft Access Tokens gegen die JWKS des Issuers (RS256/ES256), mit Schlüssel-Cache."""

    def __init__(self, issuer: str, audience: str, mandant_claim: str = "mandant", client_id: str | None = None,
                 schluessel: Callable[[str], Any] | None = None):
        self.issuer, self.audience, self.mandant_claim, self.client_id = issuer.rstrip("/"), audience, mandant_claim, client_id
        self._schluessel = schluessel

    @cached_property
    def _jwks(self) -> jwt.PyJWKClient:
        # Discovery statt fester Pfade: funktioniert mit Keycloak, Zitadel, Authentik usw.
        with urllib.request.urlopen(f"{self.issuer}/.well-known/openid-configuration", timeout=5) as r:
            jwks_uri = json.load(r)["jwks_uri"]
        return jwt.PyJWKClient(jwks_uri, cache_keys=True, lifespan=3600)

    def schluessel(self, token: str) -> Any:
        return self._schluessel(token) if self._schluessel else self._jwks.get_signing_key_from_jwt(token).key

    def pruefen(self, token: str) -> Prinzipal:
        try:
            claims = jwt.decode(token, self.schluessel(token), algorithms=["RS256", "ES256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iss", "aud", "sub"]}, leeway=30)
        except jwt.PyJWTError as e:
            raise Abgewiesen(401, f"Token ungültig: {e}") from e
        rollen = set(claims.get("realm_access", {}).get("roles", []))
        if self.client_id:
            rollen |= set(claims.get("resource_access", {}).get(self.client_id, {}).get("roles", []))
        mandant = claims.get(self.mandant_claim)
        if isinstance(mandant, list):  # Keycloak liefert Attribute je nach Mapper als Liste
            mandant = mandant[0] if len(mandant) == 1 else None
        if not mandant:
            raise Abgewiesen(403, f"Token enthält keinen Mandanten (Claim {self.mandant_claim!r})")
        return Prinzipal("person", claims.get("preferred_username") or claims["sub"], frozenset(rollen & set(ROLLEN)), mandant)


@dataclass
class Authentifizierung:
    api_key: str | None = None
    oidc: OidcPruefer | None = None

    @classmethod
    def aus_umgebung(cls) -> Authentifizierung:
        issuer = os.environ.get("OIDC_ISSUER")
        oidc = OidcPruefer(issuer, os.environ.get("OIDC_AUDIENCE", "shk-core"), os.environ.get("OIDC_MANDANT_CLAIM", "mandant"),
                           os.environ.get("OIDC_CLIENT_ID", "shk-buero")) if issuer else None
        return cls(os.environ.get("CORE_API_KEY") or None, oidc)

    @property
    def offen(self) -> bool:
        return not self.api_key and not self.oidc

    def prinzipal(self, authorization: str | None, x_api_key: str | None) -> Prinzipal:
        if authorization and authorization.lower().startswith("bearer "):
            if not self.oidc:
                raise Abgewiesen(401, "Bearer-Token übergeben, aber OIDC_ISSUER ist nicht konfiguriert")
            return self.oidc.pruefen(authorization[7:].strip())
        if x_api_key and self.api_key and secrets.compare_digest(x_api_key, self.api_key):
            return Prinzipal("maschine", "api-key")
        if self.offen:
            return Prinzipal("entwicklung", "entwicklung")
        raise Abgewiesen(401, "Anmeldung erforderlich (X-API-Key oder Bearer-Token)")

    def konfiguration(self) -> dict[str, Any]:
        return {"api_key": bool(self.api_key), "offen": self.offen,
                "oidc": {"issuer": self.oidc.issuer, "client_id": self.oidc.client_id} if self.oidc else None}
