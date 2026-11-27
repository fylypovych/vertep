FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
WORKDIR /app

# Isolated LoRA training runtime powered by ostris/ai-toolkit (MIT, #548).
# Commercial base-model allowlist is enforced fail-closed by lora_trainer_service.py.
ARG AI_TOOLKIT_COMMIT=main
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    AI_TOOLKIT_ROOT=/opt/ai-toolkit

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-pip; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --system --uid 10001 --home /nonexistent vertep; \
    git clone --depth 1 https://github.com/ostris/ai-toolkit.git /opt/ai-toolkit; \
    git -C /opt/ai-toolkit submodule update --init --recursive; \
    python3 -m pip install --extra-index-url https://download.pytorch.org/whl/cu124 torch torchvision; \
    python3 -m pip install -r /opt/ai-toolkit/requirements.txt fastapi==0.115.6 uvicorn[standard]==0.34.0

COPY adapters /app/adapters
COPY core /app/core
COPY services /app/services

USER 10001:10001
EXPOSE 8101
CMD ["uvicorn", "services.lora_trainer_service:app", "--host", "0.0.0.0", "--port", "8101"]
