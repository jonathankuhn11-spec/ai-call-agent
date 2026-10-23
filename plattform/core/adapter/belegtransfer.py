"""DATEV Belegtransfer: Ablage der Eingangsbelege in der Ordnerstruktur, die das Programm „DATEV Belegtransfer“
(bzw. der Belegtransfer-Dienst) überwacht und nach DATEV Unternehmen online hochlädt.

Struktur (verzeichnis ist der auf dem Server gemountete Belegtransfer-Ordner, z. B. per SMB/Nextcloud-Sync):
  <verzeichnis>/<Beraternummer>_<Mandantennummer>/Rechnungseingang/<JJJJ-MM>/<Datum>_<Lieferant>_<Nummer>.<ext>
  daneben Rechnungseingang/<JJJJ-MM>/belege.csv (Kopfdaten als Hilfe für die Kanzlei, keine Buchungssätze)
Der Belegtyp (Rechnungseingang) ist der Ordnername; die Kontierung bleibt in DATEV (§ 6 Nr. 3 StBerG).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from core.adapter.basis import Konfigurationsfehler, beleg_dateiname, beleg_laden, beleg_referenz, beleg_uebergeben
from core.modelle import OutboxEintrag


class BelegtransferAdapter:
    name = "belegtransfer"

    def __init__(self, konfig: dict[str, Any]):
        if not konfig.get("verzeichnis"):
            raise Konfigurationsfehler("DATEV Belegtransfer: verzeichnis fehlt")
        self.verzeichnis = Path(konfig["verzeichnis"])
        self.belegtyp = konfig.get("belegtyp", "Rechnungseingang")

    def zustellen(self, e: OutboxEintrag, s: Session) -> str | None:
        if e.aktion != "beleg_uebermitteln":
            return None
        b, a, m = beleg_laden(s, e)
        if (vorhanden := beleg_referenz(s, b, self.name)):
            return vorhanden
        if a is None:
            raise Konfigurationsfehler("Beleg ohne Datei kann nicht in den Belegtransfer-Ordner gelegt werden")
        st = m.stammdaten
        mandantenordner = f"{st.get('datev_beraternummer') or 'Berater'}_{st.get('datev_mandantennummer') or m.id[:8]}"
        ziel = self.verzeichnis / mandantenordner / self.belegtyp / b.datum.strftime("%Y-%m")
        ziel.mkdir(parents=True, exist_ok=True)
        datei = ziel / beleg_dateiname(b, a)
        datei.write_bytes(a.inhalt)
        index = ziel / "belege.csv"
        neu = not index.exists()
        with index.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";", lineterminator="\r\n")
            if neu:
                w.writerow(["Datei", "Lieferant", "Belegnummer", "Belegdatum", "Fällig", "Netto", "USt", "Brutto", "USt-Satz", "Kategorie", "Vorgang"])
            w.writerow([datei.name, b.lieferant, b.nummer or "", b.datum.isoformat(), b.faellig.isoformat() if b.faellig else "",
                        b.netto.replace(".", ","), b.umsatzsteuer.replace(".", ","), b.brutto.replace(".", ","), b.ust_satz, b.kategorie,
                        b.servicefall_id or ""])
        return beleg_uebergeben(s, b, self.name, str(datei.relative_to(self.verzeichnis)))
