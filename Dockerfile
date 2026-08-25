# syntax=docker/dockerfile:1
# Base digest resolved 2026-08-24 via:
#   docker pull python:3.12-slim
#   docker inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
# -> python@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a
# Requalify (rebuild + rerun this task's full test suite) before changing this pin.
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS builder

RUN pip install --no-cache-dir uv==0.9.7

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS runtime

# ffmpeg: required by audio_pipeline.py (T14). Hard-pinned to the exact apt
# candidate version resolved 2026-08-24 via:
#   apt-get update && apt-cache policy ffmpeg
# -> candidate 7:7.1.5-0+deb13u1 (Debian trixie mirror; note the "7:" epoch
# prefix required by apt's package=version syntax -- `ffmpeg -version` does
# not print the epoch, only the mirror does). Pinned explicitly by the
# version pin on this line, independent of the base image digest above --
# apt fetches live package indices from the Debian mirror at build time.
# If this exact version ever disappears from the mirror, the build fails
# loudly instead of silently drifting; re-resolve via the commands above and
# requalify (rebuild + rerun this task's full test suite) before bumping.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg=7:7.1.5-0+deb13u1 && \
    rm -rf /var/lib/apt/lists/*

# supercronic: non-root-capable cron for the app container's daily-send
# timer (Task 7). Version + sha256 resolved 2026-08-24 via:
#   gh release view v0.2.49 --repo aptible/supercronic --json assets
# (the `digest` field GitHub computes for the linux-amd64 asset), and
# independently re-verified by downloading the binary and running
# `sha256sum` on it locally -- both matched. Do not use `latest`.
ARG SUPERCRONIC_VERSION=v0.2.49
ARG SUPERCRONIC_SHA256=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - && \
    chmod +x /usr/local/bin/supercronic

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /build/.venv /app/.venv
COPY src /app/src
COPY scripts /app/scripts
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" PYTHONPATH="/app/src"

# /data is where the db_data named volume (docker-compose.yml) mounts the
# SQLite database the daily-send cron job (Task 7) writes as appuser. A
# fresh named volume with nothing at this path in the image is created by
# Docker owned root:root mode 755 -- appuser could not write to it at all
# (confirmed empirically: "Permission denied" on every write). Pre-creating
# and chowning the directory here means Docker copies this ownership onto
# the volume the first time it's mounted, so appuser can actually create
# /data/app.db.
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser
