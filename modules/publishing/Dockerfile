FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -u 65535 -m publishing

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY publishing_logic.py publishing_module.py manifest.yaml ./
COPY migrations/ ./migrations/
COPY ui/ ./ui/

USER 65535

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:8080/health || exit 1

CMD ["python", "publishing_module.py"]