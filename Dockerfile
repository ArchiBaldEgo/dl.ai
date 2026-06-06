# syntax=docker/dockerfile:1
# Multi-stage build: compile & fetch deps in 'builder', copy only runtime artifacts

# --- Build stage ---
FROM python:3.11 AS builder

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

# Use proxy only during build
ENV http_proxy=$HTTP_PROXY \
    https_proxy=$HTTPS_PROXY \
    no_proxy=$NO_PROXY \
    HTTP_PROXY=$HTTP_PROXY \
    HTTPS_PROXY=$HTTPS_PROXY \
    NO_PROXY=$NO_PROXY

# Install build dependencies, Node.js, and Puppeteer browser
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
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
        xdg-utils \
        ffmpeg

# Install Node.js 20 (recommended by Puppeteer)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x -o /tmp/nodesource_setup.sh && \
    bash /tmp/nodesource_setup.sh && \
    apt-get install -y --no-install-recommends nodejs && \
    rm /tmp/nodesource_setup.sh

# Create Python virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies (using proxy if set)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    if [ -n "$HTTP_PROXY" ]; then \
        proxy_arg="--proxy=$HTTP_PROXY"; \
    fi && \
    pip install $proxy_arg --default-timeout=100 -r /app/requirements.txt

# Install Node.js dependencies and Puppeteer browser
WORKDIR /app/bot
COPY bot/package.json bot/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm ci --omit=dev --no-audit --no-fund && \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-cache \
    npx puppeteer browsers install chrome && \
    mkdir -p /opt/puppeteer-runtime && \
    cp -r /opt/puppeteer-cache/. /opt/puppeteer-runtime/

# Copy application source
COPY . /app

# --- Runtime stage ---
FROM python:3.11-slim

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

# Runtime system libraries for Puppeteer (smaller set than build)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
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
        xdg-utils \
        ffmpeg \
        && rm -rf /var/lib/apt/lists/*

# Copy artifacts from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/bin/tini /usr/bin/tini
COPY --from=builder /opt/puppeteer-runtime /opt/puppeteer-runtime
COPY --from=builder /app/bot/node_modules /app/bot/node_modules
COPY --from=builder /app /app

# Set environment
ENV PATH="/opt/venv/bin:$PATH" \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-runtime \
    NODE_ENV=production \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

EXPOSE 8000 3000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-c", "node /app/bot/api/index.js & daphne -b 0.0.0.0 -p 8000 DjangoTest.asgi:application & wait -n"]