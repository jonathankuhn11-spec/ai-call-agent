"""Mitgeführte Fremdkomponenten: Lizenztexte vollständig, Brick-Abgleich der Ontologie gültig, Prüfsummen und
Versionsfixierung konsistent, XRechnung besteht den KoSIT-Validator (wenn Java und Validator vorhanden)."""
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import rdflib

from tests.test_finanz import STAMMDATEN, rechnung_stellen

WURZEL = Path(__file__).resolve().parent.parent
DRITT = WURZEL / "drittanbieter"


def test_lizenztexte_fuer_alle_komponenten_vorhanden():
    tabelle = (DRITT / "README.md").read_text(encoding="utf-8")
    verweise = set(re.findall(r"`lizenzen/([\w.-]+\.txt)`", tabelle))
    assert len(verweise) >= 20
    for datei in verweise:
        inhalt = (DRITT / "lizenzen" / datei).read_text(encoding="utf-8")
        assert len(inhalt) > 500, datei
        assert any(w in inhalt for w in ("Permission is hereby granted", "Apache License", "Redistribution and use", "LESSER GENERAL PUBLIC",
                                        "PostgreSQL", "Software License", "Copyright")), datei
    # Copyleft-Komponenten werden nicht mitgeführt
    assert not any((DRITT / "lizenzen" / f"{n}.txt").exists() for n in ("twenty", "espocrm", "odoo", "calcom", "grafana", "radicale"))


def test_brick_abgleich_der_ontologie():
    g = rdflib.Graph()
    g.parse(DRITT / "ontologie" / "Brick-1.4.1.ttl", format="turtle")
    assert len(g) > 50000
    brick = rdflib.Namespace("https://brickschema.org/schema/Brick#")
    ziele = set(re.findall(r"brick:(\w+)", (WURZEL / "ontologie" / "shk.ttl").read_text(encoding="utf-8")))
    assert ziele
    for klasse in ziele:
        assert (brick[klasse], rdflib.RDF.type, rdflib.OWL.Class) in g, f"brick:{klasse} existiert nicht in Brick 1.4.1"


def test_pruefsummen_und_quellen_konsistent():
    summen = {}
    for zeile in (DRITT / "SHA256SUMS").read_text().splitlines():
        h, _, pfad = zeile.strip().partition("  ")
        assert re.fullmatch(r"[0-9a-f]{64}", h) and pfad
        summen[pfad] = h
    quellen = [z.split("\t") for z in (DRITT / "QUELLEN.txt").read_text(encoding="utf-8").splitlines() if z and not z.startswith("#")]
    assert {q[0] for q in quellen} == set(summen)
    for ziel, _version, lizenz, url in quellen:
        assert lizenz in ("Apache-2.0", "BSD-3-Clause", "MIT") and url.startswith("https://")
    brick = DRITT / "ontologie" / "Brick-1.4.1.ttl"
    assert hashlib.sha256(brick.read_bytes()).hexdigest() == summen["ontologie/Brick-1.4.1.ttl"]


def test_keine_latest_images_und_offline_build():
    compose = (WURZEL / "docker-compose.yml").read_text(encoding="utf-8") + (WURZEL / "deploy" / "server" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert ":latest" not in compose
    images = {z.strip() for z in (DRITT / "IMAGES.txt").read_text().splitlines() if z.strip() and not z.startswith("#")}
    for img in re.findall(r"image:\s*([\w./-]+:[\w.-]+)\s*$", compose, re.M):
        assert img in images, f"{img} fehlt in drittanbieter/IMAGES.txt"
    assert "OFFLINE" in (WURZEL / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("java") or not (DRITT / "xrechnung" / "validationtool-1.5.0-standalone.jar").exists(),
                    reason="Java oder KoSIT-Validator fehlt (drittanbieter/spiegeln.sh)")
def test_xrechnung_besteht_kosit_validator(umgebung, tmp_path):
    c, db, ids, h = umgebung
    assert c.put("/betrieb/stammdaten", headers=h, json=STAMMDATEN).status_code == 200
    c.patch(f"/kunden/{ids['kunde']}", headers=h, json={"email": "kunde@example.test", "strasse": "Teststr. 1", "plz": "50667", "ort": "Köln"})
    r = rechnung_stellen(c, h, ids, positionen=[{"bezeichnung": "Umwälzpumpe", "menge": 1, "einheit": "Stk", "einzelpreis_netto": "150.00"},
                                                 {"bezeichnung": "Monteurstunde", "menge": 2.5, "einheit": "Std", "einzelpreis_netto": "69.00"}])
    datei = tmp_path / "rechnung.xml"
    datei.write_bytes(c.get(f"/rechnungen/{r['id']}/xrechnung", headers=h).content)
    x = DRITT / "xrechnung"
    lauf = subprocess.run(["java", "-jar", str(x / "validationtool-1.5.0-standalone.jar"), "-s", str(x / "konfiguration" / "scenarios.xml"),
                           "-r", str(x / "konfiguration"), "-o", str(tmp_path), "-h", str(datei)], capture_output=True, text=True, timeout=300)
    assert lauf.returncode == 0 and "ACCEPTABLE" in lauf.stdout, lauf.stdout[-2000:] + lauf.stderr[-2000:]
    bericht = (tmp_path / "rechnung-report.xml").read_text(encoding="utf-8")
    assert "<rep:accept>" in bericht and "<rep:reject>" not in bericht
