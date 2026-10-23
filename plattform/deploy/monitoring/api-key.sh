#!/bin/sh
# Setzt den API-Schlüssel aus der Umgebung in die Prometheus-Konfiguration ein und startet Prometheus.
set -e
sed "s|__CORE_API_KEY__|${CORE_API_KEY}|" /etc/prometheus/prometheus.yml > /tmp/prometheus.yml
exec /bin/prometheus --config.file=/tmp/prometheus.yml --storage.tsdb.retention.time=30d --storage.tsdb.path=/prometheus
