# Integrationsschicht als Container: python -m integrations serve (uvicorn, ein Worker, Hintergrundplaner im Prozess).
# Der lokale Agent (Ollama), der UAT und der Sprach-Layer laufen nicht im Container; sie brauchen GPU bzw. Audio.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DB_PATH=/data/crm.duckdb
WORKDIR /app

# Nur die Laufzeitabhängigkeiten der Integrationsschicht; streamlit und pandas (Dashboards) bleiben draußen
COPY requirements.txt .
RUN python -m pip install --no-cache-dir \
        $(grep -E '^(duckdb|pydantic|httpx|fastapi|uvicorn|ollama)==' requirements.txt)

COPY agent.py guided.py jev.py ./
COPY integrations/ integrations/

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown -R app:app /data /app
USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx, sys; r = httpx.get('http://127.0.0.1:8000/health', timeout=4); sys.exit(0 if r.status_code == 200 and r.json().get('status') == 'ok' else 1)"

CMD ["python", "-m", "integrations", "serve", "--host", "0.0.0.0", "--port", "8000"]
