FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl openssl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
COPY VERSION .
RUN pip install --no-cache-dir -r requirements.txt
COPY core core
COPY worker worker
COPY adapters adapters
COPY web web
COPY workflows workflows
COPY characters characters
COPY brands brands
COPY config config
COPY db db
COPY scripts/migrate.py scripts/migrate.py
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8080"]
