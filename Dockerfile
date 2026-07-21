FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/autoflow
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

EXPOSE 8765
CMD ["autoflow", "--host", "0.0.0.0", "--port", "8765"]

