FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    CONFIG=/config/config.yaml \
    DATA=/data/state.json \
    TZ=Europe/Rome

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tzdata fonts-dejavu-core libzbar0 tesseract-ocr tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ARG VERSION=dev
ENV PRINTHUB_VERSION=$VERSION
LABEL org.opencontainers.image.title="PrintHub" \
      org.opencontainers.image.description="Cockpit für Bambu Lab A1 und Snapmaker U1 mit Spoolman" \
      org.opencontainers.image.source="https://github.com/milan941899/printhub"

EXPOSE 8095
VOLUME ["/config", "/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8095/static/style.css', timeout=4)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095"]
