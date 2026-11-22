FROM python:3.12-slim
WORKDIR /app
# pg_dump/pg_restore must match the PostgreSQL server major (docker/postgres: 16). Debian's
# default client is newer and emits settings a 16 server rejects (#442: SET transaction_timeout).
ARG POSTGRES_MAJOR=16
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates redis-tools \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends "postgresql-client-${POSTGRES_MAJOR}" \
    && ln -sf "/usr/lib/postgresql/${POSTGRES_MAJOR}/bin/pg_dump" /usr/local/bin/pg_dump \
    && ln -sf "/usr/lib/postgresql/${POSTGRES_MAJOR}/bin/pg_restore" /usr/local/bin/pg_restore \
    && ln -sf "/usr/lib/postgresql/${POSTGRES_MAJOR}/bin/psql" /usr/local/bin/psql \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /nonexistent vertep
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn[standard]==0.34.0 'cryptography>=41.0.7,<47' psutil==7.0.0 redis==5.2.1
COPY services /app/services
RUN install -d -o 10001 -g 10001 /data/backups
USER 10001:10001
EXPOSE 8092
CMD ["uvicorn", "services.backup_service:app", "--host", "0.0.0.0", "--port", "8092"]
