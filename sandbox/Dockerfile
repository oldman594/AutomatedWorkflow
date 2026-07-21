FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cargo \
        cmake \
        git \
        ninja-build \
        nodejs \
        npm \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
