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
    pip config set global.proxy "$HTTP_PROXY"; \
    fi

RUN apt-get update && apt-get install -y libpq-dev
RUN pip install --upgrade pip

WORKDIR /app

COPY . /app

# Явно указываем прокси для pip
RUN if [ -n "$HTTP_PROXY" ]; then \
    pip install --proxy=$HTTP_PROXY --default-timeout=100 -r requirements.txt; \
    else \
    pip install --default-timeout=100 -r requirements.txt; \
    fi

CMD ["daphne", "-p", "8000", "-b", "0.0.0.0", "DjangoTest.asgi:application"]