FROM python:3.12-slim

WORKDIR /app

RUN useradd -u 65532 -m storyboard

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY storyboard_logic.py storyboard_module.py manifest.yaml ./
COPY migrations/ ./migrations/
COPY ui/ ./ui/

USER 65532

EXPOSE 8080

CMD ["python", "storyboard_module.py"]
