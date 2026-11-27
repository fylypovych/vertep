FROM rocm/pytorch:rocm6.4.4_ubuntu24.04_py3.12_pytorch_release_2.7.1
# Same pinned runtime identity as docker/comfyui-nvidia (Issue #120, #538): the
# image is built from immutable commits and proves the checkout equals the pin.
# COMFYUI_COMMIT is the commit of the ComfyUI tag v0.38.0.
ARG COMFYUI_COMMIT=6b747c0428c343e1417219641db93a4fb7cb69ae
ARG VHS_COMMIT=4d907bee61e92c2e65af3bd6383a4e4d356126d1
ARG IPADAPTER_COMMIT=a0f451a5113cf9becb0847b92884cb10cbdec0ef
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
# set -eux so a failed clone/install fails the build instead of being masked.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git; \
    rm -rf /var/lib/apt/lists/*; \
    git init /opt/comfyui; \
    git -C /opt/comfyui remote add origin https://github.com/Comfy-Org/ComfyUI.git; \
    git -C /opt/comfyui fetch --depth 1 origin "$COMFYUI_COMMIT"; \
    git -C /opt/comfyui checkout --detach FETCH_HEAD; \
    git init /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite; \
    git -C /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite remote add origin https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git; \
    git -C /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite fetch --depth 1 origin "$VHS_COMMIT"; \
    git -C /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite checkout --detach FETCH_HEAD; \
    git init /opt/comfyui/custom_nodes/ComfyUI_IPAdapter_plus; \
    git -C /opt/comfyui/custom_nodes/ComfyUI_IPAdapter_plus remote add origin https://github.com/cubiq/ComfyUI_IPAdapter_plus.git; \
    git -C /opt/comfyui/custom_nodes/ComfyUI_IPAdapter_plus fetch --depth 1 origin "$IPADAPTER_COMMIT"; \
    git -C /opt/comfyui/custom_nodes/ComfyUI_IPAdapter_plus checkout --detach FETCH_HEAD
# The ROCm base image already ships torch; only ComfyUI and VHS requirements are added.
RUN set -eux; \
    python3 -m pip install -r /opt/comfyui/requirements.txt; \
    python3 -m pip install -r /opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt
# Record what the image was actually built from and prove it equals the pin.
RUN set -eux; \
    python3 - "$COMFYUI_COMMIT" "$VHS_COMMIT" "$IPADAPTER_COMMIT" <<'PY'
import json, subprocess, sys

def commit(path):
    return subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()

comfyui_pin, vhs_pin, ipadapter_pin = sys.argv[1], sys.argv[2], sys.argv[3]
comfyui_commit = commit("/opt/comfyui")
vhs_commit = commit("/opt/comfyui/custom_nodes/ComfyUI-VideoHelperSuite")
ipadapter_commit = commit("/opt/comfyui/custom_nodes/ComfyUI_IPAdapter_plus")
if (comfyui_commit, vhs_commit, ipadapter_commit) != (comfyui_pin, vhs_pin, ipadapter_pin):
    raise SystemExit(
        f"runtime build pin mismatch: comfyui {comfyui_commit} != {comfyui_pin}, "
        f"ComfyUI-VideoHelperSuite {vhs_commit} != {vhs_pin}, "
        f"ipadapter {ipadapter_commit} != {ipadapter_pin}")
report = {
    "format": "comfyui_runtime/v1",
    "components": {
        "comfyui": {"version": comfyui_pin, "commit": comfyui_commit},
        "ComfyUI-VideoHelperSuite": {"version": vhs_pin, "commit": vhs_commit},
        "ComfyUI_IPAdapter_plus": {"version": ipadapter_pin, "commit": ipadapter_commit},
    },
    "wan22_nodes": [
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
        "CLIPTextEncode",
        "ModelSamplingSD3",
        "Wan22ImageToVideoLatent",
        "EmptyHunyuanLatentVideo",
        "KSampler",
        "VAEDecode",
        "VHS_VideoCombine",
    ],
}
with open("/opt/comfyui/runtime-inventory.json", "w", encoding="utf-8") as handle:
    json.dump(report, handle, indent=2, sort_keys=True)
PY
# The vendor-neutral entrypoint publishes the report to /data/runtime-report
# for the GPU Worker's readiness check (built from the repository root).
COPY docker/comfyui-nvidia/entrypoint.sh /usr/local/bin/vertep-comfyui-entrypoint.sh
RUN chmod 0755 /usr/local/bin/vertep-comfyui-entrypoint.sh
WORKDIR /opt/comfyui
EXPOSE 8188
ENTRYPOINT ["/usr/local/bin/vertep-comfyui-entrypoint.sh"]
CMD ["--listen", "0.0.0.0", "--port", "8188", "--output-directory", "/data/output"]
