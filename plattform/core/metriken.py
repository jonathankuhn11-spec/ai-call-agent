"""Prometheus-Metriken (`GET /metrics`): HTTP-Zähler und -Latenzen sowie Fachkennzahlen je Mandant beim Abruf.

Prometheus und Alertmanager laufen im Compose-Profil `monitoring` (Regeln in deploy/monitoring/). Der Endpunkt ist
nur intern erreichbar (Caddy liefert 404) und verlangt sonst den API-Schlüssel.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select

from core.modelle import Anfrage, Freigabe, Mandant, OutboxEintrag, Servicefall

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
OFFENE_STATUS = ("eingegangen", "qualifiziert", "eingeplant")


class Fachkennzahlen:
    """Sammler, der beim Abruf aus der Datenbank liest (keine Hintergrundjobs, keine Drift)."""

    def __init__(self, sitzungen: Callable, uhr: Callable[[], datetime]):
        self.sitzungen, self.uhr = sitzungen, uhr

    def collect(self) -> Iterable[GaugeMetricFamily]:
        freigaben = GaugeMetricFamily("shk_freigaben_offen", "Offene Freigaben", labels=["mandant", "klasse"])
        aelteste = GaugeMetricFamily("shk_freigabe_aelteste_stunden", "Alter der ältesten offenen Freigabe in Stunden", labels=["mandant"])
        outbox = GaugeMetricFamily("shk_outbox", "Outbox-Einträge je Status", labels=["mandant", "status"])
        vorgaenge = GaugeMetricFamily("shk_vorgaenge_offen", "Offene Vorgänge", labels=["mandant", "dringlichkeit"])
        anfragen = GaugeMetricFamily("shk_anfragen_offen", "Unbearbeitete Anfragen", labels=["mandant"])
        jetzt = self.uhr()
        with self.sitzungen() as s:
            namen = {m.id: m.name for m in s.scalars(select(Mandant))}
            for mid, klasse, n in s.execute(select(Freigabe.mandant_id, Freigabe.risikoklasse, func.count())
                                            .where(Freigabe.status == "offen").group_by(Freigabe.mandant_id, Freigabe.risikoklasse)):
                freigaben.add_metric([namen.get(mid, mid), klasse], n)
            for mid, erstellt in s.execute(select(Freigabe.mandant_id, func.min(Freigabe.erstellt)).where(Freigabe.status == "offen")
                                           .group_by(Freigabe.mandant_id)):
                if erstellt is not None:
                    erstellt = erstellt if erstellt.tzinfo else erstellt.replace(tzinfo=timezone.utc)
                    aelteste.add_metric([namen.get(mid, mid)], max(0.0, (jetzt - erstellt) / timedelta(hours=1)))
            for mid, status, n in s.execute(select(OutboxEintrag.mandant_id, OutboxEintrag.status, func.count())
                                            .group_by(OutboxEintrag.mandant_id, OutboxEintrag.status)):
                outbox.add_metric([namen.get(mid, mid), status], n)
            for mid, dringlichkeit, n in s.execute(select(Servicefall.mandant_id, Servicefall.dringlichkeit, func.count())
                                                   .where(Servicefall.status.in_(OFFENE_STATUS))
                                                   .group_by(Servicefall.mandant_id, Servicefall.dringlichkeit)):
                vorgaenge.add_metric([namen.get(mid, mid), dringlichkeit], n)
            for mid, n in s.execute(select(Anfrage.mandant_id, func.count()).where(Anfrage.status == "neu").group_by(Anfrage.mandant_id)):
                anfragen.add_metric([namen.get(mid, mid)], n)
        return [freigaben, aelteste, outbox, vorgaenge, anfragen]


class Metriken:
    def __init__(self, sitzungen: Callable, uhr: Callable[[], datetime]):
        self.registry = CollectorRegistry()
        self.anfragen = Counter("shk_http_requests", "HTTP-Anfragen", ["methode", "pfad", "status"], registry=self.registry)
        self.dauer = Histogram("shk_http_dauer_sekunden", "Antwortzeit", ["methode", "pfad"], registry=self.registry,
                               buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
        self.registry.register(Fachkennzahlen(sitzungen, uhr))

    def messen(self, methode: str, pfad: str, status: int, start: float) -> None:
        self.anfragen.labels(methode, pfad, str(status)).inc()
        self.dauer.labels(methode, pfad).observe(time.perf_counter() - start)

    def ausgabe(self) -> bytes:
        return generate_latest(self.registry)
