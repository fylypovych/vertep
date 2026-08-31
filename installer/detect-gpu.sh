#!/usr/bin/env bash
set -euo pipefail
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "CUDA: unavailable"
  exit 0
fi
nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader
nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,memory.free --format=csv,noheader
gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader,nounits | head -n1 | xargs)
vram_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | xargs)
compute_cap=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader,nounits | head -n1 | xargs)
python3 "$(dirname "$0")/gpu-profile.py" --name "$gpu_name" --vram-mb "$vram_mb" \
  --compute-capability "$compute_cap"
