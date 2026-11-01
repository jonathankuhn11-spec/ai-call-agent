"""Sprachsynthese-Engines des Stimmdienstes (stimme/server.py).

Eine Engine liefert zu Text und Referenzaufnahme ein Audiosignal (array('f'), mono, `rate` Hz). Es gibt
zwei Engines:

- `ChatterboxEngine`: Chatterbox Multilingual von Resemble AI (MIT-Lizenz, GitHub resemble-ai/chatterbox,
  Paket `chatterbox-tts`). Zero-Shot-Stimmklon aus wenigen Sekunden Referenzaudio, 23 Sprachen inkl.
  Deutsch, jede Ausgabe trägt das unhörbare Perth-Wasserzeichen (maschinenlesbare Kennzeichnung
  synthetischer Sprache nach Art. 50 Abs. 2 KI-VO). Modellgewichte werden beim ersten Start von
  Hugging Face geladen (HF_HOME für Offline-Betrieb vorab füllen).
- `TestEngine`: deterministischer Sinuston ohne Modell für Tests, CI und Inbetriebnahme ohne GPU.

Beide liefern 24 kHz; der Server streamt die Sätze einzeln, damit die erste Silbe früh beim Anrufer ist.
"""
from __future__ import annotations

import logging
import math
import os
import re
import sys
import threading
from array import array
from pathlib import Path
from typing import Protocol

log = logging.getLogger("stimme")
RATE = 24000
SATZENDE = re.compile(r"(?<=[.!?:;])\s+(?=\S)")


def saetze(text: str, max_zeichen: int = 300) -> list[str]:
    """Teilt Text in Sätze; überlange Sätze werden an Kommata umgebrochen, damit die Synthese stückweise läuft."""
    teile: list[str] = []
    for satz in SATZENDE.split(" ".join(text.split())):
        satz = satz.strip()
        while len(satz) > max_zeichen:
            schnitt = satz.rfind(",", 0, max_zeichen)
            if schnitt < max_zeichen // 3:
                schnitt = satz.rfind(" ", 0, max_zeichen)
            if schnitt <= 0:
                schnitt = max_zeichen
            teile.append(satz[:schnitt + 1].strip())
            satz = satz[schnitt + 1:].strip()
        if satz:
            teile.append(satz)
    return teile


class Engine(Protocol):
    name: str
    rate: int

    def sprechen(self, text: str, referenz: Path | None, sprache: str) -> array: ...

    def bereit(self) -> dict: ...


class TestEngine:
    """Sinuston, 50 ms je Zeichen, Tonhöhe aus dem Dateinamen der Referenz abgeleitet (so hört man im Test, welche Stimme spricht)."""
    name = "test"
    rate = RATE

    def sprechen(self, text: str, referenz: Path | None, sprache: str) -> array:
        dauer = max(0.1, 0.05 * len(text))
        frequenz = 220.0 + (sum(referenz.stem.encode()) % 200 if referenz else 0)
        n, rampe = int(dauer * self.rate), self.rate // 50   # 20 ms Ein-/Ausblenden gegen Knacken
        signal = array("f", (0.3 * math.sin(2 * math.pi * frequenz * i / self.rate) for i in range(n)))
        for i in range(min(rampe, n)):
            signal[i] *= i / rampe
            signal[n - 1 - i] *= i / rampe
        return signal

    def bereit(self) -> dict:
        return {"engine": self.name, "geraet": "cpu", "modell": "sinus", "wasserzeichen": False}


class ChatterboxEngine:
    name = "chatterbox"
    rate = RATE

    def __init__(self, geraet: str = "auto", exaggeration: float = 0.4, cfg_weight: float = 0.5, temperatur: float = 0.6):
        self.geraet_wunsch, self.exaggeration, self.cfg_weight, self.temperatur = geraet, exaggeration, cfg_weight, temperatur
        self._modell = None
        self._konditionen: dict[str, object] = {}
        self._sperre = threading.Lock()
        self.geraet = "?"

    def _laden(self):
        if self._modell is not None:
            return self._modell
        import torch  # noqa: PLC0415  (schwere Abhängigkeit erst beim ersten Aufruf)
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # noqa: PLC0415

        geraet = self.geraet_wunsch
        if geraet == "auto":
            geraet = "cuda" if torch.cuda.is_available() else ("mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu")
        log.info("Chatterbox Multilingual wird auf %s geladen", geraet)
        self._modell = ChatterboxMultilingualTTS.from_pretrained(device=geraet)
        self.geraet = geraet
        return self._modell

    def sprechen(self, text: str, referenz: Path | None, sprache: str) -> array:
        if referenz is None:
            raise ValueError("Chatterbox braucht eine Referenzaufnahme (Stimmprofil)")
        with self._sperre:   # ein Modell, eine Synthese zur Zeit (GPU-Speicher)
            modell = self._laden()
            schluessel = f"{referenz}:{referenz.stat().st_mtime_ns}"
            if schluessel not in self._konditionen:
                modell.prepare_conditionals(str(referenz), exaggeration=self.exaggeration)
                self._konditionen = {schluessel: modell.conds}   # nur die zuletzt genutzte Stimme im Speicher halten
            modell.conds = self._konditionen[schluessel]
            wav = modell.generate(text, language_id=sprache, exaggeration=self.exaggeration, cfg_weight=self.cfg_weight,
                                  temperature=self.temperatur)
            signal = array("f", wav.squeeze(0).detach().cpu().float().tolist())
        if modell.sr != self.rate:
            signal = umtasten(signal, modell.sr, self.rate)
        return signal

    def bereit(self) -> dict:
        return {"engine": self.name, "geraet": self.geraet if self._modell is not None else self.geraet_wunsch,
                "modell": "ChatterboxMultilingualTTS", "geladen": self._modell is not None, "wasserzeichen": True}


def umtasten(signal: array, von: int, nach: int) -> array:
    """Lineare Interpolation (reicht für Sprache zwischen 22,05/24/48 kHz; ohne numpy-Abhängigkeit)."""
    if von == nach or len(signal) == 0:
        return signal
    n = int(len(signal) * nach / von)
    aus = array("f", bytes(4 * n))
    for i in range(n):
        pos = i * von / nach
        j = int(pos)
        if j + 1 < len(signal):
            aus[i] = signal[j] + (signal[j + 1] - signal[j]) * (pos - j)
        else:
            aus[i] = signal[-1]
    return aus


def pcm16(signal: array) -> bytes:
    aus = array("h", (int(max(-1.0, min(1.0, x)) * 32767) for x in signal))
    if sys.byteorder == "big":
        aus.byteswap()
    return aus.tobytes()


def wav_kopf(rate: int, datenlaenge: int = 0xFFFFFFFF - 44) -> bytes:
    """RIFF-Kopf, 16 Bit mono; beim Streaming mit unbekannter Länge wird die Maximallänge eingetragen."""
    import struct  # noqa: PLC0415
    return b"RIFF" + struct.pack("<I", datenlaenge + 36) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", datenlaenge)


def engine_aus_umgebung() -> Engine:
    art = os.environ.get("STIMME_ENGINE", "chatterbox")
    if art == "test":
        return TestEngine()
    if art == "chatterbox":
        return ChatterboxEngine(os.environ.get("STIMME_GERAET", "auto"), float(os.environ.get("STIMME_EXAGGERATION", "0.4")),
                                float(os.environ.get("STIMME_CFG", "0.5")), float(os.environ.get("STIMME_TEMPERATUR", "0.6")))
    raise ValueError(f"STIMME_ENGINE {art!r} unbekannt (chatterbox, test)")
