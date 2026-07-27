"""
ASGI-конфигурация проекта DjangoTest.

Объединяет HTTP (Django) и WebSocket (Channels) в одном ProtocolTypeRouter.
WebSocket-маршруты определены в ai.routing (AI-чат).

Важно: get_asgi_application() вызывается ДО импорта ai.routing,
чтобы app registry был готов к загрузке моделей.
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'DjangoTest.settings')

from django.core.asgi import get_asgi_application

# get_asgi_application() triggers django.setup(), which loads the app registry.
# It must run BEFORE importing ai.routing (which imports ai.models at module
# load time) — otherwise importing models raises AppRegistryNotReady.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.sessions import SessionMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator
from ai.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": SessionMiddlewareStack(
        AuthMiddlewareStack(
            AllowedHostsOriginValidator(
                URLRouter(websocket_urlpatterns)
            )
        )
    ),
})