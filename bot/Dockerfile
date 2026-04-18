
FROM node:20-bookworm-slim

# Аргументы для прокси (передаются из docker-compose только на этапе сборки)
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

# Настройка APT для работы через прокси (временная, для установки пакетов)
RUN rm -rf /var/lib/apt/lists/* && \
    { \
        echo 'Acquire::Retries "5";'; \
        echo 'Acquire::http::Pipeline-Depth "0";'; \
        echo 'Acquire::http::No-Cache "true";'; \
        echo 'Acquire::http::No-Store "true";'; \
        echo 'Acquire::By-Hash "yes";'; \
        echo 'Acquire::CompressionTypes::Order { "gz"; "bz2"; "xz"; };'; \
    } > /etc/apt/apt.conf.d/99proxyfix && \
    if [ -n "$HTTP_PROXY" ]; then \
        echo "Acquire::http::Proxy \"$HTTP_PROXY\";" >> /etc/apt/apt.conf.d/99proxyfix; \
        echo "Acquire::https::Proxy \"$HTTPS_PROXY\";" >> /etc/apt/apt.conf.d/99proxyfix; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libc6 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libexpat1 \
        libfontconfig1 \
        libgbm1 \
        libgcc1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libstdc++6 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxrandr2 \
        libxrender1 \
        libxss1 \
        libxtst6 \
        xdg-utils && \
    rm -rf /var/lib/apt/lists/* && \
    # Удаляем конфиг прокси для APT, чтобы он не остался в образе
    rm -f /etc/apt/apt.conf.d/99proxyfix

# Настройка NPM для использования прокси (только на этапе сборки)
RUN if [ -n "$HTTP_PROXY" ]; then \
        npm config set proxy "$HTTP_PROXY"; \
        npm config set https-proxy "$HTTPS_PROXY"; \
        npm config set noproxy "$NO_PROXY"; \
    fi

ENV NODE_ENV=production \
    NPM_CONFIG_LOGLEVEL=error \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_PROGRESS=false

WORKDIR /app

COPY package.json package-lock.json ./

# Устанавливаем зависимости (если есть прокси, npm уже настроен)
RUN npm ci --omit=dev --no-audit --no-fund

# После установки зависимостей удаляем настройки прокси из npm, чтобы они не попали в образ
RUN if [ -n "$HTTP_PROXY" ]; then \
        npm config delete proxy; \
        npm config delete https-proxy; \
        npm config delete noproxy; \
    fi

COPY api/ ./api/
COPY worker/ ./worker/

EXPOSE 3000

# В CMD переменные прокси уже не заданы, контейнер будет использовать прямой доступ
CMD ["npm", "start"]
