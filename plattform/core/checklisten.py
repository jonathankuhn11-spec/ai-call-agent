"""Wartungs-Checklisten je Anlagentyp: Prüfpunkte mit Messwertgrenzen, Befundermittlung für den Rapport.

Die Prüfpunkte orientieren sich an den einschlägigen Regelwerken (1. BImSchV/KÜO für Abgas, DVGW W 551/W 557 für
Trinkwasser, VDI 2035 für Heizwasser, DIN EN 378 und ChemKlimaschutzV/F-Gase-Verordnung (EU) 2024/573 für Kälte,
DIN EN 12828 für Sicherheitstechnik). Grenzwerte sind Richtwerte und je Hersteller/Anlage zu prüfen; Pflichtpunkte
der jeweiligen Norm sind mit `norm` gekennzeichnet. Der Rapport speichert die Antworten unter messwerte["checkliste"],
`auswerten` liefert Befunde (ok/auffaellig/offen) daraus.
"""
from __future__ import annotations

from typing import Any

from core.ontologie import ANLAGENTYPEN

# art: ja_nein | messwert | text ; min/max bei messwert; pflicht: ohne Antwort gilt die Checkliste als unvollständig
GEMEINSAM: list[dict[str, Any]] = [
    {"schluessel": "sichtpruefung", "titel": "Sichtprüfung Anlage, Leitungen, Dichtheit", "art": "ja_nein", "pflicht": True},
    {"schluessel": "typenschild", "titel": "Typenschild/Seriennummer dokumentiert", "art": "ja_nein"},
    {"schluessel": "bemerkung", "titel": "Bemerkung", "art": "text"},
]
CHECKLISTEN: dict[str, list[dict[str, Any]]] = {
    "gas_brennwert": [
        {"schluessel": "gasdichtheit", "titel": "Gasdichtheitsprüfung (Leckmengenmessung) bestanden", "art": "ja_nein", "pflicht": True, "norm": "DVGW-TRGI 2018"},
        {"schluessel": "abgas_co", "titel": "CO unverdünnt im Abgas", "art": "messwert", "einheit": "ppm", "max": 400, "pflicht": True, "norm": "1. BImSchV / KÜO"},
        {"schluessel": "abgas_temp", "titel": "Abgastemperatur", "art": "messwert", "einheit": "°C", "max": 110, "norm": "1. BImSchV"},
        {"schluessel": "abgas_o2", "titel": "O₂-Gehalt Abgas", "art": "messwert", "einheit": "%", "min": 2, "max": 7},
        {"schluessel": "abgasverlust", "titel": "Abgasverlust", "art": "messwert", "einheit": "%", "max": 9, "norm": "1. BImSchV (Brennwert: Prüfung Kondensat)"},
        {"schluessel": "brenner_reinigung", "titel": "Brenner und Wärmetauscher gereinigt", "art": "ja_nein", "pflicht": True},
        {"schluessel": "kondensat", "titel": "Kondensatablauf/Siphon geprüft und gereinigt", "art": "ja_nein", "pflicht": True},
        {"schluessel": "anlagendruck", "titel": "Anlagendruck Heizkreis", "art": "messwert", "einheit": "bar", "min": 1.0, "max": 2.5},
        {"schluessel": "mag", "titel": "Vordruck Membran-Ausdehnungsgefäß geprüft", "art": "ja_nein", "pflicht": True, "norm": "DIN EN 12828"},
        {"schluessel": "sicherheitsventil", "titel": "Sicherheitsventil angelüftet, dicht", "art": "ja_nein", "pflicht": True, "norm": "DIN EN 12828"},
        {"schluessel": "zuendung", "titel": "Zündung/Ionisationsstrom geprüft", "art": "ja_nein"},
        {"schluessel": "heizwasser_ph", "titel": "pH-Wert Heizwasser", "art": "messwert", "einheit": "", "min": 8.2, "max": 10.0, "norm": "VDI 2035"},
    ],
    "oel": [
        {"schluessel": "abgas_co", "titel": "CO im Abgas", "art": "messwert", "einheit": "ppm", "max": 400, "pflicht": True, "norm": "1. BImSchV"},
        {"schluessel": "russzahl", "titel": "Rußzahl", "art": "messwert", "einheit": "", "max": 1, "pflicht": True, "norm": "1. BImSchV"},
        {"schluessel": "abgasverlust", "titel": "Abgasverlust", "art": "messwert", "einheit": "%", "max": 9, "pflicht": True, "norm": "1. BImSchV"},
        {"schluessel": "duese", "titel": "Öldüse und Ölfilter gewechselt", "art": "ja_nein", "pflicht": True},
        {"schluessel": "brenner_reinigung", "titel": "Brennraum und Kessel gereinigt", "art": "ja_nein", "pflicht": True},
        {"schluessel": "oelleitung", "titel": "Ölleitung und Tank auf Dichtheit geprüft", "art": "ja_nein", "pflicht": True},
        {"schluessel": "anlagendruck", "titel": "Anlagendruck Heizkreis", "art": "messwert", "einheit": "bar", "min": 1.0, "max": 2.5},
        {"schluessel": "sicherheitsventil", "titel": "Sicherheitsventil geprüft", "art": "ja_nein", "pflicht": True, "norm": "DIN EN 12828"},
    ],
    "waermepumpe": [
        {"schluessel": "kaeltemittel", "titel": "Kältemittelkreis dicht (Dichtheitskontrolle nach F-Gase-VO)", "art": "ja_nein", "pflicht": True, "norm": "VO (EU) 2024/573"},
        {"schluessel": "kaeltemittel_menge", "titel": "Kältemittelfüllmenge laut Typenschild", "art": "messwert", "einheit": "kg"},
        {"schluessel": "verdampfer", "titel": "Verdampfer/Lamellen gereinigt, Luftweg frei", "art": "ja_nein", "pflicht": True},
        {"schluessel": "kondensatablauf", "titel": "Kondensatablauf Außeneinheit frei", "art": "ja_nein"},
        {"schluessel": "vorlauf", "titel": "Vorlauftemperatur bei Prüfbetrieb", "art": "messwert", "einheit": "°C", "max": 55},
        {"schluessel": "spreizung", "titel": "Spreizung Vor-/Rücklauf", "art": "messwert", "einheit": "K", "min": 3, "max": 10},
        {"schluessel": "jaz", "titel": "Jahresarbeitszahl laut Regler (sofern verfügbar)", "art": "messwert", "einheit": "", "min": 2.7},
        {"schluessel": "anlagendruck", "titel": "Anlagendruck Heizkreis", "art": "messwert", "einheit": "bar", "min": 1.0, "max": 2.5},
        {"schluessel": "filter", "titel": "Schmutzfänger/Magnetitabscheider gereinigt", "art": "ja_nein", "pflicht": True},
        {"schluessel": "elektrik", "titel": "Elektrische Anschlüsse und Schutzleiter geprüft", "art": "ja_nein", "pflicht": True, "norm": "DIN VDE 0100-600"},
    ],
    "solarthermie": [
        {"schluessel": "frostschutz", "titel": "Frostschutzgehalt Solarflüssigkeit", "art": "messwert", "einheit": "°C", "max": -20, "pflicht": True},
        {"schluessel": "ph_solar", "titel": "pH-Wert Solarflüssigkeit", "art": "messwert", "einheit": "", "min": 7.0, "pflicht": True},
        {"schluessel": "anlagendruck", "titel": "Anlagendruck Solarkreis (kalt)", "art": "messwert", "einheit": "bar", "min": 1.5, "max": 4.0},
        {"schluessel": "kollektoren", "titel": "Kollektoren, Verglasung, Befestigung geprüft", "art": "ja_nein", "pflicht": True},
        {"schluessel": "pumpe", "titel": "Solarpumpe und Rückschlagventil funktionsfähig", "art": "ja_nein", "pflicht": True},
        {"schluessel": "mag", "titel": "Vordruck Ausdehnungsgefäß geprüft", "art": "ja_nein", "pflicht": True},
    ],
    "klima": [
        {"schluessel": "kaeltemittel", "titel": "Dichtheitskontrolle Kältekreis (F-Gase-VO)", "art": "ja_nein", "pflicht": True, "norm": "VO (EU) 2024/573"},
        {"schluessel": "filter", "titel": "Luftfilter gereinigt/gewechselt", "art": "ja_nein", "pflicht": True},
        {"schluessel": "verdampfer", "titel": "Verdampfer und Verflüssiger gereinigt", "art": "ja_nein", "pflicht": True},
        {"schluessel": "kondensat", "titel": "Kondensatwanne/-pumpe gereinigt, Ablauf frei", "art": "ja_nein", "pflicht": True},
        {"schluessel": "ausblas_temp", "titel": "Ausblastemperatur Kühlbetrieb", "art": "messwert", "einheit": "°C", "max": 16},
        {"schluessel": "hygiene", "titel": "Hygieneinspektion durchgeführt", "art": "ja_nein", "norm": "VDI 6022"},
    ],
    "trinkwasser": [
        {"schluessel": "warmwasser_temp", "titel": "Warmwassertemperatur Speicheraustritt", "art": "messwert", "einheit": "°C", "min": 60, "pflicht": True, "norm": "DVGW W 551"},
        {"schluessel": "zirkulation_temp", "titel": "Zirkulationsrücklauf", "art": "messwert", "einheit": "°C", "min": 55, "norm": "DVGW W 551"},
        {"schluessel": "kaltwasser_temp", "titel": "Kaltwassertemperatur", "art": "messwert", "einheit": "°C", "max": 25, "norm": "DIN EN 806-2"},
        {"schluessel": "legionellen", "titel": "Legionellenprobe entnommen (Pflicht bei Großanlagen alle 3 Jahre)", "art": "ja_nein", "norm": "TrinkwV § 31"},
        {"schluessel": "filter", "titel": "Hauswasserfilter rückgespült/gewechselt", "art": "ja_nein", "pflicht": True, "norm": "DIN EN 806-5"},
        {"schluessel": "sicherungsarmatur", "titel": "Rückflussverhinderer/Systemtrenner geprüft", "art": "ja_nein", "pflicht": True, "norm": "DIN EN 1717"},
        {"schluessel": "druckminderer", "titel": "Druck nach Druckminderer", "art": "messwert", "einheit": "bar", "min": 2, "max": 5},
    ],
    "warmwasser": [
        {"schluessel": "warmwasser_temp", "titel": "Speichertemperatur", "art": "messwert", "einheit": "°C", "min": 60, "pflicht": True, "norm": "DVGW W 551"},
        {"schluessel": "anode", "titel": "Opferanode geprüft/gewechselt", "art": "ja_nein", "pflicht": True},
        {"schluessel": "entkalkung", "titel": "Speicher entkalkt/gereinigt", "art": "ja_nein"},
        {"schluessel": "sicherheitsventil", "titel": "Sicherheitsventil Trinkwasser geprüft", "art": "ja_nein", "pflicht": True, "norm": "DIN 1988-200"},
    ],
    "fahrzeug": [   # Direktannahme / Dialogannahme (Kfz)
        {"schluessel": "kilometerstand", "titel": "Kilometerstand bei Annahme", "art": "messwert", "einheit": "km", "pflicht": True},
        {"schluessel": "warnleuchten", "titel": "Keine Warnleuchten aktiv (Motor, ABS, Airbag)", "art": "ja_nein", "pflicht": True},
        {"schluessel": "profil_va", "titel": "Reifenprofil Vorderachse", "art": "messwert", "einheit": "mm", "min": 1.6, "norm": "§ 36 StVZO (mind. 1,6 mm)"},
        {"schluessel": "profil_ha", "titel": "Reifenprofil Hinterachse", "art": "messwert", "einheit": "mm", "min": 1.6, "norm": "§ 36 StVZO"},
        {"schluessel": "bremsfluessigkeit", "titel": "Bremsflüssigkeit Siedepunkt geprüft", "art": "ja_nein"},
        {"schluessel": "beleuchtung", "titel": "Beleuchtung vollständig funktionsfähig", "art": "ja_nein", "pflicht": True},
        {"schluessel": "wischer", "titel": "Wischerblätter/Scheibenwaschanlage", "art": "ja_nein"},
        {"schluessel": "karosserie", "titel": "Vorschäden an Karosserie dokumentiert (Foto)", "art": "ja_nein", "pflicht": True},
        {"schluessel": "oelstand", "titel": "Ölstand/Flüssigkeitsstände geprüft", "art": "ja_nein"},
        {"schluessel": "hu_plakette", "titel": "HU-Plakette gültig", "art": "ja_nein", "norm": "§ 29 StVZO"},
    ],
    "fahrrad": [
        {"schluessel": "bremsen", "titel": "Bremsen vorn/hinten wirksam", "art": "ja_nein", "pflicht": True, "norm": "§ 65 StVZO"},
        {"schluessel": "beleuchtung", "titel": "Beleuchtung und Reflektoren vollständig", "art": "ja_nein", "pflicht": True, "norm": "§ 67 StVZO"},
        {"schluessel": "kette_verschleiss", "titel": "Kettenverschleiß", "art": "messwert", "einheit": "%", "max": 0.75},
        {"schluessel": "akku_ladezyklen", "titel": "Akku-Ladezyklen (E-Bike)", "art": "messwert", "einheit": "Zyklen"},
        {"schluessel": "speichen", "titel": "Laufräder rund, Speichenspannung geprüft", "art": "ja_nein"},
    ],
    "elektroanlage": [
        {"schluessel": "isolationswiderstand", "titel": "Isolationswiderstand", "art": "messwert", "einheit": "MΩ", "min": 1.0, "pflicht": True, "norm": "DIN VDE 0105-100 / DGUV V3"},
        {"schluessel": "schleifenimpedanz", "titel": "Schleifenimpedanz geprüft (Abschaltbedingung)", "art": "ja_nein", "pflicht": True, "norm": "DIN VDE 0100-600"},
        {"schluessel": "rcd_ausloesezeit", "titel": "RCD-Auslösezeit", "art": "messwert", "einheit": "ms", "max": 300, "pflicht": True, "norm": "DIN VDE 0100-410"},
        {"schluessel": "sichtpruefung", "titel": "Sichtprüfung Verteiler, Leitungen, Steckdosen", "art": "ja_nein", "pflicht": True},
        {"schluessel": "pruefplakette", "titel": "Prüfplakette angebracht", "art": "ja_nein"},
    ],
    "pv": [
        {"schluessel": "isolationswiderstand_dc", "titel": "Isolationswiderstand DC-Seite", "art": "messwert", "einheit": "MΩ", "min": 1.0, "norm": "DIN EN 62446-1"},
        {"schluessel": "leerlaufspannung", "titel": "Leerlaufspannung je String plausibel", "art": "ja_nein", "pflicht": True},
        {"schluessel": "module_sicht", "titel": "Module ohne Schäden/Verschattung", "art": "ja_nein"},
        {"schluessel": "wechselrichter_fehler", "titel": "Wechselrichter ohne Fehlermeldung", "art": "ja_nein", "pflicht": True},
        {"schluessel": "ertrag", "titel": "Ertrag seit letzter Prüfung", "art": "messwert", "einheit": "kWh"},
    ],
    "lueftung": [
        {"schluessel": "filter", "titel": "Filter gewechselt", "art": "ja_nein", "pflicht": True, "norm": "VDI 6022"},
        {"schluessel": "volumenstrom", "titel": "Volumenstrom gemessen", "art": "messwert", "einheit": "m³/h"},
        {"schluessel": "hygiene", "titel": "Hygieneinspektion ohne Befund", "art": "ja_nein", "norm": "VDI 6022 Blatt 1"},
        {"schluessel": "kondensat", "titel": "Kondensatablauf frei", "art": "ja_nein"},
    ],
    "dach": [
        {"schluessel": "eindeckung", "titel": "Eindeckung ohne Bruch/Verschiebung", "art": "ja_nein", "pflicht": True},
        {"schluessel": "rinnen", "titel": "Rinnen und Fallrohre frei", "art": "ja_nein", "pflicht": True},
        {"schluessel": "anschluesse", "titel": "Anschlüsse (Kamin, Gauben, Durchdringungen) dicht", "art": "ja_nein", "pflicht": True},
        {"schluessel": "flachdach_abdichtung", "titel": "Abdichtung/Blasen/Risse (Flachdach)", "art": "ja_nein", "norm": "Flachdachrichtlinie"},
        {"schluessel": "sicherung", "titel": "Anschlagpunkte/Absturzsicherung vorhanden", "art": "ja_nein", "norm": "DGUV Regel 112-198"},
    ],
    "gartenanlage": [
        {"schluessel": "bewaesserung", "titel": "Bewässerung dicht und zeitgesteuert", "art": "ja_nein"},
        {"schluessel": "baeume", "titel": "Bäume verkehrssicher (Totholz, Risse)", "art": "ja_nein", "pflicht": True, "norm": "Verkehrssicherungspflicht § 823 BGB"},
        {"schluessel": "wege", "titel": "Wege/Treppen rutschfest und eben", "art": "ja_nein"},
    ],
    "sanitaer": [
        {"schluessel": "armaturen", "titel": "Armaturen und Abläufe dicht", "art": "ja_nein", "pflicht": True},
        {"schluessel": "silikon", "titel": "Silikonfugen geprüft", "art": "ja_nein"},
        {"schluessel": "spuelkasten", "titel": "Spülkasten/Spülung geprüft", "art": "ja_nein"},
    ],
}


def checkliste(anlagentyp: str) -> dict[str, Any]:
    if anlagentyp not in ANLAGENTYPEN:
        raise ValueError(f"Unbekannter Anlagentyp {anlagentyp!r}; erlaubt: {sorted(ANLAGENTYPEN)}")
    return {"anlagentyp": anlagentyp, "bezeichnung": ANLAGENTYPEN[anlagentyp].bezeichnung,
            "punkte": [*CHECKLISTEN.get(anlagentyp, []), *GEMEINSAM]}


def alle() -> list[dict[str, Any]]:
    return [{"anlagentyp": k, "bezeichnung": v.bezeichnung, "punkte": len(CHECKLISTEN.get(k, [])) + len(GEMEINSAM)} for k, v in ANLAGENTYPEN.items()]


def _zahl(wert: Any) -> float | None:
    try:
        return float(str(wert).replace(",", "."))
    except (TypeError, ValueError):
        return None


def auswerten(anlagentyp: str, antworten: dict[str, Any]) -> dict[str, Any]:
    """Befunde je Prüfpunkt: ok, auffaellig (Grenzwert verletzt bzw. „nein“), offen (Pflichtpunkt ohne Antwort)."""
    befunde: list[dict[str, Any]] = []
    for p in checkliste(anlagentyp)["punkte"]:
        wert = (antworten or {}).get(p["schluessel"])
        eintrag: dict[str, Any] = {"schluessel": p["schluessel"], "titel": p["titel"], "wert": wert, "einheit": p.get("einheit", "")}
        if wert in (None, ""):
            eintrag["befund"] = "offen" if p.get("pflicht") else "ohne"
        elif p["art"] == "ja_nein":
            eintrag["befund"] = "ok" if wert in (True, "ja", "true", 1, "1") else "auffaellig"
        elif p["art"] == "messwert":
            z = _zahl(wert)
            if z is None:
                eintrag["befund"] = "offen"
            elif ("min" in p and z < p["min"]) or ("max" in p and z > p["max"]):
                eintrag["befund"] = "auffaellig"
                eintrag["grenze"] = f"{p.get('min', '')}–{p.get('max', '')} {p.get('einheit', '')}".strip()
            else:
                eintrag["befund"] = "ok"
        else:
            eintrag["befund"] = "ohne"
        if p.get("norm"):
            eintrag["norm"] = p["norm"]
        befunde.append(eintrag)
    auffaellig = [b for b in befunde if b["befund"] == "auffaellig"]
    offen = [b for b in befunde if b["befund"] == "offen"]
    return {"anlagentyp": anlagentyp, "befunde": befunde, "auffaellig": len(auffaellig), "offen": len(offen),
            "vollstaendig": not offen, "gesamt": "auffaellig" if auffaellig else ("unvollstaendig" if offen else "ok"),
            "empfehlung": "; ".join(f"{b['titel']}: {b['wert']} {b['einheit']} (Soll {b.get('grenze', 'ja')})".strip() for b in auffaellig) or None}
