"""BEG-Förderrechner: Heizungsförderung (KfW 458) und Einzelmaßnahmen (BAFA BEG EM) als Richtwert fürs Angebot.

Die Sätze sind als Parameter geführt (Stand der Richtlinie „Bundesförderung für effiziente Gebäude“ in der seit
2024 geltenden Fassung, STAND unten). Die Bundesregierung hat eine Neufassung der Richtlinie angekündigt; vor dem
Einsatz im Angebot sind die Sätze gegen die aktuelle Richtlinie zu prüfen und ggf. hier anzupassen. Der Rechner
ersetzt keine Förderberatung und keinen Antrag: Antragsteller ist der Eigentümer, Antrag vor Vertragsschluss
(bzw. mit aufschiebender/auflösender Bedingung), Bestätigung zum Antrag (BzA) durch Energieeffizienz-Experten bzw.
Fachunternehmer. Das Angebot weist den Betrag daher als „voraussichtlicher Zuschuss, vorbehaltlich Bewilligung“ aus.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

STAND = "BEG-Richtlinien EM (BAFA) und Heizungsförderung (KfW 458), Fassung 2024; Prüfung gegen Neufassung nötig"

HEIZUNG = {
    "grund": Decimal("0.30"),                # Grundförderung für alle Antragsteller
    "klimageschwindigkeit": Decimal("0.20"),  # Austausch funktionstüchtiger fossiler Heizung ≥ 20 Jahre bzw. Gas/Öl/Kohle, selbstnutzende Eigentümer, bis 2028
    "einkommen": Decimal("0.30"),             # zu versteuerndes Haushaltseinkommen ≤ 40.000 €, selbstnutzende Eigentümer
    "effizienz": Decimal("0.05"),             # Wärmepumpe: natürliches Kältemittel oder Wärmequelle Erde/Wasser/Abwasser
    "maximal": Decimal("0.70"),
    "emissionsminderung_biomasse_eur": Decimal("2500"),  # Biomasseheizung mit Staubgrenzwert 2,5 mg/m³
    "kosten_erste_we": Decimal("30000"),
    "kosten_we_2_bis_6": Decimal("15000"),
    "kosten_we_ab_7": Decimal("8000"),
}
EINZELMASSNAHME = {
    "grund": Decimal("0.15"),      # Gebäudehülle, Anlagentechnik (außer Heizung), Heizungsoptimierung
    "isfp": Decimal("0.05"),       # Bonus bei Umsetzung einer Maßnahme aus dem individuellen Sanierungsfahrplan
    "kosten_je_we": Decimal("30000"),
    "kosten_je_we_isfp": Decimal("60000"),
}
HEIZUNGSARTEN = {"waermepumpe": "Wärmepumpe", "biomasse": "Biomasseheizung (Pellet, Holz)", "solarthermie": "Solarthermie",
                 "fernwaerme": "Anschluss Wärmenetz", "brennstoffzelle": "Brennstoffzelle (grüner Wasserstoff/Biomethan)",
                 "hybrid": "Hybridheizung (EE-Anteil)", "wasserstofffaehig": "H2-ready-Gasheizung (nur EE-Anteil)"}


def _euro(d: Decimal) -> float:
    return float(d.quantize(Decimal("0.01"), ROUND_HALF_UP))


def foerderfaehige_kosten_heizung(wohneinheiten: int) -> Decimal:
    we = max(1, int(wohneinheiten))
    summe = HEIZUNG["kosten_erste_we"]
    summe += HEIZUNG["kosten_we_2_bis_6"] * min(max(we - 1, 0), 5)
    summe += HEIZUNG["kosten_we_ab_7"] * max(we - 6, 0)
    return summe


def heizung(kosten: Decimal | float | str, heizungsart: str, wohneinheiten: int = 1, selbstnutzend: bool = True,
            alte_heizung_ersetzt: bool = False, einkommen_unter_40k: bool = False,
            natuerliches_kaeltemittel: bool = False, erd_oder_wasserquelle: bool = False, biomasse_staubarm: bool = False) -> dict[str, Any]:
    """Zuschuss nach KfW 458 für Wohngebäude (Privatpersonen als Eigentümer). Boni gelten nur selbstnutzenden Eigentümern."""
    if heizungsart not in HEIZUNGSARTEN:
        raise ValueError(f"Unbekannte Heizungsart {heizungsart!r}; erlaubt: {sorted(HEIZUNGSARTEN)}")
    kosten = Decimal(str(kosten).replace(",", "."))
    if kosten <= 0:
        raise ValueError("Kosten müssen größer 0 sein")
    bausteine: list[dict[str, Any]] = [{"schluessel": "grund", "titel": "Grundförderung", "satz": _euro(HEIZUNG["grund"] * 100)}]
    satz = HEIZUNG["grund"]
    if selbstnutzend and alte_heizung_ersetzt:
        satz += HEIZUNG["klimageschwindigkeit"]
        bausteine.append({"schluessel": "klimageschwindigkeit", "titel": "Klimageschwindigkeits-Bonus (Austausch fossiler Heizung)", "satz": 20.0})
    if selbstnutzend and einkommen_unter_40k:
        satz += HEIZUNG["einkommen"]
        bausteine.append({"schluessel": "einkommen", "titel": "Einkommens-Bonus (zu versteuerndes Haushaltseinkommen ≤ 40.000 €)", "satz": 30.0})
    if heizungsart == "waermepumpe" and (natuerliches_kaeltemittel or erd_oder_wasserquelle):
        satz += HEIZUNG["effizienz"]
        bausteine.append({"schluessel": "effizienz", "titel": "Effizienz-Bonus (natürliches Kältemittel bzw. Erd-/Wasserquelle)", "satz": 5.0})
    gedeckelt = satz > HEIZUNG["maximal"]
    satz = min(satz, HEIZUNG["maximal"])
    deckel = foerderfaehige_kosten_heizung(wohneinheiten)
    grundlage = min(kosten, deckel)
    zuschuss = (grundlage * satz).quantize(Decimal("0.01"), ROUND_HALF_UP)
    zuschlag = HEIZUNG["emissionsminderung_biomasse_eur"] if heizungsart == "biomasse" and biomasse_staubarm else Decimal("0")
    return {"programm": "KfW 458 Heizungsförderung", "heizungsart": HEIZUNGSARTEN[heizungsart], "kosten": _euro(kosten),
            "foerderfaehige_kosten": _euro(grundlage), "kostendeckel": _euro(deckel), "wohneinheiten": max(1, int(wohneinheiten)),
            "foerdersatz_prozent": _euro(satz * 100), "gedeckelt_auf_maximum": gedeckelt, "bausteine": bausteine,
            "zuschuss": _euro(zuschuss), "zuschlag_emissionsminderung": _euro(zuschlag), "zuschuss_gesamt": _euro(zuschuss + zuschlag),
            "eigenanteil": _euro(kosten - zuschuss - zuschlag), "stand": STAND,
            "hinweise": ["Antrag vor Vorhabensbeginn (Liefer-/Leistungsvertrag mit aufschiebender oder auflösender Bedingung zulässig).",
                         "Bestätigung zum Antrag (BzA) durch Fachunternehmer oder Energieeffizienz-Experten erforderlich.",
                         "Boni nur für selbstnutzende Eigentümer; Einkommensnachweis per Steuerbescheid.",
                         "Ergänzungskredit der KfW (Nr. 358/359) möglich; Vermieter erhalten die Grundförderung."]}


def einzelmassnahme(kosten: Decimal | float | str, wohneinheiten: int = 1, isfp: bool = False, massnahme: str = "anlagentechnik") -> dict[str, Any]:
    """BAFA BEG EM: Gebäudehülle, Anlagentechnik, Heizungsoptimierung (hydraulischer Abgleich, Pumpentausch)."""
    kosten = Decimal(str(kosten).replace(",", "."))
    if kosten <= 0:
        raise ValueError("Kosten müssen größer 0 sein")
    satz = EINZELMASSNAHME["grund"] + (EINZELMASSNAHME["isfp"] if isfp else Decimal("0"))
    deckel = (EINZELMASSNAHME["kosten_je_we_isfp"] if isfp else EINZELMASSNAHME["kosten_je_we"]) * max(1, int(wohneinheiten))
    grundlage = min(kosten, deckel)
    zuschuss = (grundlage * satz).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {"programm": "BAFA BEG Einzelmaßnahmen", "massnahme": massnahme, "kosten": _euro(kosten), "foerderfaehige_kosten": _euro(grundlage),
            "kostendeckel": _euro(deckel), "foerdersatz_prozent": _euro(satz * 100), "isfp_bonus": isfp,
            "zuschuss": _euro(zuschuss), "zuschuss_gesamt": _euro(zuschuss), "eigenanteil": _euro(kosten - zuschuss), "stand": STAND,
            "hinweise": ["Antrag beim BAFA vor Vorhabensbeginn; Mindestinvestition 300 € brutto.",
                         "Technische Mindestanforderungen (TMA) der Richtlinie beachten; Fachunternehmererklärung nach Abschluss.",
                         "Alternativ Steuerermäßigung nach § 35c EStG (20 % über drei Jahre), nicht kombinierbar."]}


def _de(betrag: float) -> str:
    return f"{betrag:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def angebotstext(ergebnis: dict[str, Any]) -> str:
    """Absatz für die Angebotseinleitung: Zuschuss als Richtwert, ausdrücklich unter Vorbehalt."""
    return (f"Voraussichtliche Förderung ({ergebnis['programm']}): {ergebnis['foerdersatz_prozent']:.0f} % auf förderfähige Kosten "
            f"von {_de(ergebnis['foerderfaehige_kosten'])} €, das sind rund {_de(ergebnis['zuschuss_gesamt'])} € Zuschuss "
            f"(Eigenanteil rund {_de(ergebnis['eigenanteil'])} €). Der Betrag ist ein Richtwert ohne Gewähr und steht unter dem "
            "Vorbehalt der Bewilligung; der Förderantrag ist vor Vertragsschluss zu stellen.")
