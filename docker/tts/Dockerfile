FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /nonexistent vertep
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn[standard]==0.34.0
COPY services /app/services
USER 10001:10001
EXPOSE 8090
CMD ["uvicorn", "services.tts_service:app", "--host", "0.0.0.0", "--port", "8090"]
