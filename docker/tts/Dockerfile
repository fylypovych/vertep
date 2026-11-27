FROM python:3.12-slim
WORKDIR /app

# Hybrid TTS runtime (#540):
# - Piper (OHF-Voice/piper1-gpl, GPL-3.0) is isolated strictly inside this HTTP
#   container (never imported by CORE or Worker) as the default neural uk_UA engine.
# - espeak-ng remains installed as the fallback engine for other languages.
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /nonexistent vertep

RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn[standard]==0.34.0 piper-tts==1.8.0

# Offline Ukrainian Piper voice models verified by SHA256:
# - uk_UA-ukrainian_tts-medium (CC0-1.0)
# - uk_UA-lada-x_low (Apache-2.0)
ARG PIPER_VOICES_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main
RUN mkdir -p /opt/piper-models \
    && curl -fsSL "${PIPER_VOICES_BASE}/uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium.onnx" \
         -o /opt/piper-models/uk_UA-ukrainian_tts-medium.onnx \
    && curl -fsSL "${PIPER_VOICES_BASE}/uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium.onnx.json" \
         -o /opt/piper-models/uk_UA-ukrainian_tts-medium.onnx.json \
    && curl -fsSL "${PIPER_VOICES_BASE}/uk/uk_UA/lada/x_low/uk_UA-lada-x_low.onnx" \
         -o /opt/piper-models/uk_UA-lada-x_low.onnx \
    && curl -fsSL "${PIPER_VOICES_BASE}/uk/uk_UA/lada/x_low/uk_UA-lada-x_low.onnx.json" \
         -o /opt/piper-models/uk_UA-lada-x_low.onnx.json \
    && printf '%s\n' \
         '7920419ac5f6fd8b6450520f24b52ed5a319cb53dd018fbcd71c9e079cbac84f  /opt/piper-models/uk_UA-ukrainian_tts-medium.onnx' \
         '4e96e72917ca9b94edc77d6ccfee03a73f450ba2fc1ca93c2e562bc014e5aa55  /opt/piper-models/uk_UA-ukrainian_tts-medium.onnx.json' \
         'f8d015d3fc19ea6bd8ac3ca1fd0c0fbac5054c609599ee73799235fd2cf5c786  /opt/piper-models/uk_UA-lada-x_low.onnx' \
         '2df74b622e78930e8a62432d1cc8380f79f34f02c8c58ebd68e6ccbc857d80aa  /opt/piper-models/uk_UA-lada-x_low.onnx.json' \
       | sha256sum -c - \
    && chmod -R a+rX /opt/piper-models

ENV PIPER_MODELS_DIR=/opt/piper-models \
    PIPER_DEFAULT_UK_VOICE=uk_UA-ukrainian_tts-medium

COPY services /app/services
USER 10001:10001
EXPOSE 8090
CMD ["uvicorn", "services.tts_service:app", "--host", "0.0.0.0", "--port", "8090"]
