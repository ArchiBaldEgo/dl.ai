"""
ASGI config for DjangoTest project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
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
from ai.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": SessionMiddlewareStack(
        AuthMiddlewareStack(
            URLRouter(websocket_urlpatterns)
        )
    ),
})