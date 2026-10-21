"""Verbindungsprüfung für die Telefonie (TELEFONIE.md, Schritt 5):

    python -m voice.probe

Prüft ohne Anruf, ob alles für den ersten echten Anruf bereit ist: Umgebungsvariablen, LiveKit-Server,
SIP-Port, Core-Tool-API, Azure Speech und das Sprachmodell. Jede Zeile endet mit OK oder FEHLT/FEHLER;
der Exit-Code ist 1, sobald eine Pflichtprüfung scheitert. Eignet sich auch als Cron-Überwachung.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request

PFLICHT_VARIABLEN = ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "CORE_API_KEY", "CORE_MANDANT", "BETRIEB_NAME"]


def _zeile(name: str, ok: bool, hinweis: str = "") -> bool:
    print(f"{name:<34} {'OK' if ok else 'FEHLER'}  {hinweis}".rstrip())
    return ok


def _http(url: str, kopf: dict[str, str] | None = None, daten: bytes | None = None, timeout: float = 5.0) -> tuple[int, bytes]:
    anfrage = urllib.request.Request(url, data=daten, headers=kopf or {}, method="POST" if daten else "GET")
    try:
        with urllib.request.urlopen(anfrage, timeout=timeout) as antwort:  # noqa: S310 (eigene URLs)
            return antwort.status, antwort.read()
    except urllib.error.HTTPError as fehler:
        return fehler.code, fehler.read()


def pruefen() -> int:
    ergebnis = True
    for var in PFLICHT_VARIABLEN:
        ergebnis &= _zeile(f"Variable {var}", bool(os.environ.get(var)), "" if os.environ.get(var) else "in .env setzen")

    livekit = os.environ.get("LIVEKIT_URL", "http://127.0.0.1:7880").replace("ws://", "http://").replace("wss://", "https://")
    try:
        status, _ = _http(livekit.rstrip("/") + "/")
        ergebnis &= _zeile("LiveKit-Server", status < 500, f"{livekit} → {status}")
    except Exception as fehler:  # noqa: BLE001
        ergebnis &= _zeile("LiveKit-Server", False, f"{livekit}: {fehler}")

    sip_host = os.environ.get("SIP_HOST", "127.0.0.1")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2.0)
        options = ("OPTIONS sip:probe@%s SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:5099;branch=z9hG4bK-probe\r\n"
                   "From: <sip:probe@localhost>;tag=probe\r\nTo: <sip:probe@%s>\r\nCall-ID: probe@localhost\r\n"
                   "CSeq: 1 OPTIONS\r\nMax-Forwards: 1\r\nContent-Length: 0\r\n\r\n") % (sip_host, sip_host)
        try:
            sock.sendto(options.encode(), (sip_host, 5060))
            antwort, _ = sock.recvfrom(2048)
            ergebnis &= _zeile("SIP-Port 5060/udp (OPTIONS)", antwort.startswith(b"SIP/2.0"), antwort.split(b"\r\n", 1)[0].decode(errors="replace"))
        except (socket.timeout, OSError) as fehler:
            ergebnis &= _zeile("SIP-Port 5060/udp (OPTIONS)", False, f"keine Antwort ({fehler}); läuft livekit-sip mit Profil voice?")

    core = os.environ.get("CORE_URL", "http://127.0.0.1:8100").rstrip("/")
    kopf = {"X-API-Key": os.environ.get("CORE_API_KEY", ""), "X-Mandant": os.environ.get("CORE_MANDANT", ""),
            "Content-Type": "application/json"}
    try:
        status, _ = _http(core + "/health")
        ergebnis &= _zeile("Core /health", status == 200, f"{core} → {status}")
        status, inhalt = _http(core + "/voice/kunde-suchen", kopf, json.dumps({"telefon": "+490000000000"}).encode())
        ergebnis &= _zeile("Core Voice-Tool-API", status == 200, f"→ {status} {inhalt[:60]!r}" if status != 200 else "Schlüssel und Mandant gültig")
    except Exception as fehler:  # noqa: BLE001
        ergebnis &= _zeile("Core", False, str(fehler))

    if os.environ.get("STT_ANBIETER", "azure") == "azure":
        region, schluessel = os.environ.get("AZURE_SPEECH_REGION", "germanywestcentral"), os.environ.get("AZURE_SPEECH_KEY", "")
        if not schluessel:
            ergebnis &= _zeile("Azure Speech", False, "AZURE_SPEECH_KEY fehlt")
        else:
            try:
                status, _ = _http(f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
                                  {"Ocp-Apim-Subscription-Key": schluessel, "Content-Length": "0"}, b"")
                ergebnis &= _zeile("Azure Speech (Token)", status == 200, f"Region {region} → {status}")
            except Exception as fehler:  # noqa: BLE001
                ergebnis &= _zeile("Azure Speech (Token)", False, str(fehler))

    if os.environ.get("LLM_ANBIETER", "anthropic") == "anthropic":
        basis = (os.environ.get("LLM_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        schluessel = os.environ.get("ANTHROPIC_API_KEY", "")
        if not schluessel:
            ergebnis &= _zeile("Sprachmodell", False, "ANTHROPIC_API_KEY fehlt")
        else:
            try:
                status, _ = _http(basis + "/v1/models", {"x-api-key": schluessel, "anthropic-version": "2023-06-01"})
                ergebnis &= _zeile("Sprachmodell (Modelliste)", status == 200, f"{basis} → {status}")
            except Exception as fehler:  # noqa: BLE001
                ergebnis &= _zeile("Sprachmodell (Modelliste)", False, str(fehler))

    print("\nBereit für den Testanruf." if ergebnis else "\nMindestens eine Prüfung ist fehlgeschlagen (TELEFONIE.md).")
    return 0 if ergebnis else 1


if __name__ == "__main__":
    sys.exit(pruefen())
