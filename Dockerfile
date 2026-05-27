# syntax=docker/dockerfile:1.7
FROM python:3.10.9

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Принимаем аргументы прокси
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

# Устанавливаем переменные окружения для прокси
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY}

# Настраиваем pip для использования прокси
RUN if [ -n "$HTTP_PROXY" ]; then \
    python -m pip config set global.proxy "$HTTP_PROXY"; \
    fi

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" && \
    if [ -n "$HTTP_PROXY" ] || [ -n "$HTTPS_PROXY" ]; then \
        printf '%s\n' \
          'Acquire::Retries "5";' \
          'Acquire::http::Pipeline-Depth "0";' \
          'Acquire::http::No-Cache "true";' \
          'Acquire::http::No-Store "true";' \
          'Acquire::https::No-Cache "true";' \
          'Acquire::https::No-Store "true";' \
          'Acquire::By-Hash "yes";' \
          'Acquire::CompressionTypes::Order { "gz"; "bz2"; "xz"; };' \
          > /etc/apt/apt.conf.d/99proxyfix; \
    else \
        rm -f /etc/apt/apt.conf.d/99proxyfix; \
    fi && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get update || (rm -rf /var/lib/apt/lists/* && apt-get update) && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        tini \
        libpq-dev \
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
        xdg-utils && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs

RUN python -m pip install --upgrade pip

WORKDIR /app

COPY requirements.txt /app/requirements.txt

# Явно указываем прокси для pip
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "$HTTP_PROXY" ]; then \
    python -m pip install --proxy="$HTTP_PROXY" --default-timeout=100 -r /app/requirements.txt; \
    else \
    python -m pip install --default-timeout=100 -r /app/requirements.txt; \
    fi

ENV NODE_ENV=production \
    NPM_CONFIG_LOGLEVEL=error \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_PROGRESS=false \
    NPM_CONFIG_MAXSOCKETS=50 \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-cache

WORKDIR /app/bot

COPY bot/package.json bot/package-lock.json ./

RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    --mount=type=cache,target=/opt/puppeteer-cache,sharing=locked \
    export HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" \
        http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" && \
    npm ci --omit=dev --no-audit --no-fund && \
    mkdir -p /app/bot/.puppeteer-cache && \
    cp -r /opt/puppeteer-cache/. /app/bot/.puppeteer-cache/ 2>/dev/null || true

ENV PUPPETEER_CACHE_DIR=/app/bot/.puppeteer-cache

WORKDIR /app

COPY . /app

EXPOSE 8000 3000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-lc", "node /app/bot/api/index.js & daphne -b 0.0.0.0 -p 8000 DjangoTest.asgi:application & wait -n"]
