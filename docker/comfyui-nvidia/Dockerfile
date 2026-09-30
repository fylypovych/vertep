FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
# Pinned runtime. The tags below are the *requested* versions; the resolved git
# commits are recorded in the runtime inventory below so readiness checks can
# detect any drift from what this image was actually built from.
ARG COMFYUI_VERSION=v0.34.0
ARG VHS_VERSION=1.0.4
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
# Every step below must succeed: a masked install failure used to produce an image
# that reported itself ready while missing ComfyUI or its custom nodes.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-pip; \
    rm -rf /var/lib/apt/lists/*; \
    git clone --depth 1 --branch "$COMFYUI_VERSION" https://github.com/Comfy-Org/ComfyUI.git /opt/comfyui; \
    git clone --depth 1 --branch "v${VHS_VERSION}" https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite
RUN set -eux; \
    python3 -m pip install --extra-index-url https://download.pytorch.org/whl/cu124 torch torchvision torchaudio; \
    python3 -m pip install -r /opt/comfyui/requirements.txt; \
    python3 -m pip install -r /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt
# Record what the image was actually built from, so the node can verify its own
# runtime instead of trusting the build arguments.
RUN set -eux; \
    python3 - "$COMFYUI_VERSION" "$VHS_VERSION" <<'PY'
import json, subprocess, sys

def commit(path):
    return subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()

comfyui_version, vhs_version = sys.argv[1], sys.argv[2]
report = {
    "format": "comfyui_runtime/v1",
    "components": {
        "comfyui": {"version": comfyui_version, "commit": commit("/opt/comfyui")},
        "ComfyUI-VideoHelperSuite": {"version": vhs_version,
                                     "commit": commit("/opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite")},
    },
}
with open("/opt/comfyui/runtime-inventory.json", "w", encoding="utf-8") as handle:
    json.dump(report, handle, indent=2, sort_keys=True)
PY
WORKDIR /opt/comfyui
EXPOSE 8188
CMD ["python3", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--output-directory", "/data/output"]
