FROM python:3.15-slim

LABEL org.opencontainers.image.source="https://github.com/ahmedbaig/aximote-exporter" \
      org.opencontainers.image.description="Aximote car telemetry for Prometheus, Grafana and Postgres" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY aximote_exporter/ ./aximote_exporter/
COPY sql/ ./sql/

# Unprivileged: the service only makes outbound HTTPS calls and listens on one
# high port, so it never needs root.
RUN useradd --system --no-create-home --uid 10001 aximote
USER aximote

EXPOSE 9684

CMD ["python", "-m", "aximote_exporter"]
