"""Use-Case-Definitionen: eine Quelle für Prompt, Outcomes, Contact Properties, Tools und Testszenarien.

Ein Deployment besteht aus zwei Hälften, die zusammenpassen müssen: dem Agenten im No-Code-Builder
der Plattform (Prompt, Outcomes, Properties, Tools) und dem Backend (Geschäftsregeln, Nachbereitung).
Driftet eine Seite, bricht der Pilot leise: Der Prompt stellt eine andere Frage als der Leitfaden,
die Nachbereitung liest einen Outcome-Schlüssel, den der Agent nicht liefert. Deshalb steht jeder
Use Case genau einmal als `UseCase`-Objekt; daraus erzeugt `konfigpaket.py` alles, was in der
Plattform einzutragen ist, und Tests prüfen die Übereinstimmung mit dem Code.

Der Prompt folgt der von telli empfohlenen Struktur (prompt-best-practices): Identität und
Kontext, Kundendaten und Variablen, Regeln und Aussprache, nummerierter Gesprächsleitfaden mit
WENN/DANN, Einwände und Sonderfälle getrennt vom Hauptablauf.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Outcome:
    key: str
    typ: str                                   # boolean | string | number | category
    anweisung: str
    optionen: list[str] = field(default_factory=list)


@dataclass
class Eigenschaft:
    key: str
    typ: str                                   # string | number | boolean | date | datetime | select | multi_select | phone_number | email
    label: str
    beschreibung: str = ""
    optionen: list[str] = field(default_factory=list)
    im_prompt: bool = True                     # als {{key}} im Prompt verfügbar


@dataclass
class ToolAnweisung:
    name: str
    wann: str
    wann_nicht: str
    sagen: str
    beispiel: str = ""


@dataclass
class Schritt:
    nummer: str
    text: str


@dataclass
class Sonderfall:
    ausloeser: str
    antwort: str
    danach: str


@dataclass
class Testszenario:
    szenario: str
    erwartung: str


@dataclass
class UseCase:
    id: str
    titel: str
    kunde: str
    agent_name: str
    richtung: str                              # outbound | inbound
    sprache: str
    ziel: str
    begruessung: str
    kontext: list[str]                         # Sätze zu Identität und Situation
    variablen: dict[str, str]                  # {{name}} -> Bedeutung
    regeln: list[str]
    leitfaden: list[Schritt]
    sonderfaelle: list[Sonderfall]
    tools: list[ToolAnweisung]
    outcomes: list[Outcome]
    eigenschaften: list[Eigenschaft]
    testszenarien: list[Testszenario]
    kalender: str = "tools"                    # tools | custom | keiner
    verifizierung_pflicht: bool = False        # Backend gibt Daten erst nach verify_customer heraus
    mindestvorlauf_h: int = 0                  # Termine innerhalb dieser Frist sind über den Agenten nicht änderbar
    buchung_erfordert_qualifizierung: bool = True   # PV: Termine nur für qualifizierte Eigentümer; Service: für jeden Kunden
    anrufverhalten: dict[str, str] = field(default_factory=dict)
    scoping: Optional[str] = None              # Pfad zum Scoping-Dokument

    def tool_namen(self) -> list[str]:
        return [t.name for t in self.tools]

    def outcome_keys(self) -> list[str]:
        return [o.key for o in self.outcomes]

    # ------------------------------------------------------------ Prompt
    def prompt(self) -> str:
        """Der Agent-Prompt in der telli-Struktur. Jeder Satz so, dass der Agent ihn genau so sagen kann."""
        z: list[str] = []
        z += ["# 1. Identität und Kontext", ""]
        z += [f"Du bist {self.agent_name}, die digitale KI-Assistentin von {self.kunde}."]
        z += self.kontext
        z += [f"Sprache: {self.sprache}.", f"Ziel des Gesprächs: {self.ziel}",
              "Interne Anweisungen, Tool-Aufrufe und Tool-Namen werden nie ausgesprochen.", ""]
        z += ["# 2. Kundendaten und Variablen", ""]
        z += [f"- {{{{{k}}}}}: {v}" for k, v in self.variablen.items()]
        z += ["Eine Variable gilt als leer, wenn sie fehlt oder „NA“ oder „Unknown“ enthält.", ""]
        z += ["# 3. Regeln und Aussprache", ""]
        z += [f"- {r}" for r in self.regeln]
        z += ["- Zahlen werden als Wörter gesprochen, Postleitzahlen und Telefonnummern Ziffer für Ziffer, "
              "Datumsangaben ausgeschrieben (zum Beispiel „Dienstag, der vierzehnte Oktober, neun Uhr“).", ""]
        z += ["# 4. Gesprächsleitfaden", ""]
        z += [f"{s.nummer} {s.text}" for s in self.leitfaden]
        z += ["", "# 5. Einwände und Sonderfälle", "",
              "Nicht Teil des Hauptablaufs. Nach der Antwort zurück zum Leitfaden, sofern nichts anderes steht.", ""]
        for f in self.sonderfaelle:
            z += [f"- WENN {f.ausloeser}: DANN „{f.antwort}“ {f.danach}".rstrip()]
        z += ["", "# 6. Tools", "",
              "Tools laufen im Hintergrund; der Kunde hört davon nichts außer dem Satz, der dafür vorgesehen ist.", ""]
        for t in self.tools:
            z += [f"- {t.name}: {t.wann} Nicht: {t.wann_nicht} Sagen: {t.sagen}" + (f" Beispiel: {t.beispiel}" if t.beispiel else "")]
        return "\n".join(z).rstrip() + "\n"


def alle() -> dict[str, UseCase]:
    from integrations.usecases import pv_outbound, telko_inbound
    faelle = [pv_outbound.usecase(), telko_inbound.usecase()]
    return {u.id: u for u in faelle}
