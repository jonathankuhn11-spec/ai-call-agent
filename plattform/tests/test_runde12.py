"""Runde 12: Betreiberstimme (Stimmprofil mit Einwilligung, Widerruf, Stimmdienst, Agentenkonfiguration) und Stimmdienst-API."""
from __future__ import annotations

import io
import math
import struct
import tempfile
import wave
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from core import produkt, stimme
from core.modelle import Ereignis, Mandant, Stimmprofil
from stimme import engines
from stimme.server import app_erzeugen as stimmdienst_erzeugen

HEUTE = date(2026, 11, 17)


def wav(sekunden: float = 8.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 180 * i / rate))) for i in range(int(rate * sekunden))))
    return buf.getvalue()


def bruecke(client: TestClient) -> httpx.MockTransport:
    """Synchroner httpx-Transport auf einen TestClient (Core ↔ Stimmdienst in beide Richtungen ohne Netz)."""
    def handler(req: httpx.Request) -> httpx.Response:
        r = client.request(req.method, req.url.path, content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers=dict(r.headers))
    return httpx.MockTransport(handler)


@pytest.fixture
def stimmdienst(umgebung, monkeypatch):
    c, *_ = umgebung
    app = stimmdienst_erzeugen(engine=engines.TestEngine(), verzeichnis=Path(tempfile.mkdtemp()), api_key="geheim",
                               core_url="http://core", core_api_key="", core_transport=bruecke(c))
    sc = TestClient(app, headers={"Authorization": "Bearer geheim"})
    c.app.state.stimme_transport = bruecke(sc)
    monkeypatch.setenv("STIMME_URL", "http://stimme")
    monkeypatch.setenv("STIMME_API_KEY", "geheim")
    return sc


def test_stimmprofil_mit_einwilligung_aktivieren_und_widerrufen(umgebung, stimmdienst):
    c, db, ids, h = umgebung
    st = c.get("/betrieb/stimme", headers=h).json()
    assert st["feature"] and st["dienst"]["status"]["status"] == "ok" and st["profile"] == [] and "<Name>" in st["einwilligung_vorlage"]
    assert "nachgebildeten Stimme" not in st["begruessung"]

    r = c.post("/betrieb/stimme", headers=h, data={"name": "Jonathan Kuhn", "rolle": "inhaber", "einwilligung": "true", "einwilligung_durch": "Büro"},
               files={"datei": ("inhaber.wav", wav(), "audio/wav")})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["aktiv"] and p["status"] == "bereit" and p["probe_dauer_sek"] == 8.0 and p["probe_mime"] == "audio/wav" and p["synchronisiert"]
    assert "Jonathan Kuhn" in p["einwilligung_text"] and "Demo Haustechnik" in p["einwilligung_text"] and p["einwilligung_am"] == "2026-11-17"
    assert [v["id"] for v in stimmdienst.get("/v1/voices").json()["data"]] == [p["id"]]

    konf = c.get("/voice/konfiguration", headers=h).json()
    assert konf["stimme"]["id"] == p["id"] and konf["stimme"]["sprecher"] == "Jonathan Kuhn" and konf["stimme"]["dienst_url"] == "http://stimme"
    assert "nachgebildeten Stimme von Jonathan Kuhn" in konf["begruessung"] and "KI-System" in konf["begruessung"]
    assert konf["weiterleitung"]["erlaubt"] is False and konf["sprachminuten"] == {"verbraucht": 0.0, "limit": 1000, "erschoepft": False}

    # Hörprobe über den Stimmdienst (WAV), Aufnahme abrufbar, Probe für den Dienst nur mit Maschinenschlüssel
    r = c.post(f"/betrieb/stimme/{p['id']}/vorschau", headers=h, json={"text": "Guten Tag. Wie kann ich helfen?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav") and r.content[:4] == b"RIFF"
    assert c.get(f"/betrieb/stimme/{p['id']}/probe", headers=h).content == wav()
    r = c.get(f"/voice/stimmprobe/{p['id']}")
    assert r.status_code == 200 and r.headers["x-stimme-name"] == "Jonathan Kuhn" and len(r.content) == len(wav())

    # zweites Profil verdrängt das erste als aktives
    r = c.post("/betrieb/stimme", headers=h, data={"name": "Maria Muster", "rolle": "mitarbeiter", "einwilligung": "true"},
               files={"datei": ("maria.wav", wav(6), "audio/wav")})
    assert r.status_code == 201 and r.json()["aktiv"]
    profile = {x["name"]: x for x in c.get("/betrieb/stimme", headers=h).json()["profile"]}
    assert profile["Maria Muster"]["aktiv"] and not profile["Jonathan Kuhn"]["aktiv"]
    assert c.put(f"/betrieb/stimme/{p['id']}/aktiv?aktiv=true", headers=h).json()["aktiv"]
    assert c.get("/voice/konfiguration", headers=h).json()["stimme"]["sprecher"] == "Jonathan Kuhn"

    # Widerruf: Aufnahme weg, Stimme im Dienst gelöscht, Profil bleibt als Nachweis, Agent ohne Stimme
    r = c.delete(f"/betrieb/stimme/{p['id']}", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "widerrufen" and r.json()["probe_groesse"] == 0 and r.json()["widerrufen_am"]
    assert c.get(f"/betrieb/stimme/{p['id']}/probe", headers=h).status_code == 404
    assert c.get(f"/voice/stimmprobe/{p['id']}").status_code == 404
    assert p["id"] not in [v["id"] for v in stimmdienst.get("/v1/voices").json()["data"]]
    assert c.get("/voice/konfiguration", headers=h).json()["stimme"] is None
    assert c.put(f"/betrieb/stimme/{p['id']}/aktiv?aktiv=true", headers=h).status_code == 409
    with db.sitzungen() as s:
        assert s.get(Stimmprofil, p["id"]).probe == b""
        typen = [e.typ for e in s.query(Ereignis).filter(Ereignis.typ.like("stimme.%")).order_by(Ereignis.zeit).all()]
    assert typen[:2] == ["stimme.angelegt", "stimme.aktiviert"] and typen[-1] == "stimme.widerrufen"


def test_stimmprofil_pruefungen_und_feature_gate(umgebung, stimmdienst):
    c, db, ids, h = umgebung
    fehler = lambda daten, datei: c.post("/betrieb/stimme", headers=h, data=daten, files={"datei": datei})  # noqa: E731
    assert "Einwilligung" in fehler({"name": "X Y", "einwilligung": "false"}, ("a.wav", wav(), "audio/wav")).json()["detail"]
    assert "Format" in fehler({"name": "X Y", "einwilligung": "true"}, ("a.txt", b"x" * 30000, "text/plain")).json()["detail"]
    assert "zu kurz" in fehler({"name": "X Y", "einwilligung": "true"}, ("a.wav", wav(0.2), "audio/wav")).json()["detail"]
    assert "mindestens" in fehler({"name": "X Y", "einwilligung": "true"}, ("a.wav", wav(3), "audio/wav")).json()["detail"]
    assert "Rolle" in fehler({"name": "X Y", "rolle": "chef", "einwilligung": "true"}, ("a.wav", wav(), "audio/wav")).json()["detail"]
    assert "Name" in fehler({"name": "X", "einwilligung": "true"}, ("a.wav", wav(), "audio/wav")).json()["detail"]
    # FLAC und MP3 werden an den Magic Bytes erkannt, der gemeldete MIME-Typ zählt nicht
    assert stimme.probe_pruefen(b"fLaC" + b"\0" * 30000, "application/octet-stream") == ("audio/flac", None)
    assert stimme.probe_pruefen(b"ID3" + b"\0" * 30000, None)[0] == "audio/mpeg"

    # Starter: Anlegen gesperrt (402), vorhandenes Profil wird dem Agenten nicht mehr ausgeliefert
    r = c.post("/betrieb/stimme", headers=h, data={"name": "Jonathan Kuhn", "einwilligung": "true"}, files={"datei": ("a.wav", wav(), "audio/wav")})
    assert r.status_code == 201
    with db.sitzungen() as s:
        produkt.edition_zuweisen(s.get(Mandant, ids["mandant"]), "starter", None, HEUTE)
        s.commit()
    r = c.post("/betrieb/stimme", headers=h, data={"name": "Jonathan Kuhn", "einwilligung": "true"}, files={"datei": ("a.wav", wav(), "audio/wav")})
    assert r.status_code == 402 and "eigenen Stimme" in r.json()["detail"]
    st = c.get("/betrieb/stimme", headers=h).json()
    assert st["feature"] is False and len(st["profile"]) == 1
    assert c.get("/voice/konfiguration", headers=h).json()["stimme"] is None


def test_gespraechsdauer_und_kennzahlen_landen_im_kontingent(umgebung):
    c, db, ids, h = umgebung
    r = c.post("/voice/gespraech-beendet", headers=h, json={"transkript": "Agent: Guten Tag", "dauer_sekunden": 150, "stimme_id": "abc",
                                                            "kennzahlen": {"tts_erstes_audio": 0.31, "llm_erstes_token": 0.62}})
    assert r.status_code == 201
    c.post("/voice/gespraech-beendet", headers=h, json={"transkript": "Agent: Hallo", "dauer_sekunden": 30})
    assert c.get("/voice/konfiguration", headers=h).json()["sprachminuten"]["verbraucht"] == 3.0
    k = c.get(f"/kunden/{ids['kunde']}", headers=h)   # Kommunikation ohne Kunde: nur Prüfung, dass die Extraktion gespeichert ist
    assert k.status_code == 200
    with db.sitzungen() as s:
        from core.modelle import Kommunikation
        e = s.query(Kommunikation).filter(Kommunikation.mandant_id == ids["mandant"]).order_by(Kommunikation.zeit).first().extraktion
    assert e["dauer_sekunden"] == 150 and e["kennzahlen"]["tts_erstes_audio"] == 0.31 and e["stimme_id"] == "abc"


def test_konfiguration_ohne_stimmdienst_und_weiterleitung(umgebung, monkeypatch):
    c, db, ids, h = umgebung
    monkeypatch.delenv("STIMME_URL", raising=False)
    c.app.state.stimme_transport = None
    r = c.post("/betrieb/stimme", headers=h, data={"name": "Jonathan Kuhn", "einwilligung": "true"}, files={"datei": ("a.wav", wav(), "audio/wav")})
    assert r.status_code == 201 and r.json()["aktiv"] and r.json()["synchronisiert"] is None   # Dienst holt die Probe beim ersten Anruf
    st = c.get("/betrieb/stimme", headers=h).json()
    assert st["dienst"] == {"konfiguriert": False, "url": "", "status": None}
    assert c.post(f"/betrieb/stimme/{r.json()['id']}/vorschau", headers=h, json={"text": "Hallo"}).status_code == 503
    konf = c.get("/voice/konfiguration", headers=h).json()
    assert konf["stimme"]["dienst_url"] == "" and konf["stimme"]["synchronisiert"] is False
    with db.sitzungen() as s:
        m = s.get(Mandant, ids["mandant"])
        m.policies = {**m.policies, "notdienst_weiterleiten": True}
        s.commit()
    assert c.get("/voice/konfiguration", headers=h).json()["weiterleitung"]["erlaubt"] is True


def test_stimmdienst_api_streamt_pcm_und_holt_unbekannte_stimme_vom_core(umgebung, stimmdienst):
    c, db, ids, h = umgebung
    assert stimmdienst.get("/health").json()["engine"] == "test"
    assert TestClient(stimmdienst.app).get("/v1/voices").status_code == 401   # ohne Schlüssel
    r = stimmdienst.post("/v1/audio/speech", json={"input": "Hallo Welt. Zweiter Satz!", "voice": "egal", "response_format": "wav"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF" and r.headers["x-sample-rate"] == "24000"
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1
    r = stimmdienst.post("/v1/audio/speech", json={"input": "Hallo", "voice": "egal", "response_format": "pcm", "model": "gpt-4o-mini-tts", "stream_format": "sse"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/pcm") and len(r.content) == 2 * int(24000 * 0.25)
    assert stimmdienst.post("/v1/audio/speech", json={"input": "   ", "voice": "egal"}).status_code == 422

    # Stimme registrieren, löschen, bei Bedarf vom Core nachladen
    p = c.post("/betrieb/stimme", headers=h, data={"name": "Jonathan Kuhn", "einwilligung": "true"}, files={"datei": ("a.wav", wav(), "audio/wav")}).json()
    stimmen = stimmdienst.app.state.stimmen
    assert stimmen.pfad(p["id"]).suffix == ".wav"
    stimmen.loeschen(p["id"])
    assert stimmen.pfad(p["id"]) is None
    assert stimmdienst.post("/v1/audio/speech", json={"input": "Hallo", "voice": p["id"]}).status_code == 200
    assert stimmen.pfad(p["id"]) is not None and stimmen.alle()[0]["name"] == "Jonathan Kuhn"
    assert stimmdienst.post("/v1/voices", data={"id": "x/../y", "name": "böse"}, files={"datei": ("a.wav", wav(), "audio/wav")}).status_code == 422
    assert stimmdienst.delete(f"/v1/voices/{p['id']}").status_code == 204 and stimmen.alle() == []


def test_engine_hilfsfunktionen():
    assert engines.saetze("Guten Tag. Wie geht es Ihnen?  Danke!") == ["Guten Tag.", "Wie geht es Ihnen?", "Danke!"]
    lang = ", ".join(f"Position {i}" for i in range(60))
    teile = engines.saetze(lang, max_zeichen=120)
    assert len(teile) > 3 and all(len(t) <= 121 for t in teile) and "".join(teile).replace(" ", "") == lang.replace(" ", "")
    signal = engines.TestEngine().sprechen("Hallo", Path("a.wav"), "de")
    assert len(signal) == int(24000 * 0.25) and max(signal) <= 0.3
    assert len(engines.umtasten(signal, 24000, 16000)) == 4000 and engines.wav_kopf(24000)[:4] == b"RIFF"
    assert len(engines.pcm16(signal)) == 2 * len(signal)
