# Demo Application Module image (#495). Pin the base by digest when building a release package.
FROM python:3.12-alpine
RUN pip install --no-cache-dir pg8000==1.31.2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app.py /app/app.py
COPY ui /app/ui
USER 65532:65532
EXPOSE 8080
CMD ["python", "/app/app.py"]
