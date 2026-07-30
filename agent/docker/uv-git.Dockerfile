ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.29-python3.12-bookworm-slim
FROM ${UV_IMAGE}

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
