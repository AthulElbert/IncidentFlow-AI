# The image the Verifier runs a proposed fix's tests in (SANDBOX_EXEC=container).
#
# It holds the project's dependencies, so tests need no network at run time; the runner starts it with no
# network, a read-only root filesystem, the worktree mounted read-only, no capabilities, an unprivileged user,
# and memory, CPU and process limits. Build it from the repo root:
#
#   docker build -f ops/sandbox.Dockerfile -t incidentflow-sandbox:latest .
#
# Rebuild it when requirements.txt changes. In production, pin the base image by digest.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Nothing may be written into the (read-only) worktree: no bytecode, no pytest cache. Home is the scratch tmpfs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    HOME=/tmp

USER 65534:65534
WORKDIR /work
