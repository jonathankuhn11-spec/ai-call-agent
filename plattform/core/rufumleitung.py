"""Rufumleitungsassistent: bedingte Anrufweiterleitung vom bisherigen Anschluss auf den Telefonassistenten.

Adressiert Problemcode P7 („Mobilnummer als Hauptnummer“): Viele Inhaber führen den Betrieb über das eigene Handy; Anrufe
während der Arbeit auf der Baustelle gehen verloren oder landen auf der Mailbox. Statt Rufnummernwechsel richtet der
Inhaber eine *bedingte* Rufumleitung ein (bei Nichtannahme nach n Sekunden, bei Besetzt, bei Nichterreichbarkeit): Er
nimmt weiterhin selbst ab, wenn er kann; alles andere nimmt der Telefonassistent an.

Die Steuercodes sind die netzseitigen GSM-Zusatzdienste nach 3GPP TS 22.030 (MMI-Codes) und TS 22.082 (Call Forwarding):
CFNRy **61*, CFB **67*, CFNRc **62*, alle bedingten **004*, Löschen aller Umleitungen ##002#. Sie funktionieren bei
Telekom, Vodafone und O2/Telefónica sowie deren Discountmarken unabhängig vom Endgerät. Die Mailbox ist netzseitig selbst
eine Rufumleitung und wird durch das Setzen ersetzt. Kosten: Die umgeleitete Verbindung zahlt der Inhaber als abgehendes
Gespräch in das deutsche Netz (in Allnet-Flats üblicherweise enthalten) – im Vertrag prüfen.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import phonenumbers

from core.modelle import Mandant
from core.regeln import telefon_normalisieren

SEKUNDEN_ERLAUBT = (5, 10, 15, 20, 25, 30)   # Netzvorgabe für CFNRy (No Reply Condition Timer, TS 22.082)


def ist_mobil(nummer: str | None) -> bool:
    if not nummer:
        return False
    try:
        n = phonenumbers.parse(nummer, "DE")
    except phonenumbers.NumberParseException:
        return False
    return phonenumbers.number_type(n) in (phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE)


def _code(text: str) -> dict[str, str]:
    """Steuercode samt tel:-Link (# und * müssen in der URI kodiert sein, sonst wählt das Telefon nur bis zum ersten #)."""
    return {"code": text, "tel": "tel:" + quote(text, safe="")}


def anleitung(m: Mandant, sekunden: int = 20, ziel: str | None = None) -> dict[str, Any]:
    st = m.stammdaten or {}
    ziel_e164 = telefon_normalisieren(ziel or st.get("agent_rufnummer"))
    hauptnummer = st.get("telefon")
    if sekunden not in SEKUNDEN_ERLAUBT:
        sekunden = min(SEKUNDEN_ERLAUBT, key=lambda x: abs(x - sekunden))
    aus: dict[str, Any] = {"ziel": ziel_e164, "sekunden": sekunden, "hauptnummer": hauptnummer, "hauptnummer_mobil": ist_mobil(hauptnummer),
                           "eingerichtet": bool(st.get("rufumleitung_eingerichtet")), "codes": None, "hinweise": []}
    if not ziel_e164:
        aus["hinweise"].append("Zielrufnummer des Telefonassistenten fehlt: Nummer aus dem SIP-Trunk (TELEFONIE.md) unter „Rufumleitung“ eintragen.")
        return aus
    z = ziel_e164
    aus["codes"] = {
        "vorher_alle_loeschen": _code("##002#"),
        "nichtannahme": {"aktivieren": _code(f"**61*{z}*11*{sekunden}#"), "deaktivieren": _code("##61#"), "status": _code("*#61#")},
        "besetzt": {"aktivieren": _code(f"**67*{z}#"), "deaktivieren": _code("##67#"), "status": _code("*#67#")},
        "nicht_erreichbar": {"aktivieren": _code(f"**62*{z}#"), "deaktivieren": _code("##62#"), "status": _code("*#62#")},
        "alle_bedingten": {"aktivieren": _code(f"**004*{z}*11*{sekunden}#"), "deaktivieren": _code("##004#")},
    }
    aus["schritte"] = [
        "Am Handy der bisherigen Hauptnummer zuerst ##002# wählen (löscht Mailbox-Umleitung und alte Umleitungen).",
        f"Dann den Code „alle bedingten“ wählen: Nichtannahme nach {sekunden} s, Besetzt und Nichterreichbarkeit gehen an den Telefonassistenten.",
        "Mit *#61# prüfen: Das Telefon zeigt die Zielnummer an. Testanruf von einem zweiten Telefon, nicht abnehmen.",
        "Rückgängig jederzeit mit ##004# (danach ggf. die Mailbox beim Anbieter wieder einschalten).",
    ]
    aus["geraete"] = {
        "iphone": "iPhone: Einstellungen › Telefon › Rufweiterleitung leitet nur alle Anrufe um; für die bedingte Umleitung den Code in der Telefon-App wählen.",
        "android": "Telefon-App › Einstellungen › Anrufe/Zusatzdienste › Rufweiterleitung › „Wenn keine Antwort“, „Wenn besetzt“, „Wenn nicht erreichbar“.",
        "fritzbox": f"Festnetz über FRITZ!Box: Telefonie › Rufbehandlung › Rufumleitung › Neue Rufumleitung › „verzögert“ ({sekunden} s) auf {z}.",
        "festnetz_telekom": f"Telekom-Festnetz (Anrufweiterschaltung): *61*{z.replace('+49', '0')}# nach Zeit, *67*…# bei Besetzt, Löschen mit #61# bzw. #67#.",
    }
    aus["hinweise"] += [
        "Die umgeleitete Verbindung wird dem Anschlussinhaber als Gespräch in das deutsche Netz berechnet; in Allnet-Flatrates meist enthalten.",
        "Der Telefonassistent stellt sich als KI vor (Art. 50 Abs. 1 KI-VO) und übergibt Notfälle an den Bereitschaftsdienst.",
    ]
    if aus["hauptnummer_mobil"]:
        aus["hinweise"].append("Ihre Hauptnummer ist eine Mobilnummer: Eine Ortsnetznummer auf dem Telefonassistenten wirkt seriöser und bleibt bei Handywechsel bestehen; "
                               "die Mobilnummer kann per Umleitung parallel weiterlaufen.")
    return aus


def speichern(m: Mandant, ziel: str | None, eingerichtet: bool | None) -> dict[str, Any]:
    st = dict(m.stammdaten or {})
    if ziel is not None:
        e164 = telefon_normalisieren(ziel)
        if ziel and not e164:
            raise ValueError("Zielrufnummer ungültig")
        st["agent_rufnummer"] = e164
    if eingerichtet is not None:
        st["rufumleitung_eingerichtet"] = bool(eingerichtet)
    m.stammdaten = st
    return anleitung(m)
