#!/bin/sh
# Issue #120: publish the build-time runtime inventory onto the shared config
# volume so the GPU Worker can observe it (COMFYUI_ROOT/<report>) and verify the
# pinned ComfyUI/custom-node commit identity at readiness time. The build is the
# only writer of the report; the entrypoint merely delivers it.
set -eu

report_src="/opt/comfyui/runtime-inventory.json"
report_dir="${COMFYUI_RUNTIME_REPORT:-/data/runtime-report}"

if [ -f "$report_src" ] && [ -d "$report_dir" ]; then
    cp -f "$report_src" "$report_dir/runtime-inventory.json"
fi

exec python3 /opt/comfyui/main.py "$@"
