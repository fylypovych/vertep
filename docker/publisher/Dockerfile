FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /nonexistent vertep
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn[standard]==0.34.0 httpx==0.28.1
COPY services /app/services
RUN install -d -o 10001 -g 10001 /data/storage/publications
USER 10001:10001
EXPOSE 8091
CMD ["uvicorn", "services.publisher_service:app", "--host", "0.0.0.0", "--port", "8091"]
