# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# The optional certificate mount is for managed-cloud build verification only.
# Render builds use their normal CA store; no session certificate enters a layer.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY apps ./apps
COPY contracts ./contracts
COPY deploy/plant_runtime.py deploy/plant_service.py ./deploy/
ENV PATH="/app/.venv/bin:$PATH" \
    HMI_PLANT_RUNTIME_DIR=/var/data/runtime \
    PYTHONUNBUFFERED=1
CMD ["python", "deploy/plant_service.py"]
