"""Zählerstände und Verbrauchsanalyse je Kunde und Anlage – „nutzt der Kunde seine Anlage so, wie sie soll?“.

Erfasst werden Gas (kWh oder m³), Strom, Wasser, Wärmemenge, Betriebsstunden und Brennerstarts – vom Büro, vom
Monteur (Rapport), vom Kunden über das Portal oder per Import. Zwei aufeinanderfolgende Stände ergeben den
Verbrauch einer Periode. Heizenergie wird mit Gradtagszahlen klimabereinigt: die Monatsgewichte
GRADTAGE (Anteile am Jahres-Heizbedarf, Orientierung VDI 3807 / DWD-Langzeitmittel, Deutschland) bestimmen, welcher
Anteil des Jahresverbrauchs in eine Periode fällt; ein Grundlastanteil WARMWASSER_ANTEIL (Warmwasser, Bereitschaftsverluste)
wird zeitanteilig verteilt; Verbrauch ÷ Anteil = normierter Jahresverbrauch. Perioden mit weniger als MINDESTANTEIL des
Jahres (z. B. ein Sommerquartal) werden angezeigt, aber nicht verglichen. Liegt der
normierte Jahresverbrauch der jüngsten Periode mehr als SCHWELLE über dem Median der Vorperioden, entsteht ein
Hinweis (Effizienzverlust: Wartung, hydraulischer Abgleich, Regelungseinstellung, Verkalkung). Brennerstarts je
Betriebsstunde über TAKTEN_JE_H deuten auf Takten (Kessel überdimensioniert, Mindestleistung, Pufferspeicher).
Wasser und Strom ohne Heizbezug werden zeitanteilig (flach) normiert. Die Analyse ist Hinweis, kein Gutachten.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene
from core.modelle import Anlage, Kunde, Mandant, Objekt, Zaehlerstand

GRADTAGE = [17, 15, 13, 9, 5, 2, 1, 1, 4, 8, 12, 16]           # Januar … Dezember, Anteile in %
WARMWASSER_ANTEIL = 0.15                                       # ganzjähriger Grundlastanteil (Warmwasser, Bereitschaft), Rest heizgradgewichtet
MINDESTANTEIL = 0.15                                           # Perioden mit kleinerem Jahresanteil fließen nicht in den Vergleich ein
ARTEN = {"gas_kwh": ("Gas", "kWh", True), "gas_m3": ("Gas", "m³", True), "strom_kwh": ("Strom", "kWh", False),
         "wasser_m3": ("Wasser", "m³", False), "waerme_kwh": ("Wärmemenge", "kWh", True),
         "betriebsstunden": ("Betriebsstunden", "h", True), "brennerstarts": ("Brennerstarts", "Starts", True),
         "kilometer": ("Kilometerstand", "km", False)}
SCHWELLE = 0.15
TAKTEN_JE_H = 6.0
M3_ZU_KWH = 10.3    # Erdgas H, Brennwert·Zustandszahl als Näherung


def _heizbezogen(art: str, a: Anlage | None) -> bool:
    if art == "strom_kwh":
        return bool(a and a.anlagentyp in ("waermepumpe", "klima"))
    return ARTEN.get(art, ("", "", False))[2]


def gradtag_anteil(von: date, bis: date) -> float:
    """Anteil des Jahres-Heizbedarfs, der im Zeitraum (von, bis] liegt; Monate werden tagesanteilig gewichtet."""
    if bis <= von:
        return 0.0
    summe = sum(GRADTAGE)
    anteil, tag = 0.0, von + timedelta(days=1)
    while tag <= bis:
        monatsende = (date(tag.year + (tag.month == 12), tag.month % 12 + 1, 1) - timedelta(days=1))
        stueck = min(bis, monatsende)
        tage_im_monat = monatsende.day
        anteil += GRADTAGE[tag.month - 1] / summe * ((stueck - tag).days + 1) / tage_im_monat
        tag = stueck + timedelta(days=1)
    return anteil


def erfassen(s: Session, m: Mandant, kunde: Kunde, art: str, wert: float, datum: date, anlage: Anlage | None = None,
             quelle: str = "buero", notiz: str | None = None, akteur_name: str | None = None) -> Zaehlerstand:
    if art not in ARTEN:
        raise ValueError(f"Unbekannte Zählerart {art}")
    if wert < 0:
        raise ValueError("Zählerstand darf nicht negativ sein")
    letzter = s.scalar(select(Zaehlerstand).where(Zaehlerstand.kunde_id == kunde.id, Zaehlerstand.art == art,
                                                   Zaehlerstand.anlage_id == (anlage.id if anlage else None),
                                                   Zaehlerstand.datum <= datum).order_by(Zaehlerstand.datum.desc()).limit(1))
    if letzter and wert < letzter.wert and "wechsel" not in (notiz or "").lower():
        raise ValueError(f"Stand {wert:g} liegt unter dem letzten Stand {letzter.wert:g} vom {letzter.datum.strftime('%d.%m.%Y')} – bei Zählerwechsel „Zählerwechsel“ in die Notiz schreiben")
    z = Zaehlerstand(mandant_id=m.id, kunde_id=kunde.id, anlage_id=anlage.id if anlage else None, art=art, wert=float(wert), datum=datum, quelle=quelle, notiz=notiz)
    s.add(z)
    s.flush()
    domaene.protokollieren(s, m.id, "zaehlerstand.erfasst", "kunde", kunde.id, "system" if quelle == "import" else "mensch",
                           {"art": art, "wert": wert, "datum": datum.isoformat(), "anlage_id": z.anlage_id, "quelle": quelle}, akteur_name)
    return z


def _aus(z: Zaehlerstand) -> dict[str, Any]:
    return {"id": z.id, "anlage_id": z.anlage_id, "art": z.art, "bezeichnung": ARTEN.get(z.art, (z.art,))[0], "einheit": ARTEN.get(z.art, ("", ""))[1],
            "wert": z.wert, "datum": z.datum.isoformat(), "quelle": z.quelle, "notiz": z.notiz}


def liste(s: Session, kunde: Kunde) -> list[dict[str, Any]]:
    return [_aus(z) for z in s.scalars(select(Zaehlerstand).where(Zaehlerstand.kunde_id == kunde.id).order_by(Zaehlerstand.art, Zaehlerstand.datum))]


def _perioden(staende: list[Zaehlerstand], heiz: bool) -> list[dict[str, Any]]:
    perioden = []
    for a, b in zip(staende, staende[1:]):
        if "wechsel" in (b.notiz or "").lower() or b.wert < a.wert:
            continue
        tage = (b.datum - a.datum).days
        if tage < 14:
            continue
        verbrauch = b.wert - a.wert
        flach = tage / 365.25
        anteil = (WARMWASSER_ANTEIL * flach + (1 - WARMWASSER_ANTEIL) * gradtag_anteil(a.datum, b.datum)) if heiz else flach
        perioden.append({"von": a.datum.isoformat(), "bis": b.datum.isoformat(), "tage": tage, "verbrauch": round(verbrauch, 1),
                         "anteil_jahr": round(anteil, 3), "jahresverbrauch_normiert": round(verbrauch / anteil, 0) if anteil > 0.02 else None,
                         "aussagekraeftig": anteil >= MINDESTANTEIL})
    return perioden


def analyse(s: Session, m: Mandant, kunde: Kunde) -> dict[str, Any]:
    staende = s.scalars(select(Zaehlerstand).where(Zaehlerstand.kunde_id == kunde.id).order_by(Zaehlerstand.datum, Zaehlerstand.id)).all()
    anlagen = {a.id: a for a in s.scalars(select(Anlage).join(Objekt, Objekt.id == Anlage.objekt_id).where(Anlage.mandant_id == m.id, Objekt.kunde_id == kunde.id))}
    gruppen: dict[tuple[str | None, str], list[Zaehlerstand]] = {}
    for z in staende:
        gruppen.setdefault((z.anlage_id, z.art), []).append(z)
    reihen, hinweise = [], []
    for (anlage_id, art), zs in gruppen.items():
        a = anlagen.get(anlage_id) if anlage_id else None
        heiz = _heizbezogen(art, a)
        perioden = _perioden(zs, heiz)
        gueltig = [p["jahresverbrauch_normiert"] for p in perioden if p["jahresverbrauch_normiert"] and p["aussagekraeftig"]]
        reihe = {"anlage_id": anlage_id, "anlagentyp": a.anlagentyp if a else None, "art": art, "bezeichnung": ARTEN[art][0], "einheit": ARTEN[art][1],
                 "klimabereinigt": heiz and art not in ("betriebsstunden", "brennerstarts"), "staende": len(zs), "perioden": perioden,
                 "jahresverbrauch_normiert": gueltig[-1] if gueltig else None, "referenz": None, "abweichung": None, "bewertung": "zu_wenig_daten"}
        if len(gueltig) >= 2:
            referenz = statistics.median(gueltig[:-1])
            abw = (gueltig[-1] - referenz) / referenz if referenz else 0.0
            reihe.update(referenz=round(referenz, 0), abweichung=round(abw, 3),
                         bewertung="auffaellig" if abw > SCHWELLE else ("verbessert" if abw < -SCHWELLE else "unauffaellig"))
            if abw > SCHWELLE and art in ("gas_kwh", "gas_m3", "waerme_kwh", "strom_kwh"):
                hinweise.append({"anlage_id": anlage_id, "art": art, "schwere": "hinweis",
                                 "text": f"{ARTEN[art][0]}verbrauch klimabereinigt {abw:+.0%} gegenüber den Vorperioden ({gueltig[-1]:.0f} statt {referenz:.0f} {ARTEN[art][1]}/Jahr). "
                                         "Mögliche Ursachen: fällige Wartung, verstellte Regelung/Heizkurve, fehlender hydraulischer Abgleich, Verkalkung, Nutzerverhalten – Wartungstermin anbieten."})
            elif abw > SCHWELLE and art == "wasser_m3":
                hinweise.append({"anlage_id": anlage_id, "art": art, "schwere": "hinweis", "text": f"Wasserverbrauch {abw:+.0%} – auf Leckage (Spülkasten, tropfende Armatur, Leitungsschaden) prüfen."})
        reihen.append(reihe)
    # Taktverhalten: Brennerstarts je Betriebsstunde derselben Anlage
    for (anlage_id, art), zs in gruppen.items():
        if art != "brennerstarts" or len(zs) < 2 or (anlage_id, "betriebsstunden") not in gruppen:
            continue
        bs = gruppen[(anlage_id, "betriebsstunden")]
        if len(bs) < 2:
            continue
        starts = zs[-1].wert - zs[0].wert
        std = bs[-1].wert - bs[0].wert
        if std > 50 and starts / std > TAKTEN_JE_H:
            hinweise.append({"anlage_id": anlage_id, "art": "takten", "schwere": "hinweis",
                             "text": f"{starts / std:.1f} Brennerstarts je Betriebsstunde – Takten. Mindestleistung/Modulation, Pumpeneinstellung, hydraulischen Abgleich oder Pufferspeicher prüfen; Taktverschleiß verkürzt die Lebensdauer von Zündung und Wärmetauscher."})
    # Kennzahl: kWh je m² Wohnfläche, falls in der Kartei bekannt
    flaeche = (kunde.kartei or {}).get("wohnflaeche_m2")
    for r in reihen:
        if flaeche and r["jahresverbrauch_normiert"] and r["art"] in ("gas_kwh", "gas_m3", "waerme_kwh"):
            kwh = r["jahresverbrauch_normiert"] * (M3_ZU_KWH if r["art"] == "gas_m3" else 1)
            r["kwh_je_m2"] = round(kwh / float(flaeche), 0)
            r["effizienzklasse_hinweis"] = ("sehr gut (< 75 kWh/m²a)" if r["kwh_je_m2"] < 75 else "durchschnittlich (75–160 kWh/m²a)" if r["kwh_je_m2"] < 160
                                           else "hoch (> 160 kWh/m²a) – Sanierungs-/Fördergespräch anbieten")
    return {"kunde_id": kunde.id, "reihen": reihen, "hinweise": hinweise}


def auffaellige(s: Session, m: Mandant) -> list[dict[str, Any]]:
    """Kunden mit auffälliger Verbrauchsentwicklung (für Cockpit und Tageslauf)."""
    ergebnis = []
    kunden_ids = {z.kunde_id for z in s.scalars(select(Zaehlerstand).where(Zaehlerstand.mandant_id == m.id))}
    for k in s.scalars(select(Kunde).where(Kunde.id.in_(kunden_ids))) if kunden_ids else []:
        a = analyse(s, m, k)
        if a["hinweise"]:
            ergebnis.append({"kunde_id": k.id, "kunde": k.name, "hinweise": a["hinweise"]})
    return ergebnis
