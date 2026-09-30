FROM rocm/pytorch:rocm6.4.4_ubuntu24.04_py3.12_pytorch_release_2.7.1
ARG COMFYUI_VERSION=v0.34.0
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
# set -eux so a failed clone/install fails the build instead of being masked.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git; \
    rm -rf /var/lib/apt/lists/*; \
    git clone --depth 1 --branch "$COMFYUI_VERSION" https://github.com/Comfy-Org/ComfyUI.git /opt/comfyui; \
    python3 -m pip install -r /opt/comfyui/requirements.txt
# Record the resolved commit so readiness checks can detect runtime drift.
RUN set -eux; \
    python3 - "$COMFYUI_VERSION" <<'PY'
import json, subprocess, sys

commit = subprocess.run(["git", "-C", "/opt/comfyui", "rev-parse", "HEAD"], check=True,
                        capture_output=True, text=True).stdout.strip()
report = {"format": "comfyui_runtime/v1",
          "components": {"comfyui": {"version": sys.argv[1], "commit": commit}}}
with open("/opt/comfyui/runtime-inventory.json", "w", encoding="utf-8") as handle:
    json.dump(report, handle, indent=2, sort_keys=True)
PY
WORKDIR /opt/comfyui
EXPOSE 8188
CMD ["python3", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--output-directory", "/data/output"]
