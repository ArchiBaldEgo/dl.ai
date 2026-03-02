FROM python:3.10.9

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y libpq-dev
RUN pip install --upgrade pip

WORKDIR /app

COPY . /app

RUN pip install --default-timeout=100 -r requirements.txt

CMD ["daphne", "-p", "8000", "-b", "0.0.0.0", "DjangoTest.asgi:application"]
