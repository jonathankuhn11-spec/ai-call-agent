"""Migrationen und Row-Level Security gegen echtes PostgreSQL.

Läuft nur mit PG_TEST_URL (Superuser bzw. Eigentümer einer Wegwerf-Datenbank), z. B.
PG_TEST_URL=postgresql+psycopg://shk:ci@localhost:5432/shk python -m pytest tests/test_postgres.py
Die Tests leeren das Schema public dieser Datenbank.
"""
import os

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from core.api import app_erzeugen
from core.auth import Authentifizierung
from core.db import WURZEL, Datenbank, migrieren
from core.modelle import OutboxEintrag
from core.zustellung import ProtokollAdapter, abarbeiten

URL = os.environ.get("PG_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="PG_TEST_URL nicht gesetzt")
PASSWORT = "rls-test"


def als(rolle: str) -> str:
    return make_url(URL).set(username=rolle, password=PASSWORT).render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def pg():
    eigentuemer = create_engine(URL, isolation_level="AUTOCOMMIT")
    with eigentuemer.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        for rolle, bypass in (("shk_app", "NOBYPASSRLS"), ("shk_system", "BYPASSRLS")):
            if not c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": rolle}).first():
                c.execute(text(f"CREATE ROLE {rolle} LOGIN {bypass}"))
            c.execute(text(f"ALTER ROLE {rolle} PASSWORD '{PASSWORT}'"))
    migrieren(URL)
    yield eigentuemer
    eigentuemer.dispose()


@pytest.fixture(scope="module")
def client(pg):
    app = app_erzeugen(Datenbank(als("shk_app")), system_db=Datenbank(als("shk_system")),
                       auth=Authentifizierung(api_key="pg-test"))
    return TestClient(app, headers={"X-API-Key": "pg-test"})


def betrieb(client, name: str) -> tuple[str, str]:
    m = client.post("/mandanten", json={"name": name}).json()["id"]
    k = client.post("/kunden", json={"name": f"Kunde {name}", "telefon": "0221 1"}, headers={"X-Mandant": m}).json()["id"]
    return m, k


def test_migrationen_entsprechen_dem_modell(pg):
    cfg = Config(str(WURZEL / "alembic.ini"))
    cfg.attributes["url"] = URL
    command.check(cfg)  # wirft, wenn Modell und Migrationen auseinanderlaufen


def test_api_sieht_nur_den_eigenen_mandanten(client):
    a, ka = betrieb(client, "A")
    b, kb = betrieb(client, "B")
    sicht_a = {k["id"] for k in client.get("/kunden", headers={"X-Mandant": a}).json()}
    assert ka in sicht_a and kb not in sicht_a
    assert client.get(f"/kunden/{kb}", headers={"X-Mandant": a}).status_code == 404
    assert {m["id"] for m in client.get("/mandanten").json()} >= {a, b}


def test_datenbank_erzwingt_trennung_auch_ohne_filter(client):
    a, _ = betrieb(client, "C")
    b, _ = betrieb(client, "D")
    app_engine = create_engine(als("shk_app"))
    with app_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM kunde")).scalar() == 0  # ohne Mandant: nichts
        c.execute(text("SELECT set_config('app.mandant', :m, true)"), {"m": a})
        assert {r[0] for r in c.execute(text("SELECT mandant_id FROM kunde"))} == {a}
        assert c.execute(text("SELECT count(*) FROM mandant")).scalar() == 1
        with pytest.raises(ProgrammingError, match="row-level security"):
            c.execute(text("INSERT INTO kunde (id, mandant_id, name, einwilligungen, vertragsstatus, erstellt) "
                           "VALUES ('x', :b, 'fremd', '{}', 'ohne', now())"), {"b": b})
    app_engine.dispose()


def test_app_rolle_darf_kein_ddl(pg):
    app_engine = create_engine(als("shk_app"))
    with app_engine.connect() as c, pytest.raises(ProgrammingError):
        c.execute(text("DROP TABLE kunde"))
    app_engine.dispose()


def test_worker_arbeitet_mandantenuebergreifend(client):
    mandanten = set()
    for name in ("E", "F"):
        m, _ = betrieb(client, name)
        mandanten.add(m)
        f = client.post("/freigaben", headers={"X-Mandant": m},
                        json={"aktion": "nachricht_an_endkunde", "payload": {"text": "Test"}}).json()
        assert client.post(f"/freigaben/{f['id']}/entscheidung", headers={"X-Mandant": m},
                           json={"freigeben": True}).status_code == 200
    adapter = {ziel: ProtokollAdapter(ziel) for ziel in ("endkunde", "fachsoftware", "finanz")}
    with Datenbank(als("shk_system")).sitzungen() as s:
        abarbeiten(s, adapter)
        zugestellt = set(s.scalars(select(OutboxEintrag.mandant_id).where(OutboxEintrag.status == "zugestellt")))
    assert mandanten <= zugestellt
