"""OIDC-Anmeldung und Rollenmodell: Tokens werden lokal signiert, die JWKS-Abfrage ist ersetzt."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from core.api import app_erzeugen
from core.auth import Authentifizierung, OidcPruefer
from core.db import Datenbank
from core.demo import DEMO_ZEIT, stammdaten_anlegen

ISSUER = "https://auth.example.test/realms/shk"
SCHLUESSEL = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FREMD = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(mandant, rollen, name="b.buero", schluessel=SCHLUESSEL, **abweichend):
    claims = {"iss": ISSUER, "aud": ["shk-core", "account"], "sub": f"sub-{name}", "preferred_username": name,
              "exp": int(time.time()) + 300, "realm_access": {"roles": [*rollen, "offline_access"]}, "mandant": mandant}
    claims.update(abweichend)
    if claims.pop("kein_mandant", False):
        del claims["mandant"]
    return {"Authorization": "Bearer " + jwt.encode(claims, schluessel, algorithm="RS256")}


@pytest.fixture
def umgebung():
    auth = Authentifizierung(api_key="maschine", oidc=OidcPruefer(ISSUER, "shk-core", client_id="shk-buero",
                                                                  schluessel=lambda _t: SCHLUESSEL.public_key()))
    c = TestClient(app_erzeugen(Datenbank("sqlite://"), uhr=lambda: DEMO_ZEIT, auth=auth))
    c.headers["X-API-Key"] = "maschine"
    ids = stammdaten_anlegen(c)
    del c.headers["X-API-Key"]
    return c, ids


def test_ohne_anmeldung_abgewiesen(umgebung):
    c, ids = umgebung
    assert c.get("/kunden", headers={"X-Mandant": ids["mandant"]}).status_code == 401
    assert c.get("/kunden", headers={"X-Mandant": ids["mandant"], "X-API-Key": "falsch"}).status_code == 401
    assert c.get("/auth/konfiguration").json()["oidc"]["issuer"] == ISSUER


def test_person_bekommt_mandant_aus_dem_token(umgebung):
    c, ids = umgebung
    h = token(ids["mandant"], ["buero"])
    ich = c.get("/ich", headers=h).json()
    assert ich["name"] == "b.buero" and ich["rollen"] == ["buero"] and "freigeben_c" not in ich["rechte"]
    assert len(c.get("/kunden", headers=h).json()) == 1  # ohne X-Mandant
    assert [m["id"] for m in c.get("/mandanten", headers=h).json()] == [ids["mandant"]]
    assert c.get("/kunden", headers={**h, "X-Mandant": "anderer"}).status_code == 403


@pytest.mark.parametrize("abweichung, status", [
    ({"exp": int(time.time()) - 120}, 401),
    ({"aud": "anderer-dienst"}, 401),
    ({"iss": "https://boese.example/realms/shk"}, 401),
    ({"kein_mandant": True}, 403),
])
def test_ungueltige_tokens(umgebung, abweichung, status):
    c, ids = umgebung
    assert c.get("/kunden", headers=token(ids["mandant"], ["inhaber"], **abweichung)).status_code == status


def test_fremder_signaturschluessel(umgebung):
    c, ids = umgebung
    assert c.get("/kunden", headers=token(ids["mandant"], ["inhaber"], schluessel=FREMD)).status_code == 401


def test_monteur_liest_und_meldet_nur_durchgefuehrt(umgebung):
    c, ids = umgebung
    buero, monteur = token(ids["mandant"], ["buero"]), token(ids["mandant"], ["monteur"], name="m.monteur")
    assert c.post("/kunden", headers=monteur, json={"name": "X"}).status_code == 403
    assert c.post("/ressourcen", headers=buero, json={"name": "Neu"}).status_code == 403  # nur Inhaber
    fall = c.post("/servicefaelle", headers=buero, json={"kunde_id": ids["kunde"], "anliegen": "Tropft"}).json()
    assert c.get(f"/servicefaelle/{fall['id']}", headers=monteur).status_code == 200
    assert c.post(f"/servicefaelle/{fall['id']}/status", headers=monteur, json={"ziel": "qualifiziert"}).status_code == 403
    assert c.post(f"/servicefaelle/{fall['id']}/status", headers=buero, json={"ziel": "qualifiziert"}).status_code == 200


def test_klasse_c_nur_inhaber_und_name_aus_dem_token(umgebung):
    c, ids = umgebung
    buero, inhaber = token(ids["mandant"], ["buero"]), token(ids["mandant"], ["inhaber"], name="i.inhaber")
    f = c.post("/freigaben", headers=buero, json={"aktion": "rechnung_erstellen", "payload": {"betrag": 1}}).json()
    assert f["risikoklasse"] == "C"
    assert c.post(f"/freigaben/{f['id']}/entscheidung", headers=buero, json={"freigeben": True}).status_code == 403
    r = c.post(f"/freigaben/{f['id']}/entscheidung", headers=inhaber, json={"freigeben": True, "wer": "gefälscht"})
    assert r.status_code == 200 and r.json()["entschieden_von"] == "i.inhaber"


def test_maschine_braucht_x_mandant(umgebung):
    c, ids = umgebung
    assert c.get("/kunden", headers={"X-API-Key": "maschine"}).status_code == 422
    assert c.get("/kunden", headers={"X-API-Key": "maschine", "X-Mandant": ids["mandant"]}).status_code == 200
    assert c.post("/mandanten", headers=token(ids["mandant"], ["inhaber"]), json={"name": "Neu"}).status_code == 403
