#!/usr/bin/env bash
# Vertep appliance bootstrap for a clean Ubuntu Server 24.04 host.
set -Eeuo pipefail
umask 077

DOWNLOAD_ORIGIN=${VERTEP_DOWNLOAD_ORIGIN:-https://download.vertep.ai}
INSTALL_ROOT=${VERTEP_INSTALL_ROOT:-/opt/vertep}
NODE_ROLE=${VERTEP_NODE_ROLE:-core}
MIN_RAM_MB=${VERTEP_MIN_RAM_MB:-4096}
MIN_DISK_MB=${VERTEP_MIN_DISK_MB:-20480}

fail(){ printf 'VERTEP: %s\n' "$*" >&2; exit 1; }
progress(){ printf '\n==> %s\n' "$*"; }
[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "run bootstrap as root (curl … | sudo bash)"
. /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail "Ubuntu Server 24.04 LTS is required"
arch=$(dpkg --print-architecture)
[[ $arch == amd64 || $arch == arm64 ]] || fail "unsupported CPU architecture: $arch"
ram_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
disk_mb=$(df -Pm /opt 2>/dev/null | awk 'NR==2{print $4}' || df -Pm / | awk 'NR==2{print $4}')
(( ram_mb >= MIN_RAM_MB )) || fail "at least ${MIN_RAM_MB} MB RAM is required (found ${ram_mb})"
(( disk_mb >= MIN_DISK_MB )) || fail "at least ${MIN_DISK_MB} MB free disk is required (found ${disk_mb})"
curl -fsS --connect-timeout 10 "$DOWNLOAD_ORIGIN/health" >/dev/null || fail "Vertep download service is unreachable"
getent ahosts "${DOWNLOAD_ORIGIN#*://}" | head -1 >/dev/null 2>&1 \
  || fail "DNS cannot resolve the Vertep download service"
if command -v timedatectl >/dev/null && [[ $(timedatectl show -p NTPSynchronized --value 2>/dev/null) != yes ]]; then
  fail "system clock is not synchronized; configure NTP before installing signed releases"
fi
if command -v ss >/dev/null && ss -H -ltn 'sport = :8443' | grep -q .; then
  fail "TCP port 8443 is already in use"
fi
filesystem=$(findmnt -n -o FSTYPE /opt 2>/dev/null || findmnt -n -o FSTYPE /)
[[ $filesystem =~ ^(ext4|xfs|btrfs|zfs)$ ]] || fail "unsupported /opt filesystem: $filesystem"

progress "Detecting hardware"
gpu_vendor=none; gpu_name="GPU not found"; gpu_vram_mb=0; driver=unavailable; cuda=unavailable
if grep -qix '0x10de' /sys/bus/pci/devices/*/vendor 2>/dev/null || lspci 2>/dev/null | grep -qi nvidia; then
  gpu_vendor=nvidia
  if command -v nvidia-smi >/dev/null; then
    IFS=, read -r gpu_name gpu_vram_mb driver < <(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | head -1)
    cuda=$(nvidia-smi | sed -n 's/.*CUDA Version: \([^ ]*\).*/\1/p' | head -1); cuda=${cuda:-unavailable}
  else gpu_name="NVIDIA GPU (driver pending)"; fi
elif grep -qix '0x1002' /sys/bus/pci/devices/*/vendor 2>/dev/null || lspci 2>/dev/null | grep -Eqi 'AMD|ATI'; then gpu_vendor=amd; gpu_name="AMD GPU"; fi

progress "Installing container runtime"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl openssl jq pciutils docker.io docker-compose-v2
systemctl enable --now docker
if [[ $gpu_vendor == nvidia && $driver == unavailable ]]; then
  apt-get install -y -qq ubuntu-drivers-common
  ubuntu-drivers install || true
fi

progress "Preparing immutable Vertep runtime"
install -d -m 0750 "$INSTALL_ROOT"/{data,config,logs,backups,storage,models,runtime,tls}
manifest_tmp=$(mktemp); trap 'rm -f "$manifest_tmp"' EXIT
curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/stable/manifest.json" -o "$manifest_tmp"
public_key_tmp=$(mktemp); canonical_tmp=$(mktemp); signature_tmp=$(mktemp)
trap 'rm -f "$manifest_tmp" "$public_key_tmp" "$canonical_tmp" "$signature_tmp"' EXIT
cat > "$public_key_tmp" <<'BOOTSTRAP_PUBLIC_KEY'
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu6Tn67753+ax6mx8/cXP
TfS/BZr/PFNlfSF7EIJpShfgsm9mad+9glwYxN9AHjW1UfTM8lqUfugdItP8zCze
1ltAfQT0hAmkKInX4ONesvEKlfoW0Zu0tye9KXCvPIFVR7osiaysPQcon7KStZI4
Kfhe37wOl88yfPFWIhC41DnF6tgGm+q32OPr15fHX4jQaC95OKz/cZW+e/XC5Vlz
s7F/qWL7k2+07UG+dTjLK++ckSCzo1J+/18QZ6fyDgn7eu0DV8IAclufa/y72M9B
vpvdIWixnMowK4GLvzGsmez9Zyt1ocCCjji7hsBbwwTPeZcf8evuk2uUgsZG8MYX
ZwIDAQAB
-----END PUBLIC KEY-----
BOOTSTRAP_PUBLIC_KEY
python3 - "$manifest_tmp" "$canonical_tmp" "$signature_tmp" <<'PY'
import base64, json, sys
source, canonical, signature = sys.argv[1:]
manifest = json.load(open(source, encoding="utf-8"))
encoded = manifest.pop("signature", None)
if not isinstance(encoded, str):
    raise SystemExit("runtime manifest has no signature")
open(canonical, "wb").write(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
open(signature, "wb").write(base64.b64decode(encoded, validate=True))
PY
openssl dgst -sha256 -verify "$public_key_tmp" -signature "$signature_tmp" "$canonical_tmp" >/dev/null \
  || fail "runtime manifest signature verification failed"
version=$(jq -er '.version' "$manifest_tmp")
compose_sha=$(jq -er '.files["docker-compose.yml"].sha256' "$manifest_tmp")
proxy_sha=$(jq -er '.files["proxy.conf"].sha256' "$manifest_tmp")
roles_sha=$(jq -er '.files["node_roles.json"].sha256' "$manifest_tmp")
planner_sha=$(jq -er '.files["deployment-plan.py"].sha256' "$manifest_tmp")
update_agent_sha=$(jq -er '.files["update-agent.py"].sha256' "$manifest_tmp")
vertep_cli_sha=$(jq -er '.files["vertep"].sha256' "$manifest_tmp")
safe_extract_sha=$(jq -er '.files["safe-extract.py"].sha256' "$manifest_tmp")
release_layout_sha=$(jq -er '.files["release-layout.py"].sha256' "$manifest_tmp")
curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/docker-compose.yml" -o "$INSTALL_ROOT/docker-compose.yml"
curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/proxy.conf" -o "$INSTALL_ROOT/runtime/proxy.conf"
curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/node_roles.json" -o "$INSTALL_ROOT/config/node_roles.json"
install -d -m 0750 "$INSTALL_ROOT/scripts"
curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/update-agent.py" -o "$INSTALL_ROOT/scripts/update-agent.py"
curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/vertep" -o "$INSTALL_ROOT/scripts/vertep"
curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/safe-extract.py" -o "$INSTALL_ROOT/scripts/safe-extract.py"
curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/release-layout.py" -o "$INSTALL_ROOT/scripts/release-layout.py"
printf '%s  %s\n' "$compose_sha" "$INSTALL_ROOT/docker-compose.yml" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$proxy_sha" "$INSTALL_ROOT/runtime/proxy.conf" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$roles_sha" "$INSTALL_ROOT/config/node_roles.json" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$update_agent_sha" "$INSTALL_ROOT/scripts/update-agent.py" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$vertep_cli_sha" "$INSTALL_ROOT/scripts/vertep" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$safe_extract_sha" "$INSTALL_ROOT/scripts/safe-extract.py" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$release_layout_sha" "$INSTALL_ROOT/scripts/release-layout.py" | sha256sum -c - >/dev/null
chmod 0750 "$INSTALL_ROOT/scripts/vertep" "$INSTALL_ROOT/scripts/"*.py
mapfile -t role_services < <(jq -er --arg role "$NODE_ROLE" '.[$role].services[]' "$INSTALL_ROOT/config/node_roles.json") \
  || fail "unsupported node role: $NODE_ROLE"
[[ ${#role_services[@]} -gt 0 ]] || fail "node role has no services: $NODE_ROLE"
node_capabilities=$(jq -er --arg role "$NODE_ROLE" '.[$role].capabilities | join(",")' "$INSTALL_ROOT/config/node_roles.json")
if [[ $NODE_ROLE != core ]]; then
  [[ -n ${VERTEP_CORE_URL:-} && -n ${VERTEP_REGISTRATION_TOKEN:-} ]] \
    || fail "non-Core roles require VERTEP_CORE_URL and VERTEP_REGISTRATION_TOKEN"
  [[ -f ${VERTEP_CORE_CA_FILE:-} ]] || fail "non-Core roles require VERTEP_CORE_CA_FILE for TLS pinning"
  cp "$VERTEP_CORE_CA_FILE" "$INSTALL_ROOT/config/core-ca.crt"
  chmod 0600 "$INSTALL_ROOT/config/core-ca.crt"
fi

secret(){ openssl rand -base64 "$1" | tr -d '\n'; }
postgres_password=$(secret 36); redis_password=$(secret 36); jwt_secret=$(secret 48)
worker_secret=$(secret 48); encryption_key=$(secret 32); internal_api_key=$(secret 48); session_secret=$(secret 48)
secret_store_passphrase=$(secret 48)
setup_token=$(openssl rand -hex 6 | tr '[:lower:]' '[:upper:]')
setup_token_hash=$(printf '%s' "$setup_token" | sha256sum | awk '{print $1}')
setup_token_expires_at=$(date -u -d "+${VERTEP_SETUP_TOKEN_TTL_MINUTES:-60} minutes" +%Y-%m-%dT%H:%M:%SZ)
cat > "$INSTALL_ROOT/.env" <<EOF
VERTEP_VERSION=$version
VERTEP_IMAGE_REPOSITORY=${VERTEP_IMAGE_REPOSITORY:-registry.vertep.ai/vertep}
POSTGRES_PASSWORD=$postgres_password
REDIS_PASSWORD=$redis_password
JWT_SECRET=$jwt_secret
WORKER_SECRET=$worker_secret
ENCRYPTION_KEY=$encryption_key
INTERNAL_API_KEY=$internal_api_key
SESSION_SECRET=$session_secret
SECRET_STORE_PASSPHRASE_FILE=/run/secrets/secret_store_passphrase
REQUIRE_SECRET_KEY_SEALING=true
NODE_API_TOKEN=$(secret 48)
NODE_ROLE=$NODE_ROLE
NODE_CAPABILITIES=$node_capabilities
NODE_NAME=$(hostname -s | tr '[:upper:]_' '[:lower:]-')
CORE_URL=${VERTEP_CORE_URL:-}
REGISTRATION_TOKEN=${VERTEP_REGISTRATION_TOKEN:-}
CORE_CA_PATH=$([[ $NODE_ROLE == core ]] && echo '' || echo /data/config/core-ca.crt)
SETUP_TOKEN_HASH=$setup_token_hash
SETUP_TOKEN_EXPIRES_AT=$setup_token_expires_at
COOKIE_SECURE=true
CONFIG_ROOT=/data/config
WEB_UPDATE_ENABLED=true
VERTEP_UPDATE_SERVER=https://update.vertep.ai
REQUIRE_ROLLING_COMPATIBILITY=true
UPDATE_RELEASE_RETENTION=3
GPU_ENABLED=$([[ $gpu_vendor == nvidia ]] && echo true || echo false)
EOF
chmod 0600 "$INSTALL_ROOT/.env"
printf '%s' "$secret_store_passphrase" > "$INSTALL_ROOT/config/secret-store.passphrase"
chmod 0600 "$INSTALL_ROOT/config/secret-store.passphrase"
curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/deployment-plan.py" -o "$INSTALL_ROOT/runtime/deployment-plan.py"
printf '%s  %s\n' "$planner_sha" "$INSTALL_ROOT/runtime/deployment-plan.py" | sha256sum -c - >/dev/null
python3 "$INSTALL_ROOT/runtime/deployment-plan.py" "$INSTALL_ROOT/config/node_roles.json" "$NODE_ROLE" \
  "$version" "$INSTALL_ROOT/config/deployment-plan.json"
chmod 0600 "$INSTALL_ROOT/config/deployment-plan.json"
cat > "$INSTALL_ROOT/config/bootstrap-secrets.json" <<EOF
{"postgres_password":"$postgres_password","redis_password":"$redis_password","jwt_secret":"$jwt_secret","worker_secret":"$worker_secret","encryption_key":"$encryption_key","internal_api_key":"$internal_api_key","session_secret":"$session_secret"}
EOF
chmod 0600 "$INSTALL_ROOT/config/bootstrap-secrets.json"
cat > "$INSTALL_ROOT/config/hardware.json" <<EOF
{"architecture":"$arch","ram_mb":$ram_mb,"gpu":{"vendor":"$gpu_vendor","name":"${gpu_name//\"/}","vram_mb":${gpu_vram_mb// /},"driver":"$driver","cuda":"$cuda"},"docker_version":"$(docker --version | sed 's/"//g')"}
EOF
openssl req -x509 -newkey rsa:3072 -nodes -days 825 -subj "/CN=$(hostname -f 2>/dev/null || hostname)" \
  -keyout "$INSTALL_ROOT/tls/vertep.key" -out "$INSTALL_ROOT/tls/vertep.crt" >/dev/null 2>&1
chmod 0600 "$INSTALL_ROOT/tls/vertep.key"
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=Vertep Installation Node CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$INSTALL_ROOT/tls/node-ca.key" -out "$INSTALL_ROOT/tls/node-ca.crt" >/dev/null 2>&1
chmod 0600 "$INSTALL_ROOT/tls/node-ca.key"

progress "Installing privileged update executor"
for unit in vertep-update.service vertep-update.path vertep-update-check.service vertep-update.timer; do
  unit_sha=$(jq -er --arg name "$unit" '.files[$name].sha256' "$manifest_tmp")
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/$unit" -o "/tmp/$unit"
  printf '%s  %s\n' "$unit_sha" "/tmp/$unit" | sha256sum -c - >/dev/null
  sed "s|@VERTEP_ROOT@|$INSTALL_ROOT|g" "/tmp/$unit" > "/etc/systemd/system/$unit"
  rm -f "/tmp/$unit"
done
systemctl daemon-reload
systemctl enable --now vertep-update.path vertep-update.timer

progress "Starting Vertep $version"
docker compose --env-file "$INSTALL_ROOT/.env" -f "$INSTALL_ROOT/docker-compose.yml" pull "${role_services[@]}"
docker compose --env-file "$INSTALL_ROOT/.env" -f "$INSTALL_ROOT/docker-compose.yml" up -d --remove-orphans "${role_services[@]}"
if [[ $NODE_ROLE == text ]]; then
  ollama_ready=false
  for attempt in {1..60}; do
    if docker compose --env-file "$INSTALL_ROOT/.env" -f "$INSTALL_ROOT/docker-compose.yml" \
        exec -T ollama ollama list >/dev/null 2>&1; then
      ollama_ready=true; break
    fi
    sleep 2
  done
  [[ $ollama_ready == true ]] || fail "Ollama did not become ready for model provisioning"
  docker compose --env-file "$INSTALL_ROOT/.env" -f "$INSTALL_ROOT/docker-compose.yml" \
    exec -T ollama ollama pull "${VERTEP_OLLAMA_MODEL:-llama3.2}"
fi
deadline=$((SECONDS+600))
service_count=${#role_services[@]}
until [[ $(docker compose --env-file "$INSTALL_ROOT/.env" -f "$INSTALL_ROOT/docker-compose.yml" ps --services --filter status=running | wc -l) -eq $service_count ]] \
  && ! docker compose --env-file "$INSTALL_ROOT/.env" -f "$INSTALL_ROOT/docker-compose.yml" ps | grep -Eq '\(unhealthy\)|\(health: starting\)' \
  && { [[ $NODE_ROLE != core ]] || curl -kfsS https://127.0.0.1:8443/api/health >/dev/null; }; do
  (( SECONDS < deadline )) || { docker compose -f "$INSTALL_ROOT/docker-compose.yml" ps; fail "runtime health check timed out"; }
  sleep 5
done
server_ip=$(hostname -I | awk '{print $1}')
if [[ $NODE_ROLE == core ]]; then
  printf '\nVertep Core is ready. Open https://%s:8443 and enter setup code %s.\n' "$server_ip" "$setup_token"
else
  printf '\nVertep %s node is installed and will self-register with %s.\n' "$NODE_ROLE" "$VERTEP_CORE_URL"
fi
