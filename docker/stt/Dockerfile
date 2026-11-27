FROM python:3.12-slim
WORKDIR /app

# Local speech-to-text runtime powered by faster-whisper (MIT, #544).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /nonexistent vertep

RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn[standard]==0.34.0 \
    faster-whisper==1.2.1

ENV STT_MODELS_DIR=/data/models/stt \
    STT_MODEL=small \
    STT_DEVICE=cpu \
    STT_COMPUTE_TYPE=int8

COPY services /app/services
USER 10001:10001
EXPOSE 8099
CMD ["uvicorn", "services.stt_service:app", "--host", "0.0.0.0", "--port", "8099"]
