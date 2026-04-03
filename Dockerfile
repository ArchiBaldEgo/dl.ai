FROM python:3.10.9

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG ALL_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG all_proxy
ARG no_proxy

ENV HTTP_PROXY=$HTTP_PROXY \
	HTTPS_PROXY=$HTTPS_PROXY \
	ALL_PROXY=$ALL_PROXY \
	NO_PROXY=$NO_PROXY \
	http_proxy=$http_proxy \
	https_proxy=$https_proxy \
	all_proxy=$all_proxy \
	no_proxy=$no_proxy

RUN if [ -n "$HTTP_PROXY" ]; then \
		echo "Acquire::http::Proxy \"$HTTP_PROXY\";" > /etc/apt/apt.conf.d/99proxy; \
		echo "Acquire::https::Proxy \"${HTTPS_PROXY:-$HTTP_PROXY}\";" >> /etc/apt/apt.conf.d/99proxy; \
		echo "Acquire::Retries \"5\";" >> /etc/apt/apt.conf.d/99proxy; \
		echo "Acquire::http::No-Cache \"true\";" >> /etc/apt/apt.conf.d/99proxy; \
		echo "Acquire::https::No-Cache \"true\";" >> /etc/apt/apt.conf.d/99proxy; \
		echo "Acquire::http::Pipeline-Depth \"0\";" >> /etc/apt/apt.conf.d/99proxy; \
	fi

RUN rm -rf /var/lib/apt/lists/* && \
	apt-get clean && \
	apt-get update -o Acquire::Retries=5 && \
	apt-get install -y --no-install-recommends libpq-dev && \
	rm -rf /var/lib/apt/lists/*
RUN if [ -n "$HTTP_PROXY" ]; then \
		python -m pip config set global.proxy "${HTTPS_PROXY:-$HTTP_PROXY}"; \
	fi
RUN pip install --upgrade pip

WORKDIR /app

COPY . /app

RUN pip install --default-timeout=100 -r requirements.txt

CMD ["daphne", "-p", "8000", "-b", "0.0.0.0", "DjangoTest.asgi:application"]
