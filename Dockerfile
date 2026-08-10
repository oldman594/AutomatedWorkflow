FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        docker-cli \
        git \
        postgresql-client \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/autoflow
COPY pyproject.toml requirements.prod.lock README.md ./
RUN pip install --no-cache-dir --requirement requirements.prod.lock
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8765
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=10 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/ready', timeout=3)"]
CMD ["autoflow", "--host", "0.0.0.0", "--port", "8765"]
