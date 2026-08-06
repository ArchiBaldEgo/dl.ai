# syntax=docker/dockerfile:1
# ==================== ОБЩИЙ БАЗОВЫЙ СЛОЙ (apt — один раз) ====================
# Рантайм-зависимости Chromium + git/curl/tini ставятся ОДИН раз и наследуются
# builder'ом и рантаймом, чтобы не дублировать медленный apt через прокси в каждой
# стадии. apt-кэш (mount type=cache) НЕ используется намеренно: при обязательном
# корпоративном прокси кэшированные apt-индексы и .deb часто рассинхронизируются
# и вызывают Hash Sum mismatch. No-Cache=True форсирует свежие индексы/пакеты.
FROM python:3.11-slim-bookworm AS apt-base
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
RUN rm -f /etc/apt/apt.conf.d/docker-clean && \
    rm -rf /var/lib/apt/lists/partial /var/cache/apt/archives/partial && \
    apt-get update \
        -o Acquire::http::Proxy="$HTTP_PROXY" \
        -o Acquire::https::Proxy="$HTTPS_PROXY" \
        -o Acquire::http::Pipeline-Depth=0 \
        -o Acquire::https::Pipeline-Depth=0 \
        -o Acquire::Languages=none \
        -o Acquire::Retries=5 \
        -o Acquire::http::No-Cache=True \
        -o Acquire::https::No-Cache=True && \
    apt-get install -y --no-install-recommends --fix-missing \
        -o Acquire::http::Proxy="$HTTP_PROXY" \
        -o Acquire::https::Proxy="$HTTPS_PROXY" \
        -o Acquire::http::Pipeline-Depth=0 \
        -o Acquire::https::Pipeline-Depth=0 \
        -o Acquire::Languages=none \
        -o Acquire::Retries=10 \
        -o Acquire::http::Timeout=300 \
        -o Acquire::http::No-Cache=True \
        -o Acquire::https::No-Cache=True \
        ca-certificates curl git tini \
        fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
        libcairo2 libcups2 libdbus-1-3 libdrm2 libexpat1 libgbm1 \
        libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 \
        libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
        libxkbcommon0 libxrandr2 xdg-utils && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
# curl нужен healthcheck в compose; git — для sync_update_log (читает git log из
# примонтированного .git, см. CMD); tini — entrypoint; lib* — рантайм-зависимости
# Chromium для Puppeteer. ffmpeg НЕ ставим из apt — он приходит pip-пакетом
# imageio-ffmpeg (см. requirements.txt).

# ==================== БИЛДЕР ====================
# Базируется на apt-base, чтобы переиспользовать уже установленные curl/ca-certificates
# и не тянуть libpq-dev / lib* (psycopg2-binary — это wheel; puppeteer лишь скачивает
# архив Chromium, не запуская его — рантайм-либы на этапе сборки не нужны).
FROM apt-base AS builder
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
# Отдельный прокси для npm/Puppeteer Chromium download и NodeSource. Может содержать
# credentials, используется только внутри builder-стадии и не утекает в рантайм.
ARG NPM_HTTP_PROXY
ARG NPM_HTTPS_PROXY
# NodeSource repo недоступен напрямую с хоста сборки (в отличие от deb.debian.org),
# поэтому setup + nodejs идут через корпоративный прокси (NPM_HTTP_PROXY — тот же, что
# достаёт npm-registry и Chromium). Скрипт сначала скачивается в файл: старый
# `curl | bash -` маскировал неудачный fetch (bash выходит 0 на пустом вводе) и
# молча откатывался на debian'овский nodejs без npm -> "npm: not found".
RUN printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' \
        "$NPM_HTTP_PROXY" "$NPM_HTTPS_PROXY" > /etc/apt/apt.conf.d/99-nodesource-proxy && \
    export http_proxy="$NPM_HTTP_PROXY" https_proxy="$NPM_HTTPS_PROXY" && \
    curl --proxy "$NPM_HTTP_PROXY" -fsSL -o /tmp/nodesource-setup.sh \
        https://deb.nodesource.com/setup_20.x && \
    bash /tmp/nodesource-setup.sh && \
    rm -f /tmp/nodesource-setup.sh && \
    apt-get update \
        -o Acquire::http::Proxy="$NPM_HTTP_PROXY" \
        -o Acquire::https::Proxy="$NPM_HTTPS_PROXY" \
        -o Acquire::http::Pipeline-Depth=0 \
        -o Acquire::https::Pipeline-Depth=0 \
        -o Acquire::Languages=none \
        -o Acquire::Retries=5 \
        -o Acquire::http::No-Cache=True \
        -o Acquire::https::No-Cache=True && \
    apt-get install -y --no-install-recommends --fix-missing \
        -o Acquire::http::Proxy="$NPM_HTTP_PROXY" \
        -o Acquire::https::Proxy="$NPM_HTTPS_PROXY" \
        -o Acquire::http::Pipeline-Depth=0 \
        -o Acquire::https::Pipeline-Depth=0 \
        -o Acquire::Languages=none \
        -o Acquire::Retries=5 \
        -o Acquire::http::No-Cache=True \
        -o Acquire::https::No-Cache=True \
        nodejs && \
    { command -v npm >/dev/null 2>&1 || { echo "ERROR: npm not installed — NodeSource setup failed (is deb.nodesource.com reachable via NPM_HTTP_PROXY?)"; exit 1; }; } && \
    rm -f /etc/apt/apt.conf.d/99-nodesource-proxy && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
# Виртуальное окружение Python
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip --proxy="$HTTP_PROXY" && \
    pip install --proxy="$HTTP_PROXY" --default-timeout=100 -r requirements.txt
# Node.js зависимости
WORKDIR /app/WebDeepseek
COPY WebDeepseek/package.json WebDeepseek/package-lock.json ./
# puppeteer 20.9.0 не имеет CLI `puppeteer` — Chromium качает его postinstall (install.js)
# во время npm ci. Задаём PUPPETEER_CACHE_DIR и прокси, чтобы скачалось в /opt/puppeteer-cache.
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm config set proxy "$NPM_HTTP_PROXY" && \
    npm config set https-proxy "$NPM_HTTPS_PROXY" && \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-cache \
    HTTP_PROXY="$NPM_HTTP_PROXY" HTTPS_PROXY="$NPM_HTTPS_PROXY" \
    npm ci --omit=dev --no-audit --no-fund && \
    mkdir -p /opt/puppeteer-runtime && \
    cp -r /opt/puppeteer-cache/. /opt/puppeteer-runtime/
COPY . .
# ==================== РАНТАЙМ ====================
# Базируется на apt-base — рантайм-библиотеки уже стоят, отдельный apt не нужен
# (раньше тут был второй полный apt-get install lib* через медленный прокси).
FROM apt-base AS runtime
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/bin/node /usr/local/bin/node
COPY --from=builder /opt/puppeteer-runtime /opt/puppeteer-runtime
COPY --from=builder /app/WebDeepseek/node_modules /app/WebDeepseek/node_modules
COPY --from=builder /app /app
ENV PATH="/opt/venv/bin:$PATH" \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-runtime \
    NODE_ENV=production \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
EXPOSE 8000 3000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-c", "python manage.py sync_update_log || true; exec node /app/WebDeepseek/api/index.js & exec daphne -b 0.0.0.0 -p 8000 DjangoTest.asgi:application & wait -n"]