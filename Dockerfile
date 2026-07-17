FROM python:3.12-slim

WORKDIR /srv/app

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 grc \
    && mkdir -p /data \
    && chown -R grc:grc /srv/app /data

USER grc

ENV GRC_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

CMD ["sh", "-c", "python -m app.cli migrate && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
