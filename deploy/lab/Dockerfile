# Autonomous laboratory control image (Issue #107).
#
# The laboratory is a separate, isolated subsystem: its own gate, lease
# contract, orchestrator and GitHub governance.  It never imports production
# CORE, never touches the production Docker socket, volumes, secrets or
# databases, and unless the owner explicitly enables idle sharing it cannot
# use the production GPU.  The only dependency beyond the standard library is
# the FastAPI/HTTPX runtime, which the production runtime image already
# provides.
FROM python:3.10-slim AS base

ENV VERTEP_LAB_HOME=/srv/vertep-lab
WORKDIR $VERTEP_LAB_HOME

# Only the standard library plus FastAPI is required for the control image.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The isolated laboratory subsystem lives next to, never inside production.
COPY lab ./lab
COPY scripts/lab-orchestrator.py .

CMD ["python", "-m", "lab.cli", "status"]
