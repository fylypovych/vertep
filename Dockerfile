FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl openssl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ARG VERTEP_VERSION=dev
RUN printf '%s\n' "$VERTEP_VERSION" > VERSION
RUN pip install --no-cache-dir -r requirements.txt
COPY core core
COPY worker worker
COPY services services
COPY adapters adapters
COPY web web
COPY workflows workflows
COPY characters characters
COPY brands brands
COPY config config
COPY db db
COPY scripts/migrate.py scripts/migrate.py
COPY scripts/update-agent.py scripts/update-agent.py
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8080"]
