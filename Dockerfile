FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/receiver.db

WORKDIR /srv

COPY app/ ./app/
COPY tests/ ./tests/
COPY scripts/ ./scripts/

RUN mkdir -p /data && python -m compileall -q app

EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=10 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz', timeout=2).status == 200 else 1)"

CMD ["python", "-m", "app.server"]
