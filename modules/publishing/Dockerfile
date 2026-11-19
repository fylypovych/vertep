FROM python:3.12-slim

WORKDIR /app

RUN useradd -u 65532 -m publishing

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# #502: no database, channels or send path of its own: CORE orchestrates, the
# Worker sends, Brands owns channels and policies.
COPY publishing_module.py manifest.yaml ./
COPY ui/ ./ui/

USER 65532

EXPOSE 8080

CMD ["python", "publishing_module.py"]
