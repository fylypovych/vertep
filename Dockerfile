FROM python:3.12-slim AS backend
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl openssl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ARG VERTEP_VERSION=dev
RUN printf '%s\n' "$VERTEP_VERSION" > VERSION
RUN pip install --no-cache-dir -r requirements.txt

FROM node:20-slim AS web-v2
WORKDIR /app/web-v2
COPY web-v2/package.json web-v2/package-lock.json ./
RUN npm ci --silent
COPY web-v2/ .
RUN npm run build

FROM backend
WORKDIR /app
COPY --from=backend /app .
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
COPY --from=web-v2 /app/web-v2/dist/vertep-admin-v2 web-v2/dist/vertep-admin-v2
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8080"]
