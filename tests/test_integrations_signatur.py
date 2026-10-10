"""Signaturen: Svix (Webhook-Ereignisse), x-telli-signature (Kalender, Lookup), Bearer (Custom Tools)."""
import base64
import hashlib
import hmac

import pytest

from integrations import signatur
from integrations.signatur import SignaturFehler

SECRET = "whsec_dGVzdC13ZWJob29rLXNlY3JldA=="     # base64("test-webhook-secret")
BODY = b'{"event":"call_ended","call":{"call_id":"c1"}}'


def test_svix_signature_matches_the_reference_computation():
    kopf = signatur.svix_kopfzeilen(SECRET, "msg_1", BODY, zeitstempel=1_700_000_000)
    erwartet = hmac.new(b"test-webhook-secret", b"msg_1.1700000000." + BODY, hashlib.sha256).digest()
    assert kopf["svix-signature"] == "v1," + base64.b64encode(erwartet).decode()
    assert kopf["svix-id"] == "msg_1" and kopf["svix-timestamp"] == "1700000000"


def test_svix_roundtrip_returns_message_id_and_accepts_rotated_secrets():
    kopf = signatur.svix_kopfzeilen(SECRET, "msg_2", BODY, zeitstempel=1_700_000_000)
    assert signatur.svix_pruefen(SECRET, BODY, kopf, jetzt=1_700_000_010) == "msg_2"
    # zweite Signatur eines alten Schlüssels davor, Header-Namen in anderer Schreibweise
    kopf2 = {"Svix-Id": "msg_2", "SVIX-TIMESTAMP": "1700000000",
             "svix-signature": "v1,QUJD v1," + kopf["svix-signature"].split(",")[1] + " v2,xyz"}
    assert signatur.svix_pruefen(SECRET, BODY, kopf2, jetzt=1_700_000_010) == "msg_2"


@pytest.mark.parametrize("manipulation", ["body", "secret", "zeit", "signatur", "fehlt"])
def test_svix_rejects_tampering_replay_and_missing_headers(manipulation):
    kopf = signatur.svix_kopfzeilen(SECRET, "msg_3", BODY, zeitstempel=1_700_000_000)
    body, secret, jetzt = BODY, SECRET, 1_700_000_000
    if manipulation == "body":
        body = BODY.replace(b"c1", b"c2")
    elif manipulation == "secret":
        secret = "whsec_YW5kZXJlcy1zZWNyZXQ="
    elif manipulation == "zeit":
        jetzt = 1_700_000_000 + 301                     # eine Sekunde über der Toleranz: Replay
    elif manipulation == "signatur":
        kopf["svix-signature"] = "v1,AAAA"
    elif manipulation == "fehlt":
        del kopf["svix-timestamp"]
    with pytest.raises(SignaturFehler):
        signatur.svix_pruefen(secret, body, kopf, jetzt=jetzt)


def test_svix_secret_must_be_base64():
    with pytest.raises(SignaturFehler):
        signatur.svix_signieren("whsec_###", "m", 1, BODY)


def test_telli_signature_is_hex_hmac_over_the_raw_body():
    sig = signatur.telli_signatur("api-key", BODY)
    assert sig == hmac.new(b"api-key", BODY, hashlib.sha256).hexdigest()
    signatur.telli_pruefen("api-key", BODY, {"X-Telli-Signature": sig.upper()})
    with pytest.raises(SignaturFehler):
        signatur.telli_pruefen("api-key", BODY + b" ", {"x-telli-signature": sig})   # neu serialisiert = anderer Body
    with pytest.raises(SignaturFehler):
        signatur.telli_pruefen("api-key", BODY, {})


def test_bearer_token_check():
    signatur.bearer_pruefen("geheim", {"Authorization": "Bearer geheim"})
    for kopf in ({"Authorization": "Bearer falsch"}, {"Authorization": "Basic geheim"}, {}):
        with pytest.raises(SignaturFehler):
            signatur.bearer_pruefen("geheim", kopf)
