FROM python:3.12-slim

WORKDIR /app

RUN useradd -u 65532 -m storyboard

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# #501: no database of its own; storyboards live in the jobs and come from CORE.
COPY storyboard_module.py manifest.yaml ./
COPY ui/ ./ui/

USER 65532

EXPOSE 8080

CMD ["python", "storyboard_module.py"]
