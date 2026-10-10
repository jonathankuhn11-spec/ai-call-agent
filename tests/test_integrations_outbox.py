"""Ausgehende Webhooks: Outbox, Signatur, Idempotency-Key, Wiederholungsplan, Dead Letter."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import KUNDEN_SECRET
from integrations.modelle import Ereignis
from integrations.outbox import RETRY_PLAN_S, Outbox
from integrations.signatur import SignaturFehler, svix_pruefen


class Uhr:
    def __init__(self):
        self.t = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def vor(self, sekunden):
        self.t += timedelta(seconds=sekunden)


def ereignis(n=1):
    return Ereignis(id=f"evt_{n}", typ="anruf.beendet", zeit="2026-10-10T12:00:00.000Z", lead_id=1, daten={"n": n})


@pytest.fixture
def aufbau(con, kundensystem):
    uhr = Uhr()
    kundensystem["jetzt"] = uhr                      # Empfänger prüft den Zeitstempel gegen dieselbe Uhr
    outbox = Outbox(con, "https://kunde.example/webhooks", KUNDEN_SECRET, client=kundensystem["client"], jetzt=uhr)
    return outbox, kundensystem, uhr


def test_delivery_is_signed_and_carries_an_idempotency_key(aufbau):
    outbox, kunde, uhr = aufbau
    outbox.einreihen(ereignis(1))
    assert outbox.zustellen() == {"zugestellt": 1, "wiederholen": 0, "tot": 0}
    e = kunde["empfangen"][0]
    assert e["id"] == "evt_1" and e["typ"] == "anruf.beendet" and e["body"]["daten"] == {"n": 1}
    assert e["body"]["version"] == 1 and outbox.status() == {"zugestellt": 1}
    assert outbox.zustellen() == {"zugestellt": 0, "wiederholen": 0, "tot": 0}           # nichts doppelt


def test_signature_uses_exactly_the_bytes_sent(aufbau):
    outbox, kunde, uhr = aufbau
    outbox.einreihen(ereignis(2))
    body = outbox.eintraege()[0]["body"].encode()
    gesehen = []

    def handler(request):
        gesehen.append((request.content, dict(request.headers)))
        return httpx.Response(200)

    outbox.client = httpx.Client(transport=httpx.MockTransport(handler))
    outbox.zustellen()
    content, kopf = gesehen[0]
    assert content == body
    assert svix_pruefen(KUNDEN_SECRET, content, kopf, jetzt=uhr().timestamp()) == "evt_2"
    with pytest.raises(SignaturFehler):
        svix_pruefen(KUNDEN_SECRET, content + b" ", kopf, jetzt=uhr().timestamp())


def test_retry_plan_matches_the_documented_schedule_and_ends_in_dead_letter(aufbau):
    outbox, kunde, uhr = aufbau
    kunde["ausfaelle"] = 99
    outbox.einreihen(ereignis(3))
    for versuch, pause in enumerate(RETRY_PLAN_S[1:], start=1):
        assert outbox.zustellen()["wiederholen"] == 1
        assert outbox.zustellen() == {"zugestellt": 0, "wiederholen": 0, "tot": 0}       # noch nicht fällig
        uhr.vor(pause - 1)
        assert outbox.faellig() == []
        uhr.vor(1)
        assert len(outbox.faellig()) == 1
    assert outbox.zustellen()["tot"] == 1 and outbox.status() == {"tot": 1}
    tot = outbox.eintraege("tot")[0]
    assert tot["versuche"] == len(RETRY_PLAN_S) and tot["letzte_antwort"] == "HTTP 503"
    kunde["ausfaelle"] = 0
    outbox.erneut(tot["id"])
    assert outbox.zustellen()["zugestellt"] == 1 and kunde["empfangen"][0]["id"] == "evt_3"


def test_transient_failure_then_success(aufbau):
    outbox, kunde, uhr = aufbau
    kunde["ausfaelle"] = 2
    outbox.einreihen(ereignis(4))
    outbox.zustellen(); uhr.vor(5); outbox.zustellen(); uhr.vor(300)
    assert outbox.zustellen()["zugestellt"] == 1
    assert outbox.eintraege("zugestellt")[0]["versuche"] == 3


def test_network_errors_are_retried_like_http_errors(aufbau):
    outbox, kunde, uhr = aufbau

    def handler(request):
        raise httpx.ConnectError("keine Verbindung")

    outbox.client = httpx.Client(transport=httpx.MockTransport(handler))
    outbox.einreihen(ereignis(5))
    assert outbox.zustellen()["wiederholen"] == 1
    assert outbox.eintraege()[0]["letzte_antwort"].startswith("ConnectError")


def test_unsigned_outbox_when_no_secret_is_configured(con):
    gesehen = []

    def handler(request):
        gesehen.append(dict(request.headers)); return httpx.Response(204)

    outbox = Outbox(con, "https://kunde.example/x", "", client=httpx.Client(transport=httpx.MockTransport(handler)))
    outbox.einreihen(ereignis(6))
    assert outbox.zustellen()["zugestellt"] == 1
    assert "svix-signature" not in gesehen[0] and gesehen[0]["idempotency-key"] == "evt_6"
    assert json.loads(outbox.eintraege()[0]["body"])["typ"] == "anruf.beendet"


def test_delivery_runs_outside_the_shared_lock(con):
    """Ein langsames Kundensystem darf keinen Tool-Aufruf blockieren: während des POST ist die Sperre frei."""
    import threading
    sperre = threading.Lock()
    frei_waehrend_http = []

    def handler(request):
        frei = sperre.acquire(blocking=False)
        if frei:
            sperre.release()
        frei_waehrend_http.append(frei)
        return httpx.Response(200)

    outbox = Outbox(con, "https://kunde.example/x", "", client=httpx.Client(transport=httpx.MockTransport(handler)))
    outbox.einreihen(ereignis(7))
    outbox.einreihen(ereignis(8))
    assert outbox.zustellen(sperre=sperre) == {"zugestellt": 2, "wiederholen": 0, "tot": 0}
    assert frei_waehrend_http == [True, True]


def test_invalid_customer_secret_fails_at_startup(con):
    with pytest.raises(ValueError, match="KUNDE_WEBHOOK_SECRET"):
        Outbox(con, "https://kunde.example/x", "whsec_###")


def test_scheduler_tick_retries_without_further_webhooks(con, konfig, kundensystem, quiet):
    """Nach einem Ausfall holt der Planer die Zustellung im Takt nach, ohne dass ein neuer Webhook kommt."""
    from integrations.server import erstelle_app
    uhr = Uhr()
    kundensystem["jetzt"] = uhr
    outbox = Outbox(con, "https://kunde.example/webhooks", KUNDEN_SECRET, client=kundensystem["client"], jetzt=uhr)
    app = erstelle_app(konfig, con=con, outbox=outbox, planer_starten=False)
    kundensystem["ausfaelle"] = 1
    outbox.einreihen(ereignis(9))
    assert app.state.ctx.planer.tick()["outbox"] == {"zugestellt": 0, "wiederholen": 1, "tot": 0}
    uhr.vor(5)
    assert app.state.ctx.planer.tick()["outbox"] == {"zugestellt": 1, "wiederholen": 0, "tot": 0}
    assert kundensystem["empfangen"][0]["id"] == "evt_9" and app.state.ctx.planer.letzter_bericht["outbox"]["zugestellt"] == 1
