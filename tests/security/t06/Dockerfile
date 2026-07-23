FROM python:3.12.4-slim-bookworm@sha256:a3e58f9399353be051735f09be0316bfdeab571a5c6a24fd78b92df85bcb2d85

WORKDIR /workspace

RUN mkdir -p /workspace/tls && \
    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout /workspace/tls/ca.key \
      -out /workspace/tls/ca.crt \
      -days 2 -sha256 -subj "/CN=T06 Test CA" && \
    openssl req -newkey rsa:2048 -nodes \
      -keyout /workspace/tls/server.key \
      -out /workspace/tls/server.csr \
      -subj "/CN=public.fixture.example" && \
    printf 'subjectAltName=DNS:public.fixture.example\n' \
      > /workspace/tls/server.ext && \
    openssl x509 -req \
      -in /workspace/tls/server.csr \
      -CA /workspace/tls/ca.crt \
      -CAkey /workspace/tls/ca.key \
      -CAcreateserial \
      -out /workspace/tls/server.crt \
      -days 2 -sha256 -extfile /workspace/tls/server.ext && \
    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout /workspace/tls/untrusted.key \
      -out /workspace/tls/untrusted.crt \
      -days 2 -sha256 \
      -subj "/CN=untrusted.fixture.example" \
      -addext "subjectAltName=DNS:untrusted.fixture.example" && \
    printf 'machine public.fixture.example\nlogin fixture\npassword fixture-only\n' \
      > /root/.netrc && \
    chmod 600 /root/.netrc

RUN python -m pip install --no-cache-dir \
    aiohappyeyeballs==2.7.1 \
    aiohttp==3.14.2 \
    aiosignal==1.4.0 \
    attrs==26.1.0 \
    frozenlist==1.8.0 \
    idna==3.18 \
    iniconfig==2.3.0 \
    multidict==6.7.1 \
    packaging==26.2 \
    pluggy==1.6.0 \
    propcache==0.5.2 \
    pygments==2.20.0 \
    pytest==9.1.1 \
    typing-extensions==4.16.0 \
    yarl==1.24.5

COPY src /workspace/src
COPY pyproject.toml /workspace/pyproject.toml
COPY tests/security /workspace/tests/security

ENV PYTHONPATH=/workspace/src
