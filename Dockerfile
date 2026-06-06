# syntax=docker/dockerfile:1
# ==================== БИЛДЕР ====================
FROM python:3.11-bookworm AS builder
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
# Все системные зависимости за один RUN (быстрее)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
        -o Acquire::http::Proxy="$HTTP_PROXY" \
        -o Acquire::https::Proxy="$HTTPS_PROXY" && \
    apt-get install -y --no-install-recommends \
        -o Acquire::http::Proxy="$HTTP_PROXY" \
        -o Acquire::https::Proxy="$HTTPS_PROXY" \
        ca-certificates curl gnupg tini libpq-dev \
        fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
        libcairo2 libcups2 libdbus-1-3 libdrm2 libexpat1 libgbm1 \
        libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 \
        libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
        libxkbcommon0 libxrandr2 xdg-utils && \
    curl --proxy "$HTTP_PROXY" -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*
# ^^^ ИЗМЕНЕНИЕ 1: убрал ffmpeg из apt (ставим его через pip-пакет imageio-ffmpeg в requirements.txt)
# Виртуальное окружение Python
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip --proxy="$HTTP_PROXY" && \
    pip install --proxy="$HTTP_PROXY" --default-timeout=100 -r requirements.txt
# Node.js зависимости
WORKDIR /app/bot
COPY bot/package.json bot/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm config set proxy "$HTTP_PROXY" && \
    npm config set https-proxy "$HTTPS_PROXY" && \
    npm ci --omit=dev --no-audit --no-fund && \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-cache \
    npx puppeteer browsers install chrome && \
    mkdir -p /opt/puppeteer-runtime && \
    cp -r /opt/puppeteer-cache/. /opt/puppeteer-runtime/
COPY . .
# ==================== РАНТАЙМ ====================
FROM python:3.11-slim-bookworm
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
        -o Acquire::http::Proxy="$HTTP_PROXY" \
        -o Acquire::https::Proxy="$HTTPS_PROXY" && \
    apt-get install -y --no-install-recommends \
        -o Acquire::http::Proxy="$HTTP_PROXY" \
        -o Acquire::https::Proxy="$HTTPS_PROXY" \
        ca-certificates curl fonts-liberation libasound2 libatk-bridge2.0-0 \
        libatk1.0-0 libcairo2 libcups2 libdbus-1-3 libdrm2 libexpat1 \
        libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 \
        libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
        libxkbcommon0 libxrandr2 xdg-utils && \
    rm -rf /var/lib/apt/lists/*
# ^^^ ИЗМЕНЕНИЕ 2: убрал ffmpeg, добавил curl (его требует healthcheck в compose)
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/bin/tini /usr/bin/tini
COPY --from=builder /usr/bin/node /usr/local/bin/node
# ^^^ ИЗМЕНЕНИЕ 3: node в рантайм-образе не было — без него `node /app/bot/api/index.js` падает с "not found"
COPY --from=builder /opt/puppeteer-runtime /opt/puppeteer-runtime
COPY --from=builder /app/bot/node_modules /app/bot/node_modules
COPY --from=builder /app /app
ENV PATH="/opt/venv/bin:$PATH" \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-runtime \
    NODE_ENV=production \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
EXPOSE 8000 3000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-c", "exec node /app/bot/api/index.js & exec daphne -b 0.0.0.0 -p 8000 DjangoTest.asgi:application & wait -n"]