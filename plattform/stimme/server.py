"""Stimmdienst: selbst gehostete Sprachsynthese mit der geklonten Stimme des Betriebsinhabers.

    python -m stimme                      (Port STIMME_PORT, Standard 8300)
    docker compose --profile stimme up -d stimme

Schnittstelle kompatibel zum OpenAI-Audio-Endpunkt, damit der LiveKit-Agent sie ohne eigenes Plugin
nutzt (`openai.TTS(base_url=…)`), und von jedem anderen Werkzeug ansprechbar ist:

    POST /v1/audio/speech   {"input": "…", "voice": "<Stimmprofil-ID>", "response_format": "pcm"|"wav", "language": "de"}
    GET  /v1/voices         registrierte Stimmen
    POST /v1/voices         multipart: id, name, datei (Referenzaufnahme)        ← Core bei Aktivierung eines Profils
    DELETE /v1/voices/{id}  Referenz löschen (Widerruf der Einwilligung)
    GET  /health

Eine unbekannte Stimme holt der Dienst einmalig vom Core (`GET /voice/stimmprobe/{id}` mit CORE_API_KEY),
sodass nach einem Neustart ohne Datenträger nichts fehlt. Die Synthese läuft satzweise und wird gestreamt,
die erste Silbe ist also nach dem ersten Satz beim Anrufer. Audio wird nirgends protokolliert.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Iterator, Literal

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from stimme import engines

log = logging.getLogger("stimme")
ENDUNGEN = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/flac": ".flac", "audio/x-flac": ".flac",
            "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/ogg": ".ogg"}
MAX_TEXT = 4000


class Sprechen(BaseModel):
    input: str = Field(..., min_length=1, max_length=MAX_TEXT)
    voice: str = Field(..., min_length=1, max_length=64)
    model: str | None = None
    response_format: Literal["pcm", "wav"] = "pcm"
    language: str = "de"
    speed: float = 1.0
    stream_format: str | None = None   # von OpenAI-Clients gesendet, hier ohne Bedeutung
    instructions: str | None = None


class Stimmen:
    """Referenzaufnahmen auf dem Datenträger: <id>.<ext> plus <id>.json (Name, Zeitpunkt)."""

    def __init__(self, verzeichnis: Path):
        self.verzeichnis = verzeichnis
        verzeichnis.mkdir(parents=True, exist_ok=True)

    def pfad(self, stimme_id: str) -> Path | None:
        if not stimme_id.replace("-", "").replace("_", "").isalnum():
            return None
        for p in self.verzeichnis.glob(f"{stimme_id}.*"):
            if p.suffix != ".json":
                return p
        return None

    def speichern(self, stimme_id: str, name: str, inhalt: bytes, mime: str) -> Path:
        if not stimme_id.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(422, "Stimm-ID darf nur Buchstaben, Ziffern, - und _ enthalten")
        self.loeschen(stimme_id)
        ziel = self.verzeichnis / f"{stimme_id}{ENDUNGEN.get(mime, '.wav')}"
        ziel.write_bytes(inhalt)
        (self.verzeichnis / f"{stimme_id}.json").write_text(json.dumps({"name": name, "registriert": time.time(), "mime": mime}), encoding="utf-8")
        return ziel

    def loeschen(self, stimme_id: str) -> bool:
        gefunden = False
        for p in self.verzeichnis.glob(f"{stimme_id}.*"):
            p.unlink()
            gefunden = True
        return gefunden

    def alle(self) -> list[dict]:
        aus = []
        for meta in sorted(self.verzeichnis.glob("*.json")):
            try:
                daten = json.loads(meta.read_text(encoding="utf-8"))
            except ValueError:
                daten = {}
            aus.append({"id": meta.stem, "name": daten.get("name", meta.stem), "registriert": daten.get("registriert")})
        return aus


def app_erzeugen(engine: engines.Engine | None = None, verzeichnis: Path | None = None, api_key: str | None = None,
                 core_url: str | None = None, core_api_key: str | None = None, core_transport: httpx.BaseTransport | None = None) -> FastAPI:
    engine = engine or engines.engine_aus_umgebung()
    stimmen = Stimmen(verzeichnis or Path(os.environ.get("STIMME_VERZEICHNIS", "daten/stimmen")))
    api_key = api_key if api_key is not None else os.environ.get("STIMME_API_KEY", "")
    core_url = (core_url if core_url is not None else os.environ.get("STIMME_CORE_URL", "")).rstrip("/")
    core_api_key = core_api_key if core_api_key is not None else os.environ.get("STIMME_CORE_API_KEY", os.environ.get("CORE_API_KEY", ""))
    app = FastAPI(title="Stimmdienst", version="1", docs_url=None, redoc_url=None)
    app.state.engine, app.state.stimmen = engine, stimmen

    def geschuetzt(authorization: str | None = Header(None), x_api_key: str | None = Header(None)) -> None:
        if not api_key:
            return
        geliefert = x_api_key or (authorization.split(" ", 1)[1] if authorization and " " in authorization else "")
        if not secrets.compare_digest(geliefert or "", api_key):
            raise HTTPException(401, "API-Schlüssel fehlt oder falsch")

    def vom_core_holen(stimme_id: str) -> Path | None:
        if not core_url:
            return None
        try:
            with httpx.Client(transport=core_transport, timeout=20.0) as c:
                r = c.get(f"{core_url}/voice/stimmprobe/{stimme_id}", headers={"X-API-Key": core_api_key})
        except httpx.HTTPError as e:
            log.warning("Stimme %s konnte nicht vom Core geholt werden: %s", stimme_id, e)
            return None
        if r.status_code != 200:
            log.warning("Stimme %s: Core antwortet %s", stimme_id, r.status_code)
            return None
        name = r.headers.get("X-Stimme-Name", stimme_id)
        return stimmen.speichern(stimme_id, name, r.content, r.headers.get("content-type", "audio/wav").split(";")[0])

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "stimmen": len(stimmen.alle()), "core": bool(core_url), **engine.bereit()}

    @app.get("/v1/models", dependencies=[Depends(geschuetzt)])
    def modelle() -> dict:
        return {"object": "list", "data": [{"id": engine.name, "object": "model", "owned_by": "stimme"}]}

    @app.get("/v1/voices", dependencies=[Depends(geschuetzt)])
    def stimmen_listen() -> dict:
        return {"data": stimmen.alle()}

    @app.post("/v1/voices", status_code=201, dependencies=[Depends(geschuetzt)])
    async def stimme_registrieren(id: str = Form(...), name: str = Form(""), datei: UploadFile = File(...)) -> dict:
        inhalt = await datei.read()
        if len(inhalt) < 1000:
            raise HTTPException(422, "Referenzaufnahme ist leer oder zu kurz")
        pfad = stimmen.speichern(id, name, inhalt, (datei.content_type or "audio/wav").split(";")[0])
        return {"id": id, "name": name, "bytes": len(inhalt), "datei": pfad.name}

    @app.delete("/v1/voices/{stimme_id}", status_code=204, dependencies=[Depends(geschuetzt)])
    def stimme_loeschen(stimme_id: str) -> None:
        stimmen.loeschen(stimme_id)
        return None

    @app.post("/v1/audio/speech", dependencies=[Depends(geschuetzt)])
    def sprechen(daten: Sprechen) -> StreamingResponse:
        referenz = stimmen.pfad(daten.voice) or vom_core_holen(daten.voice)
        if referenz is None and engine.name != "test":
            raise HTTPException(404, f"Stimme {daten.voice!r} ist nicht registriert")
        saetze = engines.saetze(daten.input)
        if not saetze:
            raise HTTPException(422, "Kein Text")
        sprache = (daten.language or "de")[:2].lower()

        def strom() -> Iterator[bytes]:
            if daten.response_format == "wav":
                yield engines.wav_kopf(engine.rate)
            for satz in saetze:
                signal = engine.sprechen(satz, referenz, sprache)
                if daten.speed and daten.speed != 1.0:
                    signal = engines.umtasten(signal, int(engine.rate * daten.speed), engine.rate)
                yield engines.pcm16(signal)

        medientyp = "audio/wav" if daten.response_format == "wav" else "audio/pcm"
        return StreamingResponse(strom(), media_type=medientyp, headers={"X-Sample-Rate": str(engine.rate), "Cache-Control": "no-store"})

    return app
