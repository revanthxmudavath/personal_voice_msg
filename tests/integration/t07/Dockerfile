FROM python:3.12.4-slim-bookworm@sha256:a3e58f9399353be051735f09be0316bfdeab571a5c6a24fd78b92df85bcb2d85

WORKDIR /workspace

COPY tests/integration/t07/requirements.txt /workspace/requirements.txt
RUN python -m pip install --no-cache-dir -r /workspace/requirements.txt

COPY src /workspace/src
COPY pyproject.toml /workspace/pyproject.toml
COPY tests/integration /workspace/tests/integration

ENV PYTHONPATH=/workspace/src
