# syntax=docker/dockerfile:1.7
# ^ нужно для cache mounts — BuildKit включён в Docker Desktop по умолчанию.

FROM node:20-bookworm-slim

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

# Системные библиотеки для headless-Chromium + tini.
# apt-кэш сохраняем в cache mount, чтобы повторные сборки не качали .deb заново.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libgbm1 \
        libglib2.0-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        xdg-utils

ENV NODE_ENV=production \
    NPM_CONFIG_LOGLEVEL=error \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_PROGRESS=false \
    NPM_CONFIG_MAXSOCKETS=50 \
    # Куда Puppeteer кладёт скачанный Chromium. Кладём в стабильный путь
    # ВНЕ node_modules — чтобы можно было примонтировать как cache mount.
    PUPPETEER_CACHE_DIR=/opt/puppeteer-cache

WORKDIR /app

COPY package.json package-lock.json ./

#  скачивание пакетов кешируется чтобы не скачивать повторно
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    --mount=type=cache,target=/opt/puppeteer-cache,sharing=locked \
    export HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" \
        http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" && \
    npm ci --omit=dev --no-audit --no-fund && \
    # Cache mount живёт только в этом RUN-слое. Чтобы Chromium попал в образ,
    # копируем его в обычную директорию.
    mkdir -p /app/.puppeteer-cache && \
    cp -r /opt/puppeteer-cache/. /app/.puppeteer-cache/ 2>/dev/null || true

# В рантайме Puppeteer должен искать Chromium в той же директории.
ENV PUPPETEER_CACHE_DIR=/app/.puppeteer-cache

COPY api/ ./api/
COPY worker/ ./worker/

EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "api/index.js"]