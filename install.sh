#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
worker_ready=true
if [[ "${1:-}" == "--dry-run" ]]; then
  exec "$ROOT_DIR/installer/preflight.sh"
fi
if [[ "$(. /etc/os-release && echo "${ID}:${VERSION_ID}")" != "ubuntu:24.04" ]]; then
  echo "This installer supports Ubuntu Server 24.04 only" >&2
  exit 1
fi
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh" >&2
  exit 1
fi
echo "VERTEP INSTALLER"
echo "1. CORE"
echo "2. GPU WORKER"
echo "3. CORE + GPU WORKER"
echo "You may also enter a profile name declared in installer/manifest.json."
role=${VERTEP_ROLE:-}
if [[ -z "$role" ]]; then
  read -r -p "Choose node role [1-3]: " role
fi
case "$role" in
  1) role_name=core ;;
  2) role_name=worker ;;
  3) role_name=core-worker ;;
  *) role_name=$role ;;
esac
if ! python3 "$ROOT_DIR/installer/role-plan.py" "$ROOT_DIR/installer/manifest.json" "$role_name" packages >/dev/null 2>&1; then
  echo "Invalid role/profile: $role_name" >&2
  exit 1
fi
node_name=${NODE_NAME:-}
[[ -n "$node_name" ]] || read -r -p "Node name [vertep-01]: " node_name
node_name=${node_name:-vertep-01}
install -d -m 0750 /etc/vertep
config=$(printf 'ROLE=%s\nNODE_NAME=%s\n' "$role_name" "$node_name")
if [[ "$role_name" == "worker" || "$role_name" == "core-worker" ]]; then
  default_core=http://127.0.0.1:8080
  [[ "$role_name" == "core-worker" ]] && default_core=http://127.0.0.1:8080
  core_address=${CORE_ADDRESS:-}
  [[ -n "$core_address" ]] || read -r -p "CORE address [$default_core]: " core_address
  core_address=${core_address:-$default_core}
  config+=$(printf '\nCORE_ADDRESS=%s' "$core_address")
fi
printf '%s\n' "$config" > /etc/vertep/node.conf
chmod 0600 /etc/vertep/node.conf

mapfile -t role_packages < <(python3 "$ROOT_DIR/installer/role-plan.py" "$ROOT_DIR/installer/manifest.json" "$role_name" packages)
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y "${role_packages[@]}"
systemctl enable --now docker

ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
if [[ "$role_name" == "core" || "$role_name" == "core-worker" ]]; then
  read -r -p "LAN CIDR allowed to access Web UI [192.168.0.0/16]: " lan_cidr
  lan_cidr=${lan_cidr:-192.168.0.0/16}
  ufw allow from "$lan_cidr" to any port 8080 proto tcp
fi
ufw --force enable

ssh_user=${SUDO_USER:-root}
ssh_home=$(getent passwd "$ssh_user" | cut -d: -f6)
authorized_keys=${ssh_home:-/root}/.ssh/authorized_keys
if [[ -s "$authorized_keys" ]]; then
  install -d -m 0755 /etc/ssh/sshd_config.d
  printf '%s\n' 'PubkeyAuthentication yes' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
    > /etc/ssh/sshd_config.d/60-vertep-hardening.conf
  if sshd -t; then
    systemctl reload ssh
    echo "SSH password authentication disabled; authorized key found for $ssh_user."
  else
    rm -f /etc/ssh/sshd_config.d/60-vertep-hardening.conf
    echo "SSH hardening validation failed; original SSH configuration retained." >&2
  fi
else
  echo "SSH WARNING: no authorized_keys for $ssh_user; password authentication was not disabled." >&2
fi

if [[ "$role_name" == "core" || "$role_name" == "core-worker" ]]; then
  if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  systemctl enable --now ollama || true
fi
if [[ "$role_name" == "worker" || "$role_name" == "core-worker" ]]; then
  if ! nvidia-smi >/dev/null 2>&1; then
    ubuntu-drivers install
    echo "NVIDIA driver installed. Reboot, then rerun installer to validate GPU and configure the worker."
    worker_ready=false
  fi
  if ! dpkg-query -W nvidia-container-toolkit >/dev/null 2>&1; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update
    apt-get install -y nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
  fi
  "$ROOT_DIR/installer/detect-gpu.sh" || true
  if nvidia-smi >/dev/null 2>&1; then
    "$ROOT_DIR/installer/install-comfyui.sh"
  else
    echo "GPU is not ready; ComfyUI and vertep-worker will be configured after reboot and installer rerun."
    worker_ready=false
  fi
fi
role_hook="$ROOT_DIR/installer/roles/$role_name.sh"
if [[ -f "$role_hook" ]]; then
  bash "$role_hook" "$ROOT_DIR" "$role_name"
fi
if [[ ! -f "$ROOT_DIR/.env" ]]; then
  python3 "$ROOT_DIR/scripts/generate-env.py" --template "$ROOT_DIR/.env.example" --output "$ROOT_DIR/.env"
  chmod 0600 "$ROOT_DIR/.env"
fi
if [[ "$role_name" == "core" || "$role_name" == "core-worker" ]]; then
  install -d -m 0750 "$ROOT_DIR/runtime/update/requests"
  python3 "$ROOT_DIR/scripts/set-env.py" "$ROOT_DIR/.env" WEB_UPDATE_ENABLED true
fi
if [[ "$role_name" == "worker" ]]; then
  worker_token=${VERTEP_NODE_TOKEN:-}
  if [[ -z "$worker_token" ]]; then
    read -r -s -p "Worker token configured on CORE: " worker_token
    echo
  fi
  [[ -n "$worker_token" ]] || { echo "A CORE-issued worker token is required" >&2; exit 1; }
  python3 "$ROOT_DIR/scripts/set-env.py" "$ROOT_DIR/.env" NODE_API_TOKEN "$worker_token"
fi
ln -sfn "$ROOT_DIR/scripts/vertep" /usr/local/bin/vertep
if command -v systemctl >/dev/null 2>&1; then
  if [[ "$role_name" == "core" || "$role_name" == "core-worker" ]]; then
    sed "s|@VERTEP_ROOT@|$ROOT_DIR|g" "$ROOT_DIR/installer/vertep-core.service" > /etc/systemd/system/vertep-core.service
    sed "s|@VERTEP_ROOT@|$ROOT_DIR|g" "$ROOT_DIR/installer/vertep-update.service" > /etc/systemd/system/vertep-update.service
    sed "s|@VERTEP_ROOT@|$ROOT_DIR|g" "$ROOT_DIR/installer/vertep-update.path" > /etc/systemd/system/vertep-update.path
  fi
  if [[ "$role_name" == "worker" || "$role_name" == "core-worker" ]]; then
    sed "s|@VERTEP_ROOT@|$ROOT_DIR|g" "$ROOT_DIR/installer/vertep-worker.service" > /etc/systemd/system/vertep-worker.service
  fi
  systemctl daemon-reload
  [[ "$role_name" == "core" || "$role_name" == "core-worker" ]] && systemctl enable --now vertep-core.service
  [[ "$role_name" == "core" || "$role_name" == "core-worker" ]] && systemctl enable --now vertep-update.path
  if [[ "$role_name" == "worker" || "$role_name" == "core-worker" ]]; then
    if [[ "$worker_ready" == true ]]; then
      systemctl enable --now vertep-worker.service
    else
      systemctl disable --now vertep-worker.service 2>/dev/null || true
    fi
  fi
fi
echo "Saved /etc/vertep/node.conf"
echo "Installed CLI: /usr/local/bin/vertep"
if [[ "$worker_ready" != true && ( "$role_name" == "worker" || "$role_name" == "core-worker" ) ]]; then
  echo "CORE services (if selected) are running; GPU WORKER remains disabled until reboot and installer rerun."
else
  echo "Configured services were enabled and started."
fi
echo "Review $ROOT_DIR/.env before exposing the node outside the LAN."
