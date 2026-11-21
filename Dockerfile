FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LEDGER_DB=/data/ledger.db

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY ledger ./ledger
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 ledger \
    && mkdir /data \
    && chown ledger /data

USER ledger
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["python", "-m", "ledger", "serve", "--host", "0.0.0.0", "--port", "8000"]
