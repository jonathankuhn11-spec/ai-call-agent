"""Voice-Agent-Worker auf LiveKit Agents: nimmt Anrufe über LiveKit SIP an und führt das Gespräch.

    python -m voice.agent dev      lokal gegen einen LiveKit-Server (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    python -m voice.agent start    Produktionsbetrieb (im docker-compose: Dienst voice-agent)

Pipeline: SIP-Trunk (deutscher Carrier) → LiveKit SIP → Raum → dieser Worker:
VAD (Silero) → STT → LLM mit Function Calling auf die Core-Tool-API → TTS. Anbieter werden per
Umgebungsvariable gewählt; Standard ist eine Konfiguration mit EU-Verarbeitung (Azure Speech in
einer EU-Region für STT/TTS, Claude über einen EU-Endpunkt). Die Geschäftslogik liegt im Core,
nicht im Prompt: Dringlichkeit, Sicherheitsfälle, Qualifikationen und Freigaben entscheidet der Core.

Zu Gesprächsbeginn holt der Worker `GET /voice/konfiguration` des Mandanten: Begrüßung mit
KI-Offenlegung, aktives Stimmprofil (geklonte Betreiberstimme über den Stimmdienst, STIMME.md),
Weiterleitungsregel und Minutenkontingent. Der Mandant kommt aus CORE_MANDANT oder aus den
Job-Metadaten der Dispatch-Regel (`{"mandant": "<id>"}`), sodass ein Worker mehrere Betriebe bedient.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    metrics,
    tts,
)
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
    job: JobContext | None = None
    teilnehmer: rtc.RemoteParticipant | None = None
    weiterleitung_erlaubt: bool = False
    weitergeleitet_an: str | None = None


def stt_erzeugen():
    anbieter = os.environ.get("STT_ANBIETER", "azure")
    if anbieter == "azure":
        from livekit.plugins import azure
        return azure.STT(language="de-DE", speech_region=os.environ.get("AZURE_SPEECH_REGION", "germanywestcentral"))
    if anbieter == "deepgram":
        from livekit.plugins import deepgram
        return deepgram.STT(model="nova-3", language="de")
    raise ValueError(f"STT_ANBIETER {anbieter!r} unbekannt (azure, deepgram)")


def eigene_stimme_erzeugen(stimme: dict[str, Any] | None):
    """Geklonte Betreiberstimme über den Stimmdienst (stimme/, OpenAI-kompatibel): nur wenn der Mandant ein aktives
    Profil hat und STIMME_URL gesetzt ist. Liefert None, wenn nichts davon zutrifft."""
    url = (os.environ.get("STIMME_URL") or (stimme or {}).get("dienst_url") or "").rstrip("/")
    if not stimme or stimme.get("anbieter") != "eigen" or not url:
        return None
    from livekit.plugins import openai
    return openai.TTS(model="chatterbox", voice=stimme["id"], base_url=f"{url}/v1", api_key=os.environ.get("STIMME_API_KEY") or "stimme",
                      response_format="pcm")


def tts_erzeugen(stimme: dict[str, Any] | None = None):
    """Sprachausgabe. Mit aktivem Stimmprofil spricht der Agent über den Stimmdienst mit der geklonten Stimme des
    Inhabers; fällt der Dienst aus, übernimmt die Standardstimme (FallbackAdapter), damit kein Anruf in Stille endet.
    Anbieter der Standardstimme (TTS_ANBIETER, siehe STIMME.md für extern geklonte Stimmen):
    azure       Standardstimme (de-DE-KatjaNeural) oder Custom Neural Voice (TTS_STIMME = Stimmname, AZURE_TTS_ENDPOINT_ID = Deployment)
    elevenlabs  TTS_STIMME = voice_id des Klons (Instant/Professional Voice Clone)
    cartesia    TTS_STIMME = voice id des Klons (Sonic)"""
    standard = standard_tts_erzeugen()
    eigene = eigene_stimme_erzeugen(stimme)
    if eigene is None:
        return standard
    return tts.FallbackAdapter([eigene, standard])


def standard_tts_erzeugen():
    anbieter = os.environ.get("TTS_ANBIETER", "azure")
    stimme = os.environ.get("TTS_STIMME")
    if anbieter == "azure":
        from livekit.plugins import azure
        zusatz = {"endpoint_id": os.environ["AZURE_TTS_ENDPOINT_ID"]} if os.environ.get("AZURE_TTS_ENDPOINT_ID") else {}
        return azure.TTS(voice=stimme or "de-DE-KatjaNeural", language="de-DE",
                         speech_region=os.environ.get("AZURE_SPEECH_REGION", "germanywestcentral"), **zusatz)
    if anbieter == "elevenlabs":
        from livekit.plugins import elevenlabs
        return elevenlabs.TTS(voice_id=os.environ["TTS_STIMME"], model=os.environ.get("TTS_MODELL", "eleven_flash_v2_5"), language="de")
    if anbieter == "cartesia":
        from livekit.plugins import cartesia
        return cartesia.TTS(voice=os.environ["TTS_STIMME"], model=os.environ.get("TTS_MODELL", "sonic-2"), language="de")
    raise ValueError(f"TTS_ANBIETER {anbieter!r} unbekannt (azure, elevenlabs, cartesia)")


def turn_handling() -> dict[str, Any]:
    """Gesprächswechsel: TURN_ERKENNUNG=modell nutzt das lokale End-of-Turn-Modell von LiveKit (v1-mini, CPU,
    Deutsch; vorher `python -m livekit.agents download-files`), sonst VAD-Stille. Kürzere Wartezeit nach dem Satzende
    und vorgezogene Antwortgenerierung senken die Latenz bis zur ersten Silbe."""
    erkennung: Any = "vad"
    if os.environ.get("TURN_ERKENNUNG", "vad") == "modell":
        from livekit.agents.inference import TurnDetector
        erkennung = TurnDetector(version="v1-mini")
    return {"turn_detection": erkennung,
            "endpointing": {"min_delay": float(os.environ.get("ENDPOINTING_MIN", "0.4")), "max_delay": float(os.environ.get("ENDPOINTING_MAX", "2.5"))},
            "preemptive_generation": {"enabled": os.environ.get("VORGEZOGENE_ANTWORT", "1") == "1"}}


class Kennzahlen:
    """Sammelt die Latenzen des Gesprächs (Sekunden) für den Core: Zeit bis zum ersten Token (LLM), bis zum ersten
    Audio (TTS), Ende-der-Äußerung bis Antwortbeginn. Mittelwerte je Gespräch, keine Inhalte."""

    def __init__(self) -> None:
        self.werte: dict[str, list[float]] = {}

    def aufnehmen(self, m: metrics.AgentMetrics) -> None:
        if isinstance(m, metrics.LLMMetrics):
            self.werte.setdefault("llm_erstes_token", []).append(m.ttft)
        elif isinstance(m, metrics.TTSMetrics):
            self.werte.setdefault("tts_erstes_audio", []).append(m.ttfb)
            self.werte.setdefault("tts_audio_sekunden", []).append(m.audio_duration)
        elif isinstance(m, metrics.EOUMetrics):
            self.werte.setdefault("ende_der_aeusserung", []).append(m.end_of_utterance_delay)
            self.werte.setdefault("transkription", []).append(m.transcription_delay)

    def zusammenfassung(self) -> dict[str, float]:
        aus = {}
        for k, v in self.werte.items():
            gueltig = [x for x in v if x is not None and x >= 0]
            if gueltig:
                aus[k] = round(sum(gueltig) / len(gueltig), 3) if k != "tts_audio_sekunden" else round(sum(gueltig), 1)
        return aus


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
    def __init__(self, betrieb: str, weiterleitung: bool = False):
        hinweis = ("Der Betrieb erlaubt die Weiterleitung an die Bereitschaft: Bei echten Notfällen außerhalb der Bürozeit "
                   "`weiterleiten` aufrufen, nachdem der Servicefall angelegt ist." if weiterleitung else
                   "Eine Weiterleitung an die Bereitschaft ist nicht freigeschaltet; nenne die Rufnummer aus `notdienst_erfragen`.")
        super().__init__(instructions=PROMPT.format(betrieb=betrieb) + "\n\n" + hinweis)

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

    @function_tool()
    async def notdienst_erfragen(self, context: RunContext[Gespraech]) -> dict:
        """Liefert die Rufnummer der aktuellen Bereitschaft. Nur bei echten Notfällen außerhalb der
        Bürozeit aufrufen (Wasserrohrbruch, Heizungsausfall im Winter, Gasgeruch wird NICHT hierüber
        gelöst, sondern über den Sicherheitsfall)."""
        return await context.userdata.kern.notdienst()

    @function_tool()
    async def weiterleiten(self, context: RunContext[Gespraech]) -> dict:
        """Verbindet den Anrufer direkt mit der Bereitschaft (SIP-Weiterleitung). Nur bei echten Notfällen außerhalb
        der Bürozeit und nur, wenn der Betrieb das freigeschaltet hat; vorher den Servicefall anlegen und dem Anrufer
        sagen, dass er verbunden wird."""
        g = context.userdata
        if not g.weiterleitung_erlaubt:
            return {"moeglich": False, "hinweis": "Weiterleitung nicht freigeschaltet; Rufnummer der Bereitschaft nennen"}
        bereitschaft = await g.kern.notdienst()
        if not bereitschaft.get("bereitschaft") or not bereitschaft.get("telefon"):
            return {"moeglich": False, "hinweis": bereitschaft.get("hinweis", "keine Bereitschaft eingetragen")}
        if g.job is None or g.teilnehmer is None or g.teilnehmer.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return {"moeglich": False, "hinweis": "kein Telefonteilnehmer (Testumgebung)"}
        try:
            await g.job.transfer_sip_participant(g.teilnehmer, f"tel:{bereitschaft['telefon']}", play_dialtone=True)
        except Exception as e:  # noqa: BLE001  Weiterleitung scheitert z. B. ohne REFER-Unterstützung des Trunks
            log.warning("SIP-Weiterleitung fehlgeschlagen: %s", e)
            return {"moeglich": False, "hinweis": f"Weiterleitung fehlgeschlagen; Rufnummer nennen: {bereitschaft['telefon']}"}
        g.weitergeleitet_an = bereitschaft["telefon"]
        return {"moeglich": True, "verbunden_mit": bereitschaft.get("name")}


def mandant_aus_job(ctx: JobContext) -> str | None:
    """Mandant aus den Job-Metadaten der Dispatch-Regel ({"mandant": "<id>"}), sonst CORE_MANDANT."""
    try:
        daten = json.loads(ctx.job.metadata or "{}")
    except (ValueError, AttributeError):
        daten = {}
    return daten.get("mandant") or os.environ.get("CORE_MANDANT")


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    teilnehmer = await ctx.wait_for_participant()
    telefon = teilnehmer.attributes.get("sip.phoneNumber") if teilnehmer.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP else None
    kern = KernClient(mandant=mandant_aus_job(ctx))
    try:
        konfig = await kern.konfiguration()
    except Exception as e:  # noqa: BLE001  Core nicht erreichbar: mit Umgebungswerten weiterarbeiten statt den Anruf zu verlieren
        log.warning("Konfiguration des Mandanten nicht abrufbar (%s); Standardwerte", e)
        konfig = {}
    betrieb = konfig.get("betrieb") or os.environ.get("BETRIEB_NAME", "Ihrem Fachbetrieb")
    stimme = konfig.get("stimme")
    if konfig.get("sprachminuten", {}).get("erschoepft"):
        log.warning("Sprachminuten-Kontingent des Mandanten erschöpft (%s); Anruf wird dennoch angenommen", konfig["sprachminuten"])
    gespraech = Gespraech(telefon=telefon, kern=kern, job=ctx, teilnehmer=teilnehmer,
                          weiterleitung_erlaubt=bool(konfig.get("weiterleitung", {}).get("erlaubt")))
    session = AgentSession[Gespraech](userdata=gespraech, vad=silero.VAD.load(), stt=stt_erzeugen(),
                                      llm=llm_erzeugen(), tts=tts_erzeugen(stimme), turn_handling=turn_handling())
    kennzahlen, beginn = Kennzahlen(), time.monotonic()

    @session.on("metrics_collected")
    def _metriken(ev: metrics.MetricsCollectedEvent) -> None:
        kennzahlen.aufnehmen(ev.metrics)

    async def nachbereiten() -> None:
        zeilen = [f"{m.role}: {m.text_content}" for m in session.history.items
                  if getattr(m, "type", "message") == "message" and m.text_content]
        extraktion: dict[str, Any] = {"notizen": gespraech.notizen}
        if gespraech.weitergeleitet_an:
            extraktion["weitergeleitet_an"] = gespraech.weitergeleitet_an
        try:
            await gespraech.kern.gespraech_beendet("\n".join(zeilen), telefon=telefon,
                                                   servicefall_id=gespraech.servicefall_id, extraktion=extraktion,
                                                   dauer_sekunden=round(time.monotonic() - beginn, 1),
                                                   kennzahlen=kennzahlen.zusammenfassung(),
                                                   stimme_id=stimme["id"] if stimme else None)
        except Exception:  # noqa: BLE001  Nachbereitung darf den Worker nicht stoppen
            log.exception("Gespräch konnte nicht an den Core übergeben werden")
        await gespraech.kern.schliessen()

    ctx.add_shutdown_callback(nachbereiten)
    await session.start(room=ctx.room, agent=Assistenz(betrieb, gespraech.weiterleitung_erlaubt))
    await session.say(konfig.get("begruessung") or BEGRUESSUNG.format(betrieb=betrieb), allow_interruptions=False)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=os.environ.get("AGENT_NAME", "shk-assistenz")))
