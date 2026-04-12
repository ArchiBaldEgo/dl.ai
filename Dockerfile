<<<<<<< HEAD
FROM python:3.10.9
=======

FROM node:20-bookworm-slim
>>>>>>> 2daa7fc (Мои изменения в подмодуле)

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Принимаем аргументы прокси
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

# Устанавливаем переменные окружения для прокси
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}
ENV NO_PROXY=${NO_PROXY}

# Настраиваем pip для использования прокси
RUN if [ -n "$HTTP_PROXY" ]; then \
<<<<<<< HEAD
    pip config set global.proxy "$HTTP_PROXY"; \
=======
        npm config set proxy "$HTTP_PROXY"; \
        npm config set https-proxy "$HTTPS_PROXY"; \
        npm config set noproxy "$NO_PROXY"; \
>>>>>>> 2daa7fc (Мои изменения в подмодуле)
    fi

RUN rm -rf /var/lib/apt/lists/* && \
    printf '%s\n' \
      'Acquire::Retries "5";' \
      'Acquire::http::Pipeline-Depth "0";' \
      'Acquire::http::No-Cache "true";' \
      'Acquire::http::No-Store "true";' \
      'Acquire::By-Hash "yes";' \
      'Acquire::CompressionTypes::Order { "gz"; "bz2"; "xz"; };' \
      > /etc/apt/apt.conf.d/99proxyfix && \
    apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip

WORKDIR /app

COPY . /app

<<<<<<< HEAD
# Явно указываем прокси для pip
RUN if [ -n "$HTTP_PROXY" ]; then \
    pip install --proxy=$HTTP_PROXY --default-timeout=100 -r requirements.txt; \
    else \
    pip install --default-timeout=100 -r requirements.txt; \
    fi

CMD ["daphne", "-p", "8000", "-b", "0.0.0.0", "DjangoTest.asgi:application"]
=======
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
>>>>>>> 2daa7fc (Мои изменения в подмодуле)
