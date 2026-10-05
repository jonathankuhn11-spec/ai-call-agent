"""Sprach-Layer: Steuerung mit Attrappen für Mikrofon, Erkennung und Stimme (keine Audio-Hardware nötig)."""
import numpy as np

import agent
import guided
import voice


class FakeSpeaker:
    def __init__(self):
        self.said, self.warmed = [], []

    def say(self, text):
        self.said.append(text)

    def warm_up(self, sentences):
        self.warmed = list(sentences)
        return len(self.warmed)


class FakeRecorder:
    def __init__(self, clips):
        self.clips = list(clips)

    def record(self):
        return self.clips.pop(0) if self.clips else np.zeros(0, dtype="float32")


class FakeTranscriber:
    def __init__(self, texts):
        self.texts = list(texts)

    def transcribe(self, audio):
        return self.texts.pop(0) if self.texts else ""


def test_voice_session_speaks_each_agent_line_once_and_books(con, no_jev, quiet, llm, monkeypatch):
    monkeypatch.setattr(guided, "FORMULATE", False)
    llm([], extractions=[{"eigentuemer": True, "gebaeudetyp": "einfamilienhaus", "dachflaeche_m2": 70,
                          "jahresverbrauch_kwh": 4200}, {"termin_wahl": "erster"}])
    speaker = FakeSpeaker()
    clip = np.zeros(16_000, dtype="float32")
    session = voice.VoiceSession(transcriber=FakeTranscriber(["Ja, mir gehört ein Einfamilienhaus, 70 Quadratmeter, 4200 Kilowattstunden.", "Der erste passt."]),
                                 speaker=speaker, recorder=FakeRecorder([clip, clip]))
    r = session.run(con, 1)
    assert r["ergebnis"] == "termin_gebucht"
    said = [m["content"] for m in r["transkript"] if m["role"] == "assistant"]
    assert speaker.said == said                                  # jeder Satz genau einmal, inklusive Abschied
    assert speaker.said[0] == agent.greeting_text() and "Beratungsgespräch" in speaker.said[1]
    assert all(s in speaker.warmed for s in guided.QUESTIONS.values())
    assert len(session.timings) == 2 and r["latenz_stt_avg"] >= 0


def test_empty_transcription_is_retried_then_handed_to_the_agent(con, no_jev, quiet, llm, monkeypatch):
    monkeypatch.setattr(guided, "FORMULATE", False)
    llm([], extractions=[{}, {"eigentuemer": False}])
    speaker = FakeSpeaker()
    clip = np.zeros(16_000, dtype="float32")
    session = voice.VoiceSession(transcriber=FakeTranscriber(["", "", "Ja, hallo?", "Nein, zur Miete."]),
                                 speaker=speaker, recorder=FakeRecorder([clip] * 4), max_retries=2)
    r = session.run(con, 1)
    assert speaker.said.count(voice.RETRY_PROMPT) == 2
    assert r["ergebnis"] == "disqualifiziert"


def test_typed_input_mode_works_without_microphone(con, no_jev, quiet, llm, monkeypatch):
    monkeypatch.setattr(guided, "FORMULATE", False)
    llm([], extractions=[{"keine_zeit": True}])
    answers = iter(["Bin im Auto, morgen bitte."])
    session = voice.VoiceSession(typed_input=lambda: next(answers))
    r = session.run(con, 1)
    assert r["ergebnis"] == "rueckruf_vereinbart" and session.last_spoken.startswith("Kein Problem")


def test_template_sentences_cover_the_fixed_dialogue():
    sentences = voice.template_sentences()
    assert agent.greeting_text() in sentences and guided.CALLBACK in sentences
    assert len(sentences) == len(set(sentences))
