#!/usr/bin/env bash
# One-command Vertep laboratory install for a separate, clean Ubuntu Server 24.04 / 26.04 VM.
# One brace group: `curl … | bash` must parse the whole script before anything runs.
#
# By default the LAB is installed from its signed component release lab.<version>
# (#491 L5): package.json is checked against its sha256 and the Vertep publisher key
# (config/module-trust/publishers.json) before anything starts, and the images run
# pinned by digest. --build-from-source (or VERTEP_LAB_BUILD_FROM_SOURCE=1) builds
# from a git checkout instead — for development only.
{
set -Eeuo pipefail
umask 077

REPOSITORY=${VERTEP_LAB_REPOSITORY:-https://github.com/fylypovych/vertep.git}
GITHUB_REPO=${VERTEP_LAB_GITHUB_REPO:-fylypovych/vertep}
RELEASES_API=${VERTEP_LAB_RELEASES_API:-https://api.github.com/repos/$GITHUB_REPO/releases?per_page=100}
DOWNLOAD_BASE=${VERTEP_LAB_DOWNLOAD_BASE:-https://github.com/$GITHUB_REPO/releases/download}
LAB_ROOT=${VERTEP_LAB_ROOT:-/opt/vertep-lab}
REF=${VERTEP_LAB_REF:-}
LAB_TAG=${VERTEP_LAB_TAG:-}
TRUST_URL=${VERTEP_LAB_TRUST_URL:-https://raw.githubusercontent.com/$GITHUB_REPO/${REF:-main}/config/module-trust/publishers.json}
BUILD_FROM_SOURCE=${VERTEP_LAB_BUILD_FROM_SOURCE:-0}
[[ ${1:-} == --build-from-source ]] && BUILD_FROM_SOURCE=1
MIN_RAM_MB=${VERTEP_LAB_MIN_RAM_MB:-4096}  # owner decision 16.11.2026 (was 8192)
MIN_DISK_MB=${VERTEP_LAB_MIN_DISK_MB:-30720}

fail(){ printf 'VERTEP LAB: %s\n' "$*" >&2; exit 1; }
progress(){ printf '\n==> %s\n' "$*"; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "run as root (curl … | sudo bash)"
. /etc/os-release
# The same supported hosts as bootstrap.sh (0.0.3.60); docker.io/docker-compose-v2 come from Ubuntu.
[[ ${ID:-} == ubuntu && ( ${VERSION_ID:-} == 24.04 || ${VERSION_ID:-} == 26.04 ) ]] \
  || fail "Ubuntu Server 24.04 or 26.04 LTS is required"
# The laboratory must stay isolated from production (AGENTS.md §4.6).
[[ ! -e /opt/vertep/docker-compose.yml ]] || fail "Vertep is installed here; use a separate VM"
ram_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
disk_mb=$(df -Pm / | awk 'NR==2{print $4}')
(( ram_mb >= MIN_RAM_MB )) || fail "at least ${MIN_RAM_MB} MB RAM is required (found ${ram_mb})"
(( disk_mb >= MIN_DISK_MB )) || fail "at least ${MIN_DISK_MB} MB free disk is required (found ${disk_mb})"

progress "Installing Docker"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq </dev/null
apt-get install -y -qq ca-certificates curl git openssl python3 python3-cryptography \
  docker.io docker-compose-v2 </dev/null >/dev/null
systemctl enable --now docker >/dev/null
install -d -m 0700 "$LAB_ROOT"

# The newest stable lab.<version>, verified (sha256 + Ed25519 signature of the Vertep
# publisher) before its compose file and image digests are used. Nothing is built.
install_from_release(){
  local package=$LAB_ROOT/package trust
  install -d -m 0700 "$package"
  if [[ -z $LAB_TAG ]]; then
    LAB_TAG=$(curl -fsSL "$RELEASES_API" | python3 -I -c '
import json, re, sys
tags = [r["tag_name"] for r in json.load(sys.stdin)
        if re.fullmatch(r"lab\.\d+\.\d+\.\d+\.\d+", r.get("tag_name", "")) and not r.get("draft") and not r.get("prerelease")]
print(max(tags, key=lambda t: tuple(map(int, t.split(".")[1:]))) if tags else "")')
    [[ -n $LAB_TAG ]] || fail "no signed LAB release (lab.<version>) found"
  fi
  progress "Fetching the signed LAB package $LAB_TAG"
  curl -fsSL "$DOWNLOAD_BASE/$LAB_TAG/package.json" -o "$package/package.json"
  curl -fsSL "$DOWNLOAD_BASE/$LAB_TAG/package.json.sha256" -o "$package/package.json.sha256"
  (cd "$package" && sha256sum -c --status package.json.sha256) || fail "package.json does not match its sha256"
  # A checkout run (./bootstrap-lab.sh) uses its own trust file; `curl | bash` fetches it
  # from the same repository and release as this script.
  trust=
  [[ -f ${BASH_SOURCE[0]:-} ]] && trust=$(dirname "${BASH_SOURCE[0]}")/config/module-trust/publishers.json
  if [[ ! -f $trust ]]; then
    trust=$package/publishers.json
    curl -fsSL "$TRUST_URL" -o "$trust"
  fi
  python3 -I - "$package/package.json" "$trust" "$LAB_ROOT" > "$package/images.env" <<'PY' \
    || fail "the LAB package signature or content is not valid"
import base64, json, re, sys
from cryptography.hazmat.primitives import serialization
package_path, trust_path, root = sys.argv[1:]
package = json.load(open(package_path, encoding="utf-8"))
trust = json.load(open(trust_path, encoding="utf-8"))["vertep"]
content = {"manifest": package["manifest"], "files": package["files"]}
raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
key = serialization.load_pem_public_key(trust["keys"][package["signer"]].encode())
key.verify(base64.b64decode(package["signature"], validate=True), raw)  # raises on forgery
manifest = package["manifest"]
assert manifest["name"] == "lab" and manifest["publisher"] == "vertep"
assert manifest["placement"]["target"] == "isolated_lab"
assert manifest["provenance"]["repository"] in trust["repositories"]
images = json.loads(base64.b64decode(package["files"]["images.json"]))
pinned = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}")
assert {"lab-control", "lab-web"} <= set(images) <= {"lab-control", "lab-web", "lab-eval"} and all(pinned.fullmatch(v) for v in images.values())
assert images["lab-control"] == manifest["container"]["image"]
with open(f"{root}/docker-compose.yml", "wb") as handle:
    handle.write(base64.b64decode(package["files"]["docker-compose.yml"]))
lines = [f"LAB_CONTROL_IMAGE={images['lab-control']}", f"LAB_WEB_IMAGE={images['lab-web']}"]
if "lab-eval" in images:
    lines.append(f"LAB_EVAL_IMAGE={images['lab-eval']}")
lines.append(f"LAB_VERSION={manifest['version']}")
print("\n".join(lines))
PY
  set_value(){ sed -i "/^$1=/d" "$env_file"; printf '%s=%s\n' "$1" "$2" >> "$env_file"; }
  while IFS='=' read -r name value; do set_value "$name" "$value"; done < "$package/images.env"
  progress "Starting the signed images"
  docker compose --env-file "$env_file" -p vertep-lab-web -f "$LAB_ROOT/docker-compose.yml" pull -q </dev/null
  docker compose --env-file "$env_file" -p vertep-lab-web -f "$LAB_ROOT/docker-compose.yml" \
    up -d --no-build --wait </dev/null
  release_label=$LAB_TAG
}

# Development only: a git checkout built on the VM.
build_from_source(){
  progress "Fetching the laboratory sources (development build)"
  if [[ -z $REF ]]; then
    REF=$(curl -fsSL "https://api.github.com/repos/$GITHUB_REPO/releases/latest" \
      | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
    [[ -n $REF ]] || fail "could not determine the latest release"
  fi
# The checkout is copied into images that run as unprivileged users (nginx, the sandbox):
# it stays readable inside the 0700 LAB_ROOT. Only git runs with umask 022; the secrets
# written below keep the script's umask 077. A checkout made by an older bootstrap with
# 0600 files is opened up once.
if [[ -d $LAB_ROOT/src/.git ]]; then
  (umask 022
   git -C "$LAB_ROOT/src" fetch -q --depth 1 origin "$REF" </dev/null
   git -C "$LAB_ROOT/src" checkout -q --detach FETCH_HEAD </dev/null)
else
  (umask 022; git clone -q --depth 1 --branch "$REF" "$REPOSITORY" "$LAB_ROOT/src" </dev/null)
fi
chmod -R go+rX "$LAB_ROOT/src"
  progress "Building and starting (the first run takes several minutes)"
  docker compose --env-file "$env_file" -p vertep-lab-web \
    -f "$LAB_ROOT/src/deploy/lab/docker-compose.web.yml" \
    -f "$LAB_ROOT/src/deploy/lab/docker-compose.web.build.yml" up -d --build --wait </dev/null
  release_label="$REF (з джерел)"
}

progress "Preparing configuration"
env_file=$LAB_ROOT/lab.env
if [[ ! -f $env_file ]]; then
  cat > "$env_file" <<EOF
LAB_OWNER_TOKEN=$(openssl rand -hex 32)
LAB_OWNER_IDENTITY=owner
LAB_IDLE_SHARING_ENABLED=false
LAB_RESOURCE_URL=
LAB_RESOURCE_TOKEN=
LAB_GITHUB_TOKEN=
LAB_GITHUB_REPOSITORY=
LAB_RESOURCE_CA_FILE=
LAB_VERTEP_URL=
EOF
fi
# Added separately so a re-run upgrades an older lab.env without touching its secrets.
install -d -m 0755 "$LAB_ROOT/ca"
set_default(){ grep -q "^$1=." "$env_file" || { sed -i "/^$1=/d" "$env_file"; printf '%s=%s\n' "$1" "$2" >> "$env_file"; }; }
set_default LAB_ROLE_EXECUTOR_URL http://lab-executor:8100
set_default LAB_ROLE_EXECUTOR_ENABLED true
set_default LAB_ROLE_EXECUTOR_TOKEN "$(openssl rand -hex 32)"
set_default LAB_CA_DIR "$LAB_ROOT/ca"
set_default LAB_TLS_DIR "$LAB_ROOT/tls"
set_default LAB_WEB_BIND 0.0.0.0
chmod 600 "$env_file"

tls_dir=$(sed -n 's/^LAB_TLS_DIR=//p' "$env_file")
install -d -m 0755 "$tls_dir"
web_port=$(sed -n 's/^LAB_WEB_PORT=//p' "$env_file"); web_port=${web_port:-8444}
vm_address=$(hostname -I 2>/dev/null | awk '{print $1}'); vm_address=${vm_address:-<адреса VM>}

# Connecting to Vertep (#491): the command from «Налаштування → Лабораторія» passes CORE's
# address, a one-time token and CORE's certificate fingerprint. The fingerprint is checked on
# the same TLS connection that carries the token; the panel key is made here and never leaves
# the VM. CORE answers with the panel certificate (signed by its CA) and the GPU-lease
# credential; the Vertep address, the lease address and CORE's certificate are set from them.
connected=0
if [[ -n ${VERTEP_CORE_URL:-}${VERTEP_LAB_TOKEN:-} ]]; then
  [[ -n ${VERTEP_CORE_URL:-} && -n ${VERTEP_LAB_TOKEN:-} && -n ${VERTEP_CORE_PIN:-} ]] \
    || fail "VERTEP_CORE_URL, VERTEP_LAB_TOKEN and VERTEP_CORE_PIN are required to connect to Vertep"
  progress "Connecting the laboratory to Vertep"
  core_url=${VERTEP_CORE_URL%/}
  ca_dir=$(sed -n 's/^LAB_CA_DIR=//p' "$env_file")
  # shellcheck disable=SC2046
  resource_token=$(VERTEP_LAB_TOKEN=$VERTEP_LAB_TOKEN python3 -I - "$core_url" "$VERTEP_CORE_PIN" \
      "https://$vm_address:$web_port" "$tls_dir" "$ca_dir/vertep-core.pem" \
      "$(hostname -f 2>/dev/null || hostname)" $(hostname -I 2>/dev/null) <<'PY'
import hashlib, http.client, ipaddress, json, os, ssl, sys
from urllib.parse import urlsplit
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

core, pin, panel_url, tls_dir, ca_file, *hosts = sys.argv[1:]
parts = urlsplit(core)
if parts.scheme != "https" or not parts.hostname:
    sys.exit("VERTEP_CORE_URL must be https://host[:port]")
context = ssl.create_default_context()
context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # trust comes from the pin below
connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, context=context, timeout=60)
connection.connect()
der = connection.sock.getpeercert(binary_form=True)
if hashlib.sha256(der).hexdigest() != pin.lower().replace(":", ""):
    sys.exit("CORE certificate fingerprint does not match VERTEP_CORE_PIN; nothing was sent")
names = []
for host in dict.fromkeys(hosts):
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        names.append(x509.DNSName(host))
key = ec.generate_private_key(ec.SECP256R1())
csr = (x509.CertificateSigningRequestBuilder()
       .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Vertep LAB")]))
       .add_extension(x509.SubjectAlternativeName(names), critical=False).sign(key, hashes.SHA256()))
body = json.dumps({"token": os.environ["VERTEP_LAB_TOKEN"], "panel_url": panel_url,
                   "csr": csr.public_bytes(serialization.Encoding.PEM).decode()})
connection.request("POST", "/api/lab/connect", body, {"Content-Type": "application/json"})
response = connection.getresponse()
answer = response.read().decode("utf-8", "replace")
if response.status != 200:
    sys.exit(f"Vertep refused the connection ({response.status}): {answer[:300]}")
answer = json.loads(answer)

def write(path, data, mode):
    temporary = f"{path}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(data)
    os.replace(temporary, path)

write(os.path.join(tls_dir, "lab.key"), key.private_bytes(serialization.Encoding.PEM,
      serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), 0o600)
write(os.path.join(tls_dir, "lab.crt"), answer["panel_certificate"] + answer["panel_ca"], 0o644)
write(ca_file, ssl.DER_cert_to_PEM_cert(der), 0o644)
print(answer["resource_token"])
PY
) || fail "could not connect the laboratory to Vertep"
  set_value(){ sed -i "/^$1=/d" "$env_file"; printf '%s=%s\n' "$1" "$2" >> "$env_file"; }
  set_value LAB_VERTEP_URL "$core_url"
  set_value LAB_RESOURCE_URL "$core_url/lab-lease/v1"
  set_value LAB_RESOURCE_TOKEN "$resource_token"
  set_value LAB_RESOURCE_CA_FILE /run/lab-ca/vertep-core.pem
  chmod 600 "$env_file"
  connected=1
fi

# HTTPS panel (#491 L2/L8): a certificate from Vertep (above) or the operator's own pair is
# kept; otherwise a self-signed pair for this VM's name and addresses is made once.
if [[ ! -s $tls_dir/lab.crt || ! -s $tls_dir/lab.key ]]; then
  progress "Creating a self-signed TLS certificate for the panel"
  san="DNS:$(hostname -f 2>/dev/null || hostname),DNS:localhost"
  for address in $(hostname -I 2>/dev/null); do san="$san,IP:$address"; done
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 \
    -subj "/CN=Vertep LAB" -addext "subjectAltName=$san" \
    -keyout "$tls_dir/lab.key" -out "$tls_dir/lab.crt" 2>/dev/null
fi
# nginx in lab-web runs as uid 101; the key stays readable by it alone.
chown 101:101 "$tls_dir/lab.key" && chmod 0400 "$tls_dir/lab.key" && chmod 0644 "$tls_dir/lab.crt"

if [[ $BUILD_FROM_SOURCE == 1 ]]; then build_from_source; else install_from_release; fi

printf '\nЛабораторію встановлено (%s).\n' "$release_label"
printf 'Панель (HTTPS): https://%s:%s\n' "$vm_address" "$web_port"
if [[ $connected == 1 ]]; then
  printf 'Підключено до Vertep: %s — вхід «Увійти через Vertep»; сертифікат панелі видав Vertep.\n' "$core_url"
elif grep -q '^LAB_RESOURCE_TOKEN=.' "$env_file"; then
  printf 'Вхід: «Увійти через Vertep».\n'
else
  printf 'Не підключено до Vertep: у Vertep відкрийте «Налаштування → Лабораторія» → «Підключити лабораторію» і виконайте показану команду на цій VM.\n'
fi
printf 'Службовий токен власника — у %s (LAB_OWNER_TOKEN).\n' "$env_file"
exit 0
}
