# Stimmdienst mit GPU: docker compose --profile stimme up -d stimme   (NVIDIA Container Toolkit auf dem Host)
# Ohne GPU: STIMME_GERAET=cpu (deutlich langsamer, nur für Tests) oder STIMME_ENGINE=test.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/modelle STIMME_VERZEICHNIS=/daten/stimmen STIMME_HOST=0.0.0.0
WORKDIR /app
COPY requirements-stimme.txt .
RUN pip install --no-cache-dir -r requirements-stimme.txt
COPY stimme stimme
# Modellgewichte beim Bau vorladen (Image ~5 GB, dafür kein Download beim ersten Anruf); ohne Netz beim Bau: Zeile entfernen und /modelle als Volume füllen
RUN python -c "from chatterbox.mtl_tts import ChatterboxMultilingualTTS; ChatterboxMultilingualTTS.from_pretrained(device='cpu')" || true
VOLUME ["/daten/stimmen", "/modelle"]
EXPOSE 8300
CMD ["python", "-m", "stimme"]
