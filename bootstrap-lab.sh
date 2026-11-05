#!/usr/bin/env bash
# One-command Vertep laboratory install for a separate, clean Ubuntu Server 24.04 VM.
# One brace group: `curl … | bash` must parse the whole script before anything runs.
{
set -Eeuo pipefail
umask 077

REPOSITORY=${VERTEP_LAB_REPOSITORY:-https://github.com/fylypovych/vertep.git}
RELEASES_API=${VERTEP_LAB_RELEASES_API:-https://api.github.com/repos/fylypovych/vertep/releases/latest}
LAB_ROOT=${VERTEP_LAB_ROOT:-/opt/vertep-lab}
REF=${VERTEP_LAB_REF:-}
MIN_RAM_MB=${VERTEP_LAB_MIN_RAM_MB:-8192}
MIN_DISK_MB=${VERTEP_LAB_MIN_DISK_MB:-30720}

fail(){ printf 'VERTEP LAB: %s\n' "$*" >&2; exit 1; }
progress(){ printf '\n==> %s\n' "$*"; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "run as root (curl … | sudo bash)"
. /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail "Ubuntu Server 24.04 LTS is required"
# The laboratory must stay isolated from production (AGENTS.md §4.6).
[[ ! -e /opt/vertep/docker-compose.yml ]] || fail "Vertep is installed here; use a separate VM"
ram_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
disk_mb=$(df -Pm / | awk 'NR==2{print $4}')
(( ram_mb >= MIN_RAM_MB )) || fail "at least ${MIN_RAM_MB} MB RAM is required (found ${ram_mb})"
(( disk_mb >= MIN_DISK_MB )) || fail "at least ${MIN_DISK_MB} MB free disk is required (found ${disk_mb})"

progress "Installing Docker"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq </dev/null
apt-get install -y -qq ca-certificates curl git openssl docker.io docker-compose-v2 </dev/null >/dev/null
systemctl enable --now docker >/dev/null

progress "Fetching the laboratory"
if [[ -z $REF ]]; then
  REF=$(curl -fsSL "$RELEASES_API" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
  [[ -n $REF ]] || fail "could not determine the latest release"
fi
install -d -m 0700 "$LAB_ROOT"
if [[ -d $LAB_ROOT/src/.git ]]; then
  git -C "$LAB_ROOT/src" fetch -q --depth 1 origin "$REF" </dev/null
  git -C "$LAB_ROOT/src" checkout -q --detach FETCH_HEAD </dev/null
else
  git clone -q --depth 1 --branch "$REF" "$REPOSITORY" "$LAB_ROOT/src" </dev/null
fi

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
EOF
fi
# Added separately so a re-run upgrades an older lab.env without touching its secrets.
install -d -m 0755 "$LAB_ROOT/ca"
set_default(){ grep -q "^$1=." "$env_file" || { sed -i "/^$1=/d" "$env_file"; printf '%s=%s\n' "$1" "$2" >> "$env_file"; }; }
set_default LAB_ROLE_EXECUTOR_URL http://lab-executor:8100
set_default LAB_ROLE_EXECUTOR_ENABLED true
set_default LAB_ROLE_EXECUTOR_TOKEN "$(openssl rand -hex 32)"
set_default LAB_CA_DIR "$LAB_ROOT/ca"
chmod 600 "$env_file"

progress "Building and starting (the first run takes several minutes)"
docker compose --env-file "$env_file" -p vertep-lab-web \
  -f "$LAB_ROOT/src/deploy/lab/docker-compose.web.yml" up -d --build --wait </dev/null

owner_token=$(sed -n 's/^LAB_OWNER_TOKEN=//p' "$env_file")
printf '\nЛабораторію встановлено (%s).\n' "$REF"
printf 'Панель доступна лише локально: http://127.0.0.1:8444\n'
printf 'З вашого комп'"'"'ютера: ssh -L 8444:127.0.0.1:8444 <користувач>@<адреса VM>\n'
printf 'Токен власника: %s\n' "$owner_token"
printf 'Налаштування: %s (спільний GPU вимкнено, доки ви його не ввімкнете).\n' "$env_file"
printf 'Сертифікат Vertep для спільного GPU покладіть у %s/ca/vertep-ca.pem\n' "$LAB_ROOT"
exit 0
}
