#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
role=${VERTEP_ROLE:-}
if [[ -z "$role" && -r /etc/vertep/node.conf ]]; then
  role=$(awk -F= '$1=="ROLE"{print $2}' /etc/vertep/node.conf)
fi
role=${role:-core}
echo "VERTEP INSTALLER DRY RUN"
echo "Manifest: $root/installer/manifest.json"
echo "Role: $role"
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  echo "OS: $ID $VERSION_ID"
else
  echo "OS: unavailable (non-Linux development host)"
fi
python3 "$root/installer/role-plan.py" "$root/installer/manifest.json" "$role" packages >/dev/null
echo "Manifest: VALID"
if git -C "$root" remote get-url origin >/dev/null 2>&1; then
  remote=$(git -C "$root" remote get-url origin)
  case "$remote" in
    git@github.com:*|ssh://git@github.com/*|https://github.com/*) echo "GitHub origin: CONFIGURED" ;;
    http*://*@*) echo "GitHub origin: WARNING (embedded credentials are not allowed)" ;;
    *) echo "GitHub origin: WARNING (origin is not github.com; URL hidden)" ;;
  esac
else
  echo "GitHub origin: MISSING (Web update will be unavailable)"
fi
for command in git python3 ffmpeg docker; do
  if command -v "$command" >/dev/null 2>&1; then
    echo "$command: FOUND"
  else
    echo "$command: MISSING (will be installed when applicable)"
  fi
done
available_kb=$(df -Pk "$root" 2>/dev/null | awk 'NR==2{print $4}' || echo 0)
echo "Disk available: $(df -h "$root" 2>/dev/null | awk 'NR==2{print $4}' || echo unknown)"
if [[ "$available_kb" =~ ^[0-9]+$ ]] && (( available_kb < 10485760 )); then
  echo "Disk: WARNING (less than 10 GB free)"
else
  echo "Disk: OK"
fi
if [[ -f "$root/.env" ]]; then
  mode=$(stat -c '%a' "$root/.env" 2>/dev/null || echo unknown)
  echo ".env permissions: $mode"
  grep -Eq '=(replace-this-locally|replace-with-a-long-random-value|replace)$' "$root/.env" && echo "Secrets: WARNING placeholders found" || echo "Secrets: OK"
else
  echo ".env: MISSING (will be generated)"
fi
ssh_user=${SUDO_USER:-${USER:-root}}
ssh_home=$(getent passwd "$ssh_user" 2>/dev/null | cut -d: -f6)
if [[ -n "$ssh_home" && -s "$ssh_home/.ssh/authorized_keys" ]]; then
  echo "SSH key for $ssh_user: FOUND (password authentication can be disabled safely)"
else
  echo "SSH key for $ssh_user: MISSING (password authentication will remain enabled)"
fi
if [[ "$role" == "worker" || "$role" == "core-worker" ]]; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    "$root/installer/detect-gpu.sh"
  else
    echo "nvidia-smi: MISSING (driver installation/reboot may be required)"
  fi
fi
if [[ "$role" == "core" || "$role" == "core-worker" ]] && command -v ss >/dev/null 2>&1; then
  ss -ltn | awk '{print $4}' | grep -Eq '(^|:)8080$' && echo "Port 8080: IN USE" || echo "Port 8080: AVAILABLE"
fi
echo "No changes were made."
