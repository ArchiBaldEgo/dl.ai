"""
WSGI-конфигурация проекта DjangoTest (для gunicorn/uvicorn).

Используется только при развёртывании без Channels (без WebSocket).
Для WebSocket использовать ASGI (asgi.py).
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'DjangoTest.settings')

application = get_wsgi_application()
