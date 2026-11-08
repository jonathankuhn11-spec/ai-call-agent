"""Bedarfs-Agent: leitet aus Gewerk, Problemcodes P1–P10, Infrastruktur und Stellenanzeige ab, was ein Lead braucht.

Ergebnis ist ein `Bedarf`: Branchenprofil der Plattform, Edition (Basis/Pro/Premium wie preise.toml), Website-Modus
(neu/Ablösung/Ergänzung), Module mit Begründung und die Argumente für Brief und Gespräch. Die Zuordnung P-Code →
Produktfunktion folgt plattform/VERKAUF.md (Abdeckungsmatrix); Formulierungen für Dritte sind sachlich gehalten
(„uns ist aufgefallen …“), P9-Befunde sind Indizien und keine Rechtsberatung.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from kundenpaket.leads import Lead

# Reihenfolge = Priorität (spezifische Begriffe zuerst). Schlüssel wie plattform/core/branchen.py (BRANCHEN).
BRANCHEN_REGELN: list[tuple[str, tuple[str, ...]]] = [
    ("fahrrad", ("fahrrad", "zweirad", "e-bike", "ebike")),
    ("kfz", ("kfz", "autohaus", "automobil", "auto ", "autos", "werkstatt", "reifen", "karosserie", "car service", "serviceberater", "fahrzeug")),
    ("schluesseldienst", ("schlüssel", "sicherheitstechnik", "alarm", "videoanlage", "einbruch", "tresor")),
    ("gebaeudereinigung", ("gebäudereinigung", "reinigung", "gebäudemanagement", "hausmeister", "facility", "gebäudedienst", "gebäudeservice")),
    ("shk", ("sanitär", "heizung", "shk", "klima", "haustechnik", "installateur", "lüftung", "wärmepumpe", "bad ", "bäder", "rohrreinigung")),
    ("elektro", ("elektro", "elektriker", "photovoltaik", "solar", "e-check")),
    ("dachdecker", ("dach", "zimmerei", "zimmerer", "spengler", "klempner")),
    ("gartenbau", ("garten", "galabau", "landschaft", "grünpflege", "baumpflege")),
    ("tischler", ("tischler", "schreiner", "möbel", "bestattung", "innenausbau", "fenster", "treppen")),
    ("maler", ("maler", "lackier", "bautenschutz", "abdichtung", "sanierung", "fassade", "trockenbau", "fliesen", "stuck", "boden", "estrich", "bauservice", "bau")),
    ("friseur", ("friseur", "barbier", "barber", "coiffeur")),
    ("kosmetik", ("kosmetik", "beauty")),
    ("nagelstudio", ("nagel", "nail")),
    ("physiotherapie", ("physio", "krankengymnastik")),
    ("massage", ("massage", "wellness")),
    ("fusspflege", ("fußpflege", "podolog")),
    ("hundesalon", ("hunde", "tierpflege")),
]
NAEHERUNG = {"maler": ("bautenschutz", "abdichtung", "sanierung", "bauservice", "bau", "fliesen", "boden", "estrich", "trockenbau"),
             "tischler": ("bestattung",), "schluesseldienst": ("sicherheitstechnik", "alarm", "videoanlage")}
FARBEN = {"shk": "#1b6ec2", "elektro": "#c28a00", "kfz": "#b3261e", "fahrrad": "#2e7d32", "maler": "#6a4c93", "dachdecker": "#8d3b26",
          "gartenbau": "#2e7d32", "gebaeudereinigung": "#00838f", "tischler": "#7b5232", "schluesseldienst": "#37474f"}

# Branchen ohne freie Online-Buchung in der Plattform (Besichtigungs-/Aufmaßgeschäft, branchen.Branche.online_buchung = False)
OHNE_ONLINE_BUCHUNG = {"maler", "dachdecker", "tischler", "schluesseldienst"}
P3_BESICHTIGUNG = ("Online-Besichtigungsanfrage",
                   "Kunden fordern einen Besichtigungs- oder Aufmaßtermin online an – mit Fotos und Wunschzeitraum; Ihr Büro bestätigt mit einem Klick.")

EDITION_AUS_ANGEBOT = {"T1": "basis", "T2": "pro", "T3": "premium"}
EDITION_RANG = {"basis": 0, "pro": 1, "premium": 2}

# P-Code → (Problem kurz, Beobachtung für den Brief, Lösung, Modul, Mindestedition). Quelle: plattform/VERKAUF.md
CODES: dict[str, tuple[str, str, str, str, str]] = {
    "P1": ("keine eigene Website", "Zu Ihrem Betrieb haben wir keine eigene Website gefunden.",
           "Eine Betriebswebsite mit Anfrageformular, Notfallmeldung und eigener Domain ist in jedem Paket enthalten – einen Entwurf finden Sie auf Seite 2.",
           "Betriebswebsite", "basis"),
    "P2": ("kein Anfrageformular", "Auf Ihrer Website können Kunden ihr Anliegen bislang nicht direkt per Formular schildern.",
           "Ein Anfrageformular mit bis zu drei Fotos legt jede Anfrage vollständig als Vorgang an – ohne Abtippen.",
           "Anfrageformular mit Fotos", "basis"),
    "P3": ("keine Online-Terminbuchung", "Termine lassen sich bislang nur telefonisch oder per E-Mail vereinbaren.",
           "Mit der Online-Terminbuchung wählen Kunden freie Zeitfenster selbst – abgestimmt auf Qualifikation und Fahrzeit Ihres Teams.",
           "Online-Terminbuchung", "pro"),
    "P4": ("kein Kundenportal", "Ihre Kunden erfahren den Stand ihres Auftrags bislang nur auf Nachfrage.",
           "Ein Kundenportal je Auftrag zeigt Status, Termin, Rapport und Rechnung – das erspart Rückfragen am Telefon.",
           "Kundenportal", "basis"),
    "P5": ("Notdienst nur telefonisch", "Ihr Notdienst ist ausschließlich telefonisch erreichbar.",
           "Der Telefonagent nimmt Notrufe rund um die Uhr an, erkennt die Dringlichkeit und verbindet sofort mit Ihrer Bereitschaft.",
           "24/7-Notdienstannahme", "basis"),
    "P6": ("Website veraltet", "Ihre Website ist technisch in die Jahre gekommen.",
           "Wir lösen sie durch eine schnelle, mobiloptimierte Betriebswebsite ab – den Entwurf finden Sie auf Seite 2.",
           "Website-Ablösung", "basis"),
    "P7": ("Mobilnummer als Hauptnummer", "Als erste Kontaktnummer ist eine Mobilfunknummer angegeben.",
           "Ihre Nummer bleibt: Eine bedingte Rufumleitung leitet nicht angenommene Anrufe an den Telefonagenten – eingerichtet mit einem Tastencode.",
           "Rufumleitungsassistent", "basis"),
    "P8": ("Büro/Telefon nicht gedeckt", "Ihr Büro ist personell knapp besetzt.",
           "Der Telefonagent nimmt jeden Anruf an, erfasst Anliegen und Adresse vollständig und legt Ihnen Termine, Angebote und Rechnungen zur Freigabe vor.",
           "Telefonagent + Freigabe-Inbox", "basis"),
    "P9": ("Rechts-/DSGVO-Risiko Website", "Auf Ihrer Website sind uns Angaben aufgefallen, die nicht mehr dem aktuellen Stand entsprechen (etwa Verweise im Impressum).",
           "Unsere Betriebswebsite kommt ohne Cookies und Fremdskripte aus; Impressum und Datenschutzerklärung entstehen als Entwurf aus Ihren Stammdaten.",
           "Rechtssichere Website (Entwurf)", "basis"),
    "P10": ("keine Karriereseite", "Trotz Personalsuche gibt es auf Ihrer Website keine Karriereseite.",
            "Die Karriereseite veröffentlicht Ihre Stellen für Google for Jobs; Bewerber senden eine Kurzbewerbung in zwei Minuten.",
            "Karriereseite", "basis"),
}
PRIORITAET = ["P8", "P5", "P3", "P1", "P7", "P4", "P2", "P6", "P10", "P9"]


@dataclass
class Modul:
    code: str
    name: str
    problem: str
    beobachtung: str
    loesung: str


@dataclass
class Bedarf:
    branche: str
    branche_naeherung: bool
    edition: str
    website_modus: str              # neu | abloesung | ergaenzung
    module: list[Modul] = field(default_factory=list)
    stelle: dict | None = None      # Stelle für die Karriereseite (aus der Stellenanzeige)
    notdienst: bool = False
    rufumleitung: bool = False
    hinweise: list[str] = field(default_factory=list)

    @property
    def brief_module(self) -> list[Modul]:
        return sorted(self.module, key=lambda m: PRIORITAET.index(m.code) if m.code in PRIORITAET else 99)[:3]


def branche_bestimmen(lead: Lead) -> tuple[str, bool]:
    text = f" {lead.gewerk} {lead.firma} ".lower()
    for schluessel, begriffe in BRANCHEN_REGELN:
        treffer = next((b for b in begriffe if b in text), None)
        if treffer:
            return schluessel, treffer in NAEHERUNG.get(schluessel, ())
    return "shk", True


def _stellenart(titel: str) -> str:
    t = titel.lower()
    if "minijob" in t or "520" in t or "556" in t or "603" in t:
        return "minijob"
    if "teilzeit" in t and "vollzeit" not in t:
        return "teilzeit"
    return "vollzeit"


def _stellentitel(titel: str) -> str:
    """Stellenanzeigentitel ohne Zusätze in Klammern/nach Gedankenstrich, z. B. „Bürokraft (m/w/d) in Teilzeit – 15-20 h“ → „Bürokraft (m/w/d)“."""
    t = re.split(r"\s+[–-]\s+|\s+in (Teilzeit|Vollzeit)\b|\s+Vollzeit\s*/|,", titel)[0].strip()
    if "(m/w/d)" not in t:
        t = re.sub(r"\s*\([mwdfi/]+\)", "", t).strip() + " (m/w/d)"
    return t[:120]


def analysieren(lead: Lead) -> Bedarf:
    branche, naeherung = branche_bestimmen(lead)
    codes = list(lead.probleme)
    if not lead.website and "P1" not in codes:
        codes.insert(0, "P1")
    if lead.stelle and "P8" not in codes:
        codes.append("P8")

    edition = next((e for t, e in EDITION_AUS_ANGEBOT.items() if lead.angebot.upper().startswith(t)), "basis")
    for c in codes:
        mindest = "basis" if c == "P3" and branche in OHNE_ONLINE_BUCHUNG else CODES[c][4]
        if EDITION_RANG[mindest] > EDITION_RANG[edition]:
            edition = mindest

    if "P1" in codes:
        modus = "neu"
    elif {"P2", "P6", "P9"} & set(codes) or lead.infrastruktur.upper() in ("KEIN", "SCHWACH"):
        modus = "abloesung"
    else:
        modus = "ergaenzung"

    module = []
    for c in codes:
        problem, beobachtung, loesung, name, _ = CODES[c]
        if c == "P3" and branche in OHNE_ONLINE_BUCHUNG:
            name, loesung = P3_BESICHTIGUNG
        if c == "P8" and lead.stelle:
            beobachtung = f"Sie suchen derzeit eine Verstärkung als {_stellentitel(lead.stelle)}."
            loesung = ("Bis die Stelle besetzt ist – und danach als Entlastung – nimmt der Telefonagent jeden Anruf an, erfasst Anliegen und Adresse "
                       "vollständig und legt Ihnen Termine, Angebote und Rechnungen zur Freigabe vor.")
        if c == "P5" and "bestattung" in f"{lead.gewerk} {lead.firma}".lower():
            beobachtung = "Ihre Erreichbarkeit Tag und Nacht hängt an einer einzigen Telefonleitung."
            loesung = "Der Telefonagent nimmt Anrufe im Trauerfall rund um die Uhr ruhig und vollständig auf und verbindet sofort mit Ihrer Bereitschaft."
        module.append(Modul(c, name, problem, beobachtung, loesung))
    if not any(m.code == "P8" for m in module):   # Kernleistung gehört in jedes Paket
        module.append(Modul("–", "Telefonagent", "Anrufe außerhalb der Bürozeiten", "", CODES["P8"][2]))

    stelle = None
    if lead.stelle:
        stelle = {"titel": _stellentitel(lead.stelle), "art": _stellenart(lead.stelle), "ort": lead.ort or None,
                  "text": f"Wir suchen Verstärkung für unser Team in {lead.ort}. Die vollständige Stellenbeschreibung stimmen wir mit Ihnen ab (Entwurf).",
                  "aktiv": True}

    hinweise = []
    if naeherung:
        hinweise.append(f"Branchenprofil „{branche}“ ist eine Näherung für „{lead.gewerk}“; Leistungskatalog vor der Vorführung anpassen.")
    if not lead.strasse:
        hinweise.append("Straße fehlt: Brief nicht versandfähig, Anschrift ergänzen.")
    if not lead.entscheider:
        hinweise.append("Entscheider unbekannt: Brief geht an die Geschäftsleitung ohne Namen.")
    if "P9" in codes:
        hinweise.append("P9 ist ein Indiz, keine Rechtsberatung; im Gespräch nur sachlich ansprechen.")
    return Bedarf(branche=branche, branche_naeherung=naeherung, edition=edition, website_modus=modus, module=module, stelle=stelle,
                  notdienst="P5" in codes or bool(re.search(r"notdienst|24\s*/\s*7|tag (und|&) nacht", f"{lead.befund} {lead.gewerk}", re.I)),
                  rufumleitung="P7" in codes, hinweise=hinweise)


def ansprechpartner(lead: Lead) -> str:
    """Erste genannte Person aus „Entscheider“ (mehrere durch Komma/„und“ getrennt), mit akademischem Grad, ohne Dipl.-Zusatz."""
    erste = re.split(r",|;| und | / ", lead.entscheider)[0].strip()
    erste = re.sub(r"\b(Dipl\.-[A-Za-zäöü.]+|Inh\.|Inhaber(in)?|GF|Geschäftsführer(in)?)\s*", "", erste).strip()
    return erste if len(erste.split()) >= 2 else ""
