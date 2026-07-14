FROM python:3.12-slim

WORKDIR /srv/app

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir .

ENV GRC_DATABASE_PATH=/data/grc.db
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
