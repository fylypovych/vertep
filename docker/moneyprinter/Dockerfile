# Vertep pinned MoneyPrinterTurbo runtime (Issue #122 P2).
#
# One image carries two things:
#   /opt/moneyprinter  pinned upstream sources, byte-verified by SHA256
#   /opt/vertep        the Vertep wrapper (health/self-test + runtime inventory)
#
# The upstream API binds 127.0.0.1 inside the container; the wrapper is the only
# externally reachable endpoint. Publishing to social networks is impossible: the
# rendered config lock disables upload_post_* and the entrypoint asserts it on
# every start.
#
# Pinned upstream: harry0703/MoneyPrinterTurbo@2e1b30396e059e55939cc802c60faac2061e4d41
# The SHA256 below is the digest of the codeload tarball for exactly that commit,
# so a moving `main` branch can never silently change the build input.
FROM python:3.12-slim-bookworm

ARG MPT_COMMIT=2e1b30396e059e55939cc802c60faac2061e4d41
ARG MPT_TARBALL_SHA256=cc2031d43b0d83d47efd631f1de7f3093df31fe26bb7374a28e7f05fa7d538a2
ARG MPT_VERSION=1.3.7
ARG BRIDGE_VERSION=v1
ARG BRIDGE_SCHEMA_VERSION=v1
# Stamped into the image so an immutable digest can be traced back to the exact
# Vertep release that produced it; production consumes images by digest only.
ARG VERTEP_VERSION=dev

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MONEYPRINTER_WRAPPER_PORT=8098

# FFmpeg performs every clip, voice and subtitle operation of the pinned pipeline.
# curl backs the container healthcheck.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        libglib2.0-0 \
        libsm6; \
    rm -rf /var/lib/apt/lists/*

# --- Dependencies first so this layer caches independently of upstream sources -
COPY docker/moneyprinter/requirements.lock /tmp/requirements.lock
RUN set -eux; \
    python3 -m pip install --upgrade pip; \
    python3 -m pip install -r /tmp/requirements.lock; \
    rm -f /tmp/requirements.lock

# --- Pinned upstream sources -------------------------------------------------
# `--max-time` is the curl option that bounds the whole transfer; `--timeout` is not
# a curl option at all and made this layer fail on a clean CI build.
RUN set -eux; \
    curl -fsSL --retry 3 --connect-timeout 30 --max-time 300 \
        "https://codeload.github.com/harry0703/MoneyPrinterTurbo/tar.gz/${MPT_COMMIT}" \
        -o /tmp/mpt.tar.gz; \
    echo "${MPT_TARBALL_SHA256}  /tmp/mpt.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/moneyprinter; \
    tar -xzf /tmp/mpt.tar.gz -C /opt/moneyprinter --strip-components=1; \
    rm -f /tmp/mpt.tar.gz; \
    test -f /opt/moneyprinter/app/asgi.py

# --- Vertep wrapper ----------------------------------------------------------
COPY adapters /opt/vertep/adapters
COPY services /opt/vertep/services
COPY docker/moneyprinter/config.lock.toml /opt/moneyprinter/config.lock.toml
COPY docker/moneyprinter/entrypoint.sh /usr/local/bin/moneyprinter-entrypoint
RUN set -eux; \
    chmod 0755 /usr/local/bin/moneyprinter-entrypoint

# --- Runtime inventory -------------------------------------------------------
# Records what the image was actually built from: the upstream commit that the
# wrapper compares against MONEY_PRINTER_CONTRACT.upstream_reference, the
# installed dependency inventory, and the config lock digest. Readiness checks
# fail closed on any drift instead of trusting the build arguments.
RUN set -eux; \
    PYTHONPATH=/opt/vertep python3 - \
        "${MPT_COMMIT}" "${MPT_VERSION}" "${BRIDGE_VERSION}" "${BRIDGE_SCHEMA_VERSION}" <<'PY'
import hashlib
import json
import sys
from importlib.metadata import distributions
from pathlib import Path

from adapters.providers.runtime_manifest import (
    PINNED_UPSTREAM_REPOSITORY,
    build_inventory,
)

commit, version, bridge_version, bridge_schema_version = sys.argv[1:5]

installed = sorted(
    f"{dist.metadata['Name']}=={dist.version}"
    for dist in distributions()
    if dist.metadata["Name"]
)

config_lock = Path("/opt/moneyprinter/config.lock.toml")
# The commit recorded here is the one that was actually downloaded, not the
# constant: if MPT_COMMIT ever drifts from the pinned contract, the entrypoint's
# expected_commit gate refuses to start instead of running an unverified runtime.
inventory = build_inventory(
    repository=PINNED_UPSTREAM_REPOSITORY,
    commit=commit,
    version=version,
    bridge_version=bridge_version,
    bridge_schema_version=bridge_schema_version,
    dependencies=installed,
    file_digests={"config.lock.toml": hashlib.sha256(config_lock.read_bytes()).hexdigest()},
)
Path("/opt/moneyprinter/runtime-inventory.json").write_text(
    json.dumps(inventory, indent=2, sort_keys=True), encoding="utf-8")
PY

# --- Non-root runtime user ---------------------------------------------------
RUN set -eux; \
    groupadd --gid 10001 vertep; \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /home/vertep vertep; \
    mkdir -p /opt/moneyprinter/storage/tasks \
             /opt/moneyprinter/storage/local_videos \
             /opt/moneyprinter/storage/vertep-submits \
             /opt/moneyprinter/.vertep-voice; \
    chown -R vertep:vertep /opt/moneyprinter /opt/vertep

USER 10001:10001
WORKDIR /opt/vertep

LABEL org.opencontainers.image.title="Vertep MoneyPrinterTurbo runtime" \
      org.opencontainers.image.source="https://github.com/fylypovych/vertep" \
      org.opencontainers.image.version="${VERTEP_VERSION}" \
      org.opencontainers.image.vendor="Vertep" \
      io.vertep.moneyprinter.upstream-commit="${MPT_COMMIT}" \
      io.vertep.moneyprinter.upstream-version="${MPT_VERSION}" \
      io.vertep.moneyprinter.bridge-version="${BRIDGE_VERSION}" \
      io.vertep.moneyprinter.bridge-schema-version="${BRIDGE_SCHEMA_VERSION}"

# --- Liveness: the wrapper answers only after the upstream API is reachable ---
HEALTHCHECK --interval=20s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${MONEYPRINTER_WRAPPER_PORT}/health" || exit 1

ENTRYPOINT ["/usr/local/bin/moneyprinter-entrypoint"]