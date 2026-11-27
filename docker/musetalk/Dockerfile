FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
WORKDIR /app

# Isolated GPU lip-sync runtime powered by TMElyralab/MuseTalk (MIT, #551).
# Commercial-safe build: non-commercial face-recognition weights are excluded
# and verified absent at image build time.
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    MUSETALK_ROOT=/opt/musetalk

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git libgl1 libglib2.0-0 python3 python3-pip; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --system --uid 10001 --home /nonexistent vertep; \
    git clone --depth 1 https://github.com/TMElyralab/MuseTalk.git /opt/musetalk; \
    python3 -m pip install --extra-index-url https://download.pytorch.org/whl/cu124 torch torchvision; \
    grep -viE '^\s*(insightface|antelopev2|buffalo_)' /opt/musetalk/requirements.txt > /tmp/musetalk-requirements.txt; \
    python3 -m pip install -r /tmp/musetalk-requirements.txt fastapi==0.115.6 uvicorn[standard]==0.34.0 pyyaml; \
    rm -f /tmp/musetalk-requirements.txt; \
    python3 -c "import importlib.util, sys; sys.exit(1 if importlib.util.find_spec('insightface') else 0)"

COPY adapters /app/adapters
COPY services /app/services

USER 10001:10001
EXPOSE 8102
CMD ["uvicorn", "services.musetalk_service:app", "--host", "0.0.0.0", "--port", "8102"]
