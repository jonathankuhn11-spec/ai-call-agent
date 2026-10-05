"""
Sprach-Layer: Mikrofon → Spracherkennung → Agent (geführter Modus) → Sprachsynthese → Lautsprecher.
Alles lokal, ohne Account und ohne Cloud:
  STT  faster-whisper (Whisper "small", int8 auf CPU, deutsch)
  TTS  Piper (neuronale Stimme, z. B. de_DE-thorsten-medium), Vorlagensätze werden vorab synthetisiert
       und aus dem Cache abgespielt, sodass nur frei formulierte Sätze Latenz kosten

Einrichtung (einmalig):
  pip install -r requirements-voice.txt
  Piper-Stimme laden (zwei Dateien, ~60 MB) nach voices/:
    https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx
    https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json
  Whisper lädt sein Modell beim ersten Start selbst (~500 MB).

Start:
  python voice.py                 Push-to-talk: Enter drücken, sprechen, Enter drücken
  python voice.py --no-mic        Kunde tippt, Agent spricht (zum Testen der Stimme)
  python voice.py --no-tts        Kunde spricht, Agent schreibt (zum Testen des Mikrofons)
"""
import argparse
import hashlib
import io
import time
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

import agent
import guided

SAMPLE_RATE_IN = 16_000         # Whisper erwartet 16 kHz Mono
CACHE_DIR = Path("voice_cache")
VOICE_PATH = Path("voices/de_DE-thorsten-medium.onnx")
RETRY_PROMPT = "Entschuldigung, ich habe Sie nicht verstanden. Können Sie das bitte wiederholen?"


# ------------------------------------------------------------------ Bausteine (lazy imports: Tests brauchen sie nicht)
class Transcriber:
    """Spracherkennung mit faster-whisper."""

    def __init__(self, model_size: str = "small"):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(audio, language="de", beam_size=1, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()


class Speaker:
    """Sprachsynthese mit Piper; identische Sätze kommen aus dem WAV-Cache."""

    def __init__(self, voice_path: Path = VOICE_PATH, cache_dir: Path = CACHE_DIR):
        from piper import PiperVoice
        if not Path(voice_path).exists():
            raise FileNotFoundError(f"Piper-Stimme fehlt: {voice_path} (siehe Kopf von voice.py)")
        self.voice = PiperVoice.load(str(voice_path))
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

    def _synthesize(self, text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            if hasattr(self.voice, "synthesize_wav"):          # piper-tts >= 1.3
                self.voice.synthesize_wav(text, wav)
            else:                                              # piper-tts 1.2
                self.voice.synthesize(text, wav)
        return buf.getvalue()

    def wav_for(self, text: str) -> bytes:
        path = self.cache_dir / (hashlib.sha1(text.encode("utf-8")).hexdigest() + ".wav")
        if path.exists():
            return path.read_bytes()
        data = self._synthesize(text)
        path.write_bytes(data)
        return data

    def say(self, text: str) -> None:
        import sounddevice as sd
        import soundfile as sf
        audio, rate = sf.read(io.BytesIO(self.wav_for(text)), dtype="float32")
        sd.play(audio, rate)
        sd.wait()

    def warm_up(self, sentences) -> int:
        """Vorlagensätze vorab synthetisieren, damit sie im Gespräch ohne Verzögerung kommen."""
        return sum(1 for s in sentences if self.wav_for(s))


class Recorder:
    """Push-to-talk über die Tastatur: Enter startet, Enter stoppt die Aufnahme."""

    def record(self) -> np.ndarray:
        import sounddevice as sd
        chunks = []
        input("   [Enter] drücken und sprechen ...")
        with sd.InputStream(samplerate=SAMPLE_RATE_IN, channels=1, dtype="float32",
                            callback=lambda data, frames, t, status: chunks.append(data.copy())):
            input("   ... [Enter] zum Beenden")
        return np.concatenate(chunks)[:, 0] if chunks else np.zeros(0, dtype="float32")


def template_sentences() -> list:
    """Alle festen Sätze des geführten Modus; die dynamischen (Termine) kommen live."""
    fixed = [agent.greeting_text(), RETRY_PROMPT, guided.PRICE_LINE, guided.CALLBACK,
             *guided.QUESTIONS.values(), *guided.DISQUALIFY.values(),
             "Verstanden, ich trage Sie aus und wünsche Ihnen einen schönen Tag.",
             "Oh, Entschuldigung für die Störung. Dann versuche ich es ein anderes Mal. Schönen Tag noch!"]
    return list(dict.fromkeys(fixed))


# ------------------------------------------------------------------ Gesprächssteuerung
class VoiceSession:
    """Verbindet Aufnahme, Erkennung, Agent und Ausgabe; misst die Latenz je Schritt."""

    def __init__(self, transcriber=None, speaker=None, recorder=None, typed_input: Optional[Callable] = None,
                 max_retries: int = 2):
        self.transcriber, self.speaker, self.recorder = transcriber, speaker, recorder
        self.typed_input = typed_input                      # Ersatz fürs Mikrofon (--no-mic / Tests)
        self.max_retries = max_retries
        self.spoken = 0
        self.last_spoken = ""
        self.timings = []                                   # je Turn: (stt_s, tts_s)

    def say(self, text: str) -> None:
        if self.speaker:
            self.speaker.say(text)
        else:
            print(f"{agent.AGENT_NAME}: {text}")
        self.last_spoken = text

    def speak_new(self, messages) -> float:
        """Spricht alle noch nicht gesprochenen Agentensätze; Rückgabe: TTS-Sekunden."""
        t0 = time.time()
        for m in messages[self.spoken:]:
            if m["role"] == "assistant":
                self.say(m["content"])
        self.spoken = len(messages)
        return time.time() - t0

    def listen(self) -> tuple:
        """Rückgabe: (Text, STT-Sekunden). Leere Erkennung wird bis zu max_retries Mal wiederholt."""
        for attempt in range(self.max_retries + 1):
            if self.typed_input is not None:
                return self.typed_input(), 0.0
            audio = self.recorder.record()
            t0 = time.time()
            text = self.transcriber.transcribe(audio) if len(audio) else ""
            stt = time.time() - t0
            if text:
                return text, stt
            if attempt < self.max_retries and self.speaker:
                self.speaker.say(RETRY_PROMPT)
        return "", 0.0

    def get_input(self, messages) -> str:
        tts = self.speak_new(messages)
        text, stt = self.listen()
        self.timings.append((stt, tts))
        if text and self.typed_input is None:
            print(f"Kunde (erkannt): {text}")
        return text

    def run(self, con, lead_id: int) -> dict:
        if self.speaker:
            self.speaker.warm_up(template_sentences())
        result = guided.run_guided_call(con, lead_id, get_input=self.get_input)
        transcript = result.get("transkript", [])
        if transcript and transcript[-1]["role"] == "assistant" and transcript[-1]["content"] != self.last_spoken:
            self.say(transcript[-1]["content"])                  # Abschiedssatz nach Gesprächsende
        result["latenz_stt_avg"] = float(np.mean([t[0] for t in self.timings])) if self.timings else 0.0
        result["latenz_tts_avg"] = float(np.mean([t[1] for t in self.timings])) if self.timings else 0.0
        return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sprach-Layer für den geführten Call-Agenten")
    ap.add_argument("--lead", type=int)
    ap.add_argument("--no-mic", action="store_true", help="Kunde tippt statt zu sprechen")
    ap.add_argument("--no-tts", action="store_true", help="Agent schreibt statt zu sprechen")
    ap.add_argument("--whisper", default="small", help="faster-whisper-Modell: tiny, base, small, medium")
    ap.add_argument("--voice", default=str(VOICE_PATH), help="Pfad zur Piper-Stimme (.onnx)")
    ap.add_argument("--model", help="Ollama-Modell für Extraktion und Formulierung")
    a = ap.parse_args(argv)
    if a.model:
        agent.MODEL = a.model
    con = agent.init_db()
    lead_id = a.lead or (con.execute("SELECT id FROM leads WHERE status = 'offen' ORDER BY id LIMIT 1").fetchone()
                         or [None])[0]
    if lead_id is None:
        print("Keine offenen Leads. python agent.py --reset"); return
    session = VoiceSession(
        transcriber=None if a.no_mic else Transcriber(a.whisper),
        speaker=None if a.no_tts else Speaker(Path(a.voice)),
        recorder=None if a.no_mic else Recorder(),
        typed_input=(lambda: input("Kunde: ")) if a.no_mic else None)
    result = session.run(con, lead_id)
    print(f"\nErgebnis: {result['ergebnis']} | Turns: {result['turns']} | "
          f"Ø Spracherkennung {result['latenz_stt_avg']:.1f}s, Ø Sprachausgabe {result['latenz_tts_avg']:.1f}s, "
          f"Ø Agent {result['latenz_avg']:.1f}s je Turn")


if __name__ == "__main__":
    main()
