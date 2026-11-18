FROM python:3.12-slim

# Create non-root user (UID 65532 per module contract)
RUN useradd -u 65532 -m -s /bin/bash moduleuser

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt modules/characters/requirements.txt
RUN pip install --no-cache-dir -r modules/characters/requirements.txt

# Copy module code
COPY __init__.py modules/characters/__init__.py
COPY characters_module.py modules/characters/
COPY characters_logic.py modules/characters/
COPY manifest.yaml modules/characters/
COPY migrations/ modules/characters/migrations/
COPY ui/ modules/characters/ui/

# Create directories for persistent data
RUN mkdir -p /data/characters && chown -R moduleuser:moduleuser /data

# Switch to non-root user
USER moduleuser

# Environment variables (set by Worker supervisor)
ENV VERTEP_CORE_URL=""
ENV VERTEP_MODULE_TOKEN=""
ENV CHARACTERS_ROOT="/data/characters"

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" || exit 1

# Expose port
EXPOSE 8080

# Run module
CMD ["python", "-m", "modules.characters.characters_module"]