# syntax=docker/dockerfile:1
# ==================== ОБЩИЙ БАЗОВЫЙ СЛОЙ (apt — один раз) ====================
# Рантайм-зависимости Chromium + git/curl/tini ставятся ОДИН раз и наследуются
# builder'ом и рантаймом, чтобы не дублировать медленный apt через прокси в каждой
# стадии. apt-кэш (mount type=cache) НЕ используется намеренно: при обязательном
# корпоративном прокси кэшированные apt-индексы и .deb часто рассинхронизируются
# и вызывают Hash Sum mismatch. No-Cache=True форсирует свежие индексы/пакеты.
# Python 3.13.5 — та же версия, что в локальном .venv и WSL (одна версия везде).
FROM python:3.13.5-slim-bookworm AS apt-base
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
# imageio-ffmpeg (см. requirements.txt). Node.js НЕ ставим из apt: см. builder.

# ==================== БИЛДЕР ====================
# Базируется на apt-base, чтобы переиспользовать уже установленные curl/ca-certificates
# и не тянуть libpq-dev / lib* (psycopg2-binary — это wheel; puppeteer лишь скачивает
# архив Chromium, не запуская его — рантайм-либы на этапе сборки не нужны).
FROM apt-base AS builder
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
# Отдельный прокси для npm-registry, Puppeteer Chromium download и nodejs.org.
# Может содержать credentials, используется только внутри builder-стадии и не
# утекает в рантайм.
ARG NPM_HTTP_PROXY
ARG NPM_HTTPS_PROXY
# Node.js 20 — официальным tarball с nodejs.org/dist, НЕ из NodeSource и НЕ из
# apt. NodeSource-репозиторий через корпоративный прокси качает индексы, но
# падает на скачивании .deb, и `apt-get install --fix-missing nodejs` молча
# откатывается на дебиановский nodejs 18 без npm ("npm: not found" при exit 0
# у всех шагов — маскировка падения); дебиановский nodejs 18 не подходит и по
# engine-требованию: proxy-chain@3.0.0 (в lock-файлах пулов) требует node >=
# 20.11. Tarball — один файл через тот же прокси, что доставляет npm-registry и
# Chromium; при сбое curl -f роняет сборку громко, без молчаливых откатов.
# Версия запинена, sha256 сверяется с официальным SHASUMS256.txt. Бинарник
# линкуется с libstdc++6/libgcc — они уже есть в базовом slim-образе.
ARG NODE_VERSION=20.20.2
RUN ARCH="$(dpkg --print-architecture)" && \
    [ "$ARCH" = amd64 ] && ARCH=x64; \
    NODE_TARBALL="node-v${NODE_VERSION}-linux-${ARCH}.tar.gz" && \
    curl --proxy "$NPM_HTTP_PROXY" -fsSL --retry 5 -o "/tmp/$NODE_TARBALL" \
        "https://nodejs.org/dist/v${NODE_VERSION}/$NODE_TARBALL" && \
    curl --proxy "$NPM_HTTP_PROXY" -fsSL --retry 5 "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" \
        | grep " $NODE_TARBALL\$" | (cd /tmp && sha256sum -c -) && \
    tar -xzf "/tmp/$NODE_TARBALL" -C /usr/local --strip-components=1 && \
    rm -f "/tmp/$NODE_TARBALL" && \
    node -v && npm -v
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
# Node.js зависимости для Web Kimi bot-пула (WebKimi/) — отдельный install,
# сервисы независимы (паттерн one-service-per-dir). Зависимости идентичны WebDeepseek.
WORKDIR /app/WebKimi
COPY WebKimi/package.json WebKimi/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm config set proxy "$NPM_HTTP_PROXY" && \
    npm config set https-proxy "$NPM_HTTPS_PROXY" && \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-cache \
    HTTP_PROXY="$NPM_HTTP_PROXY" HTTPS_PROXY="$NPM_HTTPS_PROXY" \
    npm ci --omit=dev --no-audit --no-fund
COPY . .
# ==================== РАНТАЙМ ====================
# Базируется на apt-base — рантайм-библиотеки уже стоят, отдельный apt не нужен
# (раньше тут был второй полный apt-get install lib* через медленный прокси).
FROM apt-base AS runtime
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/local/bin/node /usr/local/bin/node
COPY --from=builder /opt/puppeteer-runtime /opt/puppeteer-runtime
COPY --from=builder /app/WebDeepseek/node_modules /app/WebDeepseek/node_modules
COPY --from=builder /app/WebKimi/node_modules /app/WebKimi/node_modules
COPY --from=builder /app /app
ENV PATH="/opt/venv/bin:$PATH" \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-runtime \
    NODE_ENV=production \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
EXPOSE 8000 3000 3001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-c", "python manage.py sync_update_log || true; exec node /app/WebDeepseek/api/index.js & exec node /app/WebKimi/api/index.js & exec daphne -b 0.0.0.0 -p 8000 DjangoTest.asgi:application & wait -n"]