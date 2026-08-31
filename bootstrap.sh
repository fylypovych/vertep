#!/usr/bin/env bash
# Vertep appliance bootstrap for a clean Ubuntu Server 24.04 host.
set -Eeuo pipefail
umask 077

DOWNLOAD_ORIGIN=${VERTEP_DOWNLOAD_ORIGIN:-https://api.github.com/repos/fylypovych/vertep/releases/latest}
INSTALL_ROOT=${VERTEP_INSTALL_ROOT:-/opt/vertep}
NODE_ROLE=${VERTEP_ROLE:-unassigned}
CORE_URL=${VERTEP_CORE_URL:-}
REGISTRATION_TOKEN=${VERTEP_NODE_TOKEN:-}
BOOTSTRAP_SERVICES=(proxy core license-manager dispatcher scheduler certificate-manager migrate postgres redis update-agent)
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
curl -fsS --connect-timeout 10 "$DOWNLOAD_ORIGIN" >/dev/null || fail "Vertep release service is unreachable or no release is published"
download_host=${DOWNLOAD_ORIGIN#*://}; download_host=${download_host%%/*}
getent ahosts "$download_host" | head -1 >/dev/null 2>&1 \
  || fail "DNS cannot resolve the Vertep repository"
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
apt-get install -y -qq ca-certificates curl gnupg openssl jq pciutils docker.io docker-compose-v2
systemctl enable --now docker
if [[ $gpu_vendor == nvidia && $driver == unavailable ]]; then
  apt-get install -y -qq ubuntu-drivers-common
  ubuntu-drivers install || fail "NVIDIA driver installation failed"
fi
if [[ $gpu_vendor == nvidia ]]; then
  progress "Installing NVIDIA Container Toolkit"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq
  apt-get install -y -qq nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  docker info --format '{{json .Runtimes}}' | grep -q nvidia \
    || fail "NVIDIA container runtime was not registered with Docker"
  nvidia-smi >/dev/null 2>&1 \
    || fail "NVIDIA driver was installed but is not active; reboot the host and rerun bootstrap"
  IFS=, read -r gpu_name gpu_vram_mb driver < <(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | head -1)
  cuda=$(nvidia-smi | sed -n 's/.*CUDA Version: \([^ ]*\).*/\1/p' | head -1); cuda=${cuda:-unavailable}
elif [[ $gpu_vendor == amd ]]; then
  [[ $arch == amd64 ]] || fail "AMD ROCm runtime currently requires amd64"
  progress "Installing AMD ROCm container runtime"
  rocm_version=${VERTEP_ROCM_VERSION:-6.4.1}
  curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key \
    | gpg --dearmor -o /usr/share/keyrings/rocm.gpg
  printf 'deb [arch=amd64 signed-by=/usr/share/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/%s noble main\n' "$rocm_version" \
    > /etc/apt/sources.list.d/rocm.list
  apt-get update -qq
  apt-get install -y -qq rocm-hip-runtime rocminfo
  [[ -c /dev/kfd && -d /dev/dri ]] || fail "AMD GPU devices /dev/kfd and /dev/dri are unavailable"
  rocminfo >/dev/null || fail "ROCm runtime cannot access the AMD GPU"
  driver=$(cat /sys/module/amdgpu/version 2>/dev/null || echo kernel)
  cuda="rocm-$rocm_version"
fi

progress "Preparing immutable Vertep runtime"
install -d -m 0750 "$INSTALL_ROOT"/{data,config,logs,backups,storage,models,runtime,tls}

if [[ "$DOWNLOAD_ORIGIN" == *"api.github.com"* ]]; then
  progress "Resolving signed GitHub runtime release"
  release_json=$(curl -fsS "$DOWNLOAD_ORIGIN")
  tag=$(printf '%s' "$release_json" | jq -er '.tag_name')
  version=${tag#v}
  asset_name="vertep-runtime-${version}.tar.gz"
  bundle_url=$(printf '%s' "$release_json" | jq -er --arg name "$asset_name" \
    '.assets[] | select(.name == $name) | .browser_download_url') \
    || fail "GitHub release $tag has no $asset_name asset"
  bundle_tmp=$(mktemp); bundle_root=$(mktemp -d)
  trap 'rm -f "$bundle_tmp"; rm -rf "$bundle_root"' EXIT
  curl -fsSL "$bundle_url" -o "$bundle_tmp" || fail "Failed to download $asset_name"
  python3 - "$bundle_tmp" "$bundle_root" <<'PY'
import pathlib, sys, tarfile
archive, destination = sys.argv[1:]
root = pathlib.Path(destination).resolve()
with tarfile.open(archive, "r:gz") as source:
    for member in source.getmembers():
        name = pathlib.PurePosixPath(member.name)
        target = (root / pathlib.Path(*name.parts)).resolve()
        if (name.is_absolute() or ".." in name.parts or member.issym() or member.islnk()
                or member.isdev() or (target != root and root not in target.parents)):
            raise SystemExit(f"unsafe runtime bundle member: {member.name}")
    source.extractall(root)
PY
  manifest_tmp="$bundle_root/manifest.json"
  [[ -f $manifest_tmp ]] || fail "Runtime bundle has no manifest.json"
  public_key_tmp=$(mktemp); canonical_tmp=$(mktemp); signature_tmp=$(mktemp)
  cat > "$public_key_tmp" <<'BOOTSTRAP_PUBLIC_KEY'
-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAuCVLrHiR6yVAY+tTf1oK
ggwyZO5kuH7HMUxElIPdLlfuLkj5Q9WA1CdyjZLvIvKVxUMIPxYu7om7fnmRk+LF
o7h926Wq00rSri0elYSqoZ7i8bXYrD7/fH9/TDRVQVKa8qJ7yDI7bYeg7MZGd1Bn
U0u2/bUT16qaPOSwaCYgthFyMDMUPMLokxypuk0cvhr6g0LLXD2TUGALA1D/btu5
TozgX3rjAw9Tan990XefC1LT3krScRBG7GMv4ALfq2N1AQbbyfnjkQf5a6TsF61E
4nMjZmO0RcNky8n/tG7KqmWz+w2WpwHlriuTJPJxEU4unZb1pSmFQB8VGEaxACRe
AKd7AJOENZ4XbwIjme/zfRhHAvVrxGBHp681he1kwO60+vHu3zIm2GCVvDI/JP7F
jKMVLW+e77/nGMhEXv6K5X8S8eK8JO1m02oT7GlpH13IMLbsPZbcqPpJKNM68XmQ
yuC2ZNHe3ikH7JOa+xbEBQe/Q3e0o1fxSBdI8JwnFL7BAgMBAAE=
-----END PUBLIC KEY-----
BOOTSTRAP_PUBLIC_KEY
  python3 - "$manifest_tmp" "$canonical_tmp" "$signature_tmp" "$bundle_root" <<'PY'
import base64, hashlib, json, pathlib, sys
source, canonical, signature, bundle = sys.argv[1:]
root = pathlib.Path(bundle).resolve()
manifest = json.load(open(source, encoding="utf-8"))
encoded = manifest.pop("signature", None)
if not isinstance(encoded, str):
    raise SystemExit("runtime manifest has no signature")
open(canonical, "wb").write(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
open(signature, "wb").write(base64.b64decode(encoded, validate=True))
for name, metadata in manifest.get("files", {}).items():
    relative = pathlib.PurePosixPath(name)
    path = (root / pathlib.Path(*relative.parts)).resolve()
    if relative.is_absolute() or ".." in relative.parts or root not in path.parents or not path.is_file():
        raise SystemExit(f"unsafe or missing runtime artifact: {name}")
    if path.stat().st_size != metadata.get("size"):
        raise SystemExit(f"runtime artifact size mismatch: {name}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata.get("sha256"):
        raise SystemExit(f"runtime artifact checksum mismatch: {name}")
PY
  openssl dgst -sha256 -verify "$public_key_tmp" -signature "$signature_tmp" "$canonical_tmp" >/dev/null \
    || fail "runtime manifest signature verification failed"
  [[ $(jq -er '.version' "$manifest_tmp") == "$version" ]] \
    || fail "GitHub release tag and runtime manifest version differ"
  cp -a "$bundle_root"/. "$INSTALL_ROOT/"
  chmod -R u+rX,g+rX,o-rwx "$INSTALL_ROOT"
else
  manifest_tmp=$(mktemp); trap 'rm -f "$manifest_tmp"' EXIT
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/stable/manifest.json" -o "$manifest_tmp"
  public_key_tmp=$(mktemp); canonical_tmp=$(mktemp); signature_tmp=$(mktemp)
  trap 'rm -f "$manifest_tmp" "$public_key_tmp" "$canonical_tmp" "$signature_tmp"' EXIT
  cat > "$public_key_tmp" <<'BOOTSTRAP_PUBLIC_KEY'
-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAuCVLrHiR6yVAY+tTf1oK
ggwyZO5kuH7HMUxElIPdLlfuLkj5Q9WA1CdyjZLvIvKVxUMIPxYu7om7fnmRk+LF
o7h926Wq00rSri0elYSqoZ7i8bXYrD7/fH9/TDRVQVKa8qJ7yDI7bYeg7MZGd1Bn
U0u2/bUT16qaPOSwaCYgthFyMDMUPMLokxypuk0cvhr6g0LLXD2TUGALA1D/btu5
TozgX3rjAw9Tan990XefC1LT3krScRBG7GMv4ALfq2N1AQbbyfnjkQf5a6TsF61E
4nMjZmO0RcNky8n/tG7KqmWz+w2WpwHlriuTJPJxEU4unZb1pSmFQB8VGEaxACRe
AKd7AJOENZ4XbwIjme/zfRhHAvVrxGBHp681he1kwO60+vHu3zIm2GCVvDI/JP7F
jKMVLW+e77/nGMhEXv6K5X8S8eK8JO1m02oT7GlpH13IMLbsPZbcqPpJKNM68XmQ
yuC2ZNHe3ikH7JOa+xbEBQe/Q3e0o1fxSBdI8JwnFL7BAgMBAAE=
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
  jq -e '
    .schema == 2 and .product == "vertep" and
    (.release_sequence | type == "number" and . >= 1) and
    (.compatibility.core_api | type == "number" and . >= 1) and
    (.compatibility.worker_api | type == "number" and . >= 1) and
    (.compatibility.database_schema | type == "number" and . >= 1) and
    (.roles.profiles | type == "object" and length > 0) and
    (.sbom.format == "CycloneDX-JSON")
  ' "$manifest_tmp" >/dev/null || fail "runtime manifest contract is invalid"
  version=$(jq -er '.version' "$manifest_tmp")
  compose_sha=$(jq -er '.files["docker-compose.yml"].sha256' "$manifest_tmp")
  compose_amd_sha=$(jq -er '.files["docker-compose.amd.yml"].sha256' "$manifest_tmp")
  compose_nvidia_sha=$(jq -er '.files["docker-compose.nvidia.yml"].sha256' "$manifest_tmp")
  proxy_sha=$(jq -er '.files["proxy.conf"].sha256' "$manifest_tmp")
  roles_sha=$(jq -er '.files["node_roles.json"].sha256' "$manifest_tmp")
  planner_sha=$(jq -er '.files["deployment-plan.py"].sha256' "$manifest_tmp")
  update_agent_sha=$(jq -er '.files["update-agent.py"].sha256' "$manifest_tmp")
  vertep_cli_sha=$(jq -er '.files["vertep"].sha256' "$manifest_tmp")
  safe_extract_sha=$(jq -er '.files["safe-extract.py"].sha256' "$manifest_tmp")
  release_layout_sha=$(jq -er '.files["release-layout.py"].sha256' "$manifest_tmp")
  deployment_apply_sha=$(jq -er '.files["apply-deployment.py"].sha256' "$manifest_tmp")
  sbom_file=$(jq -er '.sbom.file' "$manifest_tmp")
  sbom_sha=$(jq -er '.sbom.sha256' "$manifest_tmp")
  [[ $sbom_file != /* && $sbom_file != *..* ]] || fail "runtime manifest contains an unsafe SBOM path"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/docker-compose.yml" -o "$INSTALL_ROOT/docker-compose.yml"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/docker-compose.amd.yml" -o "$INSTALL_ROOT/docker-compose.amd.yml"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/docker-compose.nvidia.yml" -o "$INSTALL_ROOT/docker-compose.nvidia.yml"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/proxy.conf" -o "$INSTALL_ROOT/runtime/proxy.conf"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/node_roles.json" -o "$INSTALL_ROOT/config/node_roles.json"
  install -d -m 0750 "$INSTALL_ROOT/scripts"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/update-agent.py" -o "$INSTALL_ROOT/scripts/update-agent.py"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/vertep" -o "$INSTALL_ROOT/scripts/vertep"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/safe-extract.py" -o "$INSTALL_ROOT/scripts/safe-extract.py"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/release-layout.py" -o "$INSTALL_ROOT/scripts/release-layout.py"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/apply-deployment.py" -o "$INSTALL_ROOT/scripts/apply-deployment.py"
  curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/$sbom_file" -o "$INSTALL_ROOT/config/release-sbom.cdx.json"
  printf '%s  %s\n' "$compose_sha" "$INSTALL_ROOT/docker-compose.yml" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$compose_amd_sha" "$INSTALL_ROOT/docker-compose.amd.yml" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$compose_nvidia_sha" "$INSTALL_ROOT/docker-compose.nvidia.yml" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$proxy_sha" "$INSTALL_ROOT/runtime/proxy.conf" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$roles_sha" "$INSTALL_ROOT/config/node_roles.json" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$update_agent_sha" "$INSTALL_ROOT/scripts/update-agent.py" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$vertep_cli_sha" "$INSTALL_ROOT/scripts/vertep" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$safe_extract_sha" "$INSTALL_ROOT/scripts/safe-extract.py" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$release_layout_sha" "$INSTALL_ROOT/scripts/release-layout.py" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$deployment_apply_sha" "$INSTALL_ROOT/scripts/apply-deployment.py" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$sbom_sha" "$INSTALL_ROOT/config/release-sbom.cdx.json" | sha256sum -c - >/dev/null
  chmod 0750 "$INSTALL_ROOT/scripts/vertep" "$INSTALL_ROOT/scripts/"*.py
fi
planner_sha=$(jq -er '.files["runtime/deployment-plan.py"].sha256 // .files["deployment-plan.py"].sha256' "$manifest_tmp")
monitoring_files=(
  monitoring/prometheus.yml monitoring/alerts.yml monitoring/loki.yml monitoring/promtail.yml
  monitoring/grafana/provisioning/datasources/vertep.yml
  monitoring/grafana/provisioning/dashboards/vertep.yml
  monitoring/grafana/dashboards/fleet.json
)
for relative in "${monitoring_files[@]}"; do
  artifact_sha=$(jq -er --arg name "$relative" '.files[$name].sha256' "$manifest_tmp")
  destination="$INSTALL_ROOT/$relative"
  install -d -m 0750 "$(dirname "$destination")"
  [[ -f $destination ]] || curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/$relative" -o "$destination"
  printf '%s  %s\n' "$artifact_sha" "$destination" | sha256sum -c - >/dev/null
  chmod 0640 "$destination"
done
NODE_ROLE=${VERTEP_ROLE:-unassigned}
if [[ "$NODE_ROLE" == "unassigned" ]] && [[ -t 0 ]]; then
  echo "Select node role:"
  echo "1) core"
  echo "2) gpu"
  echo "3) text"
  echo "4) voice"
  echo "5) publisher"
  echo "6) backup"
  echo "7) monitoring"
  echo "8) core-worker"
  read -r -p "Role [1-8]: " role_choice
  case "$role_choice" in
    1) NODE_ROLE=core ;;
    2) NODE_ROLE=gpu ;;
    3) NODE_ROLE=text ;;
    4) NODE_ROLE=voice ;;
    5) NODE_ROLE=publisher ;;
    6) NODE_ROLE=backup ;;
    7) NODE_ROLE=monitoring ;;
    8) NODE_ROLE=core-worker ;;
    *) fail "Invalid role choice" ;;
  esac
fi
if [[ "$NODE_ROLE" != "unassigned" ]]; then
  role_services=( $(jq -r --arg role "$NODE_ROLE" '.roles.profiles[$role].services[]' "$INSTALL_ROOT/config/node_roles.json") )
fi
role_services=("${BOOTSTRAP_SERVICES[@]}" "${role_services[@]}")
mapfile -t role_services < <(printf '%s\n' "${role_services[@]}" | awk '!seen[$0]++')
node_capabilities=$(jq -r --arg role "$NODE_ROLE" '.roles.profiles[$role].capabilities[]' "$INSTALL_ROOT/config/node_roles.json" 2>/dev/null | paste -sd, - || true)
platform="linux/$arch"
jq -e --slurpfile catalog "$INSTALL_ROOT/config/node_roles.json" '
  . as $manifest |
  $manifest.roles.catalog_sha256 == $manifest.files[$manifest.roles.catalog_file].sha256 and
  ($manifest.roles.profiles | keys | sort) == ($catalog[0] | keys | sort) and
  all($catalog[0] | keys[]; . as $role |
    $manifest.roles.profiles[$role].services == $catalog[0][$role].services and
    $manifest.roles.profiles[$role].capabilities == $catalog[0][$role].capabilities and
    $manifest.roles.profiles[$role].modules == $catalog[0][$role].modules and
    all($manifest.roles.profiles[$role].services[]; . as $service |
        ($manifest.images[$service].digest | test("^sha256:[0-9a-f]{64}$"))))
' "$manifest_tmp" >/dev/null || fail "role catalog is inconsistent with the signed runtime contract"
for service in "${role_services[@]}"; do
  jq -e --arg service "$service" --arg platform "$platform" \
    '.images[$service].platforms | index($platform) != null' "$manifest_tmp" >/dev/null \
    || fail "service $service has no signed image for $platform"
done
comfyui_image_service=comfyui
[[ $gpu_vendor == amd ]] && comfyui_image_service=comfyui-amd
if printf '%s\n' "${role_services[@]}" | grep -qx comfyui; then
  jq -e --arg service "$comfyui_image_service" --arg platform "$platform" \
    '.images[$service].platforms | index($platform) != null' "$manifest_tmp" >/dev/null \
    || fail "ComfyUI image $comfyui_image_service has no signed image for $platform"
fi

secret(){ openssl rand -hex "$1"; }
postgres_password=$(secret 36); redis_password=$(secret 36); jwt_secret=$(secret 48)
worker_secret=$(secret 48); encryption_key=$(secret 32); internal_api_key=$(secret 48); session_secret=$(secret 48)
secret_store_passphrase=$(secret 48); grafana_password=$(secret 36)
setup_token=$(openssl rand -hex 6 | tr '[:lower:]' '[:upper:]')
setup_token_hash=$(printf '%s' "$setup_token" | sha256sum | awk '{print $1}')
setup_token_expires_at=$(date -u -d "+${VERTEP_SETUP_TOKEN_TTL_MINUTES:-60} minutes" +%Y-%m-%dT%H:%M:%SZ)
WEB_DOMAIN=${VERTEP_WEB_DOMAIN:-$(hostname -f 2>/dev/null || hostname)}
resolved_image(){ jq -er --arg service "$1" '.images[$service] | .reference + "@" + .digest' "$manifest_tmp"; }
cat > "$INSTALL_ROOT/.env" <<EOF
VERTEP_VERSION=$version
VERTEP_IMAGE_REPOSITORY=${VERTEP_IMAGE_REPOSITORY:-registry.vertep.ai/vertep}
VERTEP_PROXY_IMAGE=$(resolved_image proxy)
VERTEP_CORE_IMAGE=$(resolved_image core)
VERTEP_LICENSE_MANAGER_IMAGE=$(resolved_image license-manager)
VERTEP_DISPATCHER_IMAGE=$(resolved_image dispatcher)
VERTEP_SCHEDULER_IMAGE=$(resolved_image scheduler)
VERTEP_CERTIFICATE_MANAGER_IMAGE=$(resolved_image certificate-manager)
VERTEP_WORKER_IMAGE=$(resolved_image worker)
VERTEP_COMFYUI_IMAGE=$(resolved_image "$comfyui_image_service")
VERTEP_TTS_IMAGE=$(resolved_image tts)
VERTEP_PUBLISHER_WORKER_IMAGE=$(resolved_image publisher-worker)
VERTEP_BACKUP_SERVICE_IMAGE=$(resolved_image backup-service)
VERTEP_POSTGRES_IMAGE=$(resolved_image postgres)
VERTEP_REDIS_IMAGE=$(resolved_image redis)
VERTEP_OLLAMA_IMAGE=$(resolved_image ollama)
VERTEP_MONITORING_IMAGE=$(resolved_image monitoring)
VERTEP_GRAFANA_IMAGE=$(resolved_image grafana)
VERTEP_LOG_STORE_IMAGE=$(resolved_image log-store)
VERTEP_LOG_COLLECTOR_IMAGE=$(resolved_image log-collector)
VERTEP_UPDATE_AGENT_IMAGE=$(resolved_image update-agent)
POSTGRES_PASSWORD=$postgres_password
UPDATE_DATABASE_URL=postgresql://vertep:$postgres_password@127.0.0.1:5432/vertep
SYSTEM_STATE_BACKEND=postgres
REDIS_PASSWORD=$redis_password
GRAFANA_ADMIN_PASSWORD=$grafana_password
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
REGISTRATION_TOKEN=${VERTEP_NODE_TOKEN:-}
CORE_CA_PATH=
SETUP_TOKEN_HASH=$setup_token_hash
SETUP_TOKEN_EXPIRES_AT=$setup_token_expires_at
COOKIE_SECURE=true
CONFIG_ROOT=/data/config
WEB_DOMAIN=$WEB_DOMAIN
WEB_UPDATE_ENABLED=true
VERTEP_UPDATE_SERVER=https://update.vertep.ai
REQUIRE_ROLLING_COMPATIBILITY=true
UPDATE_REQUIRE_DISTRIBUTED_FENCE=true
UPDATE_RELEASE_RETENTION=3
GPU_ENABLED=$([[ $gpu_vendor == nvidia || $gpu_vendor == amd ]] && echo true || echo false)
GPU_VENDOR=$gpu_vendor
LICENSE_MANAGER_URL=http://license-manager:8093
DISPATCHER_URL=http://dispatcher:8094
SCHEDULER_URL=http://scheduler:8095
CERTIFICATE_MANAGER_URL=http://certificate-manager:8096
BACKUP_URL=http://backup-service:8092
OLLAMA_URL=http://ollama:11434
EOF
chmod 0600 "$INSTALL_ROOT/.env"
printf '%s' "$secret_store_passphrase" > "$INSTALL_ROOT/config/secret-store.passphrase"
printf '%s' "$postgres_password" > "$INSTALL_ROOT/config/postgres.password"
printf '%s' "$redis_password" > "$INSTALL_ROOT/config/redis.password"
printf '%s' "$encryption_key" > "$INSTALL_ROOT/config/backup.key"
chmod 0600 "$INSTALL_ROOT/config/secret-store.passphrase" "$INSTALL_ROOT/config/postgres.password" \
  "$INSTALL_ROOT/config/redis.password" "$INSTALL_ROOT/config/backup.key"
[[ -f "$INSTALL_ROOT/runtime/deployment-plan.py" ]] \
  || curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/deployment-plan.py" -o "$INSTALL_ROOT/runtime/deployment-plan.py"
printf '%s  %s\n' "$planner_sha" "$INSTALL_ROOT/runtime/deployment-plan.py" | sha256sum -c - >/dev/null
python3 - "$version" "$INSTALL_ROOT/config/deployment-plan.json" "${BOOTSTRAP_SERVICES[@]}" <<'PY'
import hashlib, json, sys
version, output, *services = sys.argv[1:]
plan = {"schema": 1, "role": "unassigned", "version": version, "services": services,
        "modules": ["setup_runtime"], "capabilities": []}
plan["sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
open(output, "w", encoding="utf-8").write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
PY
chmod 0600 "$INSTALL_ROOT/config/deployment-plan.json"
cat > "$INSTALL_ROOT/config/bootstrap-secrets.json" <<EOF
{"postgres_password":"$postgres_password","redis_password":"$redis_password","grafana_admin_password":"$grafana_password","jwt_secret":"$jwt_secret","worker_secret":"$worker_secret","encryption_key":"$encryption_key","internal_api_key":"$internal_api_key","session_secret":"$session_secret"}
EOF
chmod 0600 "$INSTALL_ROOT/config/bootstrap-secrets.json"
cat > "$INSTALL_ROOT/config/hardware.json" <<EOF
{"architecture":"$arch","ram_mb":$ram_mb,"gpu":{"vendor":"$gpu_vendor","name":"${gpu_name//\"/}","vram_mb":${gpu_vram_mb// /},"driver":"$driver","cuda":"$cuda"},"docker_version":"$(docker --version | sed 's/"//g')"}
EOF
openssl req -x509 -newkey rsa:3072 -nodes -days 825 -subj "/CN=${WEB_DOMAIN}" \
  -addext "subjectAltName=DNS:${WEB_DOMAIN}" \
  -keyout "$INSTALL_ROOT/tls/vertep.key" -out "$INSTALL_ROOT/tls/vertep.crt" >/dev/null 2>&1
chmod 0600 "$INSTALL_ROOT/tls/vertep.key"
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=Vertep Installation Node CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$INSTALL_ROOT/tls/node-ca.key" -out "$INSTALL_ROOT/tls/node-ca.crt" >/dev/null 2>&1
chmod 0600 "$INSTALL_ROOT/tls/node-ca.key"

progress "Installing privileged update executor"
for unit in vertep-update.service vertep-update.path vertep-update-check.service vertep-update.timer \
             vertep-deployment.service vertep-deployment.path \
             vertep-watchdog.service vertep-watchdog.timer \
             vertep-startup-recovery.service; do
  unit_sha=$(jq -er --arg name "$unit" '.files[$name].sha256' "$manifest_tmp")
  if [[ -f "$INSTALL_ROOT/$unit" ]]; then
    cp "$INSTALL_ROOT/$unit" "/tmp/$unit"
  else
    curl -fsS "$DOWNLOAD_ORIGIN/v1/runtime/$version/$unit" -o "/tmp/$unit"
  fi
  printf '%s  %s\n' "$unit_sha" "/tmp/$unit" | sha256sum -c - >/dev/null
  sed "s|@VERTEP_ROOT@|$INSTALL_ROOT|g" "/tmp/$unit" > "/etc/systemd/system/$unit"
  rm -f "/tmp/$unit"
done
systemctl daemon-reload
systemctl enable --now vertep-update.path vertep-update.timer vertep-deployment.path vertep-watchdog.timer
# Startup recovery is a boot-time guard. Running it here would race the initial
# Compose deployment that bootstrap performs immediately below.
systemctl enable vertep-startup-recovery.service

progress "Starting Vertep $version"
compose=(docker compose --env-file "$INSTALL_ROOT/.env" -f "$INSTALL_ROOT/docker-compose.yml")
[[ $gpu_vendor == amd ]] && compose+=(-f "$INSTALL_ROOT/docker-compose.amd.yml")
[[ $gpu_vendor == nvidia ]] && compose+=(-f "$INSTALL_ROOT/docker-compose.nvidia.yml")
"${compose[@]}" pull "${role_services[@]}"
"${compose[@]}" up -d --remove-orphans "${role_services[@]}"
"${compose[@]}" wait migrate
migrate_id=$("${compose[@]}" ps -aq migrate)
[[ -n $migrate_id && $(docker inspect -f '{{.State.ExitCode}}' "$migrate_id") -eq 0 ]] \
  || fail "database migration did not complete successfully"
deadline=$((SECONDS+600))
service_count=$((${#role_services[@]}-1))
until [[ $("${compose[@]}" ps --services --filter status=running | wc -l) -eq $service_count ]] \
  && ! "${compose[@]}" ps | grep -Eq '\(unhealthy\)|\(health: starting\)' \
  && curl -kfsS https://127.0.0.1:8443/api/health >/dev/null; do
  (( SECONDS < deadline )) || { "${compose[@]}" ps; fail "runtime health check timed out"; }
  sleep 5
done
inventory_rows=$(mktemp)
for service in "${role_services[@]}"; do
  container_id=$("${compose[@]}" ps -q "$service")
  [[ -n $container_id ]] || continue
  docker inspect "$container_id" | jq --arg service "$service" '.[0] | {
    service:$service,image:.Config.Image,image_digest:.Image,
    state:.State.Status,health:(.State.Health.Status // "")}' >> "$inventory_rows"
done
jq -s --arg version "$version" --arg role "$NODE_ROLE" --argjson services "$(printf '%s\n' "${role_services[@]}" | jq -R . | jq -s .)" \
  '{schema:1,version:$version,role:$role,generated_at:(now|todate),services:$services,containers:.}' \
  "$inventory_rows" > "$INSTALL_ROOT/config/runtime-inventory.json"
rm -f "$inventory_rows"
chmod 0600 "$INSTALL_ROOT/config/runtime-inventory.json"
server_ip=$(hostname -I | awk '{print $1}')
setup_url="https://$server_ip:8443/setup?token=$setup_token"
printf '\nVertep Setup is ready.\n'
printf 'Open %s in your browser to complete the First Run Wizard.\n' "$setup_url"
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$setup_url" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
  open "$setup_url" >/dev/null 2>&1 || true
fi
printf 'Setup code (if needed): %s\n' "$setup_token"
