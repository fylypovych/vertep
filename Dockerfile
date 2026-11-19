FROM python:3.12-slim AS backend
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl openssl jq && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ARG VERTEP_VERSION=dev
ARG VERTEP_RELEASE_SHA=
ARG VERTEP_ENVIRONMENT=production
RUN printf '%s\n' "$VERTEP_VERSION" > VERSION \
 && jq -n --arg v "$VERTEP_VERSION" --arg s "$VERTEP_RELEASE_SHA" --arg e "$VERTEP_ENVIRONMENT" \
      '{version:$v, commit_sha:$s, environment:$e}' > RELEASE_IDENTITY.json
RUN pip install --no-cache-dir -r requirements.txt

FROM backend
WORKDIR /app
COPY --from=backend /app .
COPY core core
COPY worker worker
COPY services services
COPY adapters adapters
COPY workflows workflows
COPY characters characters
COPY publishers publishers
COPY brands brands
COPY config config
COPY modules modules
COPY db db
COPY scripts/migrate.py scripts/migrate.py
COPY scripts/qualify_infrastructure.py scripts/qualify_infrastructure.py
COPY scripts/update-agent.py scripts/update-agent.py
COPY scripts/restore.sh scripts/restore.sh
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8080"]
