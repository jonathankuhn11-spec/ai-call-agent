"""Transkript-Review: Regeln, Quellen (calls-Tabelle, telli List Calls), Reviewliste."""

import httpx

from integrations import review
from integrations.crm import LocalCRM
from integrations.review import Transkriptpruefung

BEGRUESSUNG = "Guten Tag, hier ist Lena, die digitale KI-Assistentin von SonnenWerk Energie. Passt es gerade kurz?"


def t(*paare):
    return [{"role": r, "content": c} for r, c in paare]


def regeln(befunde):
    return [b.regel for b in befunde]


def test_clean_transcript_has_no_findings():
    p = Transkriptpruefung()
    tr = t(("assistant", BEGRUESSUNG), ("user", "Ja."), ("assistant", "Sind Sie Eigentümer der Immobilie?"),
           ("user", "Ja."), ("tool", "check_slots {}"),
           ("assistant", "Dann hätte ich Dienstag 09:00 Uhr oder Mittwoch 11:00 Uhr. Welcher passt Ihnen besser?"),
           ("user", "Dienstag."), ("tool", "book_appointment {'slot_id': 1}"),
           ("assistant", "Perfekt, der Termin ist eingetragen. Vielen Dank und auf Wiederhören!"))
    assert p.pruefen(tr, "termin_gebucht", True, ["plattform:telli"]) == []


def test_s1_rules_price_booking_claim_ai_disclosure_and_opt_out():
    p = Transkriptpruefung()
    tr = t(("assistant", "Guten Tag, hier ist Lena von SonnenWerk Energie."),                 # keine KI-Kennzeichnung
           ("user", "Was kostet das?"),
           ("assistant", "Rund 15.000 € mit 30 % Förderung."),                                # Preis
           ("user", "Kein Interesse, rufen Sie nicht mehr an."),                               # Opt-out
           ("assistant", "Verstehe. Sind Sie denn Eigentümer?"),                               # fragt weiter
           ("user", "Nein!"),
           ("assistant", "Ich habe den Termin für Sie gebucht. Auf Wiederhören!"))            # Buchung ohne Buchung
    befunde = p.pruefen(tr, "sonstiges", False, [])
    assert [b.schwere for b in befunde[:4]] == ["S1", "S1", "S1", "S1"]
    assert {"ki_kennzeichnung_fehlt", "preis_genannt", "opt_out_ignoriert", "buchung_behauptet_ohne_buchung"} <= set(regeln(befunde))
    assert next(b for b in befunde if b.regel == "preis_genannt").turn == 2


def test_s2_and_s3_rules():
    p = Transkriptpruefung()
    lang = "Das ist " + "wirklich " * 65 + "wichtig."
    tr = t(("assistant", BEGRUESSUNG),
           ("user", "Ja."),
           ("assistant", "Ich rufe jetzt check_slots auf und schaue nach."),                  # Tool-Name
           ("assistant", "Wir hätten 14:00 Uhr frei."),                                        # Zeit vor check_slots
           ("tool", "check_slots {}"),
           ("assistant", "Da der Satz keinen Termintext enthält, sage ich: Passt dir das?"),  # Meta + Duzen
           ("assistant", lang),                                                                # Monolog
           ("assistant", "Sind Sie Eigentümer?"), ("user", "Hm."), ("assistant", "Sind Sie Eigentümer?"),   # wiederholt
           ("assistant", "Dann trage ich das ein."))                                           # keine Verabschiedung
    befunde = p.pruefen(tr, "rueckruf_vereinbart", False, ["feld_verworfen:dachflaeche_m2", "ergebnis_fehlt"])
    r = regeln(befunde)
    assert {"tool_name_ausgesprochen", "termin_genannt_ohne_angebot", "meta_sprache", "geduzt", "monolog",
            "frage_wiederholt", "keine_verabschiedung", "feld_verworfen:dachflaeche_m2", "ergebnis_fehlt"} <= set(r)
    assert "ki_kennzeichnung_fehlt" not in r and "opt_out_ignoriert" not in r
    assert [b.schwere for b in befunde] == sorted((b.schwere for b in befunde), key=review.SCHWERE.get)


def test_opt_out_followed_only_by_a_farewell_is_fine():
    p = Transkriptpruefung()
    tr = t(("assistant", BEGRUESSUNG), ("user", "Keine Werbung bitte, nicht mehr anrufen."),
           ("assistant", "Verstanden, ich trage Sie aus und wünsche Ihnen einen schönen Tag."))
    assert regeln(p.pruefen(tr, "opt_out", False, [])) == []


def test_review_from_db_and_markdown_list(con):
    crm = LocalCRM(con)
    sauber = t(("assistant", BEGRUESSUNG), ("user", "Nein danke."), ("assistant", "Alles klar, schönen Tag!"))
    schlecht = t(("assistant", "Hallo, hier Lena."), ("user", "Ja?"), ("assistant", "Das kostet 9.999 €. Tschüss!"))
    crm.anruf_protokollieren(1, None, None, "kein_interesse", 1, 0, ["plattform:telli", "call:c1"], sauber)
    crm.anruf_protokollieren(2, None, None, "sonstiges", 1, 0, ["ergebnis_fehlt"], schlecht)
    reviews = review.aus_db(crm, tage=1)
    assert [r.schwerste for r in reviews] == [None, "S1"]
    text = review.reviewliste(reviews)
    assert "Anrufe: 2 · auffällig: 1 · S1: 2" in text and "| db:2 | 2 | sonstiges | 1 | S1 |" in text
    assert "> Das kostet 9.999 €" in text and "**S1-Befunde zuerst lesen" in text
    assert "| kein_interesse | 1 |" in text
    assert "keine Befunde" in review.reviewliste(reviews[:1])


def test_review_from_telli_joins_local_processing_by_call_id(con):
    from integrations.telli import TelliClient
    crm = LocalCRM(con)
    crm.anruf_protokollieren(1, None, None, "opt_out", 2, 0, ["plattform:telli", "call:c_a", "buchung_trotz_opt_out"], [])
    seiten = {None: {"calls": [
        {"call_id": "c_a", "status": "connected", "state": "ended", "ended_at_iso": "2026-10-10T09:00:00.000Z",
         "transcriptObject": [{"role": "agent", "content": BEGRUESSUNG}, {"role": "user", "content": "Nein, nicht mehr anrufen."},
                              {"role": "agent", "content": "Verstanden, schönen Tag!"}],
         "outcomes": [{"key": "ergebnis", "value": "opt_out"}]},
        {"call_id": "c_b", "status": "connected", "state": "ended",
         "transcriptObject": [{"role": "agent", "content": "Hallo hier Lena."}],
         "outcomes": [{"key": "ergebnis", "value": "kein_interesse"}]}], "next_cursor": "seite2"},
        "seite2": {"calls": [{"call_id": "c_c", "status": "voicemail", "state": "ended", "transcriptObject": None,
                              "outcomes": None}], "next_cursor": None}}
    anfragen = []

    def handler(request):
        anfragen.append(dict(request.url.params))
        return httpx.Response(200, json=seiten[request.url.params.get("cursor")])

    client = TelliClient("k", client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telli.com"))
    reviews = review.aus_telli(client, crm, agent_id="agent_x", limit=10)
    assert anfragen[0] == {"limit": "10", "agent_id": "agent_x"} and anfragen[1]["cursor"] == "seite2"
    assert [r.kennung for r in reviews] == ["c_a", "c_b", "c_c"]
    assert reviews[0].lead_id == 1 and reviews[0].ergebnis == "opt_out" and "buchung_trotz_opt_out" in regeln(reviews[0].befunde)
    assert "nicht_nachbereitet" in regeln(reviews[1].befunde) and "ki_kennzeichnung_fehlt" in regeln(reviews[1].befunde)
    assert reviews[2].ergebnis == "nicht_erreicht" and regeln(reviews[2].befunde) == ["nicht_nachbereitet"]   # Mailbox wird auch protokolliert
    assert len(client.anrufe_auflisten(limit=1)) == 1                                         # limit greift je Seite


def test_inbound_transcript_has_no_false_positives():
    p = Transkriptpruefung()
    tr = t(("assistant", "Guten Tag, hier ist Mia, die digitale KI-Assistentin von FlinkNet. Worum geht es?"),
           ("user", "Mein Termin."),
           ("assistant", "Zur Sicherheit: Wie lautet die Postleitzahl Ihrer Anschlussadresse?"),
           ("user", "48149"), ("tool", "verify_customer {'plz': '48149'}"), ("tool", "lookup_lead {}"),
           ("assistant", "Danke, das passt. Ihr Termin ist am Dienstag um 9:00 Uhr. Soll ich ihn verschieben?"),
           ("user", "Ja, seit gestern 18 Uhr geht nichts."),
           ("tool", "cancel_appointment {}"), ("tool", "check_slots {}"),
           ("assistant", "Ich habe Mittwoch 11:00 Uhr oder Donnerstag 14:00 Uhr."),
           ("user", "Mittwoch."), ("tool", "book_appointment {'slot_id': 9}"),
           ("assistant", "Der neue Termin ist im System eingetragen; die Störung habe ich im Ticketsystem aufgenommen. "
                         "Vielen Dank und auf Wiederhören."))
    assert p.pruefen(tr, "termin_verschoben", True, ["plattform:telli"]) == []
    assert "verify_customer" in review.TOOL_NAMEN and "create_ticket" in review.TOOL_NAMEN


def test_booking_evidence_comes_from_the_call_not_from_todays_state(con, uhr):
    """Ein später abgesagter Termin macht die damalige Buchungsbestätigung nicht zur Falschaussage."""
    crm = LocalCRM(con, uhr=uhr)
    tr = t(("assistant", BEGRUESSUNG), ("user", "Ja."), ("tool", "check_slots {}"),
           ("assistant", "Perfekt, ich habe den Termin am 14.10. um 09:00 Uhr für Sie eingetragen. Auf Wiederhören!"))
    crm.anruf_protokollieren(1, uhr(), uhr(), "termin_gebucht", 1, 0, ["plattform:telli"], tr)
    assert crm.termin(1) is None                                       # heute kein Termin mehr im System
    r = review.aus_db(crm, tage=1)[0]
    assert "buchung_behauptet_ohne_buchung" not in regeln(r.befunde)
    assert review._termin_laut_anruf("rueckruf_vereinbart", ["termin_behauptet_ohne_buchung"]) is False
