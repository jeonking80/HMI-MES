#!/usr/bin/env bash
set -euo pipefail

# The integration tests use only the local managed Docker daemon.
# Preserve the supplied registry credentials, proxies and CA trust.
exec env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS \
    -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
    docker --host=unix:///var/run/docker.sock pull \
    postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826
