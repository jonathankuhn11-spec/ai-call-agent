"""Branchenprofile: dieselbe Plattform für SHK, Elektro, Friseur, Kosmetik, Nagelstudio, Massage, Physiotherapie,
Fußpflege, Hundesalon, Fahrrad- und Kfz-Werkstatt.

Ein Profil legt fest: Begriffe der Oberfläche und des Sprachagenten (Monteur/Stylist/Therapeut, Einsatz/Behandlung),
den Leistungskatalog (Artikel der Kategorie „leistung“ mit Dauer und Qualifikation; Grundlage der Online-Buchung und
der Terminlänge), Felder der Kundenkartei (sensible Felder = Gesundheitsdaten nach Art. 9 DSGVO, nur mit
ausdrücklicher Einwilligung `gesundheitsdaten`), das Wiederkehr-Intervall (Erinnerung „Zeit für den nächsten Termin“
als Freigabe B) und die Hinweise des Sprachagenten. Der Branchenschlüssel steht in `Mandant.gewerk`.

Nicht abgedeckt und bewusst dokumentiert: Kassenpflicht (KassenSichV/TSE bei Bargeld), Heilmittelabrechnung
(Physiotherapie: Rezept, GKV-Abrechnung nach § 302 SGB V) und Terminvergabe mit Vorkasse; dafür bleiben die
bestehenden Werkzeuge (Kasse, Abrechnungsstelle) im Einsatz.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene
from core.modelle import Artikel, Ereignis, Kunde, Mandant, Servicefall, Termin

SHK_BEGRIFFE = {"ressource": "Monteur", "ressourcen": "Monteure", "einsatz": "Einsatz", "einsaetze": "Einsätze", "vorgang": "Vorgang",
                "vorgaenge": "Vorgänge", "kartei": "Kunden & Anlagen", "leistung": "Leistung", "anlagen": True, "mein_einsatz": "Mein Einsatz",
                "wartung": "Wartungsplanung", "rapport": "Rapport"}
SALON_BEGRIFFE = {**SHK_BEGRIFFE, "ressource": "Mitarbeiter/in", "ressourcen": "Team", "einsatz": "Behandlung", "einsaetze": "Behandlungen",
                  "vorgang": "Termin", "vorgaenge": "Termine", "kartei": "Kundenkartei", "anlagen": False, "mein_einsatz": "Mein Tag",
                  "wartung": "Wiederkehr", "rapport": "Behandlungsnotiz"}
WERKSTATT_BEGRIFFE = {**SHK_BEGRIFFE, "ressource": "Mechaniker/in", "ressourcen": "Werkstatt", "einsatz": "Auftrag", "einsaetze": "Aufträge",
                      "vorgang": "Auftrag", "vorgaenge": "Aufträge", "kartei": "Kunden & Fahrzeuge", "anlagen": True, "mein_einsatz": "Meine Aufträge",
                      "wartung": "Inspektionsplanung"}


@dataclass(frozen=True)
class Leistung:
    nummer: str
    bezeichnung: str
    dauer_minuten: int
    preis_brutto: str           # Endkundenpreis inkl. USt (Dienstleistung an Verbraucher)
    qualifikation: str | None = None
    ust_satz: str = "19"


@dataclass(frozen=True)
class KarteiFeld:
    schluessel: str
    titel: str
    art: str = "text"           # text | liste | ja_nein | datum
    sensibel: bool = False      # Gesundheitsdatum (Art. 9 DSGVO): nur mit Einwilligung gesundheitsdaten
    hinweis: str | None = None


@dataclass(frozen=True)
class Branche:
    schluessel: str
    bezeichnung: str
    begriffe: dict[str, Any]
    leistungen: list[Leistung]
    kartei: list[KarteiFeld]
    wiederkehr_tage: int | None          # None: keine Wiederkehr-Erinnerung (SHK nutzt die Wartungsplanung)
    online_buchung: bool
    puffer_minuten: int                   # Umrüst-/Nachbereitungszeit zwischen Terminen
    raster_minuten: int
    gesundheitsdaten: bool                # Kartei enthält Gesundheitsdaten → Einwilligung und Löschkonzept beachten
    sprachagent: str                      # Rolle und Ablaufhinweise für den Telefonassistenten
    hinweise_kunde: list[str] = field(default_factory=list)   # erscheinen in Buchung und Terminbestätigung
    rechtliches: list[str] = field(default_factory=list)      # Hinweise für den Betrieb (Einstellungen)


def _l(nummer: str, bez: str, dauer: int, preis: str, quali: str | None = None) -> Leistung:
    return Leistung(nummer, bez, dauer, preis, quali)


BRANCHEN: dict[str, Branche] = {
    "shk": Branche("shk", "Sanitär, Heizung, Klima", SHK_BEGRIFFE,
                   [_l("L-100", "Wartung Gas-Brennwertgerät", 90, "189.00", "gas"), _l("L-110", "Wartung Wärmepumpe", 90, "219.00", "kaeltetechnik"),
                    _l("L-120", "Wartung Ölheizung", 120, "229.00", "oel"), _l("L-130", "Trinkwasser-Check (Legionellen, Filter)", 60, "149.00", "trinkwasser"),
                    _l("L-140", "Kundendienst Störung", 90, "129.00", None), _l("L-150", "Hydraulischer Abgleich (Beratung vor Ort)", 60, "99.00", None)],
                   [KarteiFeld("zugang", "Zugang/Schlüssel/Klingel"), KarteiFeld("hinweise", "Besonderheiten vor Ort (Hund, Parken, Keller)")],
                   None, True, 30, 30, False,
                   "Du bist die telefonische Assistenz eines SHK-Fachbetriebs. Erfrage Anlage, Symptom und Fehlermeldung; Sicherheitsfälle (Gasgeruch) haben Vorrang vor jedem Termin.",
                   ["Bitte halten Sie den Zugang zur Anlage frei."],
                   ["Gasgeruch bleibt Sicherheitsfall mit Notfallansage (regeln.py)."]),
    "elektro": Branche("elektro", "Elektrotechnik", {**SHK_BEGRIFFE, "wartung": "Prüfplanung (DGUV V3, E-Check)"},
                       [_l("L-200", "E-Check Wohnung", 90, "169.00", "elektro"), _l("L-210", "Wallbox-Installation (Beratung)", 60, "0.00", "elektro"),
                        _l("L-220", "Störungsdienst Elektro", 60, "109.00", "elektro"), _l("L-230", "Prüfung ortsveränderlicher Geräte (DGUV V3)", 120, "249.00", "elektro")],
                       [KarteiFeld("zugang", "Zugang/Zählerschrank"), KarteiFeld("hinweise", "Besonderheiten vor Ort")],
                       None, True, 30, 30, False,
                       "Du bist die telefonische Assistenz eines Elektrofachbetriebs. Bei Brandgeruch, Funkenflug oder Stromschlag: Notruf 112 empfehlen, Sicherung aus, kein Termin.",
                       ["Bitte halten Sie den Zugang zum Zählerschrank frei."], []),
    "friseur": Branche("friseur", "Friseur und Barbier", SALON_BEGRIFFE,
                       [_l("L-300", "Schneiden Damen (waschen, schneiden, föhnen)", 60, "59.00"), _l("L-310", "Schneiden Herren", 30, "32.00"),
                        _l("L-320", "Kinderhaarschnitt (bis 12 Jahre)", 30, "22.00"), _l("L-330", "Färben Ansatz", 90, "69.00", "farbe"),
                        _l("L-340", "Strähnen/Balayage", 150, "149.00", "farbe"), _l("L-350", "Dauerwelle", 120, "89.00", "farbe"),
                        _l("L-360", "Bart trimmen und konturieren", 20, "18.00"), _l("L-370", "Hochsteckfrisur", 60, "79.00"), _l("L-380", "Keratin-Glättung", 180, "199.00", "farbe")],
                       [KarteiFeld("rezeptur", "Farbrezeptur (Marke, Nuancen, Mischung, Einwirkzeit)"), KarteiFeld("haar", "Haarstruktur/Zustand"),
                        KarteiFeld("vorlieben", "Vorlieben (Schnitt, Styling, Produkte)"), KarteiFeld("stammstylist", "Stammstylist/in"),
                        KarteiFeld("allergien", "Allergien/Unverträglichkeiten (z. B. PPD)", "text", True, "Allergietest 48 h vor Färbung dokumentieren"),
                        KarteiFeld("allergietest", "Letzter Allergietest", "datum", True)],
                       42, True, 10, 15, True,
                       "Du bist die telefonische Assistenz eines Friseursalons. Erfrage die gewünschte Leistung (Schneiden, Färben, Bart …) und ob ein bestimmter Stylist gewünscht ist; biete passende Zeiten an. Keine Preisauskünfte außer den Listenpreisen aus dem Leistungskatalog.",
                       ["Bitte kommen Sie mit gewaschenem Haar, wenn nur geschnitten wird.", "Bei Farbbehandlung: Allergietest 48 Stunden vorher."],
                       ["Kundenkartei mit Allergien = Gesundheitsdaten (Art. 9 DSGVO): Einwilligung „gesundheitsdaten“ einholen.",
                        "Bargeld: Kassensystem mit TSE (KassenSichV) weiterhin nötig; die Plattform stellt Rechnungen, keine Kassenbons."]),
    "kosmetik": Branche("kosmetik", "Kosmetikstudio", SALON_BEGRIFFE,
                        [_l("L-400", "Klassische Gesichtsbehandlung", 60, "69.00"), _l("L-410", "Microdermabrasion", 45, "79.00", "apparativ"),
                         _l("L-420", "Wimpernlifting", 60, "59.00"), _l("L-430", "Augenbrauen zupfen und färben", 30, "25.00"),
                         _l("L-440", "Waxing Beine komplett", 45, "49.00"), _l("L-450", "Make-up (Anlass)", 45, "55.00"), _l("L-460", "Microneedling", 60, "129.00", "apparativ")],
                        [KarteiFeld("hauttyp", "Hauttyp/Hautzustand"), KarteiFeld("produkte", "Verwendete Produkte/Pflegeplan"), KarteiFeld("vorlieben", "Vorlieben"),
                         KarteiFeld("allergien", "Allergien/Unverträglichkeiten", "text", True), KarteiFeld("kontraindikationen", "Kontraindikationen (Schwangerschaft, Medikamente, Hauterkrankungen)", "text", True),
                         KarteiFeld("einverstaendnis_behandlung", "Behandlungseinwilligung unterschrieben", "ja_nein", False)],
                        28, True, 15, 15, True,
                        "Du bist die telefonische Assistenz eines Kosmetikstudios. Erfrage die gewünschte Behandlung und biete Zeiten an. Frage nicht nach Gesundheitsdaten; das klärt das Studio vor Ort.",
                        ["Bitte kommen Sie ohne Make-up zur Gesichtsbehandlung."],
                        ["Kartei mit Kontraindikationen = Gesundheitsdaten: Einwilligung „gesundheitsdaten“ nötig.", "Apparative Kosmetik: NiSV-Fachkunde beachten."]),
    "nagelstudio": Branche("nagelstudio", "Nagelstudio", SALON_BEGRIFFE,
                           [_l("L-500", "Maniküre klassisch", 45, "35.00"), _l("L-510", "Gel-Modellage Neuanlage", 120, "69.00", "modellage"),
                            _l("L-520", "Auffüllen Gel", 90, "49.00", "modellage"), _l("L-530", "Shellac/Gellack", 60, "39.00"), _l("L-540", "Pediküre kosmetisch", 60, "45.00"),
                            _l("L-550", "Nail-Art (je Nagel)", 10, "3.00")],
                           [KarteiFeld("form_laenge", "Nagelform/Länge"), KarteiFeld("farben", "Lieblingsfarben/Designs"), KarteiFeld("produkte", "Produktsystem"),
                            KarteiFeld("allergien", "Allergien (Acrylate, HEMA)", "text", True)],
                           21, True, 10, 15, True,
                           "Du bist die telefonische Assistenz eines Nagelstudios. Erfrage Leistung (Neuanlage, Auffüllen, Maniküre, Pediküre) und biete Zeiten an.",
                           [], ["Allergien in der Kartei = Gesundheitsdaten: Einwilligung nötig."]),
    "massage": Branche("massage", "Massage und Wellness", {**SALON_BEGRIFFE, "ressource": "Therapeut/in", "ressourcen": "Therapeuten"},
                       [_l("L-600", "Klassische Massage 30 min", 30, "39.00"), _l("L-610", "Klassische Massage 60 min", 60, "69.00"),
                        _l("L-620", "Hot-Stone-Massage", 75, "89.00", "hotstone"), _l("L-630", "Thai-Massage", 90, "95.00", "thai"),
                        _l("L-640", "Lymphdrainage (Wellness)", 45, "55.00"), _l("L-650", "Paarmassage (2 Personen)", 60, "139.00")],
                       [KarteiFeld("druck", "Bevorzugter Druck/Schwerpunkte"), KarteiFeld("oel", "Öl/Duft-Vorlieben"), KarteiFeld("stammtherapeut", "Stammtherapeut/in"),
                        KarteiFeld("kontraindikationen", "Kontraindikationen (Thrombose, Schwangerschaft, Entzündungen, OPs)", "text", True),
                        KarteiFeld("beschwerden", "Beschwerden/Schmerzbereiche", "text", True), KarteiFeld("anamnese_datum", "Letzte Anamnese", "datum", True)],
                       21, True, 15, 15, True,
                       "Du bist die telefonische Assistenz eines Massagestudios. Erfrage Massageart und Dauer und biete Zeiten an. Frage nicht nach Beschwerden oder Krankheiten; die Anamnese macht der Therapeut vor Ort. Keine Heilversprechen.",
                       ["Bitte 10 Minuten vor dem Termin da sein.", "Nicht direkt nach einer schweren Mahlzeit."],
                       ["Anamnese/Kontraindikationen = Gesundheitsdaten (Art. 9 DSGVO): Einwilligung und kurze Löschfristen.",
                        "Werbung: keine Heilversprechen (HWG), Wellness-Massage ist keine Heilbehandlung."]),
    "physiotherapie": Branche("physiotherapie", "Physiotherapie", {**SALON_BEGRIFFE, "ressource": "Therapeut/in", "ressourcen": "Therapeuten"},
                              [_l("L-700", "Krankengymnastik (KG)", 20, "0.00", "kg"), _l("L-710", "Manuelle Therapie (MT)", 20, "0.00", "mt"),
                               _l("L-720", "Manuelle Lymphdrainage 45 min", 45, "0.00", "mld"), _l("L-730", "Klassische Massagetherapie (KMT)", 20, "0.00"),
                               _l("L-740", "Privatbehandlung 30 min", 30, "59.00"), _l("L-750", "Fango/Wärmetherapie", 20, "0.00")],
                              [KarteiFeld("rezept", "Verordnung (Arzt, Datum, Diagnosegruppe, Anzahl)", "text", True), KarteiFeld("diagnose", "Diagnose/ICD", "text", True),
                               KarteiFeld("befund", "Befund/Behandlungsplan", "text", True), KarteiFeld("kasse", "Krankenkasse/Versichertennummer", "text", True),
                               KarteiFeld("zuzahlung", "Zuzahlungsbefreiung", "ja_nein", True)],
                              None, True, 5, 20, True,
                              "Du bist die telefonische Assistenz einer Physiotherapiepraxis. Erfrage, ob eine ärztliche Verordnung vorliegt und welche Therapie darauf steht; biete Zeiten an. Keine medizinischen Auskünfte.",
                              ["Bitte Verordnung und Versichertenkarte mitbringen.", "Rezepte müssen innerhalb von 28 Tagen begonnen werden."],
                              ["Heilmittelabrechnung (§ 302 SGB V) läuft über Abrechnungsstelle/Praxissoftware; hier nur Terminvergabe und Privatrechnungen.",
                               "Kartei = Gesundheitsdaten: Einwilligung, Schweigepflicht (§ 203 StGB), Aufbewahrung 10 Jahre."]),
    "fusspflege": Branche("fusspflege", "Fußpflege und Podologie", SALON_BEGRIFFE,
                          [_l("L-800", "Kosmetische Fußpflege", 45, "39.00"), _l("L-810", "Podologische Behandlung", 45, "49.00", "podologie"),
                           _l("L-820", "Nagelkorrekturspange", 30, "59.00", "podologie"), _l("L-830", "Hausbesuch Fußpflege", 60, "59.00")],
                          [KarteiFeld("befund", "Befund (Hornhaut, Nägel, Druckstellen)", "text", True), KarteiFeld("diabetes", "Diabetes/Durchblutungsstörung", "ja_nein", True),
                           KarteiFeld("medikamente", "Blutverdünner/Medikamente", "text", True), KarteiFeld("vorlieben", "Vorlieben")],
                          35, True, 10, 15, True,
                          "Du bist die telefonische Assistenz einer Fußpflegepraxis. Erfrage kosmetische oder podologische Behandlung und ob ein Hausbesuch gewünscht ist.",
                          [], ["Podologie mit Rezept: Heilmittelabrechnung extern.", "Befunde = Gesundheitsdaten: Einwilligung nötig."]),
    "hundesalon": Branche("hundesalon", "Hundesalon", {**SALON_BEGRIFFE, "kartei": "Kunden & Tiere"},
                          [_l("L-900", "Baden und Föhnen (klein)", 60, "45.00"), _l("L-910", "Komplettpflege mittel (Schneiden, Krallen, Ohren)", 120, "79.00"),
                           _l("L-920", "Trimmen Rauhaar", 120, "89.00", "trimmen"), _l("L-930", "Krallen schneiden", 15, "12.00"), _l("L-940", "Entfilzung je 15 min", 15, "15.00")],
                          [KarteiFeld("tier", "Tier (Name, Rasse, Alter, Gewicht)"), KarteiFeld("fell", "Fellzustand/Schnittwunsch"), KarteiFeld("verhalten", "Verhalten (ängstlich, Maulkorb, Allergien)"),
                           KarteiFeld("tierarzt", "Tierarzt/Impfstatus")],
                          56, True, 15, 15, False,
                          "Du bist die telefonische Assistenz eines Hundesalons. Erfrage Rasse, Größe und gewünschte Pflege und biete Zeiten an.",
                          ["Bitte Hund vor dem Termin ausführen.", "Impfpass mitbringen."], []),
    "fahrrad": Branche("fahrrad", "Fahrradwerkstatt", WERKSTATT_BEGRIFFE,
                       [_l("L-1000", "Inspektion klein", 45, "49.00"), _l("L-1010", "Inspektion groß", 90, "99.00"), _l("L-1020", "E-Bike-Diagnose (Bosch/Shimano)", 30, "39.00", "ebike"),
                        _l("L-1030", "Reifenwechsel/Platten", 20, "19.00"), _l("L-1040", "Bremsen einstellen", 30, "29.00"), _l("L-1050", "Laufrad zentrieren", 30, "25.00")],
                       [KarteiFeld("rad", "Fahrrad (Marke, Modell, Rahmennummer)"), KarteiFeld("motor", "E-Bike-System/Akku"), KarteiFeld("hinweise", "Besonderheiten")],
                       365, True, 10, 15, False,
                       "Du bist die telefonische Assistenz einer Fahrradwerkstatt. Erfrage Fahrradtyp (E-Bike?) und Problem und biete Annahmezeiten an.",
                       ["Bitte das Rad gereinigt und mit geladenem Akku bringen."], []),
    "kfz": Branche("kfz", "Kfz-Werkstatt", WERKSTATT_BEGRIFFE,
                   [_l("L-1100", "Inspektion nach Herstellervorgabe", 120, "199.00", "kfz"), _l("L-1110", "Ölwechsel", 45, "89.00"), _l("L-1120", "HU/AU-Vorbereitung", 60, "79.00", "kfz"),
                    _l("L-1130", "Reifenwechsel saisonal", 30, "39.00"), _l("L-1140", "Bremsen vorn (Scheiben/Beläge)", 120, "0.00", "kfz"), _l("L-1150", "Klimaservice", 60, "99.00", "klima")],
                   [KarteiFeld("fahrzeug", "Fahrzeug (Kennzeichen, FIN, Modell, Baujahr)"), KarteiFeld("km", "Kilometerstand letzter Besuch"), KarteiFeld("reifen", "Eingelagerte Reifen (Platz)"), KarteiFeld("hinweise", "Besonderheiten")],
                   365, True, 15, 30, False,
                   "Du bist die telefonische Assistenz einer Kfz-Werkstatt. Erfrage Fahrzeug, Kennzeichen und Anliegen und biete Annahmezeiten an; Preise nur aus dem Leistungskatalog, sonst Kostenvoranschlag.",
                   ["Bitte Fahrzeugschein mitbringen."], ["Reifeneinlagerung und Ersatzwagen bleiben manuelle Prozesse."]),
}


def branche(m: Mandant) -> Branche:
    return BRANCHEN.get((m.gewerk or "shk").lower(), BRANCHEN["shk"])


def profil(m: Mandant) -> dict[str, Any]:
    b = branche(m)
    return {"schluessel": b.schluessel, "bezeichnung": b.bezeichnung, "begriffe": b.begriffe, "wiederkehr_tage": b.wiederkehr_tage,
            "online_buchung": b.online_buchung, "gesundheitsdaten": b.gesundheitsdaten, "hinweise_kunde": b.hinweise_kunde, "rechtliches": b.rechtliches,
            "kartei": [f.__dict__ for f in b.kartei], "leistungen": len(b.leistungen)}


def alle() -> list[dict[str, Any]]:
    return [{"schluessel": b.schluessel, "bezeichnung": b.bezeichnung, "leistungen": len(b.leistungen), "online_buchung": b.online_buchung,
             "wiederkehr_tage": b.wiederkehr_tage} for b in BRANCHEN.values()]


def umstellen(s: Session, m: Mandant, schluessel: str, leistungen_anlegen: bool = True, akteur_name: str | None = None) -> dict[str, Any]:
    if schluessel not in BRANCHEN:
        raise ValueError(f"Unbekannte Branche {schluessel!r}; erlaubt: {sorted(BRANCHEN)}")
    b = BRANCHEN[schluessel]
    m.gewerk = schluessel
    m.puffer_minuten, m.raster_minuten = b.puffer_minuten, b.raster_minuten
    angelegt = leistungskatalog_anlegen(s, m) if leistungen_anlegen else 0
    domaene.protokollieren(s, m.id, "betrieb.branche", "mandant", m.id, "mensch", {"branche": schluessel, "leistungen": angelegt}, akteur_name)
    return {"branche": schluessel, "leistungen_angelegt": angelegt, "puffer_minuten": m.puffer_minuten, "raster_minuten": m.raster_minuten}


def leistungskatalog_anlegen(s: Session, m: Mandant) -> int:
    """Leistungen der Branche als Artikel der Kategorie „leistung“ (Netto aus Bruttopreis); vorhandene Nummern bleiben."""
    vorhanden = {a.nummer for a in s.scalars(select(Artikel).where(Artikel.mandant_id == m.id))}
    n = 0
    for le in branche(m).leistungen:
        if le.nummer in vorhanden:
            continue
        brutto = Decimal(le.preis_brutto)
        netto = (brutto / (1 + Decimal(le.ust_satz) / 100)).quantize(Decimal("0.01"))
        s.add(Artikel(mandant_id=m.id, nummer=le.nummer, bezeichnung=le.bezeichnung, kategorie="leistung", einheit="Termin", verkauf_netto=f"{netto:.2f}",
                      ust_satz=le.ust_satz, dauer_minuten=le.dauer_minuten, qualifikation=le.qualifikation))
        n += 1
    s.flush()
    return n


def leistungen(s: Session, m: Mandant) -> list[Artikel]:
    return s.scalars(select(Artikel).where(Artikel.mandant_id == m.id, Artikel.kategorie == "leistung", Artikel.aktiv.is_(True)).order_by(Artikel.nummer)).all()


def kartei_pruefen(m: Mandant, k: Kunde, felder: dict[str, Any]) -> dict[str, Any]:
    """Nur bekannte Felder; sensible Felder verlangen die Einwilligung gesundheitsdaten (Art. 9 Abs. 2 lit. a DSGVO)."""
    erlaubt = {f.schluessel: f for f in branche(m).kartei}
    unbekannt = sorted(set(felder) - set(erlaubt))
    if unbekannt:
        raise ValueError(f"Unbekannte Karteifelder für {branche(m).bezeichnung}: {', '.join(unbekannt)}")
    sensibel = [k_ for k_, v in felder.items() if erlaubt[k_].sensibel and v not in (None, "", False)]
    if sensibel and not (k.einwilligungen or {}).get("gesundheitsdaten"):
        raise domaene.Domaenenfehler(f"Gesundheitsdaten ({', '.join(sensibel)}) nur mit Einwilligung „gesundheitsdaten“ des Kunden")
    return {k_: v for k_, v in felder.items() if v not in (None, "")}


def prompt(m: Mandant) -> dict[str, Any]:
    """Kontext für den Sprachagenten: Rolle, Leistungen mit Dauer, Hinweise, Begriffe."""
    b = branche(m)
    return {"branche": b.schluessel, "bezeichnung": b.bezeichnung, "rolle": b.sprachagent, "begriffe": b.begriffe, "hinweise_kunde": b.hinweise_kunde,
            "sicherheitsfaelle": b.schluessel in ("shk", "elektro"), "anlagen": bool(b.begriffe.get("anlagen")),
            "leistungen": [{"nummer": le.nummer, "bezeichnung": le.bezeichnung, "dauer_minuten": le.dauer_minuten, "preis_brutto": le.preis_brutto} for le in b.leistungen]}


def wiederkehr(s: Session, m: Mandant, heute: date) -> int:
    """Wiederkehr-Erinnerung: Kunden, deren letzter durchgeführter Termin genau wiederkehr_tage zurückliegt und die keinen
    künftigen Termin haben, bekommen eine Nachricht mit Buchungslink als Freigabe B (Policy kann automatisieren).
    Werbung per E-Mail/SMS nur mit Einwilligung (§ 7 UWG), daher Einwilligung email oder sms Voraussetzung."""
    b = branche(m)
    if not b.wiederkehr_tage:
        return 0
    stichtag = heute - timedelta(days=b.wiederkehr_tage)
    n = 0
    kunden = s.scalars(select(Kunde).where(Kunde.mandant_id == m.id)).all()
    for k in kunden:
        ew = k.einwilligungen or {}
        if not (ew.get("email") or ew.get("sms")) or not (k.email or k.telefon):
            continue
        termine = s.scalars(select(Termin).join(Servicefall, Termin.servicefall_id == Servicefall.id).where(Servicefall.kunde_id == k.id, Termin.status != "abgesagt")
                            .order_by(Termin.beginn.desc())).all()
        if not termine or any(domaene.als_utc(t.beginn).date() > heute for t in termine):
            continue
        letzter = domaene.als_utc(termine[0].beginn).date()
        if letzter != stichtag:
            continue
        schon = s.scalar(select(Ereignis).where(Ereignis.mandant_id == m.id, Ereignis.typ == "wiederkehr.erinnert", Ereignis.entitaet_id == k.id,
                                                Ereignis.payload["letzter_termin"].as_string() == letzter.isoformat()))
        if schon is not None:   # Tageslauf darf mehrfach laufen
            continue
        basis = (m.stammdaten or {}).get("basis_url")
        token = (m.stammdaten or {}).get("buchung_token")
        link = f"{basis.rstrip('/')}/buchung/#{token}" if basis and token else None
        text = (f"Guten Tag {k.name},\n\nIhr letzter Besuch bei {m.name} liegt {b.wiederkehr_tage} Tage zurück. Zeit für den nächsten Termin?"
                + (f"\nOnline buchen: {link}" if link else f"\nRufen Sie uns an: {m.stammdaten.get('telefon', '')}")
                + "\n\nWenn Sie keine Erinnerungen mehr wünschen, antworten Sie kurz mit „Stopp“.")
        domaene.aktion_beantragen(s, m, "nachricht_an_endkunde", {"kunde_id": k.id, "betreff": f"Zeit für Ihren nächsten Termin bei {m.name}", "text": text,
                                                                   "kurz": f"{m.name}: Zeit für Ihren nächsten Termin. Buchen: {link or m.stammdaten.get('telefon', '')}"},
                                  "system", None, f"Wiederkehr nach {b.wiederkehr_tage} Tagen")
        domaene.protokollieren(s, m.id, "wiederkehr.erinnert", "kunde", k.id, "system", {"letzter_termin": letzter.isoformat()})
        n += 1
    return n
