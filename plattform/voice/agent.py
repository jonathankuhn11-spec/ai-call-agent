"""Voice-Agent-Worker auf LiveKit Agents: nimmt Anrufe über LiveKit SIP an und führt das Gespräch.

    python -m voice.agent dev      lokal gegen einen LiveKit-Server (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    python -m voice.agent start    Produktionsbetrieb (im docker-compose: Dienst voice-agent)

Pipeline: SIP-Trunk (deutscher Carrier) → LiveKit SIP → Raum → dieser Worker:
VAD (Silero) → STT → LLM mit Function Calling auf die Core-Tool-API → TTS. Anbieter werden per
Umgebungsvariable gewählt; Standard ist eine Konfiguration mit EU-Verarbeitung (Azure Speech in
einer EU-Region für STT/TTS, Claude über einen EU-Endpunkt). Die Geschäftslogik liegt im Core,
nicht im Prompt: Dringlichkeit, Sicherheitsfälle, Qualifikationen und Freigaben entscheidet der Core.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext, RunContext, WorkerOptions, cli, function_tool
from livekit.plugins import silero

from voice.werkzeuge import KernClient

log = logging.getLogger("voice-agent")
PROMPT = (Path(__file__).resolve().parent / "prompt.md").read_text(encoding="utf-8")
# Erste Äußerung: KI-Offenlegung (Art. 50 KI-Verordnung) und Kurzinformation nach Art. 13 DSGVO,
# Wortlaut abgestimmt mit recht/DATENSCHUTZHINWEIS-TELEFON.md.
BEGRUESSUNG = ("Guten Tag, hier ist der digitale Assistent von {betrieb}, ein KI-System. Ich nehme Ihr Anliegen auf "
               "und schlage Ihnen einen Termin vor. Ihre Angaben werden dafür in Text umgewandelt und gespeichert, "
               "das Gespräch wird nicht aufgezeichnet. Wie kann ich helfen?")


@dataclass
class Gespraech:
    telefon: str | None
    kern: KernClient
    servicefall_id: str | None = None
    notizen: list[str] = field(default_factory=list)


def stt_erzeugen():
    anbieter = os.environ.get("STT_ANBIETER", "azure")
    if anbieter == "azure":
        from livekit.plugins import azure
        return azure.STT(language="de-DE", speech_region=os.environ.get("AZURE_SPEECH_REGION", "germanywestcentral"))
    if anbieter == "deepgram":
        from livekit.plugins import deepgram
        return deepgram.STT(model="nova-3", language="de")
    raise ValueError(f"STT_ANBIETER {anbieter!r} unbekannt (azure, deepgram)")


def tts_erzeugen():
    anbieter = os.environ.get("TTS_ANBIETER", "azure")
    if anbieter == "azure":
        from livekit.plugins import azure
        return azure.TTS(voice=os.environ.get("TTS_STIMME", "de-DE-KatjaNeural"), language="de-DE",
                         speech_region=os.environ.get("AZURE_SPEECH_REGION", "germanywestcentral"))
    if anbieter == "elevenlabs":
        from livekit.plugins import elevenlabs
        return elevenlabs.TTS(voice_id=os.environ["TTS_STIMME"], model="eleven_flash_v2_5", language="de")
    raise ValueError(f"TTS_ANBIETER {anbieter!r} unbekannt (azure, elevenlabs)")


def llm_erzeugen():
    anbieter = os.environ.get("LLM_ANBIETER", "anthropic")
    if anbieter == "anthropic":
        from livekit.plugins import anthropic
        # Für EU-Verarbeitung base_url auf einen EU-Endpunkt bzw. Gateway setzen (LLM_BASE_URL)
        kwargs = {"base_url": os.environ["LLM_BASE_URL"]} if os.environ.get("LLM_BASE_URL") else {}
        return anthropic.LLM(model=os.environ.get("LLM_MODELL", "claude-haiku-5-5"), temperature=0.2, **kwargs)
    if anbieter == "openai":
        from livekit.plugins import openai
        return openai.LLM(model=os.environ.get("LLM_MODELL", "gpt-4.1-mini"), temperature=0.2)
    raise ValueError(f"LLM_ANBIETER {anbieter!r} unbekannt (anthropic, openai)")


class Assistenz(Agent):
    def __init__(self, betrieb: str):
        super().__init__(instructions=PROMPT.format(betrieb=betrieb))

    @function_tool()
    async def kunde_suchen(self, context: RunContext[Gespraech]) -> dict:
        """Sucht den Anrufer anhand seiner Rufnummer im Kundenstamm. Immer zuerst aufrufen."""
        g = context.userdata
        if not g.telefon:
            return {"gefunden": False, "hinweis": "Rufnummer unterdrückt, bitte nach Name und Adresse fragen"}
        return await g.kern.kunde_suchen(g.telefon)

    @function_tool()
    async def zeitfenster_abfragen(self, context: RunContext[Gespraech], anliegen: str,
                                   anlagentyp: str | None = None) -> dict:
        """Prüft Dringlichkeit und liefert freie Termine.

        Args:
            anliegen: Anliegen in den Worten des Anrufers, z. B. "Heizung aus, Fehler F28".
            anlagentyp: einer von gas_brennwert, oel, waermepumpe, solarthermie, klima, trinkwasser,
                warmwasser, sanitaer; weglassen, wenn unklar.
        """
        g = context.userdata
        g.notizen.append(anliegen)
        return await g.kern.zeitfenster(anliegen, anlagentyp)

    @function_tool()
    async def servicefall_anlegen(self, context: RunContext[Gespraech], name: str, anliegen: str,
                                  strasse: str | None = None, plz: str | None = None, ort: str | None = None,
                                  anlagentyp: str | None = None, hersteller: str | None = None,
                                  ressource_id: str | None = None, beginn: str | None = None) -> dict:
        """Legt den Servicefall an und merkt das gewählte Zeitfenster vor.

        Args:
            name: Name des Anrufers bzw. Kunden.
            anliegen: Kurzbeschreibung des Problems.
            strasse: Straße und Hausnummer (bei neuen Kunden Pflicht).
            plz: Postleitzahl.
            ort: Ort.
            anlagentyp: wie bei zeitfenster_abfragen.
            hersteller: Hersteller der Anlage, falls genannt.
            ressource_id: ressource_id des vom Anrufer gewählten Zeitfensters.
            beginn: beginn des gewählten Zeitfensters (ISO-Format, unverändert übernehmen).
        """
        g = context.userdata
        ergebnis = await g.kern.servicefall_anlegen(
            telefon=g.telefon or "unbekannt", name=name, anliegen=anliegen, strasse=strasse, plz=plz, ort=ort,
            anlagentyp=anlagentyp, hersteller=hersteller, ressource_id=ressource_id, beginn=beginn)
        g.servicefall_id = ergebnis["servicefall_id"]
        return ergebnis

    @function_tool()
    async def rueckruf_vormerken(self, context: RunContext[Gespraech], name: str, grund: str) -> dict:
        """Merkt einen Rückruf durch das Büro vor, wenn der Anrufer einen Menschen wünscht.

        Args:
            name: Name des Anrufers.
            grund: Worum es geht, in einem Satz.
        """
        g = context.userdata
        ergebnis = await g.kern.servicefall_anlegen(telefon=g.telefon or "unbekannt", name=name,
                                                    anliegen=f"Rückruf erbeten: {grund}")
        g.servicefall_id = ergebnis["servicefall_id"]
        return {"vorgemerkt": True}


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    teilnehmer = await ctx.wait_for_participant()
    telefon = teilnehmer.attributes.get("sip.phoneNumber") if teilnehmer.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP else None
    betrieb = os.environ.get("BETRIEB_NAME", "Ihrem Fachbetrieb")
    gespraech = Gespraech(telefon=telefon, kern=KernClient())
    session = AgentSession[Gespraech](userdata=gespraech, vad=silero.VAD.load(), stt=stt_erzeugen(),
                                      llm=llm_erzeugen(), tts=tts_erzeugen())

    async def nachbereiten() -> None:
        zeilen = [f"{m.role}: {m.text_content}" for m in session.history.items
                  if getattr(m, "type", "message") == "message" and m.text_content]
        try:
            await gespraech.kern.gespraech_beendet("\n".join(zeilen), telefon=telefon,
                                                   servicefall_id=gespraech.servicefall_id,
                                                   extraktion={"notizen": gespraech.notizen})
        except Exception:  # noqa: BLE001  Nachbereitung darf den Worker nicht stoppen
            log.exception("Gespräch konnte nicht an den Core übergeben werden")
        await gespraech.kern.schliessen()

    ctx.add_shutdown_callback(nachbereiten)
    await session.start(room=ctx.room, agent=Assistenz(betrieb))
    await session.say(BEGRUESSUNG.format(betrieb=betrieb), allow_interruptions=False)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=os.environ.get("AGENT_NAME", "shk-assistenz")))
