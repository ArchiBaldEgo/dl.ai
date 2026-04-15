FROM node:20-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1

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
    pip config set global.proxy "$HTTP_PROXY"; \
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
    apt-get install -y --no-install-recommends python3 python3-pip gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade pip

WORKDIR /app

COPY requirements.txt /app/requirements.txt

# Явно указываем прокси для pip
RUN if [ -n "$HTTP_PROXY" ]; then \
    python3 -m pip install --proxy=$HTTP_PROXY --default-timeout=100 -r requirements.txt; \
    else \
    python3 -m pip install --default-timeout=100 -r requirements.txt; \
    fi

COPY . /app

CMD ["daphne", "-p", "8000", "-b", "0.0.0.0", "DjangoTest.asgi:application"]