# Publisher module container (#510/#511): platform adapters only; built with the repository
# root as context (config/components.json -> image.context ".").
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir httpx==0.28.1 \
    && useradd --system --uid 65532 --home /nonexistent publisher \
    && install -d -o 65532 -g 65532 /data
# The module data volume takes this owner on first use (the container runs as 65532).
COPY core/__init__.py core/logging_config.py /app/core/
COPY publishers /app/publishers
COPY modules/publisher/ui /app/ui
COPY modules/publisher/app/publisher_app.py /app/publisher_app.py
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
CMD ["python", "/app/publisher_app.py"]
