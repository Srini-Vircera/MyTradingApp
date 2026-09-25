# syntax=docker/dockerfile:1.7
# One image for the Python services: the operator API and the worker
# (scheduler + research/backtest jobs). The role is chosen at run time with
# AQ_ROLE=api|worker|migrate; see deploy/docker/entrypoint.sh.
# Build context: repository root.

ARG TARGETARCH=amd64

# tini (PID 1: signal forwarding, zombie reaping) as a checksum-pinned static binary
FROM scratch AS tini-amd64
ADD --checksum=sha256:c5b0666b4cb676901f90dfcb37106783c5fe2077b04590973b885950611b30ee --chmod=755 \
    https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-amd64 /tini
FROM scratch AS tini-arm64
ADD --checksum=sha256:eae1d3aa50c48fb23b8cbdf4e369d0910dfc538566bfd09df89a774aa84a48b9 --chmod=755 \
    https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-arm64 /tini
FROM tini-${TARGETARCH} AS tini

FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY deploy/requirements.lock ./requirements.lock
# Optional build secret "build_ca": an extra CA bundle for builds behind a
# TLS-inspecting proxy (docker build --secret id=build_ca,src=ca.pem). Unused on Railway.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi \
 && python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi \
 && /opt/venv/bin/pip install --no-deps . \
 && /opt/venv/bin/aq version

FROM python:3.12-slim-bookworm
# no extra OS packages: privileges are dropped with setpriv (util-linux, in the base image)
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
COPY --from=tini /tini /usr/local/bin/tini
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AQ_ENV=production \
    AQ_CONFIG_DIR=/app/config \
    TZ=UTC
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY config ./config
COPY deploy/docker/entrypoint.sh /usr/local/bin/aq-entrypoint
COPY deploy/docker/aq-job.sh /usr/local/bin/aq-job
RUN chmod 0755 /usr/local/bin/aq-entrypoint /usr/local/bin/aq-job \
 && mkdir -p /app/var \
 && chown app:app /app/var \
 && aq --env production config validate >/dev/null
# /app/var holds runtime state (market data, reports, logs). Mount a volume here
# on the worker; the API keeps nothing on disk (the kill switch is in PostgreSQL).
EXPOSE 8000
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/tini", "--", "/usr/local/bin/aq-entrypoint"]
