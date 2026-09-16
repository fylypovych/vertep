FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
ARG COMFYUI_VERSION=v0.34.0
ARG VHS_VERSION=1.0.4
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 --branch "$COMFYUI_VERSION" https://github.com/Comfy-Org/ComfyUI.git /opt/comfyui \
    && python3 -m pip install --extra-index-url https://download.pytorch.org/whl/cu124 torch torchvision torchaudio \
    && python3 -m pip install -r /opt/comfyui/requirements.txt \
    && git clone --depth 1 --branch "v${VHS_VERSION}" https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite \
    && python3 -m pip install -r /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt || true
WORKDIR /opt/comfyui
EXPOSE 8188
CMD ["python3", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--output-directory", "/data/output"]
