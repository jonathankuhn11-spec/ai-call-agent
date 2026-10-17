# Kurzbefehle für Reviewer: alles läuft ohne Ollama, ohne Plattform-Account, ohne Telefonie.
.PHONY: install test lint demo check lasttest docker

install:          ## Abhängigkeiten installieren
	python -m pip install -r requirements.txt ruff

test:             ## 300+ Tests mit Abdeckung (Ziel: mindestens 90 %)
	python -m pytest -q --cov --cov-report=term

lint:             ## Statische Prüfung nach pyproject.toml
	ruff check .

demo:             ## Integrationsschicht im Prozess: beide Use Cases, Plattform-Simulator, Kundensystem-Attrappe
	python -m integrations demo

lasttest:         ## 40 Gespräche, 10 parallel, p95-Latenz der Tool-Aufrufe
	python scripts/lasttest.py --gespraeche 40 --parallel 10

docker:           ## Container bauen und Demo darin laufen lassen
	docker build -t ai-call-agent-integrations . && docker run --rm ai-call-agent-integrations python -m integrations demo
