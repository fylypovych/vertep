FROM python:3.12-slim
ARG MPT_COMMIT=2e1b30396e059e55939cc802c60faac2061e4d41
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
# Install FFmpeg and other system dependencies required by MoneyPrinterTurbo
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl git ffmpeg libsm6 libxext6 libxrender-dev libglib2.0-0 \
        wget; \
    rm -rf /var/lib/apt/lists/*; \
    git clone --depth 1 --single-branch --branch main https://github.com/harry0703/MoneyPrinterTurbo.git /opt/moneyprinter; \
    cd /opt/moneyprinter; \
    git checkout "$MPT_COMMIT"
# Install Python dependencies
RUN set -eux; \
    cd /opt/moneyprinter; \
    python3 -m pip install --upgrade pip; \
    python3 -m pip install -r requirements.txt
# Record the resolved commit and dependency inventory so readiness checks can detect runtime drift (Issue #122 P2)
RUN set -eux; \
    python3 - "$MPT_COMMIT" <<'PY'
import json, subprocess, sys

commit = subprocess.run(["git", "-C", "/opt/moneyprinter", "rev-parse", "HEAD"], check=True,
                        capture_output=True, text=True).stdout.strip()
# Capture pip freeze for dependency inventory
pip_freeze = subprocess.run(["python3", "-m", "pip", "freeze"], check=True,
                            capture_output=True, text=True).stdout.strip()
report = {
    "format": "moneyprinter_runtime/v1",
    "bridge_version": "v1",
    "bridge_schema_version": "v1",
    "components": {
        "moneyprinter": {
            "version": "main",
            "commit": commit,
            "repository": "harry0703/MoneyPrinterTurbo"
        }
    },
    "dependencies": pip_freeze.splitlines()
}
with open("/opt/moneyprinter/runtime-inventory.json", "w", encoding="utf-8") as handle:
    json.dump(report, handle, indent=2, sort_keys=True)
PY
# Create task-scoped working directory
RUN mkdir -p /opt/moneyprinter/tasks /opt/moneyprinter/storage/local_videos
WORKDIR /opt/moneyprinter
# Expose the API port (MoneyPrinterTurbo default is 8080)
EXPOSE 8080
# Start the API server
CMD ["python3", "app/main.py", "--host", "0.0.0.0", "--port", "8080"]
