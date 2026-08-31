#!/usr/bin/env bash
set -euo pipefail
COMFY_DIR=/opt/ComfyUI
if [[ ! -d "$COMFY_DIR/.git" ]]; then
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git "$COMFY_DIR"
else
  git -C "$COMFY_DIR" pull --ff-only
fi
python3 -m venv "$COMFY_DIR/.venv"
"$COMFY_DIR/.venv/bin/pip" install --upgrade pip
gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader,nounits | head -n1 | xargs)
vram_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | xargs)
compute_cap=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 || true)
profile_tsv=$(python3 "$(dirname "$0")/gpu-profile.py" --name "$gpu_name" --vram-mb "$vram_mb" \
  --compute-capability "$compute_cap" --format tsv)
IFS=$'\t' read -r architecture profile_id torch_index torch_package_list comfyui_args <<< "$profile_tsv"
read -r -a torch_packages <<< "$torch_package_list"
echo "GPU profile: $profile_id ($architecture, ${vram_mb} MB, compute capability $compute_cap)"
echo "Installing pinned PyTorch packages from $torch_index"
"$COMFY_DIR/.venv/bin/pip" install "${torch_packages[@]}" --index-url "$torch_index"
"$COMFY_DIR/.venv/bin/pip" install -r "$COMFY_DIR/requirements.txt"
"$COMFY_DIR/.venv/bin/python" -c 'import torch; assert torch.cuda.is_available(), "PyTorch cannot access CUDA"; print(torch.cuda.get_device_name(0))'
install -d -m 0750 /etc/vertep
python3 "$(dirname "$0")/gpu-profile.py" --name "$gpu_name" --vram-mb "$vram_mb" \
  --compute-capability "$compute_cap" --format env > /etc/vertep/gpu.env
chmod 0644 /etc/vertep/gpu.env
install -m 0644 "$(dirname "$0")/vertep-comfyui.service" /etc/systemd/system/vertep-comfyui.service
systemctl daemon-reload
systemctl enable --now vertep-comfyui.service
echo "ComfyUI installed. Add a compatible checkpoint under $COMFY_DIR/models/checkpoints/."
