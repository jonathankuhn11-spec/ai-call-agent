"""
Jev (TypeSafe AI) als schneller Entscheider neben dem LLM.
Jev erzeugt keinen Text, sondern typisierte Entscheidungen mit Wahrscheinlichkeiten.
Das LLM formuliert, Jev entscheidet, der Code handelt.

API-Key: Datei .env im Projektordner mit der Zeile  TYPESAFE_API_KEY=dein_key
(.env steht in .gitignore und landet nie auf GitHub)
"""
import os
import time

import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT = 3.0          # Sekunden; der Dienst läuft in den USA, Netzlatenz einplanen
STATS = {"calls": 0, "fehler": 0, "latenzen": []}


def _load_key() -> str | None:
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    if os.path.exists(".env"):
        with open(".env", encoding="utf-8-sig") as f:
            for line in f:
                if line.strip().startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


_KEY = _load_key()


def available() -> bool:
    return bool(_KEY)


def ask(state, questions: dict):
    """Ein Aufruf, alle Fragen parallel. Gibt answers zurück oder None bei Fehler."""
    if not _KEY:
        return None
    payload = {"model": MODEL, "state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"}
    for attempt in range(2):
        t0 = time.time()
        try:
            r = httpx.post(API_URL, json=payload, headers=headers, timeout=TIMEOUT)
            if r.status_code in (429, 529) and attempt == 0:   # Rate-Limit / überlastet: kurz warten
                time.sleep(0.5)
                continue
            r.raise_for_status()
            STATS["calls"] += 1
            STATS["latenzen"].append(time.time() - t0)
            return r.json()["answers"]
        except Exception as e:
            if attempt == 1 or not isinstance(e, httpx.TimeoutException):
                STATS["fehler"] += 1
                print(f"\n   [JEV-FEHLER] {type(e).__name__}: {str(e)[:80]} -> Fallback auf Regeln", flush=True)
                return None
    STATS["fehler"] += 1
    return None


# ---------------------------------------------------------------- Fragenkatalog
# Alle Fragen und Schwellen an einer Stelle: das ist der Teil, den ein Mensch reviewen muss.

INTENT_THRESHOLD = 0.70     # darunter entscheidet nicht Jev, sondern Regeln + LLM
CLAIM_THRESHOLD = 0.75

INTENT_QUESTIONS = {
    "absicht": {
        "type": "choice",
        "instructions": ("Ein Vertriebsagent ruft wegen einer Anfrage zu einer Solaranlage an. "
                         "Was drückt die Person mit `kundenaussage` als Antwort auf `letzte_agentenfrage` aus?"),
        "criteria": {
            "antwortet": ("Beantwortet die Frage oder macht im Gespräch mit, auch mit Rückfragen, Skepsis oder "
                          "Preisfragen. Beispiele: 'Ja, ich bin Eigentümer', 'Was kostet das?', "
                          "'Ich hab eure Werbung gesehen'"),
            "opt_out": ("Will keinen Kontakt: kein Interesse, doch keine Solaranlage, nicht mehr anrufen, "
                        "Daten löschen oder beschimpft den Anrufer"),
            "keine_zeit": ("Lehnt nicht ab, hat aber gerade keine Zeit und möchte später zurückgerufen werden. "
                           "Beispiele: 'Bin gerade im Auto', 'Rufen Sie morgen an'"),
            "falsche_person": ("Ist nicht die angerufene Person, z. B. Partner, Kind oder Kollege, "
                               "oder weiß nichts von einer Anfrage"),
            "sonstiges": "Passt zu keiner der anderen Optionen",
        },
    },
}

REPLY_QUESTIONS = {
    "behauptet_buchung": {
        "type": "noul",
        "instructions": "Behauptet `antwort`, dass ein Termin bereits gebucht, eingetragen oder fest vereinbart ist?",
        "criteria": {"true": "Termin wird als erledigt oder fest dargestellt",
                     "false": "Termin wird nur vorgeschlagen, erfragt oder gar nicht erwähnt"},
    },
    "nennt_preis": {
        "type": "noul",
        "instructions": ("Nennt `antwort` einen konkreten Preis, Kosten, eine Förderhöhe, "
                         "eine Rendite oder eine Amortisationszeit?"),
    },
    "nennt_termin": {
        "type": "noul",
        "instructions": "Nennt `antwort` einen konkreten Termin mit Wochentag, Datum oder Uhrzeit?",
    },
}


# Extraktion im geführten Modus. Jev liefert Kategorien und Wahrscheinlichkeiten, keine Zahlen
# aus dem Text: Dachfläche und Verbrauch bleiben bei den Regeln in guided.parse_number.
EXTRACTION_THRESHOLD = 0.70

EXTRACTION_QUESTIONS = {
    "eigentuemer": {
        "type": "choice",
        "instructions": ("Ein Vertriebsagent fragt am Telefon zu einer Solaranlage, `letzte_agentenfrage` war die "
                         "Frage. Sagt die Person mit `kundenaussage`, ob ihr die Immobilie gehört?"),
        "criteria": {
            "ja": "Die Person ist Eigentümer oder das Haus gehört ihr bzw. der Familie",
            "nein": "Die Person wohnt zur Miete, pachtet oder das Haus gehört jemand anderem",
            "unbekannt": "Die Aussage sagt nichts über Eigentum, auch ein bloßes 'Hallo' oder Rückfragen",
        },
    },
    "gebaeudetyp": {
        "type": "choice",
        "instructions": "Welches Gebäude beschreibt die Person in `kundenaussage`?",
        "criteria": {
            "einfamilienhaus": "Freistehendes Einfamilienhaus, Bungalow, Villa",
            "doppelhaushaelfte": "Doppelhaushälfte",
            "reihenhaus": "Reihenhaus, Reihenend- oder Reihenmittelhaus",
            "mehrfamilienhaus": "Mehrfamilienhaus, Wohnung, Eigentumswohnung, Haus mit mehreren Parteien",
            "gewerbe": "Gewerbeimmobilie, Halle, Betrieb, Büro",
            "unbekannt": "Die Aussage nennt kein Gebäude",
        },
    },
    "weiss_nicht": {
        "type": "noul",
        "instructions": "Sagt die Person in `kundenaussage`, dass sie die Antwort auf `letzte_agentenfrage` nicht kennt?",
    },
    "fragt_nach_preis": {
        "type": "noul",
        "instructions": "Fragt die Person in `kundenaussage` nach Preis, Kosten, Förderung oder Rendite?",
    },
}

TERMIN_QUESTION = {
    "type": "choice",
    "instructions": ("Der Agent hat zwei Beratungstermine angeboten, `termin_1` und `termin_2`. "
                     "Welchen wählt die Person mit `kundenaussage`?"),
    "criteria": {
        "erster": "Den ersten Termin, oder sie sagt allgemein zu, ohne einen bestimmten zu nennen",
        "zweiter": "Den zweiten Termin",
        "keiner": "Keiner der beiden passt",
        "unbekannt": "Die Aussage wählt nichts, etwa eine Rückfrage",
    },
}


def extract_facts(kundenaussage: str, letzte_agentenfrage: str, termine: list):
    """Kategoriale Fakten als dict für guided.Antwort oder None, wenn Jev nicht antwortet.

    Unsichere Urteile unter der Schwelle werden 'unbekannt' bzw. False: Der Leitfaden fragt dann
    lieber noch einmal nach, als einen Lead auf einen schwachen Hinweis hin zu disqualifizieren.
    """
    state = {"kundenaussage": kundenaussage, "letzte_agentenfrage": letzte_agentenfrage}
    if termine:                     # Terminphase: Qualifizierung ist abgeschlossen, nur die Wahl zählt
        state.update({f"termin_{i + 1}": z for i, z in enumerate(termine[:2])})
        questions = {"termin_wahl": TERMIN_QUESTION,
                     "fragt_nach_preis": EXTRACTION_QUESTIONS["fragt_nach_preis"]}
    else:
        questions = dict(EXTRACTION_QUESTIONS)
    answers = ask(state, questions)
    if not answers:
        return None
    out = {}
    for key, a in answers.items():
        if "choice" in a:
            out[key] = a["choice"] if a.get("confidence", 0.0) >= EXTRACTION_THRESHOLD else "unbekannt"
        elif "noul" in a:
            out[key] = a["noul"] >= EXTRACTION_THRESHOLD
    return out


def classify_intent(kundenaussage: str, letzte_agentenfrage: str):
    """-> (label, confidence) oder None, wenn Jev nicht verfügbar oder unsicher."""
    answers = ask({"kundenaussage": kundenaussage, "letzte_agentenfrage": letzte_agentenfrage},
                  INTENT_QUESTIONS)
    if not answers:
        return None
    a = answers["absicht"]
    return a["choice"], a.get("confidence", 0.0)


def check_reply(antwort: str):
    """-> dict mit Wahrscheinlichkeiten oder None."""
    answers = ask({"antwort": antwort}, REPLY_QUESTIONS)
    if not answers:
        return None
    return {k: v["noul"] for k, v in answers.items()}
