# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    export npm_config_https_proxy="${HTTPS_PROXY:-}" npm_config_proxy="${HTTP_PROXY:-}"; \
    npm ci --ignore-scripts --no-audit --no-fund
COPY apps/web/ ./
ENV VITE_SCREEN_TRANSPORT=polling
RUN npm run build
FROM plant
COPY --from=web /web/dist /app/web
