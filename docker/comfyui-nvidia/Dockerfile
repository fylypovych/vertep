FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
# Pinned runtime identity (Issue #120). The image is built from immutable git
# *commits*, not from a floating branch or a movable tag: the build input is the
# pin, and the commit recorded below must equal it. A recorded commit never
# substitutes for a missing build pin, so a build whose checkout does not match
# its pin fails instead of silently shipping a drifted runtime.
ARG COMFYUI_COMMIT=6b747c0428c343e1417219641db93a4fb7cb69ae
ARG VHS_COMMIT=4d907bee61e92c2e65af3bd6383a4e4d356126d1
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
# Every step below must succeed: a masked install failure used to produce an image
# that reported itself ready while missing ComfyUI or its custom nodes.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-pip; \
    rm -rf /var/lib/apt/lists/*; \
    git init /opt/comfyui; \
    git -C /opt/comfyui remote add origin https://github.com/Comfy-Org/ComfyUI.git; \
    git -C /opt/comfyui fetch --depth 1 origin "$COMFYUI_COMMIT"; \
    git -C /opt/comfyui checkout --detach FETCH_HEAD; \
    git init /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite; \
    git -C /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite remote add origin https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git; \
    git -C /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite fetch --depth 1 origin "$VHS_COMMIT"; \
    git -C /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite checkout --detach FETCH_HEAD
RUN set -eux; \
    python3 -m pip install --extra-index-url https://download.pytorch.org/whl/cu124 torch torchvision torchaudio; \
    python3 -m pip install -r /opt/comfyui/requirements.txt; \
    python3 -m pip install -r /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt
# Record what the image was actually built from *and* prove it equals the pin, so
# the node can verify its own runtime instead of trusting the build arguments.
RUN set -eux; \
    python3 - "$COMFYUI_COMMIT" "$VHS_COMMIT" <<'PY'
import json, subprocess, sys

def commit(path):
    return subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()

comfyui_pin, vhs_pin = sys.argv[1], sys.argv[2]
comfyui_commit = commit("/opt/comfyui")
vhs_commit = commit("/opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite")
if comfyui_commit != comfyui_pin or vhs_commit != vhs_pin:
    raise SystemExit(
        f"runtime build pin mismatch: comfyui {comfyui_commit} != {comfyui_pin}, "
        f"ComfyUI-VideoHelperSuite {vhs_commit} != {vhs_pin}")
report = {
    "format": "comfyui_runtime/v1",
    "components": {
        "comfyui": {"version": comfyui_pin, "commit": comfyui_commit},
        "ComfyUI-VideoHelperSuite": {"version": vhs_pin, "commit": vhs_commit},
    },
}
with open("/opt/comfyui/runtime-inventory.json", "w", encoding="utf-8") as handle:
    json.dump(report, handle, indent=2, sort_keys=True)
PY
# Publish the build report onto the shared config volume so a GPU Worker on the
# same host can observe it (COMFYUI_ROOT) and verify the pin at readiness time.
COPY docker/comfyui-nvidia/entrypoint.sh /usr/local/bin/vertep-comfyui-entrypoint.sh
RUN chmod 0755 /usr/local/bin/vertep-comfyui-entrypoint.sh
WORKDIR /opt/comfyui
EXPOSE 8188
ENTRYPOINT ["/usr/local/bin/vertep-comfyui-entrypoint.sh"]
CMD ["--listen", "0.0.0.0", "--port", "8188", "--output-directory", "/data/output"]
