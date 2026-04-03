"""
ASGI config for studifyfinal project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
#from channels.auth import AuthMiddlewareStack # You are already importing this
from channels.security.websocket import AllowedHostsOriginValidator
from studifyfinal.middleware import KindeAuthMiddleware # Your custom Kinde middleware

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'studifyfinal.settings')
django_asgi = get_asgi_application()


import Users.routing as routing # This import is fine now after get_asgi_application() call




application = ProtocolTypeRouter({
    "http": django_asgi,
    "websocket": KindeAuthMiddleware( # <--- Move this up to the top of the stack

            URLRouter(
                routing.websocket_urlpatterns
            )
        
    ),
})