FROM rocm/pytorch:rocm6.4.4_ubuntu24.04_py3.12_pytorch_release_2.7.1
ARG COMFYUI_VERSION=v0.34.0
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 --branch "$COMFYUI_VERSION" https://github.com/Comfy-Org/ComfyUI.git /opt/comfyui \
    && python3 -m pip install -r /opt/comfyui/requirements.txt
WORKDIR /opt/comfyui
EXPOSE 8188
CMD ["python3", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--output-directory", "/data/output"]
