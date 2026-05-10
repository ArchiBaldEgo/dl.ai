# syntax=docker/dockerfile:1.7
FROM python:3.10.9

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
    python -m pip config set global.proxy "$HTTP_PROXY"; \
    fi

RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt/lists \
    rm -rf /var/lib/apt/lists/* && \
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

RUN python -m pip install --upgrade pip

WORKDIR /app

COPY requirements.txt /app/requirements.txt

# Явно указываем прокси для pip
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "$HTTP_PROXY" ]; then \
    python -m pip install --proxy=$HTTP_PROXY --default-timeout=100 -r /app/requirements.txt; \
    else \
    python -m pip install --default-timeout=100 -r /app/requirements.txt; \
    fi

COPY . /app

CMD ["daphne", "-p", "8000", "-b", "0.0.0.0", "DjangoTest.asgi:application"]
