FROM python:3.12-slim
ARG MIT_VOICE=0
# MIT_OCR=1 installiert Tesseract (deutsch) für fotografierte Belege; ohne OCR werden nur XRechnung, ZUGFeRD und Text-PDF gelesen
ARG MIT_OCR=0
# OFFLINE=1 installiert Python-Pakete ausschließlich aus drittanbieter/wheelhouse (vorher drittanbieter/spiegeln.sh)
ARG OFFLINE=0
WORKDIR /app
RUN if [ "$MIT_OCR" = "1" ]; then apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu \
 && rm -rf /var/lib/apt/lists/*; fi
COPY requirements.txt requirements-voice.txt ./
COPY drittanbieter/wheelhouse drittanbieter/wheelhouse
RUN if [ "$OFFLINE" = "1" ]; then PIP="--no-index --find-links drittanbieter/wheelhouse"; else PIP=""; fi \
 && pip install --no-cache-dir $PIP -r requirements.txt \
 && if [ "$MIT_VOICE" = "1" ]; then pip install --no-cache-dir $PIP -r requirements-voice.txt; fi \
 && rm -rf drittanbieter
COPY core core
COPY voice voice
COPY ontologie ontologie
COPY alembic.ini VERSION ./
COPY migrations migrations
RUN useradd --create-home app && mkdir -p /app/export /app/daten && chown app /app/export /app/daten
VOLUME ["/app/daten"]
USER app
EXPOSE 8100
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health')" || exit 1
CMD ["python", "-m", "core", "serve"]
