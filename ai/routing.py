from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.urls import re_path
from ai import consumers

websocket_urlpatterns = [
    re_path(r'^ai/chat/ws/(?P<client_id>[^/]+)$', consumers.MyConsumer.as_asgi()),
]

application = ProtocolTypeRouter({
    "websocket": AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})
