"""
Geführter Modus: Der Gesprächsleitfaden läuft als Zustandsautomat im Code.

Das Sprachmodell hat nur noch zwei eng begrenzte Aufgaben:
  1. Extraktion:   Was hat der Kunde gerade gesagt? (JSON nach Schema, Temperatur 0)
  2. Formulierung: Den vom Code bestimmten nächsten Satz natürlich aussprechen.
Welche Frage als Nächstes kommt, ob der Lead qualifiziert ist, wann Termine angeboten,
gebucht oder das Gespräch beendet wird, entscheidet der Code. Fällt die Formulierung
durch die Guardrails oder fällt das LLM aus, wird der Vorlagensatz gesprochen.

Jeder Turn braucht höchstens zwei LLM-Aufrufe und bringt das Gespräch garantiert
einen Schritt weiter oder beendet es. Das macht kleine Modelle brauchbar und den UAT schnell.

Start:  python agent.py --guided           -> Tastatur spielt den Kunden
        python simulate.py --mode guided   -> UAT (Standard)
"""
import json
import re
import time
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

import agent
from agent import AGENT_NAME, Tools, guardrail_check, log

MAX_TURNS = 10          # jeder Turn bringt den Leitfaden weiter, mehr sind nie nötig
MAX_ASKS = 2            # so oft wird eine unbeantwortete Frage wiederholt
FORMULATE = True        # False = nur Vorlagensätze (schnellster, vollständig deterministischer Betrieb)

MUST = ["eigentuemer", "gebaeudetyp"]                   # ohne diese keine Qualifizierung
SHOULD = ["dachflaeche_m2", "jahresverbrauch_kwh"]       # Soll-Angaben: fehlen sie, wird trotzdem gebucht
FIELDS = MUST + SHOULD

QUESTIONS = {
    "eigentuemer": "Sind Sie Eigentümer der Immobilie?",
    "gebaeudetyp": "Um welche Art von Gebäude handelt es sich, zum Beispiel Einfamilienhaus, "
                   "Doppelhaushälfte oder Reihenhaus?",
    "dachflaeche_m2": "Wie groß ist ungefähr Ihre Dachfläche in Quadratmetern?",
    "jahresverbrauch_kwh": "Und wie hoch ist Ihr Stromverbrauch pro Jahr, ungefähr?",
}
PRICE_LINE = "Konkrete Preise und Förderungen klärt die Fachberatung im Termin, das hängt stark vom Dach ab."
DISQUALIFY = {
    "eigentuemer": "Verstehe. Eine Anlage können wir nur für Eigentümer planen, deshalb kann ich Ihnen "
                   "leider kein Angebot machen. Vielen Dank für Ihre Zeit und einen schönen Tag!",
    "gebaeudetyp": "Verstehe. Bei einem Mehrfamilienhaus braucht es erst einen Beschluss der "
                   "Eigentümergemeinschaft, das können wir in diesem Rahmen leider nicht anbieten. "
                   "Vielen Dank für Ihre Zeit und einen schönen Tag!",
}
CALLBACK = "Kein Problem, dann melde ich mich zu einem besseren Zeitpunkt noch einmal. Einen schönen Tag!"


BUILDING_ALIASES = {"efh": "einfamilienhaus", "einzelhaus": "einfamilienhaus", "haus": "einfamilienhaus",
                    "dhh": "doppelhaushaelfte", "doppelhaus": "doppelhaushaelfte",
                    "rh": "reihenhaus", "reihenendhaus": "reihenhaus", "reihenmittelhaus": "reihenhaus",
                    "mfh": "mehrfamilienhaus", "wohnung": "mehrfamilienhaus", "eigentumswohnung": "mehrfamilienhaus"}
BUILDINGS = {"einfamilienhaus", "doppelhaushaelfte", "reihenhaus", "mehrfamilienhaus", "gewerbe", "sonstiges"}


def _as_bool(v):
    """Kleine Modelle liefern "Ja", "true" oder 1 statt true. Unklares wird None."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("ja", "yes", "true", "wahr", "1"):
            return True
        if t in ("nein", "no", "false", "falsch", "0"):
            return False
    return None


def _as_number(v, lo, hi):
    """Zahl im plausiblen Bereich, sonst None. Modelle schreiben für Unbekanntes gern 0."""
    try:
        x = float(str(v).replace(",", ".").split()[0]) if isinstance(v, str) else float(v)
    except (TypeError, ValueError, IndexError):
        return None
    return x if lo <= x <= hi else None


class Antwort(BaseModel):
    """Was der Kunde in seiner letzten Aussage mitgeteilt hat.

    Dreiwertige Felder statt true/false/null: Kleine Modelle wählen unter Schema-Zwang
    bei einem Boolean lieber false als null. "unbekannt" als eigene Option ist eindeutig.
    Jedes Feld wird einzeln normalisiert; ein unbrauchbarer Wert macht nur dieses Feld unbekannt.
    """
    eigentuemer: Literal["ja", "nein", "unbekannt"] = Field(
        "unbekannt", description="ja wenn dem Kunden das Haus gehört, nein wenn er zur Miete wohnt, sonst unbekannt")
    gebaeudetyp: Literal["einfamilienhaus", "doppelhaushaelfte", "reihenhaus", "mehrfamilienhaus",
                         "gewerbe", "sonstiges", "unbekannt"] = "unbekannt"
    dachflaeche_m2: Optional[float] = Field(None, description="nur wenn der Kunde eine Zahl nennt, sonst null")
    jahresverbrauch_kwh: Optional[int] = Field(None, description="nur wenn der Kunde eine Zahl nennt, sonst null")
    weiss_nicht: bool = Field(False, description="Kunde sagt, dass er die gefragte Angabe nicht kennt")
    termin_wahl: Literal["erster", "zweiter", "keiner", "unbekannt"] = Field(
        "unbekannt", description="nur wenn Termine angeboten wurden: erster auch bei bloßer Zusage, "
                                 "keiner wenn beide nicht passen, sonst unbekannt")
    fragt_nach_preis: bool = False
    keine_zeit: bool = Field(False, description="Kunde hat gerade keine Zeit und will später zurückgerufen werden")
    falsche_person: bool = Field(False, description="Am Telefon ist nicht die angerufene Person")
    kein_interesse: bool = Field(False, description="Kunde will keine Solaranlage oder keinen Kontakt mehr")

    @field_validator("eigentuemer", mode="before")
    @classmethod
    def _owner(cls, v):
        if isinstance(v, str) and v.strip().lower() in ("ja", "nein", "unbekannt"):
            return v.strip().lower()
        b = _as_bool(v)
        return "unbekannt" if b is None else ("ja" if b else "nein")

    @field_validator("weiss_nicht", "fragt_nach_preis", "keine_zeit", "falsche_person", "kein_interesse",
                     mode="before")
    @classmethod
    def _flag(cls, v):
        return bool(_as_bool(v))

    @field_validator("gebaeudetyp", mode="before")
    @classmethod
    def _building(cls, v):
        if not isinstance(v, str):
            return "unbekannt"
        t = v.strip().lower().replace("ä", "ae").replace("-", "").replace(" ", "")
        t = BUILDING_ALIASES.get(t, t)
        return t if t in BUILDINGS else "unbekannt"

    @field_validator("dachflaeche_m2", mode="before")
    @classmethod
    def _roof(cls, v):
        return _as_number(v, 5, 2000)

    @field_validator("jahresverbrauch_kwh", mode="before")
    @classmethod
    def _consumption(cls, v):
        x = _as_number(v, 500, 100_000)
        return int(x) if x is not None else None

    @field_validator("termin_wahl", mode="before")
    @classmethod
    def _choice(cls, v):
        if not isinstance(v, str):
            return "unbekannt"
        t = v.strip().lower()
        if t in ("erster", "erste", "ersten", "1", "a", "ja"):
            return "erster"
        if t in ("zweiter", "zweite", "zweiten", "2", "b"):
            return "zweiter"
        if t in ("keiner", "keine", "keinen", "nein", "none"):
            return "keiner"
        return "unbekannt"

    def facts(self) -> dict:
        """Die vier Qualifizierungsfelder als CRM-Werte; unbekannt wird None."""
        return {"eigentuemer": {"ja": True, "nein": False}.get(self.eigentuemer),
                "gebaeudetyp": None if self.gebaeudetyp == "unbekannt" else self.gebaeudetyp,
                "dachflaeche_m2": self.dachflaeche_m2,
                "jahresverbrauch_kwh": self.jahresverbrauch_kwh}


# ---------------------------------------------------------------- Deterministische Antwortdeutung
# Für die gerade gestellte Frage reicht meist eine Regel. Sie entscheidet vor dem LLM und bleibt
# auch dann funktionsfähig, wenn das Modell ausfällt oder das Schema ignoriert.
YES_WORDS = re.compile(r"\b(ja|jo|jep|jup|klar|genau|stimmt|richtig|natürlich|sicher|selbstverständlich|"
                       r"eigentümer|eigentuemer|besitzer|gehört (mir|uns)|ist meins|mein haus|unser haus)\b", re.IGNORECASE)
NO_WORDS = re.compile(r"\b(nein|nee|nö|nicht|kein|keine|keiner)\b", re.IGNORECASE)
DONT_KNOW = re.compile(r"weiß (ich )?nicht|keine ahnung|kann ich nicht sagen|wüsste ich nicht|normal halt|"
                       r"müsste ich nachschauen|schwer zu sagen", re.IGNORECASE)
BUILDING_KEYWORDS = [
    (re.compile(r"doppelhaus|dhh|haushälfte|haushaelfte", re.IGNORECASE), "doppelhaushaelfte"),
    (re.compile(r"reihen|rh\b", re.IGNORECASE), "reihenhaus"),
    (re.compile(r"mehrfamilien|mfh|wohnung|partei|etage|wohnblock|mietshaus|apartment", re.IGNORECASE), "mehrfamilienhaus"),
    (re.compile(r"gewerbe|firma|betrieb|halle|büro|buero|laden", re.IGNORECASE), "gewerbe"),
    (re.compile(r"einfamilien|efh|freistehend|einzelhaus|bungalow|villa|\bhaus\b", re.IGNORECASE), "einfamilienhaus"),
]
NUMBER = re.compile(r"\d{1,3}(?:[.\s]\d{3})+|\d+(?:,\d+)?")
ORDINAL_FIRST = re.compile(r"\b(erste[rn]?|ersten|zuerst|frühere[rn]?|1\.?)\b", re.IGNORECASE)
ORDINAL_SECOND = re.compile(r"\b(zweite[rn]?|zweiten|spätere[rn]?|andere[rn]?|2\.?)\b", re.IGNORECASE)
NEITHER = re.compile(r"beide nicht|keiner (von beiden|passt)|passt (mir )?(beides|beide) nicht|geht (beides|beide) nicht|"
                     r"kein(er|e)? davon|weder", re.IGNORECASE)
WEEKDAYS = {"Mo": "montag", "Di": "dienstag", "Mi": "mittwoch", "Do": "donnerstag", "Fr": "freitag",
            "Sa": "samstag", "So": "sonntag"}


def parse_owner(text: str):
    t = text.lower()
    if DONT_KNOW.search(t) or re.search(r"nicht sicher|unsicher|wie meinen", t):
        return None
    if re.search(r"miet|pacht", t):
        return False
    yes, no = bool(YES_WORDS.search(t)), bool(NO_WORDS.search(t))
    if yes and not no:
        return True
    if no and not yes:
        return False
    return None


def parse_building(text: str):
    for pattern, value in BUILDING_KEYWORDS:
        if pattern.search(text):
            return value
    return None


def parse_number(text: str, lo: float, hi: float):
    for m in NUMBER.finditer(text):
        x = _as_number(m.group(0).replace(" ", "").replace(".", "") if "." in m.group(0) and "," not in m.group(0)
                       else m.group(0), lo, hi)
        if x is not None:
            return x
    return None


def parse_choice(text: str, offered: list):
    """Welchen angebotenen Termin meint der Kunde? Rückgabe: erster | zweiter | keiner | None."""
    if NEITHER.search(text):
        return "keiner"
    scores = []
    for _, zeit in offered:                       # Zeittext wie "Di 07.10. 09:00 Uhr"
        parts = zeit.split()
        score = 0
        if parts and WEEKDAYS.get(parts[0], "") and WEEKDAYS[parts[0]] in text.lower():
            score += 1
        if len(parts) > 1 and parts[1].rstrip(".").lstrip("0").replace(".0", ".") in text.replace(" ", ""):
            score += 1
        if len(parts) > 2 and re.search(rf"\b{int(parts[2].split(':')[0])}(:00| uhr|\.00)", text.lower()):
            score += 1
        scores.append(score)
    if scores and max(scores) > 0 and scores.count(max(scores)) == 1:
        return "erster" if scores.index(max(scores)) == 0 else "zweiter"
    if ORDINAL_SECOND.search(text) and not ORDINAL_FIRST.search(text):
        return "zweiter"
    if ORDINAL_FIRST.search(text):
        return "erster"
    if YES_WORDS.search(text) or re.search(r"passt|gern|nehme|okay|ok\b|in ordnung|machen wir", text, re.IGNORECASE):
        return "erster" if not NO_WORDS.search(text) else None
    return None


def parse_pending(pending: Optional[str], text: str, offered: list) -> dict:
    """Deterministische Deutung der Antwort auf die gerade gestellte Frage."""
    out = {}
    if offered:
        choice = parse_choice(text, offered)
        if choice:
            out["termin_wahl"] = choice
        return out
    if pending in SHOULD and DONT_KNOW.search(text):
        out["weiss_nicht"] = True
    if pending == "eigentuemer":
        value = parse_owner(text)
        if value is not None:
            out["eigentuemer"] = value
    elif pending == "gebaeudetyp":
        value = parse_building(text)
        if value:
            out["gebaeudetyp"] = value
    elif pending == "dachflaeche_m2":
        value = parse_number(text, 5, 2000)
        if value is not None:
            out["dachflaeche_m2"] = value
    elif pending == "jahresverbrauch_kwh":
        value = parse_number(text, 500, 100_000)
        if value is not None:
            out["jahresverbrauch_kwh"] = int(value)
    return out


# Jev meldet "keine Zeit" auch bei Terminfragen wie "Wann hätten Sie denn Zeit?". Ohne ein Wort,
# das einen späteren Anruf erbittet, zählt das Urteil nicht; in der Terminphase nie.
NO_TIME_WORDS = re.compile(r"keine zeit|gerade (schlecht|nicht|ungünstig)|später|morgen|nachmittag|abend|im auto|"
                           r"unterwegs|rufen sie|ruf(en)? .*zurück|zurückrufen|melden sie sich|anderer zeitpunkt|"
                           r"andermal|nicht jetzt|in einer stunde|nächste woche", re.IGNORECASE)


def extraction_schema() -> dict:
    """JSON-Schema für die strukturierte Ausgabe: jedes Feld ist Pflicht, Unbekanntes wird null.

    Ohne `required` ist das leere Objekt {} schemakonform, und genau das liefern kleine Modelle
    unter Grammatik-Zwang bevorzugt. Mit `required` muss das Modell jedes Feld ausfüllen.
    """
    schema = Antwort.model_json_schema()
    schema["required"] = list(schema["properties"])
    return schema


# Plausibilitätsschutz: Ein Wert, nach dem nicht gefragt wurde, zählt nur, wenn die Aussage das
# Thema erkennbar berührt. Sonst macht ein "Ja, hallo" den Kunden zum Eigentümer oder ein Zögern
# zum Mieter. Disqualifizieren ist der teuerste Fehler, ein echter Interessent geht verloren.
PLAUSIBLE = {
    "eigentuemer": re.compile(r"miet|eigent|besitz|gehör|pacht|mein haus|unser haus", re.IGNORECASE),
    "gebaeudetyp": re.compile(r"haus|hälfte|haelfte|wohnung|partei|mfh|efh|etage|stock|apartment|gewerbe|"
                              r"betrieb|firma|halle|bungalow|villa", re.IGNORECASE),
    "dachflaeche_m2": re.compile(r"\d"),
    "jahresverbrauch_kwh": re.compile(r"\d"),
}


EXTRACTION_SCHEMA = extraction_schema()


def extract(user: str, question: str, offered: list) -> Optional[Antwort]:
    """Strukturierte Extraktion aus der Kundenaussage. None, wenn das LLM nicht antwortet."""
    context = f'Der Agent hat gefragt: "{question}"' if question else "Der Agent hat sich vorgestellt."
    if offered:
        context += " Angebotene Termine: " + "; ".join(f"{i + 1}) {z}" for i, z in enumerate(offered))
    resp = agent.safe_chat(model=agent.MODEL, format=EXTRACTION_SCHEMA, options={"temperature": 0},
                     messages=[{"role": "system", "content":
                                "Du liest die Antwort eines Kunden am Telefon. " + context +
                                " Fülle jedes Feld. Was der Kunde ausdrücklich sagt, trägst du ein; zu allem, "
                                "was er nicht erwähnt, schreibst du unbekannt beziehungsweise null. Ein bloßes "
                                "Ja oder eine Begrüßung sagt nichts über Eigentum oder Gebäude. Die Felder "
                                "keine_zeit, falsche_person und kein_interesse sind nur true, wenn der Kunde "
                                "das ausdrücklich sagt. Antworte nur mit JSON."},
                               {"role": "user", "content": user}])
    if resp is None:
        return None
    try:
        return Antwort.model_validate_json(resp.message.content or "{}")
    except Exception as e:                      # kein gültiges JSON: wie "nichts verstanden" behandeln
        log(f"   [EXTRAKTION] unbrauchbar: {str(e)[:80]}")
        return Antwort()


def formulate(template: str, user: str, tools: Tools, must_contain: tuple = ()) -> str:
    """Lässt das LLM den Vorlagensatz natürlich aussprechen. Bei jedem Zweifel gilt die Vorlage."""
    if not FORMULATE:
        return template
    resp = agent.safe_chat(model=agent.MODEL, options={"temperature": 0.4, "num_predict": 120},
                     messages=[{"role": "system", "content":
                                f"Du bist {AGENT_NAME}, Telefon-Agentin von {agent.FIRMA}. Formuliere den "
                                "folgenden Satz natürlich und freundlich, wie am Telefon, in höchstens zwei "
                                "kurzen Sätzen. Ändere den Inhalt nicht: keine zusätzlichen Fragen, keine "
                                "Preise, keine Zahlen, keine Termine, die nicht im Satz stehen. Siezen. "
                                "Kein Markdown. Antworte nur mit dem Satz."},
                               {"role": "user", "content": f'Der Kunde sagte: "{user}"\nSag jetzt: {template}'}])
    text = (resp.message.content or "").strip() if resp else ""
    if not text or guardrail_check(text) or agent.fact_check(text, tools) \
            or not all(x in text for x in must_contain) or len(text.split()) > 2 * len(template.split()) + 15:
        log("   [FORMULIERUNG] verworfen, Vorlage gesprochen")
        return template
    return text


class Leitfaden:
    """Zustand des Gesprächs: Was ist bekannt, was wurde gefragt, was wurde angeboten?"""

    def __init__(self, con, tools: Tools, lead_id: int):
        self.con, self.tools, self.lead_id = con, tools, lead_id
        self.asked = {f: 0 for f in FIELDS}
        self.unknown = set()
        self.pending: Optional[str] = None       # zuletzt gestellte Frage (Feldname)
        self.offered: list = []                  # angebotene Termine (slot_id, Zeittext)
        self.offer_rounds = 0

    def known(self) -> dict:
        row = self.con.execute(f"SELECT {', '.join(FIELDS)} FROM leads WHERE id = ?", [self.lead_id]).fetchone()
        return dict(zip(FIELDS, row))

    def store(self, facts: dict):
        """Jedes Feld einzeln durch die Validierung; unbrauchbare Werte fallen weg."""
        for key, value in facts.items():
            if value is None:
                continue
            result = self.tools.run("update_lead", {"status": "in_qualifizierung", key: value})
            if not result.get("ok"):
                log(f"   [LEITFADEN] {key}={value!r} verworfen: {result}")

    def next_question(self) -> Optional[str]:
        known = self.known()
        for f in FIELDS:
            if known[f] is None and f not in self.unknown:
                return f
        return None

    def step(self, user: str, antwort: Optional[Antwort]) -> tuple:
        """Ein Kundenturn. Rückgabe: (vorlagensatz, pflichtbestandteile, gespräch_zu_ende)."""
        tools = self.tools
        prefix = ""
        if antwort is not None:
            if antwort.kein_interesse:
                tools.run("update_lead", {"status": "opt_out", "notiz": "Opt-out (extraktion)"})
                tools.ended = "opt_out"
                return "Verstanden, ich trage Sie aus und wünsche Ihnen einen schönen Tag.", (), True
            if antwort.falsche_person:
                tools.ended = "falsche_person"
                return ("Oh, Entschuldigung für die Störung. Dann versuche ich es ein anderes Mal. "
                        "Schönen Tag noch!"), (), True
            if antwort.keine_zeit:
                tools.run("update_lead", {"status": "rueckruf", "notiz": f"Rückrufwunsch: {user[:200]}"})
                tools.ended = "rueckruf_vereinbart"
                return CALLBACK, (), True
            facts = antwort.facts()
            for f, value in list(facts.items()):
                if value is not None and f != self.pending and not PLAUSIBLE[f].search(user):
                    log(f"   [LEITFADEN] {f}={value!r} ohne Bezug in der Aussage: ignoriert")
                    facts[f] = None
            self.store(facts)
            if antwort.weiss_nicht and self.pending in SHOULD:
                self.unknown.add(self.pending)
            if antwort.fragt_nach_preis:
                prefix = PRICE_LINE + " "
        elif self.pending:                                   # Extraktion fehlgeschlagen: Frage wiederholen
            self.asked[self.pending] += 1

        # --- Terminphase
        if self.offered:
            wahl = antwort.termin_wahl if antwort else None
            if wahl in ("erster", "zweiter"):
                slot_id, zeit = self.offered[0 if wahl == "erster" else 1]
                result = tools.run("book_appointment", {"slot_id": slot_id})
                if result.get("ok"):
                    tools.run("end_call", {"grund": "termin_gebucht"})
                    return (f"Perfekt, ich habe den Termin am {result['termin']} für Sie eingetragen. "
                            "Ein Fachberater meldet sich dann bei Ihnen. Vielen Dank und auf Wiederhören!",
                            (result["termin"],), True)
            if wahl == "keiner" or self.offer_rounds >= 2:
                tools.run("update_lead", {"status": "rueckruf", "notiz": "Kein angebotener Termin passt"})
                tools.run("end_call", {"grund": "rueckruf_vereinbart"})
                return CALLBACK, (), True
            self.offer_rounds += 1
            return prefix + self._offer_text(), tuple(z for _, z in self.offered), False

        # --- Disqualifizierung, hart im Code
        known = self.known()
        if known["eigentuemer"] is False or known["gebaeudetyp"] == "mehrfamilienhaus":
            reason = "eigentuemer" if known["eigentuemer"] is False else "gebaeudetyp"
            tools.run("update_lead", {"status": "disqualifiziert"})
            tools.run("end_call", {"grund": "disqualifiziert"})
            return DISQUALIFY[reason], (), True

        # --- Nächste Frage
        field = self.next_question()
        while field and self.asked[field] >= MAX_ASKS:         # zweimal gefragt, keine Antwort
            if field in MUST:
                tools.run("update_lead", {"status": "rueckruf", "notiz": f"{field} unklar"})
                tools.run("end_call", {"grund": "rueckruf_vereinbart"})
                return CALLBACK, (), True
            self.unknown.add(field)
            field = self.next_question()
        if field:
            self.asked[field] += 1
            self.pending = field
            return prefix + QUESTIONS[field], (), False

        # --- Qualifiziert: Termine anbieten
        self.pending = None
        result = tools.run("update_lead", {"status": "qualifiziert"})
        if not result.get("ok"):                                  # sollte nach den Prüfungen oben nie passieren
            tools.run("update_lead", {"status": "rueckruf", "notiz": "Qualifizierung nicht abschließbar"})
            tools.run("end_call", {"grund": "rueckruf_vereinbart"})
            return CALLBACK, (), True
        slots = tools.run("check_slots", {})
        self.offered = [(s["slot_id"], s["zeit"]) for s in slots.get("freie_termine", [])]
        if not self.offered:
            tools.run("update_lead", {"status": "rueckruf", "notiz": "Keine freien Termine"})
            tools.run("end_call", {"grund": "rueckruf_vereinbart"})
            return CALLBACK, (), True
        self.offer_rounds = 1
        return prefix + self._offer_text(), tuple(z for _, z in self.offered), False

    def _offer_text(self) -> str:
        zeiten = [z for _, z in self.offered]
        if len(zeiten) == 1:
            return f"Ich hätte einen Beratungstermin am {zeiten[0]}. Passt Ihnen das?"
        return (f"Dann hätte ich zwei Termine für ein kostenloses Beratungsgespräch: {zeiten[0]} "
                f"oder {zeiten[1]}. Welcher passt Ihnen besser?")


def run_guided_call(con, lead_id: int, get_input=None) -> dict:
    """Geführter Modus. Gleiche Schnittstelle und gleiches Ergebnisformat wie agent.run_call."""
    get_input = get_input or (lambda _msgs: input("Kunde: "))
    lead = agent.load_lead(con, lead_id)
    if lead is None:
        log(f"Lead {lead_id} nicht gefunden."); return {}
    tools, flags = Tools(con, lead_id), []
    plan = Leitfaden(con, tools, lead_id)
    greeting = agent.greeting_text()
    messages = [{"role": "system", "content": agent.system_prompt(lead)},
                {"role": "user", "content": "(Anruf wird angenommen) Ja, hallo?"},
                {"role": "assistant", "content": greeting}]
    start, turns, latencies, llm_failures = datetime.now(), 0, [], 0

    log(f"\n=== Anruf bei {lead['name']} ({lead['telefon']}) — geführter Modus, '/q' zum Auflegen ===\n")
    log("Kunde: Ja, hallo?")
    log(f"{AGENT_NAME}: {greeting}   (fest)\n")
    while True:
        user = get_input(messages).strip()
        if get_input.__name__ != "<lambda>":
            log(f"Kunde: {user}")
        if user.lower() in ("/q", "/quit") or turns >= MAX_TURNS:
            tools.ended = tools.ended or ("timeout" if turns >= MAX_TURNS else "aufgelegt")
            break
        messages.append({"role": "user", "content": user})
        turns += 1
        t0 = time.time()

        intent, source = agent.route_intent(user, agent.last_assistant_text(messages[:-1]))
        parsed = parse_pending(plan.pending, user, plan.offered)
        if intent == "keine_zeit" and (plan.offered or not NO_TIME_WORDS.search(user)):
            log("   [LEITFADEN] 'keine Zeit' ohne Rückrufwunsch in der Aussage: ignoriert")
            intent, source = "antwortet", source
        if intent == "opt_out" and plan.pending in (None, "eigentuemer") and re.search(r"miet", user, re.IGNORECASE):
            intent = "antwortet"                    # "Ich wohne zur Miete, also kein Interesse": disqualifizieren
            parsed["eigentuemer"] = False
        if intent != "antwortet":
            reply = agent.end_deterministically(tools, intent, source, user)
            flags.append(f"entscheidung_{source}")
        else:
            question = QUESTIONS.get(plan.pending, "") if plan.pending else ""
            antwort = extract(user, question, [z for _, z in plan.offered])
            if antwort is None and parsed:          # Regeln haben verstanden, das Modell nicht
                antwort = Antwort()
            if antwort is not None:
                for key, value in parsed.items():   # die Regel für die gestellte Frage hat Vorrang
                    setattr(antwort, key, value if key != "eigentuemer" else ("ja" if value else "nein"))
                if parsed:
                    flags.append("regel_gedeutet")
            if antwort is not None and source == "jev":
                # Jev hat die Absicht bereits als "antwortet" eingestuft: Jev ist der Spezialist,
                # die Extraktion darf das nicht überstimmen
                antwort.keine_zeit = antwort.falsche_person = antwort.kein_interesse = False
            if antwort is None:
                llm_failures += 1
                flags.append("llm_fehler")
                if llm_failures >= 2:                         # ohne Extraktion kein Gespräch: sauber beenden
                    tools.run("update_lead", {"status": "rueckruf", "notiz": "LLM nicht erreichbar"})
                    tools.ended = "rueckruf_vereinbart"
                    reply = CALLBACK
                    messages.append({"role": "assistant", "content": reply})
                    latencies.append(time.time() - t0)
                    log(f"{AGENT_NAME}: {reply}   (fest)\n")
                    break
            else:
                gefunden = antwort.model_dump(exclude_defaults=True)
                log(f"   [EXTRAKTION] {gefunden}")
                # Extraktion ins Transkript, damit jeder Fehlschlag in eval_results.json nachvollziehbar ist
                messages.append({"role": "tool", "tool_name": "extraktion",
                                 "content": json.dumps({"extraktion": gefunden}, ensure_ascii=False)})
                if antwort.kein_interesse or antwort.falsche_person or antwort.keine_zeit:
                    flags.append("entscheidung_extraktion")
            template, must_contain, _ = plan.step(user, antwort)
            reply = formulate(template, user, tools, must_contain)
            if reply == template:
                flags.append("vorlage_gesprochen")

        latencies.append(time.time() - t0)
        messages.append({"role": "assistant", "content": reply})
        log(f"{AGENT_NAME}: {reply}   ({latencies[-1]:.1f}s)\n")
        if tools.ended:
            break

    return agent.finish_call(con, lead_id, tools, flags, messages, turns, latencies, start,
                             extract=False, extra={"modus": "gefuehrt"})
